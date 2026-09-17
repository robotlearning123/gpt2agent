"""Tests for gpt2agent.sentinel_vm — clean-room Turnstile token generator.

Spec: docs/dev/specs/sentinel-vm.md. Fixture-driven, seeded, fully offline.

IMPORTANT: `gpt2agent.sentinel_vm` is imported INSIDE each test function, not
at module scope and NOT via pytest.importorskip. The module does not exist
yet (tests-first); importing in the test body makes each test FAIL with
ModuleNotFoundError while a module-scope import would error collection and
importorskip would silently skip — neither shows the intended red state.
"""

from __future__ import annotations

import base64
import json
import random
from pathlib import Path

import pytest

FIXTURES_PATH = Path(__file__).parent / "fixtures" / "sentinel_vm" / "fixtures.json"


def _load() -> dict:
    return json.loads(FIXTURES_PATH.read_text())


def _fixture(i: int) -> dict:
    return _load()["fixtures"][i]


def _encode_dx(program_json: str, p: str) -> str:
    """Inverse of spec S1: cyclic-XOR the program text with `p`, then base64."""
    xored = "".join(chr(ord(c) ^ ord(p[i % len(p)])) for i, c in enumerate(program_json))
    return base64.b64encode(xored.encode("utf-8")).decode("ascii")


def test_fixture_file_shape():
    data = _load()
    assert {"fixtures", "localstorage_keys"} <= set(data)
    assert len(data["fixtures"]) == 3
    for fx in data["fixtures"]:
        assert {"dx", "p", "ip", "seed", "expected_token"} <= set(fx)


@pytest.mark.parametrize("i", [0, 1, 2])
def test_turnstile_token_byte_identical(i):
    from gpt2agent.sentinel_vm import turnstile_token

    fx = _fixture(i)
    token = turnstile_token(fx["dx"], fx["p"], fx["ip"], random.Random(fx["seed"]))
    assert isinstance(token, str)
    assert token == fx["expected_token"]


@pytest.mark.parametrize("i", [0, 1, 2])
def test_decode_instructions_row_count(i):
    from gpt2agent.sentinel_vm import decode_instructions

    fx = _fixture(i)
    rows = decode_instructions(fx["dx"], fx["p"])
    assert isinstance(rows, list)
    assert len(rows) >= 80
    assert len(rows) == fx["n_instructions"]


def test_invalid_base64_dx_raises():
    from gpt2agent.sentinel_vm import SentinelVMError, decode_instructions, turnstile_token

    fx = _fixture(0)
    with pytest.raises(SentinelVMError):
        turnstile_token("!!not-base64!!", fx["p"], fx["ip"], random.Random(0))
    with pytest.raises(SentinelVMError):
        decode_instructions("!!not-base64!!", fx["p"])


def test_structural_surprise_raises():
    from gpt2agent.sentinel_vm import SentinelVMError, turnstile_token

    fx = _fixture(0)
    p, ip = fx["p"], fx["ip"]

    # Decodes cleanly through S1 but yields a JSON object, not an
    # instruction list — a structural surprise that must fail closed.
    not_a_list_dx = _encode_dx(json.dumps({"not": "an instruction list"}), p)
    with pytest.raises(SentinelVMError):
        turnstile_token(not_a_list_dx, p, ip, random.Random(0))

    # A program of only unknown opcodes: no xor_key can be recovered, so the
    # VM cannot produce a token and must raise rather than emit garbage.
    unknown_ops_dx = _encode_dx(json.dumps([[999, 1, 2], [998, 0]]), p)
    with pytest.raises(SentinelVMError):
        turnstile_token(unknown_ops_dx, p, ip, random.Random(0))


def test_server_import_unaffected():
    import gpt2agent.server  # noqa: F401
