from __future__ import annotations

import errno
import os
import socket
import stat
from collections.abc import Sequence
from pathlib import Path

import pytest

from gpt2agent.grok_errors import GrokError
from gpt2agent.grok_upload import (
    DEFAULT_UPLOAD_MAX_BYTES,
    DEFAULT_UPLOAD_TYPES,
    AttachmentRegistry,
    GrokUploadPolicy,
    ValidatedUpload,
)


def _assert_blocked(error: GrokError, *planted: str) -> None:
    assert error.code == "GROK_UPLOAD_BLOCKED"
    assert error.retryable is False
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__dict__ == {
        "code": "GROK_UPLOAD_BLOCKED",
        "method": None,
        "route": None,
        "status_code": None,
        "retryable": False,
        "retry_after": None,
    }
    for value in planted:
        if value.strip():
            assert value not in str(error)


def _rejected_open(policy: GrokUploadPolicy, value: object) -> GrokError:
    with pytest.raises(GrokError) as caught:
        policy.open(value)  # type: ignore[arg-type]
    _assert_blocked(caught.value, str(value))
    return caught.value


def _assert_fd_closed(fd: int) -> None:
    with pytest.raises(OSError) as caught:
        os.fstat(fd)
    assert caught.value.errno == errno.EBADF


def _assert_upload_traceback_locals_are_secret_free(
    error: GrokError, planted: str, expected_function: str
) -> None:
    upload_frames: list[str] = []
    traceback = error.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        if Path(frame.f_code.co_filename).name == "grok_upload.py":
            upload_frames.append(frame.f_code.co_name)
            assert frame.f_locals.get("self") is None
            for local_name, value in frame.f_locals.items():
                assert planted not in repr(value), (
                    f"{planted!r} retained by {frame.f_code.co_name}.{local_name}"
                )
        traceback = traceback.tb_next
    assert upload_frames == [expected_function]


def test_upload_contract_constants_are_fixed() -> None:
    assert DEFAULT_UPLOAD_MAX_BYTES == 25 * 1024 * 1024
    assert DEFAULT_UPLOAD_TYPES == frozenset(
        {
            "text/plain",
            "text/markdown",
            "application/pdf",
            "application/json",
            "text/csv",
            "image/png",
            "image/jpeg",
            "image/webp",
        }
    )


def test_policy_returns_owned_rewound_descriptor_metadata(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "NOTE.TXT"
    source.write_bytes(b"descriptor source")

    upload = GrokUploadPolicy((root,)).open(str(source.absolute()))

    try:
        assert upload == ValidatedUpload(
            fd=upload.fd,
            name="NOTE.TXT",
            media_type="text/plain",
            size=17,
        )
        assert os.get_inheritable(upload.fd) is False
        assert os.lseek(upload.fd, 0, os.SEEK_CUR) == 0
        assert os.read(upload.fd, 17) == b"descriptor source"
    finally:
        os.close(upload.fd)
    _assert_fd_closed(upload.fd)


def test_returned_descriptor_is_not_a_later_path_reopen(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.txt"
    source.write_bytes(b"original")
    upload = GrokUploadPolicy((root,), maximum_bytes=64).open(str(source))
    source.unlink()
    source.write_bytes(b"replacement")

    try:
        assert upload.size == len(b"original")
        assert os.read(upload.fd, 64) == b"original"
        assert source.read_bytes() == b"replacement"
    finally:
        os.close(upload.fd)


def test_parent_replacement_after_descriptor_open_cannot_redirect_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gpt2agent.grok_upload as upload_module

    root = tmp_path / "root"
    parent = root / "parent"
    outside = tmp_path / "outside"
    parent.mkdir(parents=True)
    outside.mkdir()
    (parent / "source.txt").write_bytes(b"contained")
    (outside / "source.txt").write_bytes(b"outside")
    parked = root / "parked"
    real_open = os.open
    replaced = False

    def replacing_open(path: str, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal replaced
        fd = real_open(path, flags, *args, **kwargs)
        if path == "parent" and not replaced:
            replaced = True
            parent.rename(parked)
            parent.symlink_to(outside, target_is_directory=True)
        return fd

    monkeypatch.setattr(upload_module.os, "open", replacing_open)
    upload = GrokUploadPolicy((root,)).open(str(parent / "source.txt"))

    try:
        assert os.read(upload.fd, 64) == b"contained"
    finally:
        os.close(upload.fd)


def test_leaf_swap_immediately_before_open_is_blocked_and_closes_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gpt2agent.grok_upload as upload_module

    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    source = root / "source.txt"
    source.write_bytes(b"contained")
    outside_source = outside / "source.txt"
    outside_source.write_bytes(b"outside")
    real_open = os.open
    acquired: list[int] = []
    swapped = False

    def racing_open(path: str, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal swapped
        if path == source.name and not flags & os.O_DIRECTORY and not swapped:
            swapped = True
            source.unlink()
            source.symlink_to(outside_source)
        fd = real_open(path, flags, *args, **kwargs)
        acquired.append(fd)
        return fd

    monkeypatch.setattr(upload_module.os, "open", racing_open)

    _rejected_open(GrokUploadPolicy((root,)), str(source))

    assert swapped is True
    assert source.is_symlink()
    for fd in acquired:
        _assert_fd_closed(fd)


@pytest.mark.parametrize(
    "value",
    [None, 3, b"/tmp/file.txt", "", "   ", "bad\x00path.txt", "x" * 4_097],
)
def test_policy_rejects_invalid_and_overlong_paths(
    tmp_path: Path, value: object
) -> None:
    _rejected_open(GrokUploadPolicy((tmp_path,)), value)


@pytest.mark.parametrize(
    "unsafe_leaf",
    [
        "line\nbreak.txt",
        "control\x1fname.txt",
        "format\u200bname.txt",
        f"{'é' * 126}.txt",
    ],
)
def test_policy_rejects_nonprintable_and_encoded_overlong_leaf_components(
    tmp_path: Path, unsafe_leaf: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gpt2agent.grok_upload as upload_module

    root = tmp_path / "root"
    root.mkdir()
    policy = GrokUploadPolicy((root,))
    open_calls = 0
    real_open = os.open

    def recording_open(path: str, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal open_calls
        open_calls += 1
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(upload_module.os, "open", recording_open)

    _rejected_open(policy, str(root / unsafe_leaf))

    assert open_calls == 0


def test_policy_applies_a_cheap_character_bound_before_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gpt2agent.grok_upload as upload_module

    policy = GrokUploadPolicy((tmp_path,))
    encode_calls = 0

    def recording_encode(path: str) -> bytes:
        nonlocal encode_calls
        encode_calls += 1
        return path.encode()

    monkeypatch.setattr(upload_module.os, "fsencode", recording_encode)

    _rejected_open(policy, "/" + "x" * 4_097)

    assert encode_calls == 0


def test_policy_applies_a_cheap_component_bound_before_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gpt2agent.grok_upload as upload_module

    root = tmp_path / "root"
    root.mkdir()
    policy = GrokUploadPolicy((root,))
    encode_calls = 0

    def recording_encode(path: str) -> bytes:
        nonlocal encode_calls
        encode_calls += 1
        return path.encode()

    monkeypatch.setattr(upload_module.os, "fsencode", recording_encode)

    _rejected_open(policy, str(root / f"{'x' * 256}.txt"))

    assert encode_calls == 0


def test_policy_deliberately_allows_printable_unicode_components(tmp_path: Path) -> None:
    root = tmp_path / "资料"
    root.mkdir()
    source = root / "报告.txt"
    source.write_text("safe unicode")

    upload = GrokUploadPolicy((root,)).open(str(source))

    try:
        assert upload.name == "报告.txt"
        assert os.read(upload.fd, 64) == b"safe unicode"
    finally:
        os.close(upload.fd)


def test_policy_rejects_disabled_relative_parent_and_outside_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    relative = "source.txt"
    parent_escape = str(root / "nested" / ".." / "source.txt")
    outside_path = outside / "source.txt"
    outside_path.write_text("outside")

    _rejected_open(GrokUploadPolicy(()), str(outside_path))
    policy = GrokUploadPolicy((root,))
    _rejected_open(policy, relative)
    _rejected_open(policy, parent_escape)
    _rejected_open(policy, str(outside_path))


@pytest.mark.parametrize("location", ["parent", "leaf"])
def test_policy_rejects_symlinked_parents_and_leaves(
    tmp_path: Path, location: str
) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = outside / "secret.txt"
    target.write_text("outside")
    if location == "parent":
        link = root / "linked"
        link.symlink_to(outside, target_is_directory=True)
        candidate = link / target.name
    else:
        candidate = root / "secret.txt"
        candidate.symlink_to(target)

    _rejected_open(GrokUploadPolicy((root,)), str(candidate))


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("unknown.exe", b"data"),
        ("double.txt.exe", b"data"),
        ("credential.env.txt", b"data"),
        ("secret.pem", b"data"),
        ("empty.txt", b""),
        ("oversized.txt", b"12345"),
    ],
)
def test_policy_rejects_unsafe_suffixes_empty_and_oversized_files(
    tmp_path: Path,
    name: str,
    content: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gpt2agent.grok_upload as upload_module

    root = tmp_path / "root"
    root.mkdir()
    source = root / name
    source.write_bytes(content)
    real_open = os.open
    acquired: list[int] = []

    def recording_open(path: str, flags: int, *args: object, **kwargs: object) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        acquired.append(fd)
        return fd

    monkeypatch.setattr(upload_module.os, "open", recording_open)

    _rejected_open(GrokUploadPolicy((root,), maximum_bytes=4), str(source))
    for fd in acquired:
        _assert_fd_closed(fd)


def test_policy_rejects_a_mapped_type_removed_by_configuration(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.txt"
    source.write_text("data")

    _rejected_open(
        GrokUploadPolicy((root,), allowed_types={"application/pdf"}),
        str(source),
    )


@pytest.mark.parametrize(
    ("maximum_bytes", "allowed_types"),
    [
        (True, DEFAULT_UPLOAD_TYPES),
        (0, DEFAULT_UPLOAD_TYPES),
        (-1, DEFAULT_UPLOAD_TYPES),
        (DEFAULT_UPLOAD_MAX_BYTES + 1, DEFAULT_UPLOAD_TYPES),
        (DEFAULT_UPLOAD_MAX_BYTES, ["text/plain"]),
        (DEFAULT_UPLOAD_MAX_BYTES, {"application/x-secret"}),
        (DEFAULT_UPLOAD_MAX_BYTES, {"text/plain", 3}),
    ],
)
def test_policy_configuration_can_only_lower_limits_and_narrow_types(
    tmp_path: Path, maximum_bytes: object, allowed_types: object
) -> None:
    with pytest.raises(GrokError) as caught:
        GrokUploadPolicy(
            (tmp_path,),
            maximum_bytes=maximum_bytes,  # type: ignore[arg-type]
            allowed_types=allowed_types,  # type: ignore[arg-type]
        )
    _assert_blocked(caught.value)


def test_policy_rejects_non_regular_nodes_without_blocking_and_closes_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gpt2agent.grok_upload as upload_module

    root = tmp_path / "root"
    root.mkdir()
    directory = root / "directory.txt"
    directory.mkdir()
    fifo = root / "pipe.txt"
    os.mkfifo(fifo)
    unix_socket = root / "socket.txt"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(unix_socket))
    real_open = os.open
    acquired: list[int] = []

    def recording_open(path: str, flags: int, *args: object, **kwargs: object) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        acquired.append(fd)
        return fd

    monkeypatch.setattr(upload_module.os, "open", recording_open)
    try:
        policy = GrokUploadPolicy((root,))
        for candidate in (directory, fifo, unix_socket):
            _rejected_open(policy, str(candidate))
            for fd in acquired:
                _assert_fd_closed(fd)
            acquired.clear()
    finally:
        listener.close()


@pytest.mark.skipif(not Path("/dev/null").exists(), reason="POSIX device unavailable")
def test_policy_rejects_device_nodes() -> None:
    _rejected_open(GrokUploadPolicy((Path("/dev"),)), "/dev/null")


def test_policy_closes_leaf_descriptor_when_metadata_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gpt2agent.grok_upload as upload_module

    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.txt"
    source.write_bytes(b"stable")
    real_open = os.open
    real_fstat = os.fstat
    leaf_fd: int | None = None
    regular_stats = 0

    def recording_open(path: str, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal leaf_fd
        fd = real_open(path, flags, *args, **kwargs)
        if path == source.name and not flags & os.O_DIRECTORY:
            leaf_fd = fd
        return fd

    def mismatching_fstat(fd: int) -> os.stat_result:
        nonlocal regular_stats
        result = real_fstat(fd)
        if stat.S_ISREG(result.st_mode):
            regular_stats += 1
            if regular_stats == 2:
                values = list(result)
                values[6] = result.st_size + 1
                return os.stat_result(values)
        return result

    monkeypatch.setattr(upload_module.os, "open", recording_open)
    monkeypatch.setattr(upload_module.os, "fstat", mismatching_fstat)

    _rejected_open(GrokUploadPolicy((root,)), str(source))
    assert leaf_fd is not None
    _assert_fd_closed(leaf_fd)


def test_policy_rejects_same_size_content_change_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gpt2agent.grok_upload as upload_module

    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.txt"
    source.write_bytes(b"stable")
    real_fstat = os.fstat
    regular_stats = 0

    def changed_fstat(fd: int) -> os.stat_result:
        nonlocal regular_stats
        result = real_fstat(fd)
        if stat.S_ISREG(result.st_mode):
            regular_stats += 1
            if regular_stats == 2:
                values = list(result)
                values[8] = result.st_mtime + 1
                return os.stat_result(values)
        return result

    monkeypatch.setattr(upload_module.os, "fstat", changed_fstat)

    _rejected_open(GrokUploadPolicy((root,)), str(source))


def test_policy_closes_leaf_descriptor_and_detaches_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gpt2agent.grok_upload as upload_module

    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.txt"
    source.write_bytes(b"secret")
    planted = "xai-planted-os-path-secret"
    real_open = os.open
    leaf_fd: int | None = None

    def recording_open(path: str, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal leaf_fd
        fd = real_open(path, flags, *args, **kwargs)
        if path == source.name and not flags & os.O_DIRECTORY:
            leaf_fd = fd
        return fd

    def failing_read(fd: int, amount: int) -> bytes:
        raise OSError(planted)

    monkeypatch.setattr(upload_module.os, "open", recording_open)
    monkeypatch.setattr(upload_module.os, "read", failing_read)

    error = _rejected_open(GrokUploadPolicy((root,)), str(source))
    _assert_blocked(error, planted, str(source))
    assert leaf_fd is not None
    _assert_fd_closed(leaf_fd)


def test_policy_fails_closed_when_required_posix_primitive_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gpt2agent.grok_upload as upload_module

    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.txt"
    source.write_text("data")
    monkeypatch.delattr(upload_module.os, "O_NOFOLLOW")

    _rejected_open(GrokUploadPolicy((root,)), str(source))


def test_constructor_error_traceback_scrubs_raw_root_locals() -> None:
    planted = "xaiCtorTraceSecret42"
    unsafe_root = Path("/tmp") / planted / ".."

    with pytest.raises(GrokError) as caught:
        GrokUploadPolicy((unsafe_root,))

    _assert_blocked(caught.value, planted)
    _assert_upload_traceback_locals_are_secret_free(caught.value, planted, "__init__")


def test_open_error_traceback_scrubs_raw_path_and_name_locals(tmp_path: Path) -> None:
    planted = "xaiOpenTraceSecret42"
    root = tmp_path / "root"
    root.mkdir()
    unsafe_path = str(root / f"{planted}\n.txt")

    with pytest.raises(GrokError) as caught:
        GrokUploadPolicy((root,)).open(unsafe_path)

    _assert_blocked(caught.value, planted)
    _assert_upload_traceback_locals_are_secret_free(caught.value, planted, "open")


def test_registry_records_and_validates_current_generation_ids() -> None:
    registry = AttachmentRegistry()

    registry.record(auth_generation=3, attachment_id="att_test_1")

    assert registry.validate_many(3, ["att_test_1"]) == ("att_test_1",)
    with pytest.raises(GrokError) as caught:
        registry.validate_many(4, ["att_test_1"])
    _assert_blocked(caught.value, "att_test_1")


def test_registry_allows_no_attachments_before_any_upload() -> None:
    assert AttachmentRegistry().validate_many(7, ()) == ()


@pytest.mark.parametrize("generation", [True, -1, 1.5, "1", None])
def test_registry_rejects_invalid_auth_generations(generation: object) -> None:
    registry = AttachmentRegistry()

    with pytest.raises(GrokError) as caught:
        registry.record(generation, "att_test_1")  # type: ignore[arg-type]
    _assert_blocked(caught.value)


@pytest.mark.parametrize(
    "attachment_id",
    [
        "",
        " ",
        "a" * 129,
        "att/control\n",
        "att/slash",
        "att secret",
        "att_☃",
        3,
        None,
    ],
)
def test_registry_rejects_unbounded_or_invalid_server_ids(
    attachment_id: object,
) -> None:
    registry = AttachmentRegistry()

    with pytest.raises(GrokError) as caught:
        registry.record(1, attachment_id)  # type: ignore[arg-type]
    _assert_blocked(caught.value, str(attachment_id))


def test_record_error_traceback_scrubs_raw_attachment_id_locals() -> None:
    planted = "xaiRecordTraceSecret42"

    with pytest.raises(GrokError) as caught:
        AttachmentRegistry().record(1, f"att/{planted}")

    _assert_blocked(caught.value, planted)
    _assert_upload_traceback_locals_are_secret_free(caught.value, planted, "record")


def test_registry_rotates_generation_and_new_process_has_no_ids() -> None:
    registry = AttachmentRegistry()
    registry.record(3, "att_old")
    registry.record(4, "att_new")

    with pytest.raises(GrokError):
        registry.validate_many(3, ["att_old"])
    with pytest.raises(GrokError):
        registry.validate_many(4, ["att_old"])
    assert registry.validate_many(4, ["att_new"]) == ("att_new",)

    with pytest.raises(GrokError):
        AttachmentRegistry().validate_many(4, ["att_new"])


def test_registry_rejects_generation_rollback_without_reactivating_old_ids() -> None:
    registry = AttachmentRegistry()
    registry.record(3, "att_old")
    registry.record(4, "att_current")

    with pytest.raises(GrokError):
        registry.record(3, "att_stale")

    assert registry.validate_many(4, ["att_current"]) == ("att_current",)
    with pytest.raises(GrokError):
        registry.validate_many(3, ["att_stale"])


def test_registry_capacity_is_256_with_oldest_first_eviction() -> None:
    registry = AttachmentRegistry()
    for index in range(257):
        registry.record(8, f"att_{index:03d}")

    with pytest.raises(GrokError):
        registry.validate_many(8, ["att_000"])
    assert registry.validate_many(8, ["att_001", "att_256"]) == (
        "att_001",
        "att_256",
    )


def test_registry_rerecord_is_idempotent_without_refreshing_eviction_order() -> None:
    registry = AttachmentRegistry()
    registry.record(1, "att_oldest")
    for index in range(255):
        registry.record(1, f"att_{index:03d}")
    registry.record(1, "att_oldest")
    registry.record(1, "att_new")

    with pytest.raises(GrokError):
        registry.validate_many(1, ["att_oldest"])
    assert registry.validate_many(1, ["att_new"]) == ("att_new",)


@pytest.mark.parametrize(
    "values",
    [
        "att_test_1",
        b"att_test_1",
        ["unknown"],
        ["att_test_1", "att_test_1"],
        [f"att_{index}" for index in range(21)],
        ["att_test_1", "bad/id"],
    ],
)
def test_registry_rejects_invalid_unknown_duplicate_and_overlong_lists(
    values: object,
) -> None:
    registry = AttachmentRegistry()
    registry.record(1, "att_test_1")

    with pytest.raises(GrokError) as caught:
        registry.validate_many(1, values)  # type: ignore[arg-type]
    _assert_blocked(caught.value)


def test_registry_returns_an_immutable_tuple_in_caller_order() -> None:
    registry = AttachmentRegistry()
    for value in ("att_a", "att_b", "att_c"):
        registry.record(2, value)

    result = registry.validate_many(2, ["att_c", "att_a", "att_b"])

    assert result == ("att_c", "att_a", "att_b")
    assert isinstance(result, tuple)


def test_registry_uses_one_bounded_iterator_instead_of_a_lying_length() -> None:
    class LyingSequence(Sequence[str]):
        def __init__(self) -> None:
            self.pulls = 0

        def __len__(self) -> int:
            return 0

        def __getitem__(self, index: int) -> str:
            if index >= 21:
                raise IndexError
            self.pulls += 1
            return f"att_{index:02d}"

    registry = AttachmentRegistry()
    for index in range(21):
        registry.record(1, f"att_{index:02d}")
    values = LyingSequence()

    with pytest.raises(GrokError) as caught:
        registry.validate_many(1, values)

    _assert_blocked(caught.value)
    assert values.pulls == 21


def test_registry_does_not_call_mutating_sequence_length() -> None:
    class MutatingLengthSequence(Sequence[str]):
        def __init__(self) -> None:
            self.values = ["att_safe"]
            self.length_calls = 0

        def __len__(self) -> int:
            self.length_calls += 1
            self.values.extend(f"att_added_{index}" for index in range(20))
            return 1

        def __getitem__(self, index: int) -> str:
            return self.values[index]

    registry = AttachmentRegistry()
    registry.record(1, "att_safe")
    values = MutatingLengthSequence()

    assert registry.validate_many(1, values) == ("att_safe",)
    assert values.length_calls == 0


def test_registry_bounds_a_sequence_that_does_not_terminate_itself() -> None:
    class GuardedNonterminatingSequence(Sequence[str]):
        def __init__(self) -> None:
            self.pulls = 0

        def __len__(self) -> int:
            return 0

        def __getitem__(self, index: int) -> str:
            self.pulls += 1
            if self.pulls > 25:
                raise RuntimeError("test guard: iteration was not bounded")
            return "att_forever"

    registry = AttachmentRegistry()
    registry.record(1, "att_forever")
    values = GuardedNonterminatingSequence()

    with pytest.raises(GrokError) as caught:
        registry.validate_many(1, values)

    _assert_blocked(caught.value)
    assert values.pulls == 21


def test_validate_many_error_traceback_scrubs_raw_id_and_sequence_locals() -> None:
    planted = "xaiValidateTraceSecret42"
    registry = AttachmentRegistry()
    registry.record(2, "att_safe")

    with pytest.raises(GrokError) as caught:
        registry.validate_many(2, [f"att_{planted}"])

    _assert_blocked(caught.value, planted)
    _assert_upload_traceback_locals_are_secret_free(
        caught.value, planted, "validate_many"
    )
