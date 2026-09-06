from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import verify_linuxcnc_interpreter_oracle as oracle


def test_g43_fixture_separates_loaded_tool_h_tool_and_pocket_row() -> None:
    repo = Path(__file__).resolve().parents[2]
    oracle._verify_lookup_fixture(
        repo / "tests" / "linuxcnc-oracle" / "g43-sign.ngc",
        repo / "tests" / "linuxcnc-oracle" / "tool.tbl",
    )

    assert oracle.EXPECTED_M6_TOOL_WORDS == (7, 8)
    assert oracle.EXPECTED_G43_H_WORDS == (17, 18)
    for loaded_tool, h_word in zip(
        oracle.EXPECTED_M6_TOOL_WORDS,
        oracle.EXPECTED_G43_H_WORDS,
        strict=True,
    ):
        target_pocket, target_z = oracle.EXPECTED_TOOL_TABLE_ROWS[h_word]
        pocket_tool, (_pocket, pocket_z) = next(
            (tool, row) for tool, row in oracle.EXPECTED_TOOL_TABLE_ROWS.items() if row[0] == h_word
        )
        assert loaded_tool != h_word
        assert target_pocket != h_word
        assert pocket_tool != h_word
        assert target_z != pocket_z


def test_rs274_command_keeps_options_before_input_and_isolates_getopt_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_command: list[str] = []
    captured_environment: dict[str, str] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured_command.extend(command)
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        captured_environment.update(environment)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setenv("POSIXLY_CORRECT", "1")
    monkeypatch.setattr("scripts.verify_linuxcnc_interpreter_oracle.subprocess.run", fake_run)
    runtime_dir = tmp_path / "runtime"
    input_file = tmp_path / "oracle.ngc"
    parameter_file = tmp_path / "production.var"
    parameter_file.write_bytes(b"5220 1.000000\n5223 -60.000000\n")

    completed = oracle._run_rs274(
        "/opt/trusted/rs274",
        "-i",
        "metric.ini",
        "-t",
        "tool.tbl",
        "-g",
        input_file=input_file,
        runtime_dir=runtime_dir,
        parameter_file=parameter_file,
    )

    assert completed.returncode == 0
    assert (runtime_dir / "rs274ngc.var").read_bytes() == parameter_file.read_bytes()
    assert captured_command == [
        "/opt/trusted/rs274",
        "-v",
        str(runtime_dir / "rs274ngc.var"),
        "-i",
        "metric.ini",
        "-t",
        "tool.tbl",
        "-g",
        str(input_file),
    ]
    assert "POSIXLY_CORRECT" not in captured_environment
    assert captured_environment["HOME"] == str(runtime_dir)


def _completed(returncode: int, diagnostic: str, block: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["rs274"],
        returncode,
        stdout="",
        stderr=f"diagnostic phase\n{diagnostic}\n{block}\n",
    )


def test_signed_integer_boundary_uses_differential_locale_agnostic_diagnostics() -> None:
    oracle._verify_signed_integer_boundary_results(
        integer_max_result=_completed(0, "", ""),
        t_overflow_result=_completed(1, "mot T hors plage", "T2147483648 M6"),
        h_overflow_result=_completed(1, "mot H hors plage", "G43 H2147483648"),
        t_missing_result=_completed(1, "outil demandé absent", "T999 M6"),
        h_missing_result=_completed(1, "outil demandé absent", "G43 H999"),
    )


@pytest.mark.parametrize("word", ("T", "H"))
def test_signed_integer_boundary_rejects_missing_lookup_false_positive(word: str) -> None:
    t_overflow = _completed(1, "mot T hors plage", "T2147483648 M6")
    h_overflow = _completed(1, "mot H hors plage", "G43 H2147483648")
    if word == "T":
        t_overflow = _completed(1, "outil demandé absent", "T999 M6")
    else:
        h_overflow = _completed(1, "outil demandé absent", "G43 H999")

    with pytest.raises(SystemExit, match="indistinguishable"):
        oracle._verify_signed_integer_boundary_results(
            integer_max_result=_completed(0, "", ""),
            t_overflow_result=t_overflow,
            h_overflow_result=h_overflow,
            t_missing_result=_completed(1, "outil demandé absent", "T999 M6"),
            h_missing_result=_completed(1, "outil demandé absent", "G43 H999"),
        )
