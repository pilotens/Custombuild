import pytest
from app.main import app
from fastapi.testclient import TestClient

from .catalog import LEVELS, all_cases


def pytest_addoption(parser):
    group = parser.getgroup("product acceptance")
    group.addoption(
        "--product",
        choices=sorted({case.product for case in all_cases()}),
        help="Run one product profile.",
    )
    group.addoption(
        "--product-level",
        choices=[*LEVELS, *LEVELS.values()],
        help="Run one difficulty level (English ID or Swedish name).",
    )


def pytest_generate_tests(metafunc):
    if "case" not in metafunc.fixturenames:
        return
    product = metafunc.config.getoption("product")
    level = metafunc.config.getoption("product_level")
    selected = [
        c
        for c in all_cases()
        if (not product or c.product == product)
        and (not level or level in (c.level, LEVELS[c.level]))
    ]
    if not selected:
        raise pytest.UsageError("Selected product and level have no acceptance cases")
    metafunc.parametrize("case", selected, ids=lambda c: c.id)


@pytest.fixture
def customer():
    with TestClient(app, headers={"Authorization": "Bearer demo-nordic-owner"}) as client:
        yield client
