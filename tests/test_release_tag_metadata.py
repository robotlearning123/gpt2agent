"""Tests for the closed annotated release-tag metadata contract."""

from __future__ import annotations

import copy
import importlib
import json
import os
from pathlib import Path

import pytest

import scripts.release_tag_metadata as tag_metadata


COMMIT = "1" * 40
TREE = "2" * 40
RECEIPT_SHA256 = "3" * 64
ARTIFACT_SET_SHA256 = "4" * 64
ARTIFACT_DIGEST = "sha256:" + "5" * 64


def _metadata() -> dict[str, object]:
    return tag_metadata.build_metadata(
        repository="robotlearning123/gpt2agent",
        tag="v0.0.12",
        version="0.0.12",
        commit=COMMIT,
        tree=TREE,
        receipt_sha256=RECEIPT_SHA256,
        artifact_set_sha256=ARTIFACT_SET_SHA256,
        candidate_run_id=123456,
        candidate_run_attempt=2,
        candidate_artifact_id=987654,
        candidate_artifact_digest=ARTIFACT_DIGEST,
        candidate_artifact_size=314159,
        candidate_artifact_expires_at="2099-07-10T13:17:42Z",
    )


def _tag_object(metadata: dict[str, object] | None = None) -> bytes:
    value = metadata or _metadata()
    return (
        f"object {COMMIT}\n"
        "type commit\n"
        "tag v0.0.12\n"
        "tagger Release Bot <release@example.invalid> 4070908800 +0000\n"
        "\n"
        f"{tag_metadata.render_tag_message(value)}"
    ).encode("ascii")


def test_release_tag_metadata_exposes_closed_contract() -> None:
    module = importlib.import_module("scripts.release_tag_metadata")

    assert all(
        callable(getattr(module, name, None))
        for name in (
            "build_metadata",
            "validate_metadata",
            "render_tag_message",
            "parse_tag_message",
            "build_github_tag_request",
            "render_github_tag_request",
            "verify_tag_object_file",
            "main",
        )
    )


def test_build_render_parse_and_request_are_one_exact_canonical_contract() -> None:
    metadata = _metadata()

    assert metadata == {
        "schema_version": "1",
        "repository": "robotlearning123/gpt2agent",
        "tag": "v0.0.12",
        "version": "0.0.12",
        "source": {"commit": COMMIT, "tree": TREE},
        "account": {
            "receipt_sha256": RECEIPT_SHA256,
            "artifact_set_sha256": ARTIFACT_SET_SHA256,
        },
        "candidate": {
            "run_id": 123456,
            "run_attempt": 2,
            "artifact_id": 987654,
            "artifact_digest": ARTIFACT_DIGEST,
            "artifact_size": 314159,
            "artifact_expires_at": "2099-07-10T13:17:42Z",
        },
    }
    message = tag_metadata.render_tag_message(metadata)
    expected_json = json.dumps(metadata, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    assert message == f"gpt2agent 0.0.12\n\n{expected_json}\n"
    assert message.isascii()
    assert tag_metadata.parse_tag_message(message) == metadata
    assert tag_metadata.parse_tag_message(message.encode("ascii")) == metadata
    assert tag_metadata.validate_metadata(metadata) == metadata

    request = tag_metadata.build_github_tag_request(metadata)
    assert request == {
        "message": message,
        "object": COMMIT,
        "tag": "v0.0.12",
        "type": "commit",
    }
    expected_request = (
        json.dumps(
            request,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        + b"\n"
    )
    assert tag_metadata.render_github_tag_request(metadata) == expected_request


@pytest.mark.parametrize(
    ("path", "invalid"),
    [
        (("schema_version",), 1),
        (("schema_version",), "2"),
        (("repository",), "robotlearning123//gpt2agent"),
        (("repository",), "röbot/gpt2agent"),
        (("tag",), "0.0.12"),
        (("tag",), "v0.0.13"),
        (("version",), "00.0.12"),
        (("source", "commit"), "A" * 40),
        (("source", "tree"), "2" * 39),
        (("account", "receipt_sha256"), "A" * 64),
        (("account", "artifact_set_sha256"), "4" * 63),
        (("candidate", "run_id"), True),
        (("candidate", "run_id"), "123456"),
        (("candidate", "run_id"), 0),
        (("candidate", "run_attempt"), 1_000_000_001),
        (("candidate", "artifact_id"), -1),
        (("candidate", "artifact_digest"), "5" * 64),
        (("candidate", "artifact_size"), 1_000_000_000_001),
        (("candidate", "artifact_expires_at"), "2099-02-30T13:17:42Z"),
        (("candidate", "artifact_expires_at"), "2099-07-10T13:17:42+00:00"),
    ],
)
def test_validate_metadata_rejects_invalid_fields(
    path: tuple[str, ...],
    invalid: object,
) -> None:
    metadata = copy.deepcopy(_metadata())
    target = metadata
    for component in path[:-1]:
        target = target[component]  # type: ignore[assignment,index]
    target[path[-1]] = invalid

    with pytest.raises(tag_metadata.TagMetadataError):
        tag_metadata.validate_metadata(metadata)


@pytest.mark.parametrize("container", [(), ("source",), ("account",), ("candidate",)])
def test_validate_metadata_rejects_unknown_keys(container: tuple[str, ...]) -> None:
    metadata = copy.deepcopy(_metadata())
    target = metadata
    for component in container:
        target = target[component]  # type: ignore[assignment,index]
    target["unexpected"] = "not permitted"

    with pytest.raises(tag_metadata.TagMetadataError, match="schema"):
        tag_metadata.validate_metadata(metadata)


def test_parse_rejects_duplicate_keys_at_every_json_depth() -> None:
    message = tag_metadata.render_tag_message(_metadata())
    top_level = message.replace(
        '"tag":"v0.0.12"',
        '"tag":"v0.0.12","tag":"v0.0.12"',
    )
    nested = message.replace(
        f'"receipt_sha256":"{RECEIPT_SHA256}"',
        f'"receipt_sha256":"{RECEIPT_SHA256}","receipt_sha256":"{RECEIPT_SHA256}"',
    )

    for invalid in (top_level, nested):
        with pytest.raises(tag_metadata.TagMetadataError, match="duplicate"):
            tag_metadata.parse_tag_message(invalid)


def test_parse_rejects_valid_json_that_is_not_byte_exact_canonical_json() -> None:
    metadata = _metadata()
    noncanonical = f"gpt2agent 0.0.12\n\n{json.dumps(metadata)}\n"

    with pytest.raises(tag_metadata.TagMetadataError, match="canonical"):
        tag_metadata.parse_tag_message(noncanonical)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value.replace("gpt2agent 0.0.12", "gpt2agent v0.0.12", 1),
        lambda value: value[:-1],
        lambda value: value + "\n",
        lambda value: value.replace("\n", "\r\n"),
        lambda value: value.replace("\n\n", "\ncomment\n", 1),
    ],
)
def test_parse_rejects_nonexact_message_envelopes(mutator) -> None:
    message = tag_metadata.render_tag_message(_metadata())

    with pytest.raises(tag_metadata.TagMetadataError):
        tag_metadata.parse_tag_message(mutator(message))


@pytest.mark.parametrize("invalid", [b"\xff", b"gpt2agent 0.0.12\n\n{}\x00\n"])
def test_parse_rejects_non_ascii_and_nul_bytes(invalid: bytes) -> None:
    with pytest.raises(tag_metadata.TagMetadataError):
        tag_metadata.parse_tag_message(invalid)


def test_verify_tag_object_returns_only_the_eight_validated_handoff_scalars(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tag-object"
    path.write_bytes(_tag_object())

    outputs = tag_metadata.verify_tag_object_file(
        tag_object_file=path,
        expected_repository="robotlearning123/gpt2agent",
        expected_tag="v0.0.12",
        expected_commit=COMMIT,
        expected_tree=TREE,
    )

    assert outputs == {
        "receipt_sha256": RECEIPT_SHA256,
        "account_artifact_set_sha256": ARTIFACT_SET_SHA256,
        "candidate_run_id": "123456",
        "candidate_run_attempt": "2",
        "candidate_artifact_id": "987654",
        "candidate_artifact_digest": ARTIFACT_DIGEST,
        "candidate_artifact_size": "314159",
        "candidate_artifact_expires_at": "2099-07-10T13:17:42Z",
    }


@pytest.mark.parametrize(
    ("argument", "invalid"),
    [
        ("expected_repository", "other/project"),
        ("expected_tag", "v0.0.13"),
        ("expected_commit", "6" * 40),
        ("expected_tree", "7" * 40),
    ],
)
def test_verify_tag_object_rejects_expected_identity_mismatches(
    tmp_path: Path,
    argument: str,
    invalid: str,
) -> None:
    path = tmp_path / "tag-object"
    path.write_bytes(_tag_object())
    arguments = {
        "tag_object_file": path,
        "expected_repository": "robotlearning123/gpt2agent",
        "expected_tag": "v0.0.12",
        "expected_commit": COMMIT,
        "expected_tree": TREE,
    }
    arguments[argument] = invalid

    with pytest.raises(tag_metadata.TagMetadataError):
        tag_metadata.verify_tag_object_file(**arguments)


@pytest.mark.parametrize(
    "invalid",
    [
        lambda value: value.replace(b"type commit\n", b"type blob\n", 1),
        lambda value: value.replace(b"tag v0.0.12\n", b"tag v0.0.13\n", 1),
        lambda value: value.replace(b"object " + COMMIT.encode(), b"object " + b"6" * 40, 1),
        lambda value: value.replace(b"tagger Release Bot", b"x-extra value\ntagger Release Bot", 1),
        lambda value: value.replace(
            b"tagger Release Bot", b"object " + COMMIT.encode() + b"\ntagger Release Bot", 1
        ),
        lambda value: value.replace(b"tagger Release Bot", b"release-bot", 1),
        lambda value: value.replace(b"\n", b"\r\n"),
        lambda value: value + b"\x00",
    ],
)
def test_verify_tag_object_rejects_malformed_raw_tag_objects(tmp_path: Path, invalid) -> None:
    path = tmp_path / "tag-object"
    path.write_bytes(invalid(_tag_object()))

    with pytest.raises(tag_metadata.TagMetadataError):
        tag_metadata.verify_tag_object_file(
            tag_object_file=path,
            expected_repository="robotlearning123/gpt2agent",
            expected_tag="v0.0.12",
            expected_commit=COMMIT,
            expected_tree=TREE,
        )


def test_verify_tag_object_rejects_symlink_and_oversized_inputs(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(_tag_object())
    symlink = tmp_path / "tag-object-symlink"
    symlink.symlink_to(target)
    oversized = tmp_path / "oversized"
    oversized.write_bytes(b"x" * (tag_metadata.MAX_TAG_OBJECT_BYTES + 1))
    common = {
        "expected_repository": "robotlearning123/gpt2agent",
        "expected_tag": "v0.0.12",
        "expected_commit": COMMIT,
        "expected_tree": TREE,
    }

    for path in (symlink, oversized):
        with pytest.raises(tag_metadata.TagMetadataError):
            tag_metadata.verify_tag_object_file(tag_object_file=path, **common)


def test_cli_appends_exact_validated_outputs_without_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "tag-object"
    path.write_bytes(_tag_object())
    github_output = tmp_path / "github-output"
    github_output.write_text("prior=kept\n", encoding="ascii")
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

    result = tag_metadata.main(
        [
            "verify-tag-object",
            "--tag-object-file",
            str(path),
            "--repository",
            "robotlearning123/gpt2agent",
            "--tag",
            "v0.0.12",
            "--commit",
            COMMIT,
            "--tree",
            TREE,
        ]
    )

    assert result == 0
    assert capsys.readouterr() == ("", "")
    assert github_output.read_text(encoding="ascii").splitlines() == [
        "prior=kept",
        f"receipt_sha256={RECEIPT_SHA256}",
        f"account_artifact_set_sha256={ARTIFACT_SET_SHA256}",
        "candidate_run_id=123456",
        "candidate_run_attempt=2",
        "candidate_artifact_id=987654",
        f"candidate_artifact_digest={ARTIFACT_DIGEST}",
        "candidate_artifact_size=314159",
        "candidate_artifact_expires_at=2099-07-10T13:17:42Z",
    ]


def test_cli_verifies_before_touching_github_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "tag-object"
    path.write_bytes(_tag_object().replace(b"type commit", b"type blob"))
    github_output = tmp_path / "github-output"
    github_output.write_text("prior=kept\n", encoding="ascii")
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

    with pytest.raises(SystemExit) as raised:
        tag_metadata.main(
            [
                "verify-tag-object",
                "--tag-object-file",
                str(path),
                "--repository",
                "robotlearning123/gpt2agent",
                "--tag",
                "v0.0.12",
                "--commit",
                COMMIT,
                "--tree",
                TREE,
            ]
        )

    assert raised.value.code == 2
    assert github_output.read_text(encoding="ascii") == "prior=kept\n"


def test_cli_completes_short_regular_file_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "tag-object"
    path.write_bytes(_tag_object())
    github_output = tmp_path / "github-output"
    github_output.touch()
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    real_write = os.write

    def short_write(descriptor: int, payload: bytes) -> int:
        return real_write(descriptor, payload[:17])

    monkeypatch.setattr(tag_metadata.os, "write", short_write)

    assert (
        tag_metadata.main(
            [
                "verify-tag-object",
                "--tag-object-file",
                str(path),
                "--repository",
                "robotlearning123/gpt2agent",
                "--tag",
                "v0.0.12",
                "--commit",
                COMMIT,
                "--tree",
                TREE,
            ]
        )
        == 0
    )
    assert github_output.read_text(encoding="ascii").splitlines()[-1] == (
        "candidate_artifact_expires_at=2099-07-10T13:17:42Z"
    )
