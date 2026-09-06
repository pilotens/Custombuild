#!/usr/bin/env python3
"""Verify generated production G-code against the real LinuxCNC interpreter."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

EXPECTED_MESSAGES = (
    ("POSITIVE_CURRENT_T7_H17_USES_T17_NOT_P17_ABS_Z", "35.000000"),
    ("NEGATIVE_CURRENT_T8_H18_USES_T18_NOT_P18_ABS_Z", "-45.000000"),
)
POCKET_KEYED_MESSAGES = (
    ("POSITIVE_CURRENT_T7_H17_USES_T17_NOT_P17_ABS_Z", "135.000000"),
    ("NEGATIVE_CURRENT_T8_H18_USES_T18_NOT_P18_ABS_Z", "-145.000000"),
)
CURRENT_TOOL_MESSAGES = (
    ("POSITIVE_CURRENT_T7_H17_USES_T17_NOT_P17_ABS_Z", "106.000000"),
    ("NEGATIVE_CURRENT_T8_H18_USES_T18_NOT_P18_ABS_Z", "217.000000"),
)
EXPECTED_TOOL_TABLE_ROWS = {
    7: (27, "111.000"),
    17: (37, "40.000"),
    27: (17, "140.000"),
    8: (28, "222.000"),
    18: (38, "-40.000"),
    28: (18, "-140.000"),
}
EXPECTED_M6_TOOL_WORDS = (7, 8)
EXPECTED_G43_H_WORDS = (17, 18)
LINUXCNC_WORD_INT_MAX = 2_147_483_647
LINUXCNC_WORD_INT_OVERFLOW = 2_147_483_648
MISSING_TOOL_OR_OFFSET_NUMBER = 999
EXPECTED_PRODUCTION_PARAMETERS = {
    5210: 0.0,
    5211: 0.0,
    5212: 0.0,
    5213: 0.0,
    5214: 0.0,
    5215: 0.0,
    5216: 0.0,
    5217: 0.0,
    5218: 0.0,
    5219: 0.0,
    5220: 1.0,
    5221: 0.0,
    5222: 0.0,
    5223: -60.0,
    5224: 0.0,
    5225: 0.0,
    5226: 0.0,
    5227: 0.0,
    5228: 0.0,
    5229: 0.0,
    5230: 0.0,
}
MESSAGE_PATTERN = re.compile(r'MESSAGE\("(?P<label>[A-Z0-9_]+)=(?P<value>-?[0-9]+\.[0-9]+)"\)')
TOOL_TABLE_ROW_PATTERN = re.compile(
    r"^T(?P<tool>[0-9]+)\s+P(?P<pocket>[0-9]+).*\sZ(?P<z>[+-]?[0-9]+\.[0-9]+)\s"
)
G43_H_PATTERN = re.compile(r"^G43\s+H(?P<h>[0-9]+)\s*$", re.MULTILINE)
M6_TOOL_PATTERN = re.compile(r"^T(?P<tool>[0-9]+)\s+M6\s*$", re.MULTILINE)
DIAGNOSTIC_INTEGER_PATTERN = re.compile(r"[0-9]+")


def _verify_lookup_fixture(fixture: Path, tool_table: Path) -> None:
    rows: dict[int, tuple[int, str]] = {}
    for line in tool_table.read_text(encoding="ascii").splitlines():
        match = TOOL_TABLE_ROW_PATTERN.match(line)
        if match is None:
            raise SystemExit(f"invalid LinuxCNC oracle tool-table row: {line!r}")
        tool = int(match.group("tool"))
        if tool in rows:
            raise SystemExit(f"duplicate LinuxCNC oracle tool number T{tool}")
        rows[tool] = (int(match.group("pocket")), match.group("z"))
    if rows != EXPECTED_TOOL_TABLE_ROWS:
        raise SystemExit(
            "LinuxCNC G43 lookup fixture drift: expected deliberately crossed "
            f"T/P rows {EXPECTED_TOOL_TABLE_ROWS!r}, observed {rows!r}"
        )

    fixture_text = fixture.read_text(encoding="ascii")
    m6_tools = tuple(int(match.group("tool")) for match in M6_TOOL_PATTERN.finditer(fixture_text))
    h_words = tuple(int(match.group("h")) for match in G43_H_PATTERN.finditer(fixture_text))
    if m6_tools != EXPECTED_M6_TOOL_WORDS or h_words != EXPECTED_G43_H_WORDS:
        raise SystemExit(
            "LinuxCNC G43 lookup fixture drift: "
            f"expected M6 tools {EXPECTED_M6_TOOL_WORDS!r} and H words "
            f"{EXPECTED_G43_H_WORDS!r}, observed {m6_tools!r} and {h_words!r}"
        )


def _verify_production_parameter_fixture(parameter_file: Path) -> None:
    observed: dict[int, float] = {}
    previous_number = 0
    for line in parameter_file.read_text(encoding="ascii").splitlines():
        fields = line.split()
        if len(fields) != 2:
            raise SystemExit(f"invalid LinuxCNC production parameter row: {line!r}")
        number = int(fields[0])
        if number <= previous_number:
            raise SystemExit("LinuxCNC production parameter fixture is not strictly ordered")
        previous_number = number
        observed[number] = float(fields[1])
    if observed != EXPECTED_PRODUCTION_PARAMETERS:
        raise SystemExit(
            "LinuxCNC production parameter fixture drift: expected active G54 with "
            "X=0, Y=0, Z=-60 mm and cleared G92/other axes; "
            f"observed {observed!r}"
        )


def _replace_fixture_word(content: bytes, old: bytes, new: bytes) -> bytes:
    if content.count(old) != 1:
        raise SystemExit(f"LinuxCNC production fixture drift: expected exactly one {old!r} word")
    return content.replace(old, new)


def _diagnostic_signature(result: subprocess.CompletedProcess[str]) -> str:
    """Return a locale-agnostic signature for one interpreter failure.

    LinuxCNC echoes the failing block after its diagnostic.  Replacing integer
    values prevents a different T/H spelling from making two otherwise
    identical missing-lookup failures appear distinct.  The oracle compares
    diagnostics produced by the same pinned interpreter; it does not depend on
    any particular English message or wording from another LinuxCNC version.
    """

    normalized = " ".join(result.stderr.split())
    return DIAGNOSTIC_INTEGER_PATTERN.sub("#", normalized)


def _verify_signed_integer_boundary_results(
    *,
    integer_max_result: subprocess.CompletedProcess[str],
    t_overflow_result: subprocess.CompletedProcess[str],
    h_overflow_result: subprocess.CompletedProcess[str],
    t_missing_result: subprocess.CompletedProcess[str],
    h_missing_result: subprocess.CompletedProcess[str],
) -> None:
    """Prove overflow rejection is distinct from an ordinary missing lookup."""

    failures = (t_overflow_result, h_overflow_result, t_missing_result, h_missing_result)
    if integer_max_result.returncode != 0 or any(result.returncode == 0 for result in failures):
        raise SystemExit(
            "LinuxCNC T/H signed-int boundary oracle mismatch: expected "
            f"{LINUXCNC_WORD_INT_MAX} to pass, {LINUXCNC_WORD_INT_OVERFLOW} "
            "to fail independently for T and H, and the in-range missing-lookup "
            "controls to fail\n"
            f"int-max stderr:\n{integer_max_result.stderr}\n"
            f"T-overflow stderr:\n{t_overflow_result.stderr}\n"
            f"H-overflow stderr:\n{h_overflow_result.stderr}\n"
            f"T-missing-control stderr:\n{t_missing_result.stderr}\n"
            f"H-missing-control stderr:\n{h_missing_result.stderr}"
        )

    signatures = {
        "T overflow": _diagnostic_signature(t_overflow_result),
        "H overflow": _diagnostic_signature(h_overflow_result),
        "T missing lookup": _diagnostic_signature(t_missing_result),
        "H missing lookup": _diagnostic_signature(h_missing_result),
    }
    if any(not signature for signature in signatures.values()):
        raise SystemExit(
            "LinuxCNC T/H signed-int boundary oracle produced an empty failure diagnostic"
        )
    if (
        signatures["T overflow"] == signatures["T missing lookup"]
        or signatures["H overflow"] == signatures["H missing lookup"]
    ):
        raise SystemExit(
            "LinuxCNC T/H signed-int overflow diagnostic is indistinguishable from "
            "an ordinary in-range missing tool-table lookup"
        )


def _run_rs274(
    rs274: str,
    *options: str,
    input_file: Path,
    runtime_dir: Path,
    parameter_file: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    runtime_dir.mkdir(mode=0o700)
    runtime_parameter_file = runtime_dir / "rs274ngc.var"
    if parameter_file is not None:
        shutil.copyfile(parameter_file, runtime_parameter_file)
    runtime_path = str(runtime_dir)
    environment = os.environ.copy()
    environment.pop("POSIXLY_CORRECT", None)
    environment.update(
        {
            "HOME": runtime_path,
            "LANG": "C",
            "LC_ALL": "C",
            "TMPDIR": runtime_path,
            "TZ": "UTC",
            "XDG_CACHE_HOME": str(runtime_dir / "xdg-cache"),
            "XDG_CONFIG_HOME": str(runtime_dir / "xdg-config"),
            "XDG_DATA_HOME": str(runtime_dir / "xdg-data"),
            "XDG_STATE_HOME": str(runtime_dir / "xdg-state"),
        }
    )
    return subprocess.run(  # noqa: S603 - executable resolved from trusted CI PATH
        [rs274, "-v", str(runtime_parameter_file), *options, str(input_file)],
        cwd=runtime_dir,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    fixture = repo / "tests" / "linuxcnc-oracle" / "g43-sign.ngc"
    production_fixture = repo / "tests" / "linuxcnc-oracle" / "production-output.ngc"
    production_parameters = repo / "tests" / "linuxcnc-oracle" / "production-g54.var"
    metric_ini = repo / "tests" / "linuxcnc-oracle" / "metric.ini"
    tool_table = repo / "tests" / "linuxcnc-oracle" / "tool.tbl"
    _verify_lookup_fixture(fixture, tool_table)
    _verify_production_parameter_fixture(production_parameters)
    rs274 = shutil.which("rs274")
    if rs274 is None:
        raise SystemExit("LinuxCNC rs274 is not installed")
    with tempfile.TemporaryDirectory(prefix="custombuild-linuxcnc-oracle-") as workdir:
        workdir_path = Path(workdir)
        completed = _run_rs274(
            rs274,
            "-i",
            str(metric_ini),
            "-t",
            str(tool_table),
            "-g",
            input_file=fixture,
            runtime_dir=workdir_path / "g43-sign",
        )
        production_result = _run_rs274(
            rs274,
            "-i",
            str(metric_ini),
            "-t",
            str(tool_table),
            "-g",
            input_file=production_fixture,
            runtime_dir=workdir_path / "production-output",
            parameter_file=production_parameters,
        )
        production_content = production_fixture.read_bytes()
        integer_max_content = _replace_fixture_word(
            production_content,
            b"T7 M6",
            f"T{LINUXCNC_WORD_INT_MAX} M6".encode("ascii"),
        )
        integer_max_content = _replace_fixture_word(
            integer_max_content,
            b"G43 H17",
            f"G43 H{LINUXCNC_WORD_INT_MAX}".encode("ascii"),
        )
        integer_max_fixture = workdir_path / "production-int-max.ngc"
        integer_max_fixture.write_bytes(integer_max_content)
        integer_max_tool_table = workdir_path / "production-int-max.tbl"
        integer_max_tool_table.write_bytes(
            tool_table.read_bytes()
            + f"T{LINUXCNC_WORD_INT_MAX} P47 D6.000 Z40.000 ; INT MAX TARGET\n".encode("ascii")
        )
        integer_max_result = _run_rs274(
            rs274,
            "-i",
            str(metric_ini),
            "-t",
            str(integer_max_tool_table),
            "-g",
            input_file=integer_max_fixture,
            runtime_dir=workdir_path / "production-int-max",
            parameter_file=production_parameters,
        )
        t_overflow_fixture = workdir_path / "production-t-int-overflow.ngc"
        t_overflow_fixture.write_bytes(
            _replace_fixture_word(
                production_content,
                b"T7 M6",
                f"T{LINUXCNC_WORD_INT_OVERFLOW} M6".encode("ascii"),
            )
        )
        t_overflow_result = _run_rs274(
            rs274,
            "-i",
            str(metric_ini),
            "-t",
            str(tool_table),
            "-g",
            input_file=t_overflow_fixture,
            runtime_dir=workdir_path / "production-t-int-overflow",
            parameter_file=production_parameters,
        )
        h_overflow_fixture = workdir_path / "production-h-int-overflow.ngc"
        h_overflow_fixture.write_bytes(
            _replace_fixture_word(
                production_content,
                b"G43 H17",
                f"G43 H{LINUXCNC_WORD_INT_OVERFLOW}".encode("ascii"),
            )
        )
        h_overflow_result = _run_rs274(
            rs274,
            "-i",
            str(metric_ini),
            "-t",
            str(tool_table),
            "-g",
            input_file=h_overflow_fixture,
            runtime_dir=workdir_path / "production-h-int-overflow",
            parameter_file=production_parameters,
        )
        t_missing_fixture = workdir_path / "production-t-missing-control.ngc"
        t_missing_fixture.write_bytes(
            _replace_fixture_word(
                production_content,
                b"T7 M6",
                f"T{MISSING_TOOL_OR_OFFSET_NUMBER} M6".encode("ascii"),
            )
        )
        t_missing_result = _run_rs274(
            rs274,
            "-i",
            str(metric_ini),
            "-t",
            str(tool_table),
            "-g",
            input_file=t_missing_fixture,
            runtime_dir=workdir_path / "production-t-missing-control",
            parameter_file=production_parameters,
        )
        h_missing_fixture = workdir_path / "production-h-missing-control.ngc"
        h_missing_fixture.write_bytes(
            _replace_fixture_word(
                production_content,
                b"G43 H17",
                f"G43 H{MISSING_TOOL_OR_OFFSET_NUMBER}".encode("ascii"),
            )
        )
        h_missing_result = _run_rs274(
            rs274,
            "-i",
            str(metric_ini),
            "-t",
            str(tool_table),
            "-g",
            input_file=h_missing_fixture,
            runtime_dir=workdir_path / "production-h-missing-control",
            parameter_file=production_parameters,
        )
        safe_line = workdir_path / "line-limit-safe.ngc"
        safe_line.write_bytes(("%\n(" + ("A" * 250) + ")\nM2\n%\n").encode("ascii"))
        safe_result = _run_rs274(
            rs274,
            "-i",
            str(metric_ini),
            "-t",
            str(tool_table),
            "-g",
            input_file=safe_line,
            runtime_dir=workdir_path / "line-limit-safe",
        )
        overlong_line = workdir_path / "line-limit-overflow.ngc"
        overlong_line.write_bytes(("%\n(" + ("A" * 251) + ")\nM2\n%\n").encode("ascii"))
        overlong_result = _run_rs274(
            rs274,
            "-i",
            str(metric_ini),
            "-t",
            str(tool_table),
            "-g",
            input_file=overlong_line,
            runtime_dir=workdir_path / "line-limit-overflow",
        )
        if completed.returncode != 0:
            raise SystemExit(
                "LinuxCNC rs274 oracle failed:\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )
        if production_result.returncode != 0:
            raise SystemExit(
                "LinuxCNC rejected the byte-pinned production postprocessor output:\n"
                f"stdout:\n{production_result.stdout}\n"
                f"stderr:\n{production_result.stderr}"
            )
        _verify_signed_integer_boundary_results(
            integer_max_result=integer_max_result,
            t_overflow_result=t_overflow_result,
            h_overflow_result=h_overflow_result,
            t_missing_result=t_missing_result,
            h_missing_result=h_missing_result,
        )
        if safe_result.returncode != 0 or overlong_result.returncode == 0:
            raise SystemExit(
                "LinuxCNC file line-limit oracle mismatch: expected 252 ASCII bytes "
                "before LF to pass and 253 to fail\n"
                f"safe stderr:\n{safe_result.stderr}\n"
                f"overflow stderr:\n{overlong_result.stderr}"
            )
    observed = tuple(
        (match.group("label"), match.group("value"))
        for match in MESSAGE_PATTERN.finditer(completed.stdout)
    )
    if observed != EXPECTED_MESSAGES:
        if observed == POCKET_KEYED_MESSAGES:
            lookup_diagnosis = " interpreter resolved Hn by pocket Pn instead of tool number Tn;"
        elif observed == CURRENT_TOOL_MESSAGES:
            lookup_diagnosis = " interpreter ignored Hn and used the loaded tool's offset;"
        else:
            lookup_diagnosis = ""
        raise SystemExit(
            f"LinuxCNC G43 tool-number lookup oracle mismatch:{lookup_diagnosis} "
            f"expected {EXPECTED_MESSAGES!r}, observed {observed!r}\n"
            f"raw output:\n{completed.stdout}"
        )
    print(
        "LinuxCNC oracle PASS: byte-pinned production output accepted; "
        "T/H signed-int boundary = 2147483647; "
        "G43 Hn uses tool-table Tn, not pocket Pn; signed M = P + G5x + H; "
        "file line limit = 252 bytes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
