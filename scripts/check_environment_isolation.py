"""Fail closed when local production and test Compose stacks can collide.

The application is intentionally run from two independent working copies.  A
shared Compose project name silently reuses containers, networks and volumes,
which can make a test exercise the wrong source tree.  This command resolves
the effective Compose configurations and verifies their externally visible
identity before either stack is accepted.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class IsolationError(RuntimeError):
    """Raised when two deployment surfaces are not isolated."""


def docker_executable() -> str:
    executable = shutil.which("docker")
    if not executable:
        raise IsolationError("Docker CLI is not available")
    return executable


@dataclass(frozen=True)
class DeploymentSurface:
    source: Path
    project: str
    published_ports: frozenset[int]
    volume_names: frozenset[str]


def surface_from_config(source: Path, config: dict[str, Any]) -> DeploymentSurface:
    project = str(config.get("name", "")).strip()
    if not project or project == "custombuild":
        raise IsolationError(
            f"{source}: use a role-specific Compose project name, not {project or 'an empty name'}"
        )

    ports: set[int] = set()
    for service in config.get("services", {}).values():
        for binding in service.get("ports", []) or []:
            published = binding.get("published") if isinstance(binding, dict) else None
            if published is not None:
                ports.add(int(published))

    volumes = {
        str(definition.get("name"))
        for definition in config.get("volumes", {}).values()
        if isinstance(definition, dict) and definition.get("name")
    }
    if not ports:
        raise IsolationError(f"{source}: no published ports were resolved")
    if not volumes:
        raise IsolationError(f"{source}: no named persistent volumes were resolved")

    return DeploymentSurface(source.resolve(), project, frozenset(ports), frozenset(volumes))


def load_surface(source: Path) -> DeploymentSurface:
    source = source.resolve()
    if not source.is_file():
        raise IsolationError(f"Compose file does not exist: {source}")

    environment = os.environ.copy()
    for key in (
        "COMPOSE_PROJECT_NAME",
        "API_BIND_PORT",
        "WEB_BIND_PORT",
        "S3_BIND_PORT",
    ):
        environment.pop(key, None)

    process = subprocess.run(  # noqa: S603 - argv is fixed and shell execution is disabled.
        [
            docker_executable(),
            "compose",
            "--file",
            str(source),
            "--project-directory",
            str(source.parent),
            "config",
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if process.returncode:
        detail = process.stderr.strip() or process.stdout.strip() or "unknown Compose error"
        raise IsolationError(f"Could not resolve {source}: {detail}")
    return surface_from_config(source, json.loads(process.stdout))


def assert_isolated(primary: DeploymentSurface, peer: DeploymentSurface) -> None:
    if primary.project == peer.project:
        raise IsolationError(f"Compose project name collision: {primary.project}")

    shared_ports = primary.published_ports & peer.published_ports
    if shared_ports:
        ports = ", ".join(map(str, sorted(shared_ports)))
        raise IsolationError(f"Published port collision: {ports}")

    shared_volumes = primary.volume_names & peer.volume_names
    if shared_volumes:
        raise IsolationError(f"Persistent volume collision: {', '.join(sorted(shared_volumes))}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("compose", type=Path, help="Primary Compose file")
    parser.add_argument("--peer", type=Path, help="Optional second working copy Compose file")
    args = parser.parse_args()

    primary = load_surface(args.compose)
    surfaces = [primary]
    if args.peer:
        peer = load_surface(args.peer)
        assert_isolated(primary, peer)
        surfaces.append(peer)

    for surface in surfaces:
        ports = ", ".join(map(str, sorted(surface.published_ports)))
        print(f"{surface.project}: ports {ports}; source {surface.source}")
    print("Environment isolation: PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except IsolationError as exc:
        print(f"Environment isolation: FAIL - {exc}")
        raise SystemExit(1) from exc
