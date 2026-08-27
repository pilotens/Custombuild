from pathlib import Path

import pytest

from scripts.check_environment_isolation import (
    DeploymentSurface,
    IsolationError,
    assert_isolated,
    surface_from_config,
)


def compose_config(name: str, web_port: int, volume: str) -> dict:
    return {
        "name": name,
        "services": {"web": {"ports": [{"published": str(web_port), "target": 3000}]}},
        "volumes": {"postgres-data": {"name": volume}},
    }


def test_surface_extracts_resolved_identity() -> None:
    surface = surface_from_config(
        Path("compose.yml"), compose_config("custombuild-prod", 3000, "custombuild_prod_db")
    )

    assert surface.project == "custombuild-prod"
    assert surface.published_ports == frozenset({3000})
    assert surface.volume_names == frozenset({"custombuild_prod_db"})


@pytest.mark.parametrize("name", ["", "custombuild"])
def test_generic_project_identity_is_rejected(name: str) -> None:
    with pytest.raises(IsolationError, match="role-specific"):
        surface_from_config(Path("compose.yml"), compose_config(name, 3000, "db"))


def test_peer_collisions_are_reported_individually() -> None:
    prod = DeploymentSurface(
        Path("prod"), "custombuild-prod", frozenset({3000}), frozenset({"prod-db"})
    )

    with pytest.raises(IsolationError, match="project name"):
        assert_isolated(
            prod,
            DeploymentSurface(
                Path("test"), "custombuild-prod", frozenset({3100}), frozenset({"test-db"})
            ),
        )
    with pytest.raises(IsolationError, match="port collision"):
        assert_isolated(
            prod,
            DeploymentSurface(
                Path("test"), "custombuild-test", frozenset({3000}), frozenset({"test-db"})
            ),
        )
    with pytest.raises(IsolationError, match="volume collision"):
        assert_isolated(
            prod,
            DeploymentSurface(
                Path("test"), "custombuild-test", frozenset({3100}), frozenset({"prod-db"})
            ),
        )


def test_distinct_surfaces_pass() -> None:
    assert_isolated(
        DeploymentSurface(
            Path("prod"), "custombuild-prod", frozenset({3000, 8000}), frozenset({"prod-db"})
        ),
        DeploymentSurface(
            Path("test"), "custombuild-test", frozenset({3100, 8100}), frozenset({"test-db"})
        ),
    )
