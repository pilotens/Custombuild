"""Collect bounded, read-only Compose failure diagnostics without container configuration."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

SERVICES = ("scheduler", "maintenance-worker", "worker", "storage-reaper-worker")
STATE_FORMAT = (
    '{"State":{"Status":{{json .State.Status}},'
    '"OOMKilled":{{json .State.OOMKilled}},"ExitCode":{{json .State.ExitCode}},'
    '"Health":{"Log":{{if .State.Health}}{{json .State.Health.Log}}{{else}}null{{end}}}},'
    '"RestartCount":{{json .RestartCount}}}'
)
SCHEDULE_STAT = """
import json
import pathlib
import time

now = time.time()
entries = []
for path in sorted(pathlib.Path("/tmp").glob("celerybeat-schedule*")):
    try:
        metadata = path.stat()
        entries.append({
            "path": str(path),
            "size_bytes": metadata.st_size,
            "mtime_unix": metadata.st_mtime,
            "age_seconds": round(now - metadata.st_mtime, 3),
        })
    except OSError as error:
        entries.append({"path": str(path), "stat_error": str(error)})
print(json.dumps(entries))
"""


def capture(command: list[str], destination: Path) -> str:
    """Keep each diagnostic independent so a stopped service cannot hide the others."""
    stdout = ""
    try:
        result = subprocess.run(  # noqa: S603 - argv-only and assembled internally.
            command, check=False, capture_output=True, text=True, timeout=20,
        )
        stdout = result.stdout
        content = result.stdout + result.stderr
        if result.returncode:
            content += f"\nDiagnostic command exited {result.returncode}.\n"
            stdout = ""
    except subprocess.TimeoutExpired:
        content = "Diagnostic timed out after 20 seconds.\n"
    except OSError as error:
        content = f"Diagnostic unavailable: {error.strerror or type(error).__name__}\n"
    destination.write_text(content, encoding="utf-8")
    print(f"::group::{destination.name}", flush=True)
    print(content, end="" if content.endswith("\n") else "\n", flush=True)
    print("::endgroup::", flush=True)
    return stdout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_directory
    output.mkdir(parents=True, exist_ok=True)
    compose = ["docker", "compose", "--file", "compose.yml"]
    for service in SERVICES:
        capture(
            [*compose, "logs", "--no-color", "--timestamps", "--tail=1000", service],
            output / f"{service}.log",
        )
        containers = capture(
            [*compose, "ps", "--all", "--quiet", service],
            output / f"{service}-containers.txt",
        )
        for index, container in enumerate(containers.split()):
            # Never inspect Config, Env, or the unfiltered container object.
            capture(
                ["docker", "container", "inspect", "--format", STATE_FORMAT, container],
                output / f"{service}-{index}-state.json",
            )
            if service == "scheduler":
                # Stat the schedule files only; never open or deserialize their contents.
                capture(
                    ["docker", "exec", container, "python", "-c", SCHEDULE_STAT],
                    output / f"{service}-{index}-schedule-stat.json",
                )


if __name__ == "__main__":
    main()
