"""Process-environment guard for the account network transport."""

from __future__ import annotations

import os


def reject_tls_key_logging() -> None:
    """Reject libcurl TLS-secret logging before any account transport import."""
    if "SSLKEYLOGFILE" in os.environ:
        raise RuntimeError("SSLKEYLOGFILE is set; refusing account network access")


# Importing the account backend must enforce this before curl_cffi is imported.
reject_tls_key_logging()
