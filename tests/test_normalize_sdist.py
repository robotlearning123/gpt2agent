"""Security and reproducibility checks for source-distribution normalization."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import io
import os
import stat
import struct
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "normalize_sdist.py"
ROOT = "gpt2agent-1.2.3"
EPOCH = 1_700_000_000


def _load_module() -> ModuleType:
    assert SCRIPT.is_file(), "the release sdist normalizer is missing"
    spec = importlib.util.spec_from_file_location("normalize_sdist", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _directory(name: str) -> tuple[tarfile.TarInfo, bytes | None]:
    member = tarfile.TarInfo(name)
    member.type = tarfile.DIRTYPE
    member.mode = 0o775
    member.mtime = EPOCH + 3.25
    member.pax_headers = {"mtime": str(member.mtime)}
    return member, None


def _file(
    name: str,
    payload: bytes = b"payload",
    *,
    mode: int = 0o664,
) -> tuple[tarfile.TarInfo, bytes]:
    member = tarfile.TarInfo(name)
    member.mode = mode
    member.mtime = EPOCH + 4.5
    member.pax_headers = {"mtime": str(member.mtime)}
    return member, payload


def _required_entries() -> list[tuple[tarfile.TarInfo, bytes | None]]:
    return [
        _directory(ROOT),
        _file(
            f"{ROOT}/PKG-INFO",
            b"Metadata-Version: 2.4\nName: gpt2agent\nVersion: 1.2.3\n",
        ),
        _file(f"{ROOT}/pyproject.toml", b"[build-system]\nrequires = []\n"),
    ]


def _source_entries() -> list[tuple[tarfile.TarInfo, bytes | None]]:
    return [
        *_required_entries(),
        _directory(f"{ROOT}/gpt2agent"),
        _file(f"{ROOT}/gpt2agent/__init__.py", b'__version__ = "1.2.3"\n'),
        _file(f"{ROOT}/install.sh", b"#!/bin/sh\nexit 0\n", mode=0o775),
    ]


def _write_custom_archive(
    path: Path,
    entries: list[tuple[tarfile.TarInfo, bytes | None]],
    *,
    gzip_mtime: int = EPOCH + 1,
    global_pax: dict[str, str] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as raw:
        with gzip.GzipFile(
            filename=path.name,
            mode="wb",
            fileobj=raw,
            mtime=gzip_mtime,
        ) as compressed:
            with tarfile.open(
                fileobj=compressed,
                mode="w",
                format=tarfile.PAX_FORMAT,
                pax_headers=global_pax,
            ) as archive:
                for member, payload in entries:
                    member.uid = 1234
                    member.gid = 5678
                    member.uname = "builder"
                    member.gname = "builders"
                    if payload is not None:
                        member.size = len(payload)
                        archive.addfile(member, io.BytesIO(payload))
                    else:
                        archive.addfile(member)


def _raw_tar_header(name: str, *, size: int = 0, member_type: bytes = tarfile.REGTYPE) -> bytes:
    member = tarfile.TarInfo(name)
    member.type = member_type
    member.size = size
    return member.tobuf(format=tarfile.GNU_FORMAT)


def _octal_header_size(header: bytes) -> int:
    field = header[124:136].rstrip(b"\0 ").lstrip(b" ")
    return int(field or b"0", 8)


def test_normalizer_makes_order_and_metadata_distinct_archives_byte_identical(
    tmp_path: Path,
) -> None:
    module = _load_module()
    first = tmp_path / "first" / f"{ROOT}.tar.gz"
    second = tmp_path / "second" / f"{ROOT}.tar.gz"
    first_entries = _source_entries()
    second_entries = list(reversed(_source_entries()))
    for member, _payload in second_entries:
        member.mtime += 100
        member.pax_headers = {"mtime": str(member.mtime)}
        member.mode = 0o700 if member.mode & 0o111 else 0o600
    _write_custom_archive(first, first_entries, gzip_mtime=EPOCH + 1)
    _write_custom_archive(second, second_entries, gzip_mtime=EPOCH + 101)

    first_digest = module.normalize_sdist(first, epoch=EPOCH)
    second_digest = module.normalize_sdist(second, epoch=EPOCH)

    assert first.read_bytes() == second.read_bytes()
    assert first_digest == second_digest == hashlib.sha256(first.read_bytes()).hexdigest()
    header = first.read_bytes()[:10]
    assert header[3] & 0x08 == 0
    assert struct.unpack("<I", header[4:8])[0] == EPOCH
    with tarfile.open(first, "r:gz") as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == sorted(member.name for member in members)
        assert {member.mtime for member in members} == {EPOCH}
        assert {member.uid for member in members} == {0}
        assert {member.gid for member in members} == {0}
        assert {member.uname for member in members} == {""}
        assert {member.gname for member in members} == {""}
        assert not any("mtime" in member.pax_headers for member in members)
        assert archive.getmember(ROOT).mode == 0o755
        assert archive.getmember(f"{ROOT}/PKG-INFO").mode == 0o644
        assert archive.getmember(f"{ROOT}/install.sh").mode == 0o755
    assert stat.S_IMODE(first.stat().st_mode) == 0o600

    normalized = first.read_bytes()
    assert module.normalize_sdist(first, epoch=EPOCH) == first_digest
    assert first.read_bytes() == normalized


@pytest.mark.parametrize("suffix", [b"hidden trailing bytes", gzip.compress(b"second")])
def test_normalizer_rejects_trailing_or_concatenated_gzip_data(
    tmp_path: Path,
    suffix: bytes,
) -> None:
    module = _load_module()
    archive = tmp_path / f"{ROOT}.tar.gz"
    _write_custom_archive(archive, _source_entries())
    archive.write_bytes(archive.read_bytes() + suffix)
    original = archive.read_bytes()

    with pytest.raises(module.SdistNormalizationError, match="exactly one gzip"):
        module.normalize_sdist(archive, epoch=EPOCH)

    assert archive.read_bytes() == original


def test_normalizer_rejects_nonzero_data_after_tar_eof_inside_one_gzip(
    tmp_path: Path,
) -> None:
    module = _load_module()
    archive = tmp_path / f"{ROOT}.tar.gz"
    _write_custom_archive(archive, _source_entries())
    expanded = gzip.decompress(archive.read_bytes())
    archive.write_bytes(gzip.compress(expanded + b"hidden in one gzip", mtime=EPOCH))

    with pytest.raises(module.SdistNormalizationError, match="after tar EOF"):
        module.normalize_sdist(archive, epoch=EPOCH)


def test_normalizer_rejects_truncated_gzip(tmp_path: Path) -> None:
    module = _load_module()
    archive = tmp_path / f"{ROOT}.tar.gz"
    _write_custom_archive(archive, _source_entries())
    archive.write_bytes(archive.read_bytes()[:-8])

    with pytest.raises(module.SdistNormalizationError, match="truncated|malformed"):
        module.normalize_sdist(archive, epoch=EPOCH)


@pytest.mark.parametrize(
    "unsafe_name",
    [
        f"{ROOT}/../outside",
        f"{ROOT}//double",
        f"{ROOT}\\windows-path",
        "/absolute",
        "another-root/file",
        f"{ROOT}/nul\x00name",
    ],
)
def test_normalizer_rejects_noncanonical_member_paths(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    module = _load_module()
    archive = tmp_path / f"{ROOT}.tar.gz"
    entries = [*_required_entries(), _file(unsafe_name, b"bad")]
    _write_custom_archive(archive, entries)

    with pytest.raises(module.SdistNormalizationError, match="path|root|terminator"):
        module.normalize_sdist(archive, epoch=EPOCH)


@pytest.mark.parametrize(
    "member_type",
    [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE],
)
def test_normalizer_rejects_every_special_member_type(
    tmp_path: Path,
    member_type: bytes,
) -> None:
    module = _load_module()
    archive = tmp_path / f"{ROOT}.tar.gz"
    special = tarfile.TarInfo(f"{ROOT}/special")
    special.type = member_type
    special.linkname = "../../outside"
    _write_custom_archive(archive, [*_required_entries(), (special, None)])

    with pytest.raises(module.SdistNormalizationError, match="regular files and directories"):
        module.normalize_sdist(archive, epoch=EPOCH)


def test_normalizer_rejects_duplicates_and_missing_or_wrong_required_metadata(
    tmp_path: Path,
) -> None:
    module = _load_module()
    duplicate = tmp_path / "duplicate" / f"{ROOT}.tar.gz"
    missing = tmp_path / "missing" / f"{ROOT}.tar.gz"
    wrong_type = tmp_path / "wrong" / f"{ROOT}.tar.gz"
    _write_custom_archive(
        duplicate,
        [*_required_entries(), _file(f"{ROOT}/PKG-INFO", b"duplicate")],
    )
    _write_custom_archive(missing, _required_entries()[:-1])
    wrong_entries = _required_entries()
    wrong_entries[1] = _directory(f"{ROOT}/PKG-INFO")
    _write_custom_archive(wrong_type, wrong_entries)

    for archive in (duplicate, missing, wrong_type):
        with pytest.raises(module.SdistNormalizationError):
            module.normalize_sdist(archive, epoch=EPOCH)


@pytest.mark.parametrize("parent_kind", ["missing", "file"])
def test_normalizer_rejects_missing_or_regular_intermediate_parent(
    tmp_path: Path,
    parent_kind: str,
) -> None:
    module = _load_module()
    archive = tmp_path / f"{ROOT}.tar.gz"
    entries = _required_entries()
    if parent_kind == "file":
        entries.append(_file(f"{ROOT}/parent", b"not a directory"))
    entries.append(_file(f"{ROOT}/parent/child", b"child"))
    _write_custom_archive(archive, entries)

    with pytest.raises(module.SdistNormalizationError, match="parent"):
        module.normalize_sdist(archive, epoch=EPOCH)


def test_normalizer_rejects_global_and_unsupported_member_pax_metadata(
    tmp_path: Path,
) -> None:
    module = _load_module()
    global_archive = tmp_path / "global" / f"{ROOT}.tar.gz"
    member_archive = tmp_path / "member" / f"{ROOT}.tar.gz"
    _write_custom_archive(global_archive, _source_entries(), global_pax={"comment": "hidden"})
    entries = _source_entries()
    entries[-1][0].pax_headers["ctime"] = "123.5"
    _write_custom_archive(member_archive, entries)

    with pytest.raises(module.SdistNormalizationError, match="global PAX"):
        module.normalize_sdist(global_archive, epoch=EPOCH)
    with pytest.raises(module.SdistNormalizationError, match="unsupported PAX"):
        module.normalize_sdist(member_archive, epoch=EPOCH)


def test_normalizer_preserves_long_pax_path_without_volatile_metadata(
    tmp_path: Path,
) -> None:
    module = _load_module()
    archive = tmp_path / f"{ROOT}.tar.gz"
    long_name = f"{ROOT}/" + "a" * 120
    _write_custom_archive(archive, [*_required_entries(), _file(long_name)])

    module.normalize_sdist(archive, epoch=EPOCH)

    with tarfile.open(archive, "r:gz") as normalized:
        member = normalized.getmember(long_name)
        assert member.pax_headers == {"path": long_name}
        assert member.mtime == EPOCH


def test_normalizer_rejects_symlink_input_and_noncanonical_archive_root(
    tmp_path: Path,
) -> None:
    module = _load_module()
    target = tmp_path / f"{ROOT}.tar.gz"
    link = tmp_path / "linked-1.0.tar.gz"
    invalid_root = tmp_path / "invalidroot.tar.gz"
    _write_custom_archive(target, _source_entries())
    link.symlink_to(target)
    invalid_root.write_bytes(target.read_bytes())

    with pytest.raises(module.SdistNormalizationError, match="non-symlink"):
        module.normalize_sdist(link, epoch=EPOCH)
    with pytest.raises(module.SdistNormalizationError, match="canonical archive root"):
        module.normalize_sdist(invalid_root, epoch=EPOCH)


@pytest.mark.parametrize("epoch", [-1, 2**32, True, "1700000000", 1.5])
def test_normalizer_rejects_invalid_gzip_epochs(epoch: object) -> None:
    module = _load_module()

    with pytest.raises(ValueError, match="epoch"):
        module.validate_epoch(epoch)


@pytest.mark.parametrize("value", ["01", "+1", " 1", "1 ", "-1"])
def test_normalizer_cli_rejects_noncanonical_epoch_spellings(value: str) -> None:
    module = _load_module()

    with pytest.raises(argparse.ArgumentTypeError, match="canonical"):
        module._parse_epoch(value)


def test_normalizer_accepts_gzip_epoch_boundaries() -> None:
    module = _load_module()

    assert module.validate_epoch(0) == 0
    assert module.validate_epoch(2**32 - 1) == 2**32 - 1


def test_gzip_validation_bounds_each_decompression_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    limits: list[int] = []

    class BoundedDecoder:
        eof = False
        unused_data = b""
        unconsumed_tail = b""

        def decompress(self, _data: bytes, max_length: int) -> bytes:
            limits.append(max_length)
            self.eof = True
            return b"tar payload"

    monkeypatch.setattr(module.zlib, "decompressobj", lambda *, wbits: BoundedDecoder())

    module._validate_complete_gzip(io.BytesIO(b"compressed input"))

    assert limits
    assert max(limits) <= module._MAX_DECOMPRESS_BYTES


def test_raw_tar_preflight_stops_on_member_limit_plus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_MAX_MEMBERS", 3)

    class TrackingStream(io.BytesIO):
        furthest_offset = 0

        def read(self, size: int = -1) -> bytes:
            result = super().read(size)
            self.furthest_offset = max(self.furthest_offset, self.tell())
            return result

    stream = TrackingStream(
        b"".join(_raw_tar_header(f"{ROOT}/member-{index}") for index in range(5))
        + b"\0" * 1024
    )

    with pytest.raises(module.SdistNormalizationError, match="member count"):
        module._preflight_tar_stream(stream)

    assert stream.furthest_offset == 4 * 512


def test_raw_tar_preflight_rejects_noncanonical_base256_size_field() -> None:
    module = _load_module()
    header = bytearray(_raw_tar_header(f"{ROOT}/member"))
    header[124:136] = bytes([0x80]) + b"\0" * 11

    with pytest.raises(module.SdistNormalizationError, match="noncanonical size"):
        module._preflight_tar_stream(io.BytesIO(bytes(header)))


@pytest.mark.parametrize(
    "member_type",
    [
        tarfile.XHDTYPE,
        tarfile.XGLTYPE,
        tarfile.SOLARIS_XHDTYPE,
        tarfile.GNUTYPE_LONGNAME,
        tarfile.GNUTYPE_LONGLINK,
    ],
    ids=["pax", "global-pax", "solaris-pax", "gnu-longname", "gnu-longlink"],
)
def test_raw_tar_preflight_rejects_oversized_extended_header_without_reading_payload(
    member_type: bytes,
) -> None:
    module = _load_module()
    declared_size = module._MAX_EXTENDED_HEADER_BYTES + 1

    class HeaderOnlyStream(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            if self.tell() >= 512:
                raise AssertionError("oversized extended-header payload was read")
            return super().read(size)

    stream = HeaderOnlyStream(
        _raw_tar_header("././@LongLink", size=declared_size, member_type=member_type)
    )

    with pytest.raises(module.SdistNormalizationError, match="extended header.*bound"):
        module._preflight_tar_stream(stream)


def test_snapshot_tee_rejects_oversized_header_before_reading_or_copying_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    declared_size = module._MAX_EXTENDED_HEADER_BYTES + 1
    header = _raw_tar_header(
        "././@PaxHeader",
        size=declared_size,
        member_type=tarfile.XHDTYPE,
    )

    class HeaderOnlyStream(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            if self.tell() >= 512:
                raise AssertionError("oversized extended-header payload was read")
            return super().read(size)

    class RecordingSnapshot(io.BytesIO):
        close_called = False

        def close(self) -> None:
            self.close_called = True

    snapshot = RecordingSnapshot()
    monkeypatch.setattr(module.tempfile, "TemporaryFile", lambda *, mode: snapshot)

    with pytest.raises(module.SdistNormalizationError, match="extended header.*bound"):
        module._snapshot_preflighted_tar(HeaderOnlyStream(header))

    assert snapshot.getvalue() == header
    assert snapshot.close_called


def test_raw_tar_preflight_rejects_consecutive_local_extended_headers() -> None:
    module = _load_module()
    header = _raw_tar_header("././@PaxHeader", member_type=tarfile.XHDTYPE)

    with pytest.raises(module.SdistNormalizationError, match="consecutive"):
        module._preflight_tar_stream(io.BytesIO(header + header + b"\0" * 1024))


def test_raw_tar_preflight_requires_extended_header_to_have_a_member() -> None:
    module = _load_module()
    header = _raw_tar_header("././@PaxHeader", member_type=tarfile.XHDTYPE)

    with pytest.raises(module.SdistNormalizationError, match="followed by a member"):
        module._preflight_tar_stream(io.BytesIO(header + b"\0" * 1024))


def test_normalizer_rejects_nonzero_regular_member_padding(tmp_path: Path) -> None:
    module = _load_module()
    archive = tmp_path / f"{ROOT}.tar.gz"
    target_name = f"{ROOT}/padding-test"
    _write_custom_archive(archive, [*_required_entries(), _file(target_name, b"x")])
    expanded = bytearray(gzip.decompress(archive.read_bytes()))
    with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as parsed:
        target = parsed.getmember(target_name)
    padding_offset = target.offset_data + target.size
    assert padding_offset % 512 != 0
    expanded[padding_offset] = 1
    archive.write_bytes(gzip.compress(bytes(expanded), mtime=EPOCH))

    with pytest.raises(module.SdistNormalizationError, match="payload padding"):
        module.normalize_sdist(archive, epoch=EPOCH)


def test_normalizer_rejects_nonzero_extended_header_padding(tmp_path: Path) -> None:
    module = _load_module()
    archive = tmp_path / f"{ROOT}.tar.gz"
    _write_custom_archive(archive, _source_entries())
    expanded = bytearray(gzip.decompress(archive.read_bytes()))

    offset = 0
    while True:
        header = bytes(expanded[offset : offset + 512])
        assert len(header) == 512 and any(header), "test archive has no extended header"
        payload_size = _octal_header_size(header)
        if header[156:157] == tarfile.XHDTYPE:
            break
        offset += 512 + ((payload_size + 511) // 512) * 512

    padding_offset = offset + 512 + payload_size
    assert padding_offset % 512 != 0
    expanded[padding_offset] = 1
    archive.write_bytes(gzip.compress(bytes(expanded), mtime=EPOCH))

    with pytest.raises(module.SdistNormalizationError, match="payload padding"):
        module.normalize_sdist(archive, epoch=EPOCH)


def test_normalizer_enforces_member_size_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    archive = tmp_path / f"{ROOT}.tar.gz"
    _write_custom_archive(archive, [*_required_entries(), _file(f"{ROOT}/large", b"12")])
    monkeypatch.setattr(module, "_MAX_MEMBER_BYTES", 1)

    with pytest.raises(module.SdistNormalizationError, match="supported bound"):
        module.normalize_sdist(archive, epoch=EPOCH)


def test_normalizer_semantic_rescan_rejects_mode_drift_without_replacing_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    archive = tmp_path / f"{ROOT}.tar.gz"
    _write_custom_archive(archive, _source_entries())
    original = archive.read_bytes()
    real_canonical = module._canonical_tarinfo

    def drift_mode(member: tarfile.TarInfo, epoch: int) -> tarfile.TarInfo:
        result = real_canonical(member, epoch)
        if member.name.endswith("PKG-INFO"):
            result.mode = 0o755
        return result

    monkeypatch.setattr(module, "_canonical_tarinfo", drift_mode)

    with pytest.raises(module.SdistNormalizationError, match="modes"):
        module.normalize_sdist(archive, epoch=EPOCH)

    assert archive.read_bytes() == original
    assert not list(tmp_path.glob(f".{archive.name}.*.tmp"))


def test_normalizer_rejects_same_size_source_mutation_even_with_restored_mtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    archive = tmp_path / f"{ROOT}.tar.gz"
    _write_custom_archive(archive, _source_entries())
    original_stat = archive.stat()
    real_canonical = module._canonical_tarinfo
    mutated = False

    def mutate_source(member: tarfile.TarInfo, epoch: int) -> tarfile.TarInfo:
        nonlocal mutated
        if not mutated:
            with archive.open("r+b") as stream:
                stream.seek(9)
                current = stream.read(1)
                stream.seek(9)
                stream.write(bytes([current[0] ^ 1]))
            os.utime(archive, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            mutated = True
        return real_canonical(member, epoch)

    monkeypatch.setattr(module, "_canonical_tarinfo", mutate_source)

    with pytest.raises(module.SdistNormalizationError, match="changed"):
        module.normalize_sdist(archive, epoch=EPOCH)

    assert mutated
    assert archive.stat().st_size == original_stat.st_size
    assert archive.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert not list(tmp_path.glob(f".{archive.name}.*.tmp"))


def test_tarfile_reads_exact_validated_snapshot_and_detects_source_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    archive = tmp_path / f"{ROOT}.tar.gz"
    _write_custom_archive(archive, _source_entries())
    original_tar_open = module.tarfile.open
    mutated = False
    parsed_snapshot = False

    def mutate_before_parse(*args: object, **kwargs: object) -> tarfile.TarFile:
        nonlocal mutated, parsed_snapshot
        if kwargs.get("mode") == "r:" and not mutated:
            parsed_snapshot = not isinstance(kwargs.get("fileobj"), gzip.GzipFile)
            archive.write_bytes(b"rewritten after tar preflight")
            mutated = True
        return original_tar_open(*args, **kwargs)

    monkeypatch.setattr(module.tarfile, "open", mutate_before_parse)

    with pytest.raises(module.SdistNormalizationError, match="changed"):
        module.normalize_sdist(archive, epoch=EPOCH)

    assert mutated
    assert parsed_snapshot
    assert archive.read_bytes() == b"rewritten after tar preflight"
    assert not list(tmp_path.glob(f".{archive.name}.*.tmp"))


def test_normalizer_wraps_snapshot_creation_failure_without_payload_or_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    archive = tmp_path / f"{ROOT}.tar.gz"
    _write_custom_archive(archive, _source_entries())
    original = archive.read_bytes()

    def fail_snapshot(*_args: object, **_kwargs: object) -> None:
        raise OSError("sensitive snapshot creation payload")

    monkeypatch.setattr(module.tempfile, "TemporaryFile", fail_snapshot)

    with pytest.raises(module.SdistNormalizationError) as captured:
        module.normalize_sdist(archive, epoch=EPOCH)

    assert str(captured.value) == "sdist tar snapshot is unavailable"
    assert "sensitive" not in str(captured.value)
    assert archive.read_bytes() == original
    assert not list(tmp_path.glob(f".{archive.name}.*.tmp"))


def test_snapshot_read_failure_has_stable_payload_free_error() -> None:
    module = _load_module()

    class FailingStream:
        def read(self, _size: int = -1) -> bytes:
            raise OSError("sensitive snapshot read payload")

    with pytest.raises(module.SdistNormalizationError) as captured:
        module._snapshot_preflighted_tar(FailingStream())

    assert str(captured.value) == "sdist tar snapshot is unavailable"
    assert "sensitive" not in str(captured.value)


@pytest.mark.parametrize(
    "failing_snapshot_number",
    [1, 2],
    ids=["scan", "write"],
)
def test_normalizer_maps_snapshot_close_failure_without_mutation_or_named_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_snapshot_number: int,
) -> None:
    module = _load_module()
    archive = tmp_path / f"{ROOT}.tar.gz"
    _write_custom_archive(archive, _source_entries())
    original = archive.read_bytes()
    real_temporary_file = module.tempfile.TemporaryFile
    snapshot_number = 0

    class Snapshot:
        def __init__(self, wrapped: io.BytesIO, *, fail_close: bool) -> None:
            self.wrapped = wrapped
            self.fail_close = fail_close

        def __getattr__(self, name: str) -> object:
            return getattr(self.wrapped, name)

        def __enter__(self) -> "Snapshot":
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

        def close(self) -> None:
            self.wrapped.close()
            if self.fail_close:
                raise OSError("sensitive snapshot close payload")

    def make_snapshot(*, mode: str) -> Snapshot:
        nonlocal snapshot_number
        snapshot_number += 1
        return Snapshot(
            real_temporary_file(mode=mode),
            fail_close=snapshot_number == failing_snapshot_number,
        )

    monkeypatch.setattr(module.tempfile, "TemporaryFile", make_snapshot)

    with pytest.raises(module.SdistNormalizationError) as captured:
        module.normalize_sdist(archive, epoch=EPOCH)

    assert str(captured.value) == module._SNAPSHOT_ERROR
    assert "sensitive" not in str(captured.value)
    assert archive.read_bytes() == original
    assert not list(tmp_path.glob(f".{archive.name}.*.tmp"))


def test_snapshot_close_failure_preserves_active_validation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    archive = tmp_path / f"{ROOT}.tar.gz"
    _write_custom_archive(archive, [_directory(ROOT)])
    original = archive.read_bytes()
    real_temporary_file = module.tempfile.TemporaryFile

    class Snapshot:
        def __init__(self, wrapped: io.BytesIO) -> None:
            self.wrapped = wrapped

        def __getattr__(self, name: str) -> object:
            return getattr(self.wrapped, name)

        def __enter__(self) -> "Snapshot":
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

        def close(self) -> None:
            self.wrapped.close()
            raise OSError("sensitive snapshot close payload")

    monkeypatch.setattr(
        module.tempfile,
        "TemporaryFile",
        lambda *, mode: Snapshot(real_temporary_file(mode=mode)),
    )

    with pytest.raises(module.SdistNormalizationError) as captured:
        module.normalize_sdist(archive, epoch=EPOCH)

    assert str(captured.value) == "sdist is missing its root, PKG-INFO, or pyproject.toml"
    assert "sensitive" not in str(captured.value)
    assert archive.read_bytes() == original
    assert not list(tmp_path.glob(f".{archive.name}.*.tmp"))


def test_normalizer_replace_failure_is_atomic_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    archive = tmp_path / f"{ROOT}.tar.gz"
    _write_custom_archive(archive, _source_entries())
    original = archive.read_bytes()

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic replace failure"):
        module.normalize_sdist(archive, epoch=EPOCH)

    assert archive.read_bytes() == original
    assert not list(tmp_path.glob(f".{archive.name}.*.tmp"))
