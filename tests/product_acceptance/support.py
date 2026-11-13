"""Test-side measurements. Expectations come from the work order and shop dimensions."""

import json
from decimal import Decimal
from uuid import uuid4


def response_json(response, status=200):
    assert response.status_code == status, response.text[:2_000]
    return response.json()


class Evidence:
    def __init__(self, record):
        self.record = record

    def note(self, label, value):
        self.record(label, json.dumps(value, ensure_ascii=False, sort_keys=True))

    def equal(self, label, actual, expected):
        self.note(label, {"expected": expected, "actual": actual})
        assert actual == expected, f"{label}: expected {expected!r}, observed {actual!r}"


def save_furniture(client, case_id, workspace):
    saved = response_json(
        client.post(
            "/v1/furniture/projects",
            json={
                "name": f"{case_id}-{uuid4()}",
                "workspace": workspace,
            },
        ),
        201,
    )
    path = f"/v1/furniture/projects/{saved['project']['id']}"
    draft = saved["draft"]
    assert response_json(client.get(f"{path}/draft")) == draft
    return path, draft


def micrometres(mm):
    return Decimal(str(mm)) * 1000


def verify_cut(row, part):
    # The broad face fixes the shop U/V order. A swapped axis can change grain
    # or put a machining operation on the wrong edge even if the rectangle fits.
    normal = {"left": "x", "right": "x", "front": "y", "back": "y", "top": "z", "bottom": "z"}[
        part["a_side"]
    ]
    keys = {"x": "width_um", "y": "depth_um", "z": "height_um"}
    u, v = [axis for axis in "xyz" if axis != normal]
    for csv_prefix, size in (("finished", "finished_size"), ("raw", "raw_size")):
        assert micrometres(row[csv_prefix + "_width_mm"]) == part[size][keys[u]]
        assert micrometres(row[csv_prefix + "_height_mm"]) == part[size][keys[v]]
    assert micrometres(row["thickness_mm"]) == part["finished_size"][keys[normal]]
    assert row["material_id"] == part["material_id"]
    assert row["material_version"] == part["material_version"]
    if "grain_axis_uv" in row:
        assert row["grain_axis_uv"] == {u: "U", v: "V", "none": "NONE"}[part["grain_direction"]]


def check_carcass(
    parts,
    *,
    width,
    height,
    depth,
    rows,
    bays,
    backs,
    plinth,
    evidence,
    name="semantic_key",
    size="finished_size",
    loc="placement",
    role="role",
    ratios=(),
    centres=(),
    floor=18_000,
):
    by_name = {p[name]: p for p in parts}
    t = 18_000
    shelves = [p for p in parts if p[role] == "shelf"]
    evidence.equal(
        "shelves_backs_plinth",
        [
            len(shelves),
            sum(p[role] == "back" for p in parts),
            sum(p[role] == "plinth" for p in parts),
        ],
        [rows * bays, backs, int(bool(plinth))],
    )
    left, right = by_name["left-side"], by_name["right-side"]
    evidence.equal(
        "carcass_um",
        [
            right[loc]["x_um"] + right[size]["width_um"],
            left[size]["depth_um"],
            left[size]["height_um"],
        ],
        [width, depth, height],
    )
    assert left[loc]["x_um"] == left[loc]["z_um"] == 0
    if not rows:
        return
    # Fixed dado shelves extend 6 mm into each support. Divide the measured
    # clear opening by the customer's ratio, permitting a 1 µm rounding remainder.
    available = width - t * (bays + 1)
    weights = ratios or [1] * bays
    for bay in range(bays):
        shelf = by_name[f"shelf-r0-b{bay}"]
        measured = shelf[size]["width_um"] - 12_000
        assert abs(measured - available * weights[bay] / sum(weights)) <= 1
    if centres:
        heights = [by_name[f"shelf-r{i}-b0"][loc]["z_um"] + t // 2 for i in range(rows)]
        target = [floor + round((height - t - floor) * fraction) for fraction in centres]
        evidence.note("shelf_centres_um", {"expected": target, "actual": heights})
        assert all(abs(a - b) <= 1 for a, b in zip(heights, target, strict=True))
