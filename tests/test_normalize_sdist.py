"""Security and reproducibility checks for source-distribution normalization."""

from __future__ import annotations

import gzip
import importlib.util
import io
import struct
import tarfile
from pathlib import Path
from types import ModuleType

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "normalize_sdist.py"
ROOT = "gpt2agent-0.0.12"


def _load_module() -> ModuleType:
    assert SCRIPT.is_file(), "the release sdist normalizer is missing"
    spec = importlib.util.spec_from_file_location("normalize_sdist", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_sdist(path: Path, *, gzip_mtime: int, member_mtime: int, reverse: bool) -> None:
    entries = [
        (ROOT, None, 0o775),
        (f"{ROOT}/PKG-INFO", b"Metadata-Version: 2.4\nName: gpt2agent\nVersion: 0.0.12\n", 0o664),
        (f"{ROOT}/pyproject.toml", b"[build-system]\nrequires = []\n", 0o664),
        (f"{ROOT}/gpt2agent", None, 0o775),
        (f"{ROOT}/gpt2agent/__init__.py", b'__version__ = "0.0.12"\n', 0o664),
    ]
    if reverse:
        entries.reverse()

    with path.open("wb") as raw:
        with gzip.GzipFile(
            filename=path.name,
            mode="wb",
            fileobj=raw,
            mtime=gzip_mtime,
        ) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for name, payload, mode in entries:
                    member = tarfile.TarInfo(name)
                    member.mtime = member_mtime
                    member.uid = 1234
                    member.gid = 5678
                    member.uname = "builder"
                    member.gname = "builders"
                    member.mode = mode
                    member.pax_headers = {"mtime": f"{member_mtime}.25"}
                    if payload is None:
                        member.type = tarfile.DIRTYPE
                        archive.addfile(member)
                    else:
                        member.size = len(payload)
                        archive.addfile(member, io.BytesIO(payload))


def _required_entries() -> list[tuple[str, bytes | None, bytes]]:
    return [
        (ROOT, None, tarfile.DIRTYPE),
        (f"{ROOT}/PKG-INFO", b"Metadata-Version: 2.4\n", tarfile.REGTYPE),
        (f"{ROOT}/pyproject.toml", b"[build-system]\n", tarfile.REGTYPE),
    ]


def _write_custom_sdist(
    path: Path,
    entries: list[tuple[str, bytes | None, bytes]],
    *,
    global_pax: dict[str, str] | None = None,
) -> None:
    with path.open("wb") as raw:
        with gzip.GzipFile(filename=path.name, mode="wb", fileobj=raw, mtime=1) as compressed:
            with tarfile.open(
                fileobj=compressed,
                mode="w",
                format=tarfile.PAX_FORMAT,
                pax_headers=global_pax,
            ) as archive:
                for name, payload, member_type in entries:
                    member = tarfile.TarInfo(name)
                    member.type = member_type
                    member.mode = 0o777
                    member.mtime = 2
                    if member_type == tarfile.SYMTYPE:
                        member.linkname = "../../outside"
                    if payload is not None:
                        member.size = len(payload)
                        archive.addfile(member, io.BytesIO(payload))
                    else:
                        archive.addfile(member)


def test_normalization_makes_distinct_sdist_metadata_byte_identical(tmp_path: Path) -> None:
    module = _load_module()
    first = tmp_path / "first" / f"{ROOT}.tar.gz"
    second = tmp_path / "second" / f"{ROOT}.tar.gz"
    first.parent.mkdir()
    second.parent.mkdir()
    _write_sdist(first, gzip_mtime=1_700_000_001, member_mtime=1_700_000_003, reverse=False)
    _write_sdist(second, gzip_mtime=1_700_000_101, member_mtime=1_700_000_103, reverse=True)

    epoch = 1_700_000_000
    first_digest = module.normalize_sdist(first, epoch=epoch)
    second_digest = module.normalize_sdist(second, epoch=epoch)

    assert first.read_bytes() == second.read_bytes()
    assert first_digest == second_digest
    header = first.read_bytes()[:10]
    assert header[3] == 0  # No variable original-filename field.
    assert struct.unpack("<I", header[4:8])[0] == epoch
    with tarfile.open(first, "r:gz") as archive:
        members = archive.getmembers()
    assert [member.name for member in members] == sorted(member.name for member in members)
    assert {member.mtime for member in members} == {epoch}
    assert {member.uid for member in members} == {0}
    assert {member.gid for member in members} == {0}
    assert {member.uname for member in members} == {""}
    assert {member.gname for member in members} == {""}
    assert not any(member.pax_headers for member in members)
    assert next(member for member in members if member.name == ROOT).mode == 0o755
    assert next(member for member in members if member.name.endswith("PKG-INFO")).mode == 0o644
    assert first.stat().st_mode & 0o777 == 0o600


def test_normalization_rejects_unparsed_trailing_archive_data(tmp_path: Path) -> None:
    module = _load_module()
    sdist = tmp_path / f"{ROOT}.tar.gz"
    _write_sdist(sdist, gzip_mtime=1_700_000_001, member_mtime=1_700_000_003, reverse=False)
    sdist.write_bytes(sdist.read_bytes() + b"unreviewed trailing data")
    original = sdist.read_bytes()

    try:
        module.normalize_sdist(sdist, epoch=1_700_000_000)
    except module.SdistNormalizationError:
        pass
    else:
        raise AssertionError("trailing archive data was accepted")

    assert sdist.read_bytes() == original


def test_normalization_rejects_concatenated_gzip_members(tmp_path: Path) -> None:
    module = _load_module()
    sdist = tmp_path / f"{ROOT}.tar.gz"
    _write_sdist(sdist, gzip_mtime=1_700_000_001, member_mtime=1_700_000_003, reverse=False)
    sdist.write_bytes(sdist.read_bytes() + gzip.compress(b"second gzip member", mtime=0))

    try:
        module.normalize_sdist(sdist, epoch=1_700_000_000)
    except module.SdistNormalizationError:
        pass
    else:
        raise AssertionError("a concatenated gzip member was accepted")


@pytest.mark.parametrize(
    "unsafe_name",
    [
        f"{ROOT}/../outside",
        f"{ROOT}//double",
        f"{ROOT}\\windows-path",
        "/absolute",
        "another-root/file",
    ],
)
def test_normalization_rejects_noncanonical_member_paths(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    module = _load_module()
    sdist = tmp_path / f"{ROOT}.tar.gz"
    _write_custom_sdist(
        sdist,
        [*_required_entries(), (unsafe_name, b"bad", tarfile.REGTYPE)],
    )
    original = sdist.read_bytes()

    with pytest.raises(module.SdistNormalizationError):
        module.normalize_sdist(sdist, epoch=1_700_000_000)

    assert sdist.read_bytes() == original
    assert not list(tmp_path.glob(f".{sdist.name}.*.tmp"))


@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_normalization_rejects_links_and_special_files(
    tmp_path: Path,
    member_type: bytes,
) -> None:
    module = _load_module()
    sdist = tmp_path / f"{ROOT}.tar.gz"
    _write_custom_sdist(
        sdist,
        [*_required_entries(), (f"{ROOT}/unsafe", None, member_type)],
    )

    with pytest.raises(module.SdistNormalizationError):
        module.normalize_sdist(sdist, epoch=1_700_000_000)


def test_normalization_rejects_duplicates_and_missing_required_metadata(tmp_path: Path) -> None:
    module = _load_module()
    duplicate = tmp_path / "duplicate" / f"{ROOT}.tar.gz"
    missing = tmp_path / "missing" / f"{ROOT}.tar.gz"
    duplicate.parent.mkdir()
    missing.parent.mkdir()
    _write_custom_sdist(
        duplicate,
        [*_required_entries(), (f"{ROOT}/PKG-INFO", b"duplicate", tarfile.REGTYPE)],
    )
    _write_custom_sdist(missing, _required_entries()[:-1])

    with pytest.raises(module.SdistNormalizationError):
        module.normalize_sdist(duplicate, epoch=1_700_000_000)
    with pytest.raises(module.SdistNormalizationError):
        module.normalize_sdist(missing, epoch=1_700_000_000)


def test_normalization_rejects_global_pax_headers_and_corrupt_inputs(tmp_path: Path) -> None:
    module = _load_module()
    global_pax = tmp_path / "global" / f"{ROOT}.tar.gz"
    corrupt = tmp_path / "corrupt" / f"{ROOT}.tar.gz"
    global_pax.parent.mkdir()
    corrupt.parent.mkdir()
    _write_custom_sdist(global_pax, _required_entries(), global_pax={"comment": "variable"})
    corrupt.write_bytes(b"not a gzip archive")

    with pytest.raises(module.SdistNormalizationError):
        module.normalize_sdist(global_pax, epoch=1_700_000_000)
    with pytest.raises(module.SdistNormalizationError):
        module.normalize_sdist(corrupt, epoch=1_700_000_000)


def test_normalization_rejects_symlink_inputs_and_invalid_epochs(tmp_path: Path) -> None:
    module = _load_module()
    target = tmp_path / f"{ROOT}.tar.gz"
    link = tmp_path / "linked.tar.gz"
    _write_custom_sdist(target, _required_entries())
    link.symlink_to(target)

    with pytest.raises(module.SdistNormalizationError):
        module.normalize_sdist(link, epoch=1_700_000_000)
    for invalid in (-1, 1 << 32, True, 1.5):
        with pytest.raises(module.SdistNormalizationError):
            module.normalize_sdist(target, epoch=invalid)


def test_normalization_enforces_member_size_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    sdist = tmp_path / f"{ROOT}.tar.gz"
    _write_custom_sdist(
        sdist,
        [*_required_entries(), (f"{ROOT}/large", b"12", tarfile.REGTYPE)],
    )
    monkeypatch.setattr(module, "MAX_MEMBER_BYTES", 1)

    with pytest.raises(module.SdistNormalizationError):
        module.normalize_sdist(sdist, epoch=1_700_000_000)


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
    assert max(limits) <= 1024 * 1024


def test_normalization_rejects_same_size_source_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    sdist = tmp_path / f"{ROOT}.tar.gz"
    _write_custom_sdist(sdist, _required_entries())
    original_size = sdist.stat().st_size
    original_normalized_member = module._normalized_member
    mutated = False

    def mutate_source(member: tarfile.TarInfo, epoch: int) -> tarfile.TarInfo:
        nonlocal mutated
        if not mutated:
            with sdist.open("r+b") as stream:
                stream.seek(9)  # The gzip OS byte is ignored by the open reader.
                current = stream.read(1)
                stream.seek(9)
                stream.write(bytes([current[0] ^ 1]))
            mutated = True
        return original_normalized_member(member, epoch)

    monkeypatch.setattr(module, "_normalized_member", mutate_source)

    with pytest.raises(module.SdistNormalizationError, match="changed"):
        module.normalize_sdist(sdist, epoch=1_700_000_000)

    assert mutated
    assert sdist.stat().st_size == original_size
    assert not list(tmp_path.glob(f".{sdist.name}.*.tmp"))


def test_normalization_rejects_a_regular_file_used_as_a_parent(tmp_path: Path) -> None:
    module = _load_module()
    sdist = tmp_path / f"{ROOT}.tar.gz"
    _write_custom_sdist(
        sdist,
        [
            *_required_entries(),
            (f"{ROOT}/parent", b"file", tarfile.REGTYPE),
            (f"{ROOT}/parent/child", b"child", tarfile.REGTYPE),
        ],
    )

    with pytest.raises(module.SdistNormalizationError, match="parent"):
        module.normalize_sdist(sdist, epoch=1_700_000_000)
