from pathlib import Path

from scripts.check_text_encoding import find_encoding_issues


def test_encoding_scan_accepts_real_swedish_and_other_latin_text(tmp_path: Path) -> None:
    (tmp_path / "labels.ts").write_text(
        'const labels = ["Skåpsfront", "Förrän", "ÅÄÖ åäö", "São Tomé", '
        '"François", "trä – hållbart"];\n',
        encoding="utf-8",
    )

    assert find_encoding_issues(tmp_path) == []


def test_encoding_scan_rejects_mojibake_replacement_and_invalid_utf8(tmp_path: Path) -> None:
    mojibake = b"Sk\xc3\xa5psfront".decode("latin-1")
    uppercase_mojibake = b"\xc3\x85\xc3\x84\xc3\x96".decode("windows-1252")
    punctuation_mojibake = b"\xe2\x80\x93".decode("windows-1252")
    emoji_mojibake = b"\xf0\x9f\x94\xa5".decode("windows-1252")
    (tmp_path / "broken.ts").write_text(
        f'const labels = ["{mojibake}", "{uppercase_mojibake}", '
        f'"{punctuation_mojibake}", "{emoji_mojibake}"];\n',
        encoding="utf-8",
    )
    (tmp_path / "replacement.md").write_text(
        f"broken {_replacement_character()}\n", encoding="utf-8"
    )
    (tmp_path / "invalid.py").write_bytes(b"label = '\xff'\n")

    issues = find_encoding_issues(tmp_path)

    assert any("broken.ts:1: UTF-8 bytes decoded" in issue for issue in issues)
    assert any("replacement.md:1: replacement character" in issue for issue in issues)
    assert any("invalid.py: invalid UTF-8" in issue for issue in issues)


def test_encoding_scan_ignores_generated_tmp_build_outputs(tmp_path: Path) -> None:
    generated = tmp_path / "apps" / "web" / "tmp" / "next-build"
    generated.mkdir(parents=True)
    (generated / "chunk.js").write_text(
        f"generated {_replacement_character()}\n", encoding="utf-8"
    )

    assert find_encoding_issues(tmp_path) == []


def test_encoding_scan_ignores_named_next_build_outputs(tmp_path: Path) -> None:
    generated = tmp_path / "apps" / "web" / ".next-accessibility-qa"
    generated.mkdir(parents=True)
    (generated / "chunk.js").write_text(
        f"generated {_replacement_character()}\n", encoding="utf-8"
    )

    assert find_encoding_issues(tmp_path) == []


def test_active_repository_has_clean_text_encoding() -> None:
    assert find_encoding_issues(Path(".")) == []


def _replacement_character() -> str:
    return chr(0xFFFD)
