import pytest
from app.main import app
from fastapi.testclient import TestClient

from .catalog import LEVELS, all_cases


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
