import re

# PII patterns.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"\+?\d[\d ()\-]{8,}\d")
# Calendar dates satisfy _PHONE_RE ("2026-05-26" is 10 digit/dash chars), so a
# phone match that *starts* with a date shape keeps the date — dates in
# memories/tasks/conversations vastly outnumber phone numbers written with a
# leading date. Only the date itself is preserved; the rest of the match is
# re-scanned so "2026-05-26 617-555-0123" still masks the phone.
_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}|\d{1,2}-\d{1,2}-\d{4})(?=$|[^\d])")


def _phone_repl(m: re.Match) -> str:
    text = m.group(0)
    parts = []
    position = 0
    while position < len(text):
        candidate = _PHONE_RE.search(text, position)
        if candidate is None:
            parts.append(text[position:])
            break

        parts.append(text[position:candidate.start()])
        date = _DATE_PREFIX_RE.match(candidate.group(0))
        if date is None:
            parts.append("<PHONE>")
            position = candidate.end()
            continue

        prefix = date.group(1)
        parts.append(prefix)
        position = candidate.start() + len(prefix)
    return "".join(parts)

# Secret patterns. Users routinely paste API keys / tokens into ChatGPT, so they
# end up in memories, tasks, and custom instructions — which these tools return
# verbatim. Redact the common structured-secret shapes here so tool OUTPUT never
# echoes one back. Kept separate from gpt2agent._log_redact (which scrubs session
# headers from error bodies) so a change to one scope can't silently weaken the
# other. Each pattern is long/structured enough not to match ordinary prose.
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/\-]{16,}=*", re.IGNORECASE)
_APIKEY_RE = re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}")
_GH_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")
_SLACK_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:xox[baprs]-[A-Za-z0-9-]{20,}|xapp-[A-Za-z0-9-]{20,})"
    r"(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
_GOOGLE_API_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9_-])AIza[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])"
)
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?P<kind>(?:(?:DSA|EC|ENCRYPTED|OPENSSH|RSA) )?PRIVATE KEY)-----"
    r".{0,262144}?"
    r"-----END (?P=kind)-----",
    re.DOTALL,
)
_DB_CREDENTIAL_URL_RE = re.compile(
    r"(?P<scheme>\b(?:cockroachdb|mariadb|mongodb(?:\+srv)?|"
    r"mysql(?:\+[A-Za-z0-9_.-]+)?|postgres(?:ql)?|rediss?)://)"
    r"[^:@/\s]+:[^@\s]+@",
    re.IGNORECASE,
)

# Assignment matching is deliberately label-aware instead of entropy-based.
# The prefixed branch recognizes established credential labels such as
# ``AWS_SECRET_ACCESS_KEY`` and ``service_token``. Bare ``key`` uses a separate
# assignment-position regex so ordinary prose and names such as ``keyboard_key``
# remain intact.
_SECRET_LABEL = (
    r"(?:(?:[A-Za-z][A-Za-z0-9]*[_-])*"
    r"(?:password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|"
    r"secret[_-]?key|private[_-]?key|encryption[_-]?key|signing[_-]?key))"
)
_SECRET_VALUE = r"\"[^\"\r\n]+\"|'[^'\r\n]+'|Bearer\s+[^\s,;}\]]+|[^\s,;}\]]+"
_SECRET_ASSIGNMENT_RE = re.compile(
    rf"(?P<prefix>(?P<label_quote>[\"']?)(?P<label>\b{_SECRET_LABEL}\b)"
    rf"(?P=label_quote)\s*(?:=|:)\s*)"
    rf"(?P<value>{_SECRET_VALUE})",
    re.IGNORECASE,
)
_BARE_KEY_ASSIGNMENT_RE = re.compile(
    rf"(?P<prefix>(?:^|(?<=[{{[,;]))[ \t]*(?P<label_quote>[\"']?)\bkey\b"
    rf"(?P=label_quote)\s*(?:=|:)\s*)(?P<value>{_SECRET_VALUE})",
    re.IGNORECASE | re.MULTILINE,
)
_REDACTION_MARKERS = frozenset(
    {
        "<APIKEY>",
        "<JWT>",
        "<PRIVATE_KEY>",
        "<REDACTED>",
        "<TOKEN>",
        "Bearer <REDACTED>",
    }
)


def _secret_assignment_repl(match: re.Match) -> str:
    """Preserve an assignment's label/quoting while removing its value."""
    raw_value = match.group("value")
    quote = raw_value[0] if raw_value[:1] in {"\"", "'"} else ""
    value = raw_value[1:-1] if quote and raw_value.endswith(quote) else raw_value
    if value in _REDACTION_MARKERS:
        return match.group(0)
    return f"{match.group('prefix')}{quote}<REDACTED>{quote}"


def redact(s: object) -> object:
    if not isinstance(s, str):
        return s
    # Secrets first (a JWT/bearer must not survive by being partly eaten later).
    s = _JWT_RE.sub("<JWT>", s)
    s = _BEARER_RE.sub("Bearer <REDACTED>", s)
    s = _APIKEY_RE.sub("<APIKEY>", s)
    s = _GH_TOKEN_RE.sub("<TOKEN>", s)
    s = _SLACK_TOKEN_RE.sub("<TOKEN>", s)
    s = _GOOGLE_API_KEY_RE.sub("<APIKEY>", s)
    s = _PEM_PRIVATE_KEY_RE.sub("<PRIVATE_KEY>", s)
    s = _DB_CREDENTIAL_URL_RE.sub(r"\g<scheme><REDACTED>@", s)
    s = _SECRET_ASSIGNMENT_RE.sub(_secret_assignment_repl, s)
    s = _BARE_KEY_ASSIGNMENT_RE.sub(_secret_assignment_repl, s)
    s = _EMAIL_RE.sub("<EMAIL>", s)
    s = _PHONE_RE.sub(_phone_repl, s)
    return s
