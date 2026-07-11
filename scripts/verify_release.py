#!/usr/bin/env python3
"""Fail closed when release versions or changelog metadata disagree."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from datetime import date
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 after installing project dependencies
    import tomli as tomllib  # type: ignore[no-redef]


_VERSION_RE = re.compile(
    r"(?P<base>\d+\.\d+\.\d+)"
    r"(?:-(?P<label>alpha|beta|rc)(?P<number>0|[1-9]\d*))?\Z"
)


def distribution_version(version: str) -> str:
    """Map the supported SemVer spelling to canonical PEP 440 metadata."""
    match = _VERSION_RE.fullmatch(version)
    if match is None:
        raise ValueError(
            f"version {version!r} is not supported; expected X.Y.Z or X.Y.Z-(alpha|beta|rc)N"
        )
    label = match.group("label")
    if label is None:
        return match.group("base")
    pep440_label = {"alpha": "a", "beta": "b", "rc": "rc"}[label]
    return f"{match.group('base')}{pep440_label}{match.group('number')}"


def _load_unique_json(path: Path) -> dict[str, object]:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
    )
    if not isinstance(value, dict):
        raise TypeError("top-level JSON value must be an object")
    return value


class _ModuleVersionBindingVisitor(ast.NodeVisitor):
    """Find ``__version__`` bindings executed in the module namespace."""

    def __init__(self) -> None:
        self.bindings: list[ast.AST] = []

    def _visit_scope_header(self, node: ast.AST, bound_name: str | None = None) -> None:
        if bound_name == "__version__":
            self.bindings.append(node)
        for field, value in ast.iter_fields(node):
            if field == "body":
                continue
            if isinstance(value, ast.AST):
                self.visit(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        self.visit(item)

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if node.id == "__version__" and isinstance(node.ctx, (ast.Store, ast.Del)):
            self.bindings.append(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_scope_header(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_scope_header(node, node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        self._visit_scope_header(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self._visit_scope_header(node, node.name)

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            bound_name = alias.asname or alias.name.partition(".")[0]
            if bound_name == "__version__":
                self.bindings.append(alias)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        for alias in node.names:
            bound_name = alias.asname or alias.name
            if bound_name in {"__version__", "*"}:
                self.bindings.append(alias)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        if node.name == "__version__":
            self.bindings.append(node)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:  # noqa: N802
        if node.name == "__version__":
            self.bindings.append(node)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:  # noqa: N802
        if node.name == "__version__":
            self.bindings.append(node)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:  # noqa: N802
        if node.rest == "__version__":
            self.bindings.append(node)
        self.generic_visit(node)


def _package_version(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assignments: list[ast.expr | None] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
            value: ast.expr | None = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
            value = None
        else:
            continue
        binding_count = sum(
            1
            for target in targets
            for child in ast.walk(target)
            if isinstance(child, ast.Name)
            and isinstance(child.ctx, ast.Store)
            and child.id == "__version__"
        )
        assignments.extend([value] * binding_count)
    module_bindings = _ModuleVersionBindingVisitor()
    module_bindings.visit(tree)
    if len(assignments) != 1 or len(module_bindings.bindings) != 1:
        raise ValueError(
            "gpt2agent/__init__.py must contain exactly one top-level "
            "__version__ assignment"
        )
    value = assignments[0]
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        raise ValueError(
            "gpt2agent/__init__.py __version__ assignment must be a literal string"
        )
    return value.value


def changelog_section(path: Path, version: str) -> str:
    """Return the one exact dated release section for ``version``."""
    text = path.read_text(encoding="utf-8")
    heading = re.compile(
        rf"^## \[{re.escape(version)}\] - (?P<date>\d{{4}}-\d{{2}}-\d{{2}})\s*$",
        re.MULTILINE,
    )
    matches = list(heading.finditer(text))
    if len(matches) != 1:
        if not matches:
            raise ValueError(f"CHANGELOG.md missing exact dated section for {version}")
        raise ValueError(f"CHANGELOG.md has duplicate dated sections for {version}")
    match = matches[0]
    try:
        date.fromisoformat(match.group("date"))
    except ValueError:
        raise ValueError(
            f"CHANGELOG.md section for {version} has no valid calendar date"
        ) from None
    next_section = re.search(r"^## \[", text[match.end() :], re.MULTILINE)
    end = match.end() + next_section.start() if next_section else len(text)
    section = text[match.start() : end].rstrip() + "\n"
    body = section[match.end() - match.start() :]
    visible_body = re.sub(r"<!--.*?(?:-->|$)", "", body, flags=re.DOTALL)
    body_lines = [
        line.strip()
        for line in visible_body.splitlines()
        if line.strip() and not line.lstrip().startswith("###")
    ]
    if not body_lines:
        raise ValueError(f"CHANGELOG.md section for {version} has no release notes")
    return section


def _changelog_errors(path: Path, version: str) -> list[str]:
    try:
        changelog_section(path, version)
    except ValueError as exc:
        return [str(exc)]
    return []


def verify(root: Path, tag: str | None = None) -> tuple[str | None, list[str]]:
    errors: list[str] = []
    try:
        with (root / "pyproject.toml").open("rb") as stream:
            version = str(tomllib.load(stream)["project"]["version"])
    except (OSError, KeyError, TypeError, ValueError) as exc:
        return None, [f"pyproject.toml: {exc}"]

    try:
        distribution_version(version)
    except ValueError as exc:
        errors.append(str(exc))

    try:
        package_version = _package_version(root / "gpt2agent" / "__init__.py")
        if package_version != version:
            errors.append(
                "gpt2agent/__init__.py version "
                f"{package_version!r} does not match pyproject.toml {version!r}"
            )
    except (OSError, SyntaxError, ValueError) as exc:
        errors.append(str(exc))

    try:
        plugin_version = str(
            _load_unique_json(root / ".claude-plugin" / "plugin.json")["version"]
        )
        if plugin_version != version:
            errors.append(
                ".claude-plugin/plugin.json version "
                f"{plugin_version!r} does not match pyproject.toml {version!r}"
            )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(f".claude-plugin/plugin.json: {exc}")

    try:
        server = _load_unique_json(root / "server.json")
        server_version = str(server["version"])
        package_entry_version = str(server["packages"][0]["version"])
        if server_version != version:
            errors.append(
                f"server.json version {server_version!r} does not match pyproject.toml {version!r}"
            )
        if package_entry_version != version:
            errors.append(
                "server.json packages[0].version "
                f"{package_entry_version!r} does not match pyproject.toml {version!r}"
            )
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        errors.append(f"server.json: {exc}")

    try:
        errors.extend(_changelog_errors(root / "CHANGELOG.md", version))
    except OSError as exc:
        errors.append(f"CHANGELOG.md: {exc}")

    if tag is not None:
        expected_tag = f"v{version}"
        if tag != expected_tag:
            errors.append(f"tag {tag} does not match project version tag {expected_tag}")

    return version, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--tag", help="Expected git tag, including the leading v")
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--print-distribution-version",
        action="store_true",
        help="Print only the canonical PEP 440 distribution version on success",
    )
    output.add_argument(
        "--print-changelog-section",
        action="store_true",
        help="Print only the exact current-version CHANGELOG section on success",
    )
    args = parser.parse_args(argv)

    version, errors = verify(args.root.resolve(), args.tag)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    if args.print_distribution_version:
        print(distribution_version(version or ""))
    elif args.print_changelog_section:
        try:
            sys.stdout.write(changelog_section(args.root / "CHANGELOG.md", version or ""))
        except (OSError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    else:
        print(f"release metadata verified: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
