from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "statement",
    (
        "import custombuild_cam; import custombuild_postprocessors",
        "import custombuild_postprocessors; import custombuild_cam",
    ),
)
def test_cam_and_postprocessors_import_cleanly_in_either_order(statement: str) -> None:
    repository = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        str(repository / source)
        for source in (
            "packages/manufacturing/src",
            "cam/src",
            "postprocessors/src",
        )
    )

    completed = subprocess.run(  # noqa: S603 -- fixed interpreter, isolated import regression
        [sys.executable, "-c", statement],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
