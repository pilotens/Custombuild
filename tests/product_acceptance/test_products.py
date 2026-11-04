from collections import Counter

from custombuild_domain.furniture import DiningTableIntent

from .catalog import FAMILIES, LEVELS, STUDIO, all_cases
from .furniture import exercise_furniture
from .measured import exercise_measured
from .modules import exercise_modules
from .studio import exercise_studio
from .support import Evidence, response_json
from .trestle import exercise_trestle


def test_catalog_covers_every_available_product_and_level(customer):
    families = response_json(customer.get("/v1/furniture/catalog"))["families"]
    templates = response_json(customer.get("/v1/capabilities/templates"))["templates"]
    assert {f["id"] for f in families} == set(FAMILIES.values())
    assert {t["template_id"] for t in templates} == set(STUDIO)
    shapes = DiningTableIntent.model_json_schema()["properties"]["top_shape"]["enum"]
    assert {"dining-" + shape for shape in shapes} == {
        p for p, family in FAMILIES.items() if family == "dining_table"
    }
    cases = all_cases()
    products = {
        *FAMILIES,
        "walnut-modules",
        "measured-frame",
        "reference-trestle",
        *(f"studio-{key}" for key in STUDIO),
    }
    expected = {(product, level): 1 for product in products for level in LEVELS}
    assert Counter((c.product, c.level) for c in cases) == expected
    assert len({c.order.brief for c in cases}) == len(cases)
    assert all(c.order.expected for c in cases)


def test_product_work_order(case, customer, monkeypatch, tmp_path, record_property):
    record_property("product", case.product)
    record_property("level", case.level)
    record_property("order", case.order.brief)
    record_property("acceptance", case.order.expected)
    e = Evidence(record_property)
    e.note("inputs", case.order.inputs)
    if case.product == "reference-trestle":
        exercise_trestle(customer, case, e, tmp_path)
    elif case.product == "measured-frame":
        exercise_measured(customer, case, e)
    elif case.product == "walnut-modules":
        exercise_modules(customer, case, e)
    elif case.product.startswith("studio-"):
        exercise_studio(customer, case, e)
    else:
        exercise_furniture(customer, case, e, monkeypatch, tmp_path)
