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
import shutil
import stat
import subprocess
import sys
import unicodedata
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
    "compose.registry.yml",
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
PRODUCTION_SEMANTIC_ROOT_PATH = "security/production-semantic-root.json"
PRODUCTION_SEMANTIC_ROOT_SCHEMA = "custombuild.production-semantic-root.v1"
_CONTENT_LEAF_DOMAIN = b"custombuild.repository-content-leaf.v1\0"
_CONTENT_NODE_DOMAIN = b"custombuild.repository-content-node.v1\0"
_CONTENT_ROOT_DOMAIN = b"custombuild.repository-content-root.v1\0"


class SourceManifestError(RuntimeError):
    """The source tree cannot be represented safely and deterministically."""


@dataclass(frozen=True, slots=True)
class RepositoryContentRoot:
    """Exact identity of the safe, stage-0 tracked repository contents."""

    digest: str
    git_object_format: str
    tracked_entry_count: int


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
                result = (
                    path_index < len(path)
                    and fnmatch.fnmatchcase(path[path_index], pattern[pattern_index])
                    and matches(path_index + 1, pattern_index + 1)
                )
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
RepositoryFileState = tuple[int, int, int, int, int, int]


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


def _git_bytes(repo: Path, *arguments: str) -> bytes:
    git = shutil.which("git")
    if git is None:
        raise SourceManifestError("git is required for repository content identity")
    try:
        completed = subprocess.run(  # noqa: S603 - resolved trusted executable
            [git, "-c", "core.quotepath=false", *arguments],
            cwd=repo,
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise SourceManifestError("git is required for repository content identity") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise SourceManifestError(
            f"git {' '.join(arguments)} failed" + (f": {detail}" if detail else "")
        )
    return completed.stdout


def _safe_git_path(raw_path: bytes) -> str:
    if not raw_path or raw_path.startswith(b"/") or b"\\" in raw_path:
        raise SourceManifestError("tracked repository contains an unsafe path")
    try:
        path = raw_path.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SourceManifestError("tracked repository paths must be valid UTF-8") from exc
    if unicodedata.normalize("NFC", path) != path:
        raise SourceManifestError(f"tracked repository path is not NFC-normalised: {path!r}")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in path):
        raise SourceManifestError(f"tracked repository path contains control bytes: {path!r}")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise SourceManifestError(f"tracked repository path is not canonical: {path!r}")
    return path


def _parse_stage_zero_inventory(payload: bytes) -> list[tuple[bytes, str, str]]:
    entries: list[tuple[bytes, str, str]] = []
    seen: set[bytes] = set()
    for record in payload.split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode_bytes, object_id_bytes, stage_bytes = header.split(b" ")
            mode = mode_bytes.decode("ascii")
            object_id = object_id_bytes.decode("ascii")
            stage = stage_bytes.decode("ascii")
        except (UnicodeDecodeError, ValueError) as exc:
            raise SourceManifestError("Git index contains a malformed stage entry") from exc
        _safe_git_path(raw_path)
        if stage != "0":
            raise SourceManifestError("Git index contains unresolved non-stage-0 entries")
        if raw_path in seen:
            raise SourceManifestError("Git index contains a duplicate tracked path")
        seen.add(raw_path)
        entries.append((raw_path, mode, object_id))
    entries.sort(key=lambda item: item[0])
    return entries


def _index_snapshot(repo: Path) -> tuple[tuple[int, int, int, int, int, int], str]:
    raw_index_path = _git_bytes(repo, "rev-parse", "--git-path", "index").strip()
    try:
        index_path_text = raw_index_path.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SourceManifestError("Git index path must be valid UTF-8") from exc
    index_path = Path(index_path_text)
    if not index_path.is_absolute():
        index_path = repo / index_path
    try:
        before = index_path.stat()
        payload = index_path.read_bytes()
        after = index_path.stat()
    except OSError as exc:
        raise SourceManifestError("cannot read the Git index atomically") from exc
    before_state = (
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
        before.st_dev,
        before.st_ino,
    )
    after_state = (
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        after.st_dev,
        after.st_ino,
    )
    if before_state != after_state or len(payload) != after.st_size:
        raise SourceManifestError("Git index changed while it was read")
    return after_state, hashlib.sha256(payload).hexdigest()


def _untracked_inventory(repo: Path) -> bytes:
    return _git_bytes(
        repo,
        "ls-files",
        "--others",
        "--exclude-standard",
        "--full-name",
        "-z",
    )


def _assert_no_untracked_inputs(payload: bytes) -> None:
    allowed = PRODUCTION_SEMANTIC_ROOT_PATH.encode("utf-8")
    unexpected: list[str] = []
    for raw_path in payload.split(b"\0"):
        if not raw_path:
            continue
        path = _safe_git_path(raw_path)
        if raw_path != allowed:
            unexpected.append(path)
    if unexpected:
        preview = ", ".join(repr(path) for path in unexpected[:5])
        suffix = " ..." if len(unexpected) > 5 else ""
        raise SourceManifestError(
            f"repository contains non-ignored untracked paths: {preview}{suffix}"
        )


def _read_index_bound_regular_file(
    repo: Path,
    raw_path: bytes,
    *,
    git_mode: str,
    object_id: str,
    object_format: str,
) -> tuple[bytes, RepositoryFileState]:
    path_text = _safe_git_path(raw_path)
    if git_mode == "120000":
        raise SourceManifestError(f"tracked symlinks are forbidden: {path_text}")
    if git_mode == "160000":
        raise SourceManifestError(f"tracked submodules are forbidden: {path_text}")
    if git_mode not in {"100644", "100755"}:
        raise SourceManifestError(f"unsupported Git mode {git_mode} for {path_text}")
    path = repo.joinpath(*path_text.split("/"))
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise SourceManifestError(f"tracked repository file is missing: {path_text}") from exc
    except OSError as exc:
        raise SourceManifestError(f"cannot safely open tracked file: {path_text}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SourceManifestError(f"tracked path is not a regular file: {path_text}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    state_before = (
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
        before.st_dev,
        before.st_ino,
    )
    state_after = (
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        after.st_dev,
        after.st_ino,
    )
    payload = b"".join(chunks)
    if state_before != state_after or len(payload) != after.st_size:
        raise SourceManifestError(f"tracked file changed while being read: {path_text}")
    try:
        path_after = path.lstat()
    except OSError as exc:
        raise SourceManifestError(
            f"tracked file disappeared while being read: {path_text}"
        ) from exc
    path_state = (
        path_after.st_mode,
        path_after.st_size,
        path_after.st_mtime_ns,
        path_after.st_ctime_ns,
        path_after.st_dev,
        path_after.st_ino,
    )
    if path_state != state_after:
        raise SourceManifestError(f"tracked path changed while being read: {path_text}")
    executable = bool(after.st_mode & 0o111)
    if executable != (git_mode == "100755"):
        raise SourceManifestError(f"working-tree mode differs from the Git index: {path_text}")
    object_hash = hashlib.new(object_format)
    object_hash.update(f"blob {len(payload)}\0".encode("ascii"))
    object_hash.update(payload)
    if object_hash.hexdigest() != object_id:
        raise SourceManifestError(f"working-tree bytes differ from the Git index: {path_text}")
    return payload, state_after


def _repository_path_state(repo: Path, raw_path: bytes) -> RepositoryFileState:
    path_text = _safe_git_path(raw_path)
    path = repo.joinpath(*path_text.split("/"))
    try:
        current = path.lstat()
    except OSError as exc:
        raise SourceManifestError(
            f"tracked path disappeared during repository verification: {path_text}"
        ) from exc
    return (
        current.st_mode,
        current.st_size,
        current.st_mtime_ns,
        current.st_ctime_ns,
        current.st_dev,
        current.st_ino,
    )


def _merkle_root(leaves: list[bytes], *, object_format: str) -> str:
    if not leaves:
        tree = hashlib.sha256(_CONTENT_NODE_DOMAIN + b"empty").digest()
    else:
        level = leaves
        while len(level) > 1:
            next_level: list[bytes] = []
            for index in range(0, len(level), 2):
                left = level[index]
                right = level[index + 1] if index + 1 < len(level) else left
                next_level.append(hashlib.sha256(_CONTENT_NODE_DOMAIN + left + right).digest())
            level = next_level
        tree = level[0]
    root = hashlib.sha256()
    root.update(_CONTENT_ROOT_DOMAIN)
    root.update(object_format.encode("ascii") + b"\0")
    root.update(len(leaves).to_bytes(8, "big"))
    root.update(tree)
    return root.hexdigest()


def _build_repository_content_snapshot(
    repo: Path,
    *,
    bind_control: bool,
) -> tuple[RepositoryContentRoot, bytes | None]:
    """Hash a single index/worktree snapshot and optionally bind its control bytes.

    Non-ignored untracked paths fail closed. Ignored untracked files remain outside
    this diagnostic by design. The checked control file excludes itself to avoid a
    recursive identity; its exact payload is verified separately.
    """

    root = repo.resolve()
    try:
        top_level = (
            _git_bytes(root, "rev-parse", "--show-toplevel")
            .strip()
            .decode("utf-8", errors="strict")
        )
    except UnicodeDecodeError as exc:
        raise SourceManifestError("Git top-level path must be valid UTF-8") from exc
    if Path(top_level).resolve() != root:
        raise SourceManifestError("repository content root must run at the Git top level")
    try:
        object_format = (
            _git_bytes(root, "rev-parse", "--show-object-format")
            .strip()
            .decode("ascii", errors="strict")
        )
    except UnicodeDecodeError as exc:
        raise SourceManifestError("Git object format must be ASCII") from exc
    if object_format not in {"sha1", "sha256"}:
        raise SourceManifestError(f"unsupported Git object format: {object_format!r}")

    index_before = _index_snapshot(root)
    inventory_before = _git_bytes(
        root,
        "ls-files",
        "--stage",
        "--full-name",
        "-z",
    )
    untracked_before = _untracked_inventory(root)
    if _index_snapshot(root) != index_before:
        raise SourceManifestError("Git index changed while inventory was collected")
    _assert_no_untracked_inputs(untracked_before)

    lock_path = PRODUCTION_SEMANTIC_ROOT_PATH.encode("utf-8")
    entries = _parse_stage_zero_inventory(inventory_before)
    leaves: list[bytes] = []
    tracked_states: dict[bytes, RepositoryFileState] = {}
    control_bytes: bytes | None = None
    for raw_path, git_mode, object_id in entries:
        if raw_path == lock_path:
            if git_mode == "120000":
                raise SourceManifestError(
                    f"tracked symlinks are forbidden: {PRODUCTION_SEMANTIC_ROOT_PATH}"
                )
            if git_mode == "160000":
                raise SourceManifestError(
                    f"tracked submodules are forbidden: {PRODUCTION_SEMANTIC_ROOT_PATH}"
                )
            if git_mode != "100644":
                raise SourceManifestError(
                    "production semantic root control must use Git mode 100644"
                )
            if bind_control:
                control_bytes, tracked_states[raw_path] = _read_index_bound_regular_file(
                    root,
                    raw_path,
                    git_mode=git_mode,
                    object_id=object_id,
                    object_format=object_format,
                )
            continue
        payload, tracked_states[raw_path] = _read_index_bound_regular_file(
            root,
            raw_path,
            git_mode=git_mode,
            object_id=object_id,
            object_format=object_format,
        )
        leaf = hashlib.sha256()
        leaf.update(_CONTENT_LEAF_DOMAIN)
        leaf.update(len(raw_path).to_bytes(8, "big"))
        leaf.update(raw_path)
        leaf.update(git_mode.encode("ascii"))
        leaf.update(len(payload).to_bytes(8, "big"))
        leaf.update(payload)
        leaf.update(object_format.encode("ascii") + b"\0")
        leaf.update(bytes.fromhex(object_id))
        leaves.append(leaf.digest())

    if bind_control and control_bytes is None:
        raise SourceManifestError("production semantic root control is not tracked")
    for raw_path, expected_state in tracked_states.items():
        if _repository_path_state(root, raw_path) != expected_state:
            path_text = _safe_git_path(raw_path)
            raise SourceManifestError(
                f"tracked path changed during repository verification: {path_text}"
            )

    inventory_after = _git_bytes(
        root,
        "ls-files",
        "--stage",
        "--full-name",
        "-z",
    )
    untracked_after = _untracked_inventory(root)
    if (
        inventory_after != inventory_before
        or untracked_after != untracked_before
        or _index_snapshot(root) != index_before
    ):
        raise SourceManifestError("repository inventory changed while content was hashed")
    return (
        RepositoryContentRoot(
            digest=_merkle_root(leaves, object_format=object_format),
            git_object_format=object_format,
            tracked_entry_count=len(leaves),
        ),
        control_bytes,
    )


def build_repository_content_root(repo: Path) -> RepositoryContentRoot:
    """Hash every safe tracked stage-0 file and prove index/worktree agreement."""

    return _build_repository_content_snapshot(repo, bind_control=False)[0]


def production_semantic_root_payload(identity: RepositoryContentRoot) -> dict[str, Any]:
    return {
        "authorization": False,
        "external_semantic_approval_required": True,
        "git_object_format": identity.git_object_format,
        "repository_content_root_sha256": identity.digest,
        "schema_version": PRODUCTION_SEMANTIC_ROOT_SCHEMA,
        "tracked_entry_count": identity.tracked_entry_count,
    }


def _canonical_control_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def verify_production_semantic_root(repo: Path) -> RepositoryContentRoot:
    identity, raw = _build_repository_content_snapshot(repo, bind_control=True)
    if raw is None:  # pragma: no cover - the snapshot helper fails closed first.
        raise SourceManifestError("production semantic root control is not tracked")
    if len(raw) > 4096:
        raise SourceManifestError("production semantic root control exceeds its size limit")

    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SourceManifestError(f"production semantic root control repeats key {key!r}")
            result[key] = value
        return result

    try:
        parsed = json.loads(raw, object_pairs_hook=reject_duplicate)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceManifestError("production semantic root control is not valid JSON") from exc
    expected = production_semantic_root_payload(identity)
    if not isinstance(parsed, dict) or parsed != expected:
        raise SourceManifestError("production semantic root control is stale or malformed")
    if raw != _canonical_control_bytes(expected):
        raise SourceManifestError("production semantic root control is not canonical JSON")
    return identity


def update_production_semantic_root(repo: Path) -> RepositoryContentRoot:
    identity = build_repository_content_root(repo)
    path = repo.resolve() / PRODUCTION_SEMANTIC_ROOT_PATH
    _atomic_write(path, _canonical_control_bytes(production_semantic_root_payload(identity)))
    return identity


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
    if not dockerignore.is_ignored(relative, is_dir=False) or _is_release_control_path_or_ancestor(
        relative
    ):
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
    semantic_root = parser.add_mutually_exclusive_group()
    semantic_root.add_argument("--check-production-semantic-root", action="store_true")
    semantic_root.add_argument("--update-production-semantic-root", action="store_true")
    arguments = parser.parse_args(argv)

    try:
        repo = arguments.repo.resolve()
        if (
            arguments.check_production_semantic_root or arguments.update_production_semantic_root
        ) and any(
            value is not None
            for value in (
                arguments.output,
                arguments.sha256_output,
                arguments.expect_sha256,
            )
        ):
            raise SourceManifestError(
                "production semantic-root mode cannot emit or compare the build manifest"
            )
        if arguments.check_production_semantic_root:
            identity = verify_production_semantic_root(repo)
            print(identity.digest)
            return 0
        if arguments.update_production_semantic_root:
            identity = update_production_semantic_root(repo)
            print(identity.digest)
            return 0
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
                arguments.output.name if arguments.output is not None else "source-manifest.json"
            )
            _atomic_write(sha_output, f"{digest}  {label}\n".encode("ascii"))
    except (OSError, SourceManifestError) as exc:
        print(f"source manifest error: {exc}", file=sys.stderr)
        return 2

    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
