from __future__ import annotations

import argparse
import os
import re
from collections.abc import Iterable
from pathlib import Path

TEXT_SUFFIXES = frozenset(
    {
        ".cfg",
        ".css",
        ".html",
        ".ini",
        ".js",
        ".json",
        ".jsx",
        ".md",
        ".mjs",
        ".ps1",
        ".py",
        ".pyi",
        ".scss",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".yaml",
        ".yml",
    }
)
TEXT_FILENAMES = frozenset({"Dockerfile", "Makefile", ".env.example", ".grype.yaml"})
EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".next",
        ".pytest_cache",
        ".ruff_cache",
        ".tmp-security",
        ".tmp-visual-final",
        ".tmp-visual-frontfix",
        ".venv",
        "__pycache__",
        "coverage",
        "node_modules",
        "playwright-report",
        "prod",
        "test-results",
        "tmp",
    }
)


def _character(codepoint: int) -> str:
    return chr(codepoint)


def _misdecoded_continuation_characters() -> str:
    latin_1 = "".join(_character(codepoint) for codepoint in range(0x80, 0xC0))
    windows_1252 = "".join(
        bytes((byte,)).decode("windows-1252", errors="ignore")
        for byte in range(0x80, 0xC0)
    )
    return "".join(dict.fromkeys(latin_1 + windows_1252))


# The expressions describe byte-decoding artefacts, not natural-language letters.
# Constructing them from code points keeps the scanner from reporting its own source.
MOJIBAKE_PATTERNS = (
    (
        "UTF-8 bytes decoded as Latin-1 or Windows-1252",
        re.compile(
            f"[{_character(0x00C2)}{_character(0x00C3)}"
            f"{_character(0x00E2)}{_character(0x00EF)}{_character(0x00F0)}]"
            f"[{re.escape(_misdecoded_continuation_characters())}]"
        ),
    ),
    (
        "double-encoded UTF-8",
        re.compile(f"{_character(0x00C3)}{_character(0x0192)}"),
    ),
    (
        "replacement character",
        re.compile(_character(0xFFFD)),
    ),
    (
        "C1 control character",
        re.compile(f"[{_character(0x0080)}-{_character(0x009F)}]"),
    ),
)


def iter_text_files(root: Path) -> Iterable[Path]:
    for directory, child_directories, filenames in os.walk(root):
        child_directories[:] = sorted(
            name
            for name in child_directories
            if name not in EXCLUDED_PARTS
            and not name.startswith(".tmp-")
            and not name.startswith(".next-")
        )
        parent = Path(directory)
        for filename in sorted(filenames):
            path = parent / filename
            if path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_FILENAMES:
                yield path


def find_encoding_issues(root: Path) -> list[str]:
    issues: list[str] = []
    for path in iter_text_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            issues.append(f"{path.relative_to(root)}: invalid UTF-8 at byte {exc.start}")
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            for description, pattern in MOJIBAKE_PATTERNS:
                if pattern.search(line):
                    issues.append(f"{path.relative_to(root)}:{line_number}: {description}")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reject invalid UTF-8 and characteristic mojibake in active source files."
    )
    parser.add_argument("root", nargs="?", type=Path, default=Path("."))
    args = parser.parse_args()

    issues = find_encoding_issues(args.root.resolve())
    if issues:
        print("Text encoding check failed:")
        for issue in issues:
            print(f"- {issue}")
        return 1
    print("Text encoding check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
