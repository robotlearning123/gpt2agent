"""Session simulation profile — one consistent "browser identity" per host.

Everything a real Chrome session presents to chatgpt.com, kept consistent
across the seed GET, sentinel requirements, conduit prepare, and the
conversation POST:

* TLS/HTTP fingerprint (curl_cffi ``impersonate``) — ONE profile for the
  whole flow; mixing impersonations between mint and POST is a bot signal.
* User-Agent + sec-ch-ua suite matching that profile.
* ``oai-device-id`` / ``oai-session-id`` — persistent per host (a real
  browser keeps the same did for weeks), stored in
  ``~/.gpt2agent/sim-state.json`` (0600).
* ``oai-client-version`` — scraped from the live site's ``data-build``
  attribute on seed; a fabricated version string is itself a flag.
* Geo identity — timezone (IANA), JS-style ``timezone_offset_min``,
  ``oai-language``/``Accept-Language`` locale, and the IP lat/lng embedded
  in the turnstile token. Auto-detected from the egress IP once (ipinfo),
  cached; every field can be pinned via ``[sentinel]`` config keys.
* One warm ``curl_cffi`` Session reused across mints — CF cookies
  (``__cf_bm``) accumulate like a real tab instead of a fresh browser
  fingerprint per message.

Config overrides (``[sentinel]`` in config.toml): ``timezone``, ``locale``,
``ip_latlng``, ``screen``, ``dark_mode``, ``impersonate``.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_log = logging.getLogger(__name__)

#: curl_cffi impersonation + matching UA/sec-ch-ua. Bump together when the
#: fleet upgrades; TLS fingerprint, UA, and sec-ch-ua must always agree.
IMPERSONATE = "chrome136"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
_SEC_CH_UA = '"Chromium";v="136", "Not=A?Brand";v="24", "Google Chrome";v="136"'

_FALLBACK_CLIENT_VERSION = "prod-0917-fallback"
_DEFAULT_TIMEZONE = "America/New_York"
_DEFAULT_LOCALE = "en-US"
_DEFAULT_LATLNG = "39.04,-77.49"  # Ashburn, VA
_DEFAULT_SCREEN = "3440x1440"

_STATE_PATH = Path.home() / ".gpt2agent" / "sim-state.json"
_GEO_TTL_S = 24 * 3600
_BUILD_TTL_S = 12 * 3600

_state_lock = threading.Lock()

#: A subset of the ``window`` keys a real page's script enumerates — the
#: upstream fingerprint picks one at random per token.
_WINDOW_KEYS = (
    "window", "self", "document", "name", "location", "customElements",
    "history", "navigation", "locationbar", "menubar", "personalbar",
    "scrollbars", "statusbar", "toolbar", "status", "closed", "frames",
    "length", "top", "opener", "parent", "navigator", "origin",
    "external", "screen", "innerWidth", "innerHeight", "devicePixelRatio",
    "event", "clientInformation", "crypto", "indexedDB", "sessionStorage",
    "localStorage", "performance", "trustedTypes", "styleMedia",
)


def _load_state() -> dict:
    try:
        return json.loads(_STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(patch: dict) -> None:
    """Best-effort merge-write of the state file (0600)."""
    try:
        with _state_lock:
            data = _load_state()
            data.update(patch)
            _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(_STATE_PATH.parent), prefix=".sim-state-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(json.dumps(data, indent=1))
                os.chmod(tmp, 0o600)
                os.replace(tmp, _STATE_PATH)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort
        _log.debug("sim-state save failed: %s", exc)


def _tz_offset_min(tz_name: str) -> int:
    """JS ``getTimezoneOffset()`` semantics: minutes *west* of UTC (NY = 240)."""
    try:
        off = datetime.now(ZoneInfo(tz_name)).utcoffset()
        return int(-off.total_seconds() / 60) if off else 0
    except Exception:
        return 240


class SimProfile:
    """A persistent, internally-consistent browser identity."""

    def __init__(self, config: dict | None = None) -> None:
        cfg = (config or {}).get("sentinel", {}) or {}
        state = _load_state()

        # `or` not `.get(..., default)`: the shipped _DEFAULTS set
        # impersonate=None explicitly, and None must fall back — a bare
        # (non-impersonated) TLS fingerprint gets 403'd by Cloudflare.
        self.impersonate = cfg.get("impersonate") or IMPERSONATE
        self.ua = _UA

        self.device_id = state.get("device_id") or str(uuid.uuid4())
        self.session_id = state.get("session_id") or str(uuid.uuid4())
        if self.device_id != state.get("device_id") or self.session_id != state.get("session_id"):
            _save_state({"device_id": self.device_id, "session_id": self.session_id})

        self.timezone = cfg.get("timezone") or self._geo_field(state, "timezone", _DEFAULT_TIMEZONE)
        self.locale = cfg.get("locale") or self._geo_field(state, "locale", _DEFAULT_LOCALE)
        self.ip_latlng = cfg.get("ip_latlng") or self._geo_field(state, "latlng", _DEFAULT_LATLNG)
        # Full egress identity (ip/city/region) for the VM turnstile payload —
        # upstream passes str([ip, city, region, lat, lng]).
        self.ip_info: list[str] = cfg.get("ip_info") or self._geo_ip_info(state)
        self.timezone_offset_min = _tz_offset_min(self.timezone)
        lang = self.locale.split("-")[0]
        if self.locale.lower().startswith("en"):
            self.nav_languages = f"{self.locale},{lang}"
            self.accept_language = f"{self.locale},{lang};q=0.9"
        else:
            self.nav_languages = f"{self.locale},{lang},en-US,en"
            self.accept_language = f"{self.locale},{lang};q=0.9,en-US;q=0.8,en;q=0.7"

        screen = cfg.get("screen") or _DEFAULT_SCREEN
        try:
            w, h = (int(x) for x in str(screen).lower().split("x"))
        except Exception:
            w, h = 3440, 1440
        self.screen_w, self.screen_h = w, h
        self.dark_mode = bool(cfg.get("dark_mode", True))

        self._client_version: str | None = state.get("client_version")
        self._client_version_at: float = float(state.get("client_version_at") or 0)
        self._page_loaded_at = time.time()

        # Shared warm session — CF cookies accumulate across mints.
        self.session = None  # created lazily in ensure_session()

    # ── geo ─────────────────────────────────────────────────────────────

    def _geo_field(self, state: dict, key: str, default: str) -> str:
        geo = state.get("geo") or {}
        if geo.get(key) and time.time() - float(geo.get("at") or 0) < _GEO_TTL_S:
            return geo[key]
        detected = self._detect_geo()
        if detected:
            return detected.get(key, default)
        return geo.get(key) or default

    def _geo_ip_info(self, state: dict) -> list[str]:
        """[ip, city, region, lat, lng] for the VM turnstile payload."""
        geo = state.get("geo") or {}
        fresh = time.time() - float(geo.get("at") or 0) < _GEO_TTL_S
        if geo.get("ip_info") and fresh:
            return list(geo["ip_info"])
        detected = self._detect_geo()
        if detected and detected.get("ip_info"):
            return list(detected["ip_info"])
        if geo.get("ip_info"):
            return list(geo["ip_info"])
        lat, _, lng = (self.ip_latlng or _DEFAULT_LATLNG).partition(",")
        return ["", "", "", lat.strip(), lng.strip()]

    @staticmethod
    def _detect_geo() -> dict | None:
        """Resolve egress-IP geo once (timezone/locale/latlng must agree with
        the IP Cloudflare sees). Fails soft — callers keep cached/defaults."""
        try:
            from curl_cffi import requests as cr

            r = cr.get("https://ipinfo.io/json", timeout=6,
                       impersonate=IMPERSONATE)
            if r.status_code != 200:
                return None
            d = r.json()
            lat, _, lng = (d.get("loc") or _DEFAULT_LATLNG).partition(",")
            geo = {
                "at": time.time(),
                "timezone": d.get("timezone") or _DEFAULT_TIMEZONE,
                "latlng": d.get("loc") or _DEFAULT_LATLNG,
                "locale": _DEFAULT_LOCALE,
                "ip_info": [
                    d.get("ip") or "",
                    d.get("city") or "",
                    d.get("region") or "",
                    lat.strip(),
                    lng.strip(),
                ],
            }
            _save_state({"geo": geo})
            return geo
        except Exception:
            return None

    # ── client version ──────────────────────────────────────────────────

    @property
    def client_version(self) -> str:
        stale = time.time() - self._client_version_at > _BUILD_TTL_S
        if not self._client_version or stale:
            self._scrape_build()
        return self._client_version or _FALLBACK_CLIENT_VERSION

    @property
    def cached_client_version(self) -> str:
        """Non-blocking variant — never scrapes; for header construction at
        client init where network I/O would be a surprising side effect."""
        return self._client_version or _FALLBACK_CLIENT_VERSION

    def _scrape_build(self) -> None:
        """Real ``prod-*`` build id from the homepage's data-build attribute."""
        try:
            sess = self.ensure_session()
            html = getattr(sess, "_last_home_html", None)
            if not html:
                r = sess.get("https://chatgpt.com/", timeout=25)
                html = r.text or ""
                sess._last_home_html = html
            m = re.search(r'data-build="([^"]+)"', html)
            if m:
                self._client_version = m.group(1)
                self._client_version_at = time.time()
                _save_state(
                    {
                        "client_version": self._client_version,
                        "client_version_at": self._client_version_at,
                    }
                )
        except Exception as exc:  # noqa: BLE001 - keep fallback
            _log.debug("build scrape failed: %s", exc)

    # ── session ─────────────────────────────────────────────────────────

    def ensure_session(self):
        """The one warm session for this profile (lazy, re-seeded if the
        CF cookie is gone)."""
        if self.session is not None:
            return self.session
        from curl_cffi import requests as cr

        sess = cr.Session(impersonate=self.impersonate)
        sess.headers.update(self.frontend_headers())
        # A real browser profile keeps cookies across restarts — reload the
        # jar so a server restart looks like reopening the same browser, not
        # a brand-new client fingerprint.
        saved = (_load_state().get("cookies") or {})
        for k, v in saved.items():
            try:
                sess.cookies.set(k, v)
            except Exception:
                pass
        self.seed_session(sess)
        # Humans don't send a message 0ms after the page loads — give the
        # freshly seeded session a beat before the requirements POST.
        time.sleep(random.uniform(0.8, 2.5))
        self.session = sess
        return sess

    def persist_cookies(self) -> None:
        """Snapshot the warm session's cookie jar into sim-state so the next
        process inherits the same browser identity (esp. ``__cf_bm``)."""
        if self.session is None:
            return
        try:
            _save_state({"cookies": dict(self.session.cookies)})
        except Exception:
            pass

    def drop_session(self) -> None:
        """Discard the warm session after a mint failure — a poisoned CF
        cookie jar or a flagged session should not be retried as-is."""
        sess = self.session
        self.session = None
        # The jar was dropped because it looked poisoned/flagged — don't
        # resurrect it on the next process start.
        _save_state({"cookies": {}})
        if sess is not None:
            try:
                sess.close()
            except Exception:
                pass

    def seed_session(self, sess) -> None:
        nav = self.navigation_headers()
        last = None
        for _ in range(4):
            try:
                r = sess.get(
                    "https://chatgpt.com/", headers=nav, timeout=25
                )
                sess._last_home_html = r.text or ""
                self._page_loaded_at = time.time()
                return
            except Exception as exc:  # noqa: BLE001 - retry loop
                last = exc
                time.sleep(2)
        raise RuntimeError(f"sim: cannot seed session: {last}")

    # ── header/payload fragments ────────────────────────────────────────

    def frontend_headers(self) -> dict:
        """The header suite a real Chrome fetch sends (API requests)."""
        return {
            "User-Agent": self.ua,
            "Accept-Language": self.accept_language,
            "oai-language": self.locale,
            "oai-device-id": self.device_id,
            "oai-client-version": self.cached_client_version,
            "sec-ch-ua": _SEC_CH_UA,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "priority": "u=1, i",
        }

    def navigation_headers(self) -> dict:
        """sec-fetch-* suite for a real document navigation (the seed GET)."""
        return {
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
            "priority": "u=0, i",
        }

    def echo_logs(self) -> str:
        """``oai-echo-logs`` timing telemetry — simulates ~6–9 s of human
        think/type time before the send."""
        t1 = random.randint(6000, 9000)
        return f"0,{t1},1,{t1 + random.randint(1000, 1200)}"

    def contextual_info(self) -> dict:
        return {
            "is_dark_mode": self.dark_mode,
            "time_since_loaded": max(3, int(time.time() - self._page_loaded_at)),
            "page_height": int(self.screen_h * 0.85),
            "page_width": self.screen_w,
            "pixel_ratio": 1,
            "screen_height": self.screen_h,
            "screen_width": self.screen_w,
        }

    def fingerprint_config(self) -> list:
        """The 18-element browser fingerprint array for the ``p`` token.

        Matches the upstream layout (wrapper/chatgpt.py): indices 11–12 are
        randomized react-container/window keys, 13 is ms-scale page
        interaction time, 14 the per-session id, 17 the page-load timestamp.
        """
        now = datetime.now(ZoneInfo(self.timezone))
        tz_abbr = now.strftime("%Z") or "UTC"
        react_key = "".join(random.choices("abcdefghijklmnopqrstuvwxyz", k=10))
        return [
            4880,
            now.strftime(f"%a %b %d %Y %H:%M:%S GMT%z ({tz_abbr})"),
            4294705152,
            random.random(),
            self.ua,
            None,
            self.client_version,
            self.locale,
            self.nav_languages,
            random.random(),
            "webkitGetUserMedia−function webkitGetUserMedia() "
            "{ [native code] }",
            random.choice(
                ["location", f"__reactContainer${react_key}",
                 f"_reactListening${react_key}"]
            ),
            random.choice(_WINDOW_KEYS),
            random.randint(800, 1400) + random.random(),
            self.session_id,
            "",
            20,
            int(self._page_loaded_at * 1000),
        ]


_profile: SimProfile | None = None
_profile_lock = threading.Lock()


def get_profile(config: dict | None = None) -> SimProfile:
    """Process-wide singleton — one "browser" per server process."""
    global _profile
    with _profile_lock:
        if _profile is None:
            _profile = SimProfile(config)
        return _profile


def reset_profile() -> None:
    """Tests only."""
    global _profile
    with _profile_lock:
        if _profile is not None and _profile.session is not None:
            try:
                _profile.session.close()
            except Exception:
                pass
        _profile = None
