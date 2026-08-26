"""Create a deterministic manifest for image inputs and release workflows.

The manifest deliberately contains no clock, host, Git, or filesystem timestamp
metadata. It records Docker-visible application inputs after applying
``.dockerignore`` plus explicitly named release-control inputs (currently the
GitHub workflows) that govern how those images are built and accepted.
``VCS_REF`` and the final clean-tree gate bind the remainder of the repository;
this manifest identifies the exact build-and-workflow source subset.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shlex
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = "custombuild.build-control-source-manifest.v2"
APPLICATION_BUILD_INPUTS = (
    ".github/workflows",
    ".dockerignore",
    ".grype.yaml",
    "Makefile",
    "apps/web",
    "cad",
    "cam",
    "compose.external-production.yml",
    "compose.yml",
    "infra",
    "package.json",
    "packages",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "postprocessors",
    "pyproject.toml",
    "scripts",
    "security",
    "services",
    "uv.lock",
)
APPLICATION_DOCKERFILES = (
    "apps/web/Dockerfile",
    "infra/seaweedfs/Dockerfile",
    "services/api/Dockerfile",
    "services/worker/Dockerfile",
)
RELEASE_CONTROL_INPUTS = (".github/workflows",)
LEGACY_SOURCE_ROOT = "prod"


class SourceManifestError(RuntimeError):
    """The source tree cannot be represented safely and deterministically."""


@dataclass(frozen=True, slots=True)
class IgnoreRule:
    pattern: str
    negated: bool
    directory_only: bool
    anchored: bool


class DockerIgnore:
    """Small, platform-neutral implementation of Docker ignore matching.

    It implements the Docker features used by this repository: root-relative
    slash patterns, basename patterns at any depth, ``*``, ``?``, character
    classes, ``**``, directory patterns, comments, and ordered negation.
    """

    def __init__(self, source: str) -> None:
        rules: list[IgnoreRule] = []
        for raw_line in source.splitlines():
            line = raw_line.rstrip()
            if not line or line == "." or line.startswith("#"):
                continue
            negated = line.startswith("!")
            if negated or line.startswith(r"\!"):
                line = line[1:]
            escaped_comment = line.startswith(r"\#")
            if escaped_comment:
                line = line[1:]
            elif line.startswith("#"):
                continue
            line = line.replace("\\", "/")
            anchored = line.startswith("/")
            directory_only = line.endswith("/")
            line = line.strip("/")
            if not line:
                continue
            if any(part == ".." for part in PurePosixPath(line).parts):
                raise SourceManifestError(f".dockerignore pattern escapes the context: {raw_line}")
            rules.append(IgnoreRule(line, negated, directory_only, anchored))
        self.rules = tuple(rules)
        self.has_negation = any(rule.negated for rule in rules)

    @staticmethod
    def _match_segments(path: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
        cache: dict[tuple[int, int], bool] = {}

        def matches(path_index: int, pattern_index: int) -> bool:
            key = (path_index, pattern_index)
            if key in cache:
                return cache[key]
            if pattern_index == len(pattern):
                result = path_index == len(path)
            elif pattern[pattern_index] == "**":
                result = matches(path_index, pattern_index + 1) or (
                    path_index < len(path) and matches(path_index + 1, pattern_index)
                )
            else:
                result = path_index < len(path) and fnmatch.fnmatchcase(
                    path[path_index], pattern[pattern_index]
                ) and matches(path_index + 1, pattern_index + 1)
            cache[key] = result
            return result

        return matches(0, 0)

    @classmethod
    def _matches_rule(cls, path: str, *, is_dir: bool, rule: IgnoreRule) -> bool:
        if rule.directory_only and not is_dir:
            return False
        path_parts = PurePosixPath(path).parts
        pattern_parts = PurePosixPath(rule.pattern).parts
        if "/" not in rule.pattern and not rule.anchored:
            return bool(path_parts) and fnmatch.fnmatchcase(path_parts[-1], rule.pattern)
        return cls._match_segments(path_parts, pattern_parts)

    def is_ignored(self, path: str, *, is_dir: bool) -> bool:
        """Return Docker's final ignore state, including ignored ancestors."""

        parts = PurePosixPath(path).parts
        ignored = False
        for prefix_length in range(1, len(parts) + 1):
            candidate = "/".join(parts[:prefix_length])
            candidate_is_dir = prefix_length < len(parts) or is_dir
            for rule in self.rules:
                if self._matches_rule(candidate, is_dir=candidate_is_dir, rule=rule):
                    ignored = not rule.negated
        return ignored


FileState = tuple[int, int, int, int]


def _state(path: Path) -> FileState:
    value = path.lstat()
    return (value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _sha256_file(path: Path) -> tuple[str, int, FileState]:
    before = path.stat()
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    after = path.stat()
    before_state = (before.st_mode, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    after_state = (after.st_mode, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if before_state != after_state or size != after.st_size:
        raise SourceManifestError(f"source changed while being hashed: {path}")
    return digest.hexdigest(), size, after_state


def _relative_posix(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _normalised_file_mode(mode: int) -> int:
    """Preserve Docker-relevant executability without host-specific write bits."""

    return 0o755 if mode & 0o111 else 0o644


def _is_application_build_input(relative: str) -> bool:
    return any(
        relative == input_path or relative.startswith(f"{input_path}/")
        for input_path in APPLICATION_BUILD_INPUTS
    )


def _is_release_control_path_or_ancestor(relative: str) -> bool:
    return any(
        relative == input_path
        or relative.startswith(f"{input_path}/")
        or input_path.startswith(f"{relative}/")
        for input_path in RELEASE_CONTROL_INPUTS
    )


def _dockerfile_local_copy_sources(path: Path) -> tuple[str, ...]:
    sources: list[str] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw_line.strip()
        if not stripped.upper().startswith("COPY "):
            continue
        if stripped[4:].lstrip().startswith("["):
            raise SourceManifestError(
                f"unsupported JSON COPY syntax in {path}:{line_number}; update source manifest"
            )
        try:
            tokens = shlex.split(stripped, posix=True)
        except ValueError as exc:
            raise SourceManifestError(f"invalid COPY in {path}:{line_number}") from exc
        arguments = tokens[1:]
        local_copy = True
        while arguments and arguments[0].startswith("--"):
            option = arguments.pop(0)
            if option == "--from" and arguments:
                arguments.pop(0)
                local_copy = False
            elif option.startswith("--from="):
                local_copy = False
        if not local_copy:
            continue
        if len(arguments) < 2:
            raise SourceManifestError(f"invalid COPY in {path}:{line_number}")
        for source in arguments[:-1]:
            normalised = source.replace("\\", "/").strip("/")
            if not normalised or any(character in normalised for character in "*?["):
                raise SourceManifestError(
                    f"unsupported broad or wildcard COPY in {path}:{line_number}; "
                    "update source manifest"
                )
            sources.append(normalised)
    return tuple(sources)


def _validate_dockerfile_copy_contract(root: Path) -> None:
    dockerfiles = [root / Path(*PurePosixPath(path).parts) for path in APPLICATION_DOCKERFILES]
    present = [path.is_file() for path in dockerfiles]
    if not any(present):
        return  # Minimal unit-test fixture, not an application repository.
    if not all(present):
        missing = [
            relative
            for relative, exists in zip(APPLICATION_DOCKERFILES, present, strict=True)
            if not exists
        ]
        raise SourceManifestError(f"missing application Dockerfiles: {', '.join(missing)}")
    for dockerfile in dockerfiles:
        for source in _dockerfile_local_copy_sources(dockerfile):
            if not _is_application_build_input(source):
                relative = dockerfile.relative_to(root).as_posix()
                raise SourceManifestError(
                    f"Docker COPY source is outside the manifest contract: {relative}: {source}"
                )


def _discover_paths(root: Path, dockerignore: DockerIgnore) -> list[tuple[str, str, Path]]:
    discovered: list[tuple[str, str, Path]] = []

    def fail_walk(error: OSError) -> None:
        raise SourceManifestError(
            f"cannot enumerate build/control source inputs: {error}"
        ) from error

    for current, directory_names, file_names in os.walk(
        root,
        topdown=True,
        onerror=fail_walk,
        followlinks=False,
    ):
        current_path = Path(current)
        visible_directories: list[str] = []
        for name in sorted(directory_names):
            path = current_path / name
            relative = _relative_posix(root, path)
            is_junction = bool(getattr(path, "is_junction", lambda: False)())
            is_link = path.is_symlink() or is_junction
            ignored = dockerignore.is_ignored(relative, is_dir=not is_link)
            release_control = _is_release_control_path_or_ancestor(relative)
            if ignored and not dockerignore.has_negation and not release_control:
                continue
            if not ignored or release_control:
                discovered.append((relative, "symlink" if is_link else "directory", path))
            if not is_link:
                visible_directories.append(name)
        directory_names[:] = visible_directories

        for name in sorted(file_names):
            path = current_path / name
            relative = _relative_posix(root, path)
            if dockerignore.is_ignored(
                relative, is_dir=False
            ) and not _is_release_control_path_or_ancestor(relative):
                continue
            discovered.append((relative, "symlink" if path.is_symlink() else "file", path))
    try:
        discovered.sort(key=lambda item: item[0].encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise SourceManifestError("Docker build input paths must be valid UTF-8") from exc
    return [item for item in discovered if _is_application_build_input(item[0])]


def build_source_manifest(repo: Path) -> tuple[dict[str, Any], bytes, str]:
    """Return the manifest object, canonical bytes, and SHA-256 digest."""

    root = repo.resolve()
    legacy_root = root / LEGACY_SOURCE_ROOT
    if legacy_root.exists():
        raise SourceManifestError(
            "legacy prod/ source tree is present; the repository root must be the only "
            "release source"
        )
    dockerignore_path = root / ".dockerignore"
    if not dockerignore_path.is_file():
        raise SourceManifestError(f"missing Docker ignore contract: {dockerignore_path}")
    dockerignore_bytes = dockerignore_path.read_bytes()
    try:
        dockerignore_source = dockerignore_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceManifestError(".dockerignore must be UTF-8") from exc
    dockerignore = DockerIgnore(dockerignore_source)
    _validate_dockerfile_copy_contract(root)
    discovered = _discover_paths(root, dockerignore)
    entries: list[dict[str, Any]] = []
    path_states: dict[str, FileState] = {}
    for relative, kind, path in discovered:
        if kind == "directory":
            entries.append({"mode": 0o755, "path": relative, "type": kind})
            path_states[relative] = _state(path)
            continue
        if kind == "symlink":
            entries.append(
                {"mode": 0o777, "path": relative, "target": os.readlink(path), "type": kind}
            )
            path_states[relative] = _state(path)
            continue
        try:
            mode = path.stat().st_mode
        except OSError as exc:
            raise SourceManifestError(f"cannot inspect build input: {relative}") from exc
        if not stat.S_ISREG(mode):
            raise SourceManifestError(f"unsupported build input type: {relative}")
        try:
            digest, size, state = _sha256_file(path)
        except OSError as exc:
            raise SourceManifestError(f"cannot read build input: {relative}") from exc
        entries.append(
            {
                "mode": _normalised_file_mode(mode),
                "path": relative,
                "sha256": digest,
                "size": size,
                "type": kind,
            }
        )
        path_states[relative] = state

    rediscovered = [(path, kind) for path, kind, _ in _discover_paths(root, dockerignore)]
    if rediscovered != [(path, kind) for path, kind, _ in discovered]:
        raise SourceManifestError("source paths changed while the manifest was created; rerun")
    for relative, state in path_states.items():
        if _state(root / Path(*PurePosixPath(relative).parts)) != state:
            raise SourceManifestError(f"source changed while the manifest was created: {relative}")

    manifest: dict[str, Any] = {
        "dockerignore_sha256": hashlib.sha256(dockerignore_bytes).hexdigest(),
        "entries": entries,
        "input_roots": list(APPLICATION_BUILD_INPUTS),
        "release_control_roots": list(RELEASE_CONTROL_INPUTS),
        "schema_version": SCHEMA_VERSION,
    }
    canonical = json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    try:
        canonical_bytes = canonical.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SourceManifestError("build/control input paths must be valid UTF-8") from exc
    return manifest, canonical_bytes, hashlib.sha256(canonical_bytes).hexdigest()


def _ensure_output_is_not_a_build_input(
    *,
    repo: Path,
    output: Path,
    dockerignore: DockerIgnore,
) -> Path:
    resolved = output.resolve()
    try:
        relative = resolved.relative_to(repo.resolve()).as_posix()
    except ValueError:
        return resolved
    if not dockerignore.is_ignored(
        relative, is_dir=False
    ) or _is_release_control_path_or_ancestor(relative):
        raise SourceManifestError(
            f"manifest output would change the build/control source set: {relative}; "
            "write it outside the repository or under an ignored path such as artifacts/"
        )
    return resolved


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sha256-output", type=Path)
    parser.add_argument("--expect-sha256")
    arguments = parser.parse_args(argv)

    try:
        repo = arguments.repo.resolve()
        dockerignore = DockerIgnore((repo / ".dockerignore").read_text(encoding="utf-8"))
        _, canonical, digest = build_source_manifest(repo)
        if arguments.expect_sha256 is not None and digest != arguments.expect_sha256:
            raise SourceManifestError(
                f"source manifest mismatch: expected {arguments.expect_sha256}, got {digest}"
            )
        if arguments.output is not None:
            output = _ensure_output_is_not_a_build_input(
                repo=repo,
                output=arguments.output,
                dockerignore=dockerignore,
            )
            _atomic_write(output, canonical)
        if arguments.sha256_output is not None:
            sha_output = _ensure_output_is_not_a_build_input(
                repo=repo,
                output=arguments.sha256_output,
                dockerignore=dockerignore,
            )
            label = (
                arguments.output.name
                if arguments.output is not None
                else "source-manifest.json"
            )
            _atomic_write(sha_output, f"{digest}  {label}\n".encode("ascii"))
    except (OSError, SourceManifestError) as exc:
        print(f"source manifest error: {exc}", file=sys.stderr)
        return 2

    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
