"""v0.0.12 backend safety and immutable-auth contract tests."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from curl_cffi import CurlOpt
from mcp import types
from mcp.shared.exceptions import UrlElicitationRequiredError

from gpt2agent import backend as backend_mod
from gpt2agent import sentinel as sentinel_mod
from gpt2agent import sse as sse_mod
from gpt2agent.errors import BackendContractError, BackendHTTPError
from gpt2agent.request_policy import RequestPolicy
from gpt2agent.tools._errors import SafeFastMCP
from gpt2agent.tools._validation import validate_int


def _write_auth(home: Path, token: str) -> Path:
    path = home / ".codex" / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tokens": {"access_token": token}}), encoding="utf-8")
    return path


class _Response:
    def __init__(
        self,
        status_code: int = 200,
        *,
        payload: Any = None,
        text: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)
        self.content = self.text.encode()
        self.headers = headers or {}

    def json(self) -> Any:
        if self._payload is ...:
            raise ValueError("not json")
        return self._payload


class _Session:
    def __init__(self, *_: Any, **kwargs: Any) -> None:
        self.headers: dict[str, str] = {}
        self.init_kwargs = kwargs
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.callback_results: list[int] = []
        self.delivered_chunks = 0
        self.response = _Response(payload={"ok": True})

    def _respond(self, method: str, url: str, kwargs: dict[str, Any]) -> _Response:
        self.calls.append((method, url, kwargs))
        callback = kwargs.get("content_callback")
        if callback is not None:
            chunks = getattr(self.response, "chunks", [self.response.content])
            for chunk in chunks:
                self.delivered_chunks += 1
                result = callback(chunk)
                self.callback_results.append(result)
                if result == 0xFFFFFFFF:
                    raise RuntimeError("synthetic curl write abort")
        return self.response

    def get(self, url: str, **kwargs: Any) -> _Response:
        return self._respond("GET", url, kwargs)

    def post(self, url: str, **kwargs: Any) -> _Response:
        return self._respond("POST", url, kwargs)


def _deliver_response(response: _Response, kwargs: dict[str, Any]) -> _Response:
    callback = kwargs.get("content_callback")
    if callback is None:
        return response
    for chunk in getattr(response, "chunks", [response.content]):
        if callback(chunk) == 0xFFFFFFFF:
            raise RuntimeError("synthetic curl write abort")
    return response


def _exception_tree(error: BaseException) -> list[BaseException]:
    """Return every explicit or implicit exception reachable from ``error``."""
    pending = [error]
    found: list[BaseException] = []
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        found.append(current)
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                pending.append(linked)
    return found


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> backend_mod.BackendClient:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_auth(tmp_path, "TOKEN_A")
    monkeypatch.setattr(backend_mod.requests, "Session", _Session)
    monkeypatch.setattr(backend_mod, "_REQUEST_POLICY", RequestPolicy())
    return backend_mod.BackendClient()


def test_authorization_is_request_local_and_params_are_structured(
    client: backend_mod.BackendClient,
) -> None:
    assert "Authorization" not in client._session.headers

    result = client.get("/backend-api/example", params={"limit": 1})

    assert result == {"ok": True}
    method, url, kwargs = client._session.calls[-1]
    assert method == "GET"
    assert url == "https://chatgpt.com/backend-api/example"
    assert kwargs["params"] == {"limit": 1}
    assert kwargs["headers"]["Authorization"] == "Bearer TOKEN_A"
    assert kwargs["allow_redirects"] is False
    assert "Authorization" not in client._session.headers


def test_backend_post_never_follows_redirects(
    client: backend_mod.BackendClient,
) -> None:
    client.post("/backend-api/example", json={"prompt": "private"})

    method, _url, kwargs = client._session.calls[-1]
    assert method == "POST"
    assert kwargs["allow_redirects"] is False


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (None, 20.0),
        (-5.0, 0.1),
        (0.0, 0.1),
        (0.05, 0.1),
        (1.25, 1.25),
        (20.0, 20.0),
        (90.0, 20.0),
    ],
)
def test_backend_get_clamps_per_call_timeout(
    client: backend_mod.BackendClient,
    requested: float | None,
    expected: float,
) -> None:
    client.get("/backend-api/example", timeout_seconds=requested)

    _method, _url, kwargs = client._session.calls[-1]
    assert kwargs["timeout"] == expected


@pytest.mark.parametrize("requested", [float("nan"), float("inf"), float("-inf")])
def test_backend_get_rejects_non_finite_timeout_before_network(
    client: backend_mod.BackendClient,
    requested: float,
) -> None:
    with pytest.raises(ValueError, match="timeout_seconds must be finite"):
        client.get("/backend-api/example", timeout_seconds=requested)

    assert client._session.calls == []


def test_auth_snapshot_is_immutable_across_rotation(
    client: backend_mod.BackendClient,
    tmp_path: Path,
) -> None:
    first = client.request_headers()
    first_generation = client.auth_generation

    auth = _write_auth(tmp_path, "TOKEN_B")
    stat = auth.stat()
    os.utime(auth, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    second = client.request_headers()

    assert first["Authorization"] == "Bearer TOKEN_A"
    assert second["Authorization"] == "Bearer TOKEN_B"
    assert first["Authorization"] == "Bearer TOKEN_A"
    assert client.auth_generation == first_generation + 1
    assert "Authorization" not in client._session.headers


def test_backend_http_error_is_structured_and_never_contains_body(
    client: backend_mod.BackendClient,
) -> None:
    secret = "Bearer SECRET account@example.com"
    client._session.response = _Response(403, payload=..., text=secret)

    with pytest.raises(BackendHTTPError) as caught:
        client.get("/backend-api/private?raw=secret")

    error = caught.value
    assert error.code == "access_indeterminate"
    assert error.method == "GET"
    assert error.route == "/backend-api/private"
    assert error.status_code == 403
    assert not error.retryable
    rendered = str(error)
    assert secret not in rendered
    assert "example.com" not in rendered
    assert "raw=secret" not in rendered


@pytest.mark.parametrize(
    ("route", "secret", "normalized"),
    [
        (
            "/backend-api/conversation/PRIVATE_CONVERSATION_ID?raw=secret",
            "PRIVATE_CONVERSATION_ID",
            "/backend-api/conversation/{id}",
        ),
        (
            "/backend-api/files/PRIVATE_FILE_ID/download",
            "PRIVATE_FILE_ID",
            "/backend-api/files/{id}/download",
        ),
    ],
)
def test_backend_error_normalizes_dynamic_account_identifiers(
    route: str, secret: str, normalized: str
) -> None:
    error = BackendHTTPError("GET", route, 404, code="contract_changed")

    assert error.route == normalized
    assert secret not in str(error)


@pytest.mark.parametrize(
    ("status", "established", "fixed_probe", "expected"),
    [
        (401, True, False, "login_required"),
        (403, True, False, "access_indeterminate"),
        (404, True, False, "contract_changed"),
        (405, False, False, "unsupported"),
        (422, True, False, "invalid_input"),
        (422, True, True, "contract_changed"),
        (429, True, False, "temporarily_failed"),
        (503, True, False, "temporarily_failed"),
    ],
)
def test_http_status_mapping_is_explicit(
    client: backend_mod.BackendClient,
    status: int,
    established: bool,
    fixed_probe: bool,
    expected: str,
) -> None:
    client._session.response = _Response(status, payload=..., text="private body")

    with pytest.raises(BackendHTTPError) as caught:
        client.get(
            "/backend-api/example",
            established=established,
            fixed_probe=fixed_probe,
        )

    assert caught.value.code == expected


def test_429_activates_only_the_normalized_route_cooldown(
    client: backend_mod.BackendClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    policy = RequestPolicy(clock=lambda: now[0], wall_clock=lambda: 0.0)
    monkeypatch.setattr(backend_mod, "_REQUEST_POLICY", policy)
    client._session.response = _Response(
        429,
        payload=...,
        text="private body",
        headers={"Retry-After": "7"},
    )

    with pytest.raises(BackendHTTPError) as first:
        client.get("/backend-api/rate-limited?private=one")
    assert first.value.retry_after == pytest.approx(7.0)
    assert len(client._session.calls) == 1

    with pytest.raises(BackendHTTPError) as blocked:
        client.get("/backend-api/rate-limited?private=two")
    assert blocked.value.route == "/backend-api/rate-limited"
    assert blocked.value.retry_after == pytest.approx(7.0)
    assert len(client._session.calls) == 1

    client._session.response = _Response(payload={"ok": True})
    assert client.get("/backend-api/unrelated") == {"ok": True}
    assert len(client._session.calls) == 2


def test_ordinary_json_rejects_declared_and_actual_oversize(
    client: backend_mod.BackendClient,
) -> None:
    client._session.response = _Response(
        payload={"ok": True}, headers={"Content-Length": str(4 * 1024 * 1024 + 1)}
    )
    with pytest.raises(BackendContractError, match="response exceeds"):
        client.get("/backend-api/example")

    client._session.response = _Response(payload={"data": "x" * (4 * 1024 * 1024)})
    with pytest.raises(BackendContractError, match="response exceeds"):
        client.get("/backend-api/example")


@pytest.mark.parametrize("method", ["get", "post"])
def test_backend_aborts_oversize_body_in_native_content_callback(
    client: backend_mod.BackendClient,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    monkeypatch.setattr(backend_mod, "_MAX_JSON_BYTES", 8)
    response = _Response(payload=..., text="")
    response.chunks = [b"12345678", b"9", b"must-not-be-consumed"]
    client._session.response = response

    with pytest.raises(BackendContractError, match="response exceeds"):
        getattr(client, method)("/backend-api/example")

    assert client._session.callback_results == [8, 0xFFFFFFFF]
    assert client._session.delivered_chunks == 2


def test_backend_sets_native_curl_filesize_limit(
    client: backend_mod.BackendClient,
) -> None:
    assert client._session.init_kwargs["curl_options"] == {
        CurlOpt.MAXFILESIZE_LARGE: 4 * 1024 * 1024
    }


@pytest.mark.parametrize("method", ["get", "post"])
def test_backend_network_failure_discards_unsafe_exception_chain(
    client: backend_mod.BackendClient,
    method: str,
) -> None:
    secret = "SYNTHETIC_BACKEND_NETWORK_SECRET account@example.com"

    def _fail(*_: Any, **__: Any) -> None:
        raise RuntimeError(secret)

    setattr(client._session, method, _fail)

    with pytest.raises(BackendHTTPError) as caught:
        getattr(client, method)("/backend-api/example")

    error = caught.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in "\n".join(str(item) for item in _exception_tree(error))


def test_backend_json_decode_failure_discards_unsafe_exception_chain(
    client: backend_mod.BackendClient,
) -> None:
    secret = "SYNTHETIC_BACKEND_JSON_SECRET account@example.com"

    class _UnsafeJSONResponse(_Response):
        def json(self) -> Any:
            raise ValueError(secret)

    client._session.response = _UnsafeJSONResponse(payload=..., text=secret)

    with pytest.raises(BackendContractError) as caught:
        client.get("/backend-api/example")

    error = caught.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in "\n".join(str(item) for item in _exception_tree(error))


def test_sentinel_uses_supplied_operation_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}
    request_options: dict[str, Any] = {}

    class _AsyncSession:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> "_AsyncSession":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def post(self, _url: str, *, headers: dict[str, str], **kwargs: Any) -> _Response:
            captured.update(headers)
            request_options.update(kwargs)
            return _deliver_response(_Response(payload={"token": "sentinel"}), kwargs)

    class _Backend:
        def request_headers(self) -> dict[str, str]:
            raise AssertionError("must not create a second snapshot")

    monkeypatch.setattr(sentinel_mod, "AsyncSession", _AsyncSession)
    monkeypatch.setattr(sentinel_mod._pow, "get_requirements_token", lambda _ua: "p")
    snapshot = {"Authorization": "Bearer OPERATION_A", "User-Agent": "ua"}

    result = asyncio.run(sentinel_mod.SentinelGate(_Backend()).get_tokens(snapshot))  # type: ignore[arg-type]

    assert result["chat-requirements"] == "sentinel"
    assert captured["Authorization"] == "Bearer OPERATION_A"
    assert request_options["allow_redirects"] is False
    assert snapshot == {"Authorization": "Bearer OPERATION_A", "User-Agent": "ua"}


def test_sentinel_http_failure_never_exposes_response_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "Bearer SENTINEL_SECRET account@example.com"

    class _AsyncSession:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> "_AsyncSession":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def post(self, *_: Any, **kwargs: Any) -> _Response:
            return _deliver_response(_Response(403, payload=..., text=secret), kwargs)

    class _Backend:
        def request_headers(self) -> dict[str, str]:
            return {"Authorization": "Bearer OPERATION_A", "User-Agent": "ua"}

    monkeypatch.setattr(sentinel_mod, "AsyncSession", _AsyncSession)
    monkeypatch.setattr(sentinel_mod._pow, "get_requirements_token", lambda _ua: "p")

    with pytest.raises(BackendHTTPError) as caught:
        asyncio.run(sentinel_mod.SentinelGate(_Backend()).get_tokens())  # type: ignore[arg-type]

    assert caught.value.code == "access_indeterminate"
    assert caught.value.route == "/backend-api/sentinel/chat-requirements"
    assert secret not in str(caught.value)
    assert "example.com" not in str(caught.value)


def test_sentinel_json_decode_failure_discards_unsafe_exception_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "SYNTHETIC_SENTINEL_JSON_SECRET account@example.com"

    class _UnsafeJSONResponse(_Response):
        def json(self) -> Any:
            raise ValueError(secret)

    class _AsyncSession:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> "_AsyncSession":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def post(self, *_: Any, **kwargs: Any) -> _Response:
            return _deliver_response(
                _UnsafeJSONResponse(payload=..., text=secret), kwargs
            )

    class _Backend:
        def request_headers(self) -> dict[str, str]:
            return {"Authorization": "Bearer OPERATION_A", "User-Agent": "ua"}

    monkeypatch.setattr(sentinel_mod, "AsyncSession", _AsyncSession)
    monkeypatch.setattr(sentinel_mod._pow, "get_requirements_token", lambda _ua: "p")

    with pytest.raises(BackendContractError) as caught:
        asyncio.run(sentinel_mod.SentinelGate(_Backend()).get_tokens())  # type: ignore[arg-type]

    error = caught.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in "\n".join(str(item) for item in _exception_tree(error))


def test_sentinel_rejects_declared_and_actual_oversize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _AsyncSession:
        response = _Response(payload={"token": "sentinel"})

        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> "_AsyncSession":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def post(self, *_: Any, **kwargs: Any) -> _Response:
            return _deliver_response(self.response, kwargs)

    class _Backend:
        def request_headers(self) -> dict[str, str]:
            return {"Authorization": "Bearer OPERATION_A", "User-Agent": "ua"}

    monkeypatch.setattr(sentinel_mod, "AsyncSession", _AsyncSession)
    monkeypatch.setattr(sentinel_mod._pow, "get_requirements_token", lambda _ua: "p")
    monkeypatch.setattr(sentinel_mod, "_MAX_JSON_BYTES", 8, raising=False)
    gate = sentinel_mod.SentinelGate(_Backend())  # type: ignore[arg-type]

    _AsyncSession.response = _Response(
        payload={"token": "sentinel"}, headers={"Content-Length": "9"}
    )
    with pytest.raises(BackendContractError, match="response exceeds"):
        asyncio.run(gate.get_tokens())

    _AsyncSession.response = _Response(payload={"token": "sentinel"})
    _AsyncSession.response.headers = {}
    _AsyncSession.response.content = b"x" * 9
    with pytest.raises(BackendContractError, match="response exceeds"):
        asyncio.run(gate.get_tokens())


def test_sentinel_aborts_oversize_body_in_native_content_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    class _AsyncSession:
        def __init__(self, *_: Any, **kwargs: Any) -> None:
            observed["init"] = kwargs

        async def __aenter__(self) -> "_AsyncSession":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def post(self, *_: Any, **kwargs: Any) -> _Response:
            observed["request"] = kwargs
            response = _Response(payload=..., text="")
            response.chunks = [b"12345678", b"9", b"must-not-be-consumed"]
            return _deliver_response(response, kwargs)

    class _Backend:
        def request_headers(self) -> dict[str, str]:
            return {"Authorization": "Bearer OPERATION_A", "User-Agent": "ua"}

    monkeypatch.setattr(sentinel_mod, "AsyncSession", _AsyncSession)
    monkeypatch.setattr(sentinel_mod._pow, "get_requirements_token", lambda _ua: "p")
    monkeypatch.setattr(sentinel_mod, "_MAX_JSON_BYTES", 8)

    with pytest.raises(BackendContractError, match="response exceeds"):
        asyncio.run(sentinel_mod.SentinelGate(_Backend()).get_tokens())  # type: ignore[arg-type]

    assert observed["init"]["curl_options"] == {CurlOpt.MAXFILESIZE_LARGE: 8}
    assert callable(observed["request"]["content_callback"])


def test_sentinel_network_failure_discards_unsafe_exception_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "SYNTHETIC_SENTINEL_NETWORK_SECRET account@example.com"

    class _AsyncSession:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> "_AsyncSession":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def post(self, *_: Any, **__: Any) -> _Response:
            raise RuntimeError(secret)

    class _Backend:
        def request_headers(self) -> dict[str, str]:
            return {"Authorization": "Bearer OPERATION_A", "User-Agent": "ua"}

    monkeypatch.setattr(sentinel_mod, "AsyncSession", _AsyncSession)
    monkeypatch.setattr(sentinel_mod._pow, "get_requirements_token", lambda _ua: "p")

    with pytest.raises(BackendHTTPError) as caught:
        asyncio.run(sentinel_mod.SentinelGate(_Backend()).get_tokens())  # type: ignore[arg-type]

    error = caught.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in "\n".join(str(item) for item in _exception_tree(error))


def test_sse_network_failure_discards_unsafe_exception_chain() -> None:
    secret = "SYNTHETIC_SSE_NETWORK_SECRET account@example.com"

    class _Session:
        async def post(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError(secret)

    with pytest.raises(BackendHTTPError) as caught:
        asyncio.run(
            sse_mod._post_account_stream(
                _Session(),
                "https://chatgpt.com/backend-api/conversation",
                "/backend-api/conversation",
            )
        )

    error = caught.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in "\n".join(str(item) for item in _exception_tree(error))


def test_sse_stream_post_never_follows_redirects() -> None:
    captured: dict[str, Any] = {}

    class _Session:
        async def post(self, *_: Any, **kwargs: Any) -> _Response:
            captured.update(kwargs)
            return _Response(payload={})

    asyncio.run(
        sse_mod._post_account_stream(
            _Session(),
            "https://chatgpt.com/backend-api/conversation",
            "/backend-api/conversation",
            json={"prompt": "private"},
        )
    )

    assert captured["allow_redirects"] is False


def test_sse_lines_have_per_event_and_cumulative_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Chunks:
        def __init__(self, chunks: list[bytes]) -> None:
            self.chunks = chunks

        async def aiter_content(self):
            for chunk in self.chunks:
                yield chunk

    async def _collect(chunks: list[bytes]) -> list[str]:
        return [
            line
            async for line in sse_mod._bounded_sse_lines(
                _Chunks(chunks), route="/backend-api/conversation"
            )
        ]

    monkeypatch.setattr(sse_mod, "_MAX_SSE_LINE_BYTES", 8, raising=False)
    monkeypatch.setattr(sse_mod, "_MAX_SSE_STREAM_BYTES", 12, raising=False)

    assert asyncio.run(_collect([b"data: x\r", b"\nok\n"])) == ["data: x", "ok"]
    with pytest.raises(BackendContractError, match="event line exceeds"):
        asyncio.run(_collect([b"x" * 9]))
    with pytest.raises(BackendContractError, match="stream exceeds"):
        asyncio.run(_collect([b"x" * 7 + b"\n", b"y" * 7 + b"\n"]))


def test_sse_line_limit_aborts_before_consuming_newline_free_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Chunks:
        produced = 0

        async def aiter_content(self):
            for _ in range(100):
                self.produced += 1
                yield b"x"

        async def aiter_lines(self):
            buffered = bytearray()
            async for chunk in self.aiter_content():
                buffered.extend(chunk)
            yield bytes(buffered)

    response = _Chunks()
    monkeypatch.setattr(sse_mod, "_MAX_SSE_LINE_BYTES", 8)
    monkeypatch.setattr(sse_mod, "_MAX_SSE_STREAM_BYTES", 1_000)

    async def _collect() -> list[str]:
        return [
            line
            async for line in sse_mod._bounded_sse_lines(
                response, route="/backend-api/conversation"
            )
        ]

    with pytest.raises(BackendContractError, match="event line exceeds"):
        asyncio.run(_collect())
    assert response.produced == 9


def test_sse_byte_parser_handles_split_crlf_bare_cr_and_eof() -> None:
    class _Chunks:
        async def aiter_content(self):
            for chunk in [b"data: a\r", b"\n\rdata: b\rdata: c\n", b"tail"]:
                yield chunk

    async def _collect() -> list[str]:
        return [
            line
            async for line in sse_mod._bounded_sse_lines(
                _Chunks(), route="/backend-api/conversation"
            )
        ]

    assert asyncio.run(_collect()) == ["data: a", "", "data: b", "data: c", "tail"]


def test_sse_read_failure_discards_unsafe_exception_chain() -> None:
    secret = "SYNTHETIC_SSE_READ_SECRET account@example.com"

    class _Chunks:
        async def aiter_content(self):
            yield b"data: ok\n"
            raise RuntimeError(secret)

    async def _collect() -> list[str]:
        return [
            line
            async for line in sse_mod._bounded_sse_lines(
                _Chunks(), route="/backend-api/conversation"
            )
        ]

    with pytest.raises(BackendHTTPError) as caught:
        asyncio.run(_collect())

    error = caught.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in "\n".join(str(item) for item in _exception_tree(error))


def test_stream_session_sets_native_curl_filesize_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    class _AsyncSession:
        def __init__(self, *_: Any, **kwargs: Any) -> None:
            observed.update(kwargs)

    monkeypatch.setattr(sse_mod, "AsyncSession", _AsyncSession)

    assert isinstance(sse_mod._stream_session(), _AsyncSession)
    assert observed["curl_options"] == {
        CurlOpt.MAXFILESIZE_LARGE: sse_mod._MAX_SSE_STREAM_BYTES
    }


def test_stream_http_failure_never_exposes_response_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "Bearer STREAM_SECRET account@example.com"

    class _Backend:
        def request_headers(self) -> dict[str, str]:
            return {"Authorization": "Bearer OPERATION_A", "User-Agent": "ua"}

    class _Sentinel:
        def __init__(self, _backend: Any) -> None:
            pass

        async def get_tokens(self, _headers: dict[str, str]) -> dict[str, str]:
            return {"chat-requirements": "s", "proof": "", "turnstile": ""}

    class _AsyncSession:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> "_AsyncSession":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def post(self, *_: Any, **__: Any) -> _Response:
            return _Response(403, payload=..., text=secret)

    monkeypatch.setattr(sse_mod, "SentinelGate", _Sentinel)
    monkeypatch.setattr(sse_mod, "AsyncSession", _AsyncSession)
    stream = sse_mod.ConversationClient(_Backend()).stream(  # type: ignore[arg-type]
        "gpt-5-3", [{"role": "user", "content": "q"}]
    )

    with pytest.raises(BackendHTTPError) as caught:
        asyncio.run(anext(stream))

    assert caught.value.code == "access_indeterminate"
    assert caught.value.route == "/backend-api/conversation"
    assert secret not in str(caught.value)
    assert "example.com" not in str(caught.value)


def test_stream_pairs_sentinel_and_conversation_to_one_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    class _Backend:
        def __init__(self) -> None:
            self.calls = 0

        def request_headers(self) -> dict[str, str]:
            self.calls += 1
            return {"Authorization": f"Bearer TOKEN_{self.calls}", "User-Agent": "ua"}

    class _Sentinel:
        def __init__(self, _backend: Any) -> None:
            pass

        async def get_tokens(self, headers: dict[str, str]) -> dict[str, str]:
            seen["sentinel"] = headers["Authorization"]
            return {"chat-requirements": "s", "proof": "", "turnstile": ""}

    class _StreamResponse(_Response):
        async def aiter_content(self):
            payload = {
                "message": {
                    "id": "m",
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["ok"]},
                    "status": "finished_successfully",
                }
            }
            yield ("data: " + json.dumps(payload) + "\n").encode()
            yield b"data: [DONE]\n"

    class _AsyncSession:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> "_AsyncSession":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def post(self, _url: str, *, headers: dict[str, str], **__: Any) -> _StreamResponse:
            seen["conversation"] = headers["Authorization"]
            return _StreamResponse(payload={})

    monkeypatch.setattr(sse_mod, "SentinelGate", _Sentinel)
    monkeypatch.setattr(sse_mod, "AsyncSession", _AsyncSession)
    backend = _Backend()
    client = sse_mod.ConversationClient(backend)  # type: ignore[arg-type]

    async def _collect() -> list[Any]:
        return [part async for part in client.stream("gpt-5-3", [{"role": "user", "content": "q"}])]

    assert asyncio.run(_collect()) == ["ok"]
    assert seen == {"sentinel": "Bearer TOKEN_1", "conversation": "Bearer TOKEN_1"}
    assert backend.calls == 1


def test_token_rotation_waits_for_next_compound_stream_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel_started = asyncio.Event()
    release_sentinel = asyncio.Event()
    seen: dict[str, list[str]] = {"sentinel": [], "conversation": []}

    class _Backend:
        def __init__(self) -> None:
            self.token = "TOKEN_A"
            self.snapshots = 0

        def request_headers(self) -> dict[str, str]:
            self.snapshots += 1
            return {
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "ua",
            }

    class _Sentinel:
        def __init__(self, _backend: Any) -> None:
            pass

        async def get_tokens(self, headers: dict[str, str]) -> dict[str, str]:
            seen["sentinel"].append(headers["Authorization"])
            if len(seen["sentinel"]) == 1:
                sentinel_started.set()
                await release_sentinel.wait()
            return {"chat-requirements": "s", "proof": "", "turnstile": ""}

    class _StreamResponse(_Response):
        async def aiter_content(self):
            payload = {
                "message": {
                    "id": "m",
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["ok"]},
                    "status": "finished_successfully",
                }
            }
            yield ("data: " + json.dumps(payload) + "\n").encode()
            yield b"data: [DONE]\n"

    class _AsyncSession:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> "_AsyncSession":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def post(
            self, _url: str, *, headers: dict[str, str], **__: Any
        ) -> _StreamResponse:
            seen["conversation"].append(headers["Authorization"])
            return _StreamResponse(payload={})

    monkeypatch.setattr(sse_mod, "SentinelGate", _Sentinel)
    monkeypatch.setattr(sse_mod, "AsyncSession", _AsyncSession)
    backend = _Backend()
    client = sse_mod.ConversationClient(backend)  # type: ignore[arg-type]

    async def _collect() -> list[Any]:
        return [
            part
            async for part in client.stream(
                "gpt-5-3", [{"role": "user", "content": "q"}]
            )
        ]

    async def _exercise_rotation() -> None:
        first = asyncio.create_task(_collect())
        await sentinel_started.wait()
        backend.token = "TOKEN_B"
        release_sentinel.set()
        assert await first == ["ok"]
        assert await _collect() == ["ok"]

    asyncio.run(_exercise_rotation())

    assert seen == {
        "sentinel": ["Bearer TOKEN_A", "Bearer TOKEN_B"],
        "conversation": ["Bearer TOKEN_A", "Bearer TOKEN_B"],
    }
    assert backend.snapshots == 2


def _call_tool_result(mcp: SafeFastMCP, name: str):
    request = types.CallToolRequest(
        params=types.CallToolRequestParams(name=name, arguments={})
    )
    handler = mcp._mcp_server.request_handlers[types.CallToolRequest]
    return asyncio.run(handler(request)).root


def test_safe_tool_boundary_preserves_typed_error_and_protocol_is_error() -> None:
    mcp = SafeFastMCP("typed-error-test")

    @mcp.tool()
    async def typed_failure() -> str:
        raise BackendHTTPError(
            "GET",
            "/backend-api/conversation/PRIVATE_ID?token=SECRET",
            403,
            code="access_indeterminate",
        )

    result = _call_tool_result(mcp, "typed_failure")
    rendered = result.content[0].text

    assert result.isError is True
    assert rendered == (
        "access_indeterminate: GET /backend-api/conversation/{id} failed (403)"
    )
    assert "PRIVATE_ID" not in rendered
    assert "SECRET" not in rendered


def test_safe_tool_boundary_preserves_local_invalid_input() -> None:
    mcp = SafeFastMCP("invalid-input-test")

    @mcp.tool()
    async def bounded_read(limit: int = 0) -> str:
        validate_int(limit, name="limit", minimum=1, maximum=50)
        return "ok"

    result = _call_tool_result(mcp, "bounded_read")

    assert result.isError is True
    assert result.content[0].text == "invalid_input: limit must be an integer from 1 through 50"


def test_safe_tool_boundary_never_serializes_unknown_exception_content() -> None:
    mcp = SafeFastMCP("unknown-error-test")

    @mcp.tool()
    async def unknown_failure() -> str:
        raise ValueError("Bearer SECRET account@example.com PRIVATE_ACCOUNT_ID")

    result = _call_tool_result(mcp, "unknown_failure")
    rendered = result.content[0].text

    assert result.isError is True
    assert rendered == "unavailable: tool execution failed"
    assert "SECRET" not in rendered
    assert "example.com" not in rendered
    assert "PRIVATE_ACCOUNT_ID" not in rendered


def test_safe_tool_boundary_preserves_sdk_url_elicitation_control_flow() -> None:
    mcp = SafeFastMCP("elicitation-test")
    required = UrlElicitationRequiredError([], "authorization required")

    @mcp.tool()
    async def requires_elicitation() -> str:
        raise required

    with pytest.raises(UrlElicitationRequiredError) as caught:
        asyncio.run(mcp.call_tool("requires_elicitation", {}))

    assert caught.value is required


def test_complete_reuses_supplied_auth_snapshot_for_async_poll() -> None:
    snapshot = {"Authorization": "Bearer OPERATION_A", "User-Agent": "ua"}
    seen: list[dict[str, str]] = []

    class _Backend:
        def request_headers(self) -> dict[str, str]:
            raise AssertionError("complete must reuse the supplied snapshot")

        def get(self, _path: str, **kwargs: Any) -> dict:
            seen.append(kwargs["auth_headers"])
            return {
                "mapping": {
                    "n": {
                        "message": {
                            "author": {"role": "assistant"},
                            "content": {"content_type": "text", "parts": ["done"]},
                            "status": "finished_successfully",
                            "create_time": 1,
                        }
                    }
                }
            }

    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    async def stream(*_args: Any, auth_headers=None, **_kwargs: Any):
        assert auth_headers == snapshot
        yield {"_conversation_id": "PRIVATE_CONVERSATION_ID"}

    client.stream = stream  # type: ignore[method-assign]
    result = asyncio.run(
        client.complete(
            "model",
            [{"role": "user", "content": "q"}],
            poll_async=True,
            auth_headers=snapshot,
        )
    )

    assert result == (
        "done\n\n---\nTool activity receipt: `none`. "
        "Private dispatch payloads were withheld."
    )
    assert seen == [snapshot]


def test_image_poll_reuses_initial_operation_auth_snapshot() -> None:
    snapshot = {"Authorization": "Bearer OPERATION_A", "User-Agent": "ua"}
    seen: list[dict[str, str]] = []

    class _Backend:
        def get(self, _path: str, **kwargs: Any) -> dict:
            seen.append(kwargs["auth_headers"])
            return {
                "mapping": {
                    "n": {
                        "message": {
                            "id": "image-result",
                            "author": {"role": "tool", "name": "image-tool"},
                            "recipient": "all",
                            "status": "finished_successfully",
                            "content": {
                                "content_type": "multimodal_text",
                                "parts": [
                                    {
                                        "content_type": "image_asset_pointer",
                                        "asset_pointer": "sediment://file-safe",
                                        "metadata": {
                                            "generation": {
                                                "serialization_title": (
                                                    "Image Generation metadata"
                                                )
                                            }
                                        },
                                    }
                                ],
                            },
                            "metadata": {"async_task_type": "image_gen"},
                            "create_time": 1,
                        }
                    }
                }
            }

    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]
    result = asyncio.run(
        client._poll_image_result(
            "PRIVATE_CONVERSATION_ID",
            poll_interval=0,
            max_wait=1,
            auth_headers=snapshot,
            marked_message_ids={"image-result"},
            marker_protocol_seen=True,
        )
    )

    assert result["assets"][0]["file_id"] == "file-safe"
    assert seen == [snapshot]


@pytest.mark.parametrize("poller", ["agent", "image"])
def test_poll_retry_exhaustion_discards_unsafe_exception_chain(poller: str) -> None:
    secret = "SYNTHETIC_POLL_NETWORK_SECRET account@example.com"

    class _Backend:
        def get(self, *_: Any, **__: Any) -> dict:
            raise RuntimeError(secret)

    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]
    if poller == "agent":
        awaitable = client._poll_async_response(
            "PRIVATE_CONVERSATION_ID", poll_interval=0, max_wait=1
        )
    else:
        awaitable = client._poll_image_result(
            "PRIVATE_CONVERSATION_ID", poll_interval=0, max_wait=1
        )

    with pytest.raises(BackendHTTPError) as caught:
        asyncio.run(awaitable)

    error = caught.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in "\n".join(str(item) for item in _exception_tree(error))


def test_heavy_dr_poll_propagates_nonretryable_http_failure() -> None:
    class _Backend:
        calls = 0

        def get(self, *_: Any, **__: Any) -> dict:
            self.calls += 1
            raise BackendHTTPError(
                "GET",
                "/backend-api/conversation/PRIVATE_CONVERSATION_ID",
                401,
                code="login_required",
                retryable=False,
            )

    backend = _Backend()
    client = sse_mod.ConversationClient(backend)  # type: ignore[arg-type]

    async def _poll() -> list[dict]:
        return [
            event
            async for event in client._poll_dr_completion(
                "PRIVATE_CONVERSATION_ID", interval=0, max_wait=0.01
            )
        ]

    with pytest.raises(BackendHTTPError) as caught:
        asyncio.run(_poll())

    assert caught.value.code == "login_required"
    assert caught.value.retryable is False
    assert backend.calls == 1
