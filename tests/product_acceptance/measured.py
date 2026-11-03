"""Independent frame work orders expressed in global millimetre datums."""

from copy import deepcopy
from math import prod

from .support import response_json


def member(key, size, origin):
    return {
        "id": key,
        "code": key.split("-")[0],
        "name": key,
        "material": "Provvirke",
        "size_um": size,
        "blank_lbt_um": sorted(size, reverse=True),
        "origin_um": origin,
        "operations": [],
    }


def feature(key, shape, face, z, depth):
    return {
        "id": key,
        "shape": shape,
        "face": face,
        "axes": "xz",
        "u_um": 18_000,
        "v_um": z,
        "width_um": 14_002,
        "height_um": 15_998,
        "depth_um": depth,
        "source": "Arbetsprovets måttsatta nominella fog.",
    }


def frame_file(frames=1, levels=1, two_ended=True):
    parts, joints = [], []
    for i in range(frames):
        x = i * 390_001
        front = member(f"post-{i}-front", [36_000, 42_000, levels * 158_007], [x, 0, 0])
        rear = member(f"post-{i}-rear", [36_000, 42_000, levels * 158_007], [x, 232_004, 0])
        parts.append(front)
        if two_ended:
            parts.append(rear)
        for j in range(levels):
            z = 31_005 + j * 152_001
            rail = member(f"rail-{i}-{j}", [36_000, 230_004, 30_000], [x, 22_000, z])
            parts.append(rail)
            endpoints = (
                ((front, "front", "back"), (rear, "back", "front"))
                if two_ended
                else ((front, "front", "back"),)
            )
            for post, tip, face in endpoints:
                rail["operations"].append(feature(tip, "tenon", tip, 15_000, 20_000))
                pocket = feature(f"pocket-{j}", "pocket", face, z + 15_000, 21_125)
                post["operations"].append(pocket)
                joints.append(
                    {
                        "id": f"j-{i}-{j}-{tip}",
                        "kind": "blind_tenon",
                        "a": {"part": rail["id"], "operation": tip},
                        "b": {"part": post["id"], "operation": pocket["id"]},
                        "source": "Arbetsprovets fogtabell.",
                    }
                )
    return {
        "schema_version": "custombuild.measured-assembly.v1",
        "title": "Arbetsprov för ram",
        "source": "Testbeställning",
        "parts": parts,
        "joints": joints,
        "open_items": ["Fysisk provning återstår."],
    }


def crossing_screw_work():
    cleat = member("cleat", [60_000, 24_000, 40_000], [0, 0, 0])
    side = member("side", [8_000, 24_000, 40_000], [-8_000, 0, 0])
    back = member("back-panel", [60_000, 6_000, 40_000], [0, 24_000, 0])
    joints = []
    for i, (board, face, axes, u, v, thickness, penetration, diameter) in enumerate(
        (
            (side, "left", "yz", 16_000, 20_000, 8_000, 20_000, 4_000),
            (back, "back", "xz", 12_000, 21_000, 6_000, 18_000, 3_000),
        )
    ):
        sku = f"test-screw-{i}"
        through = dict(
            id="pass",
            shape="drill",
            face=face,
            axes=axes,
            u_um=u,
            v_um=v,
            width_um=diameter + 500,
            height_um=diameter + 500,
            depth_um=thickness,
            through=True,
            source="Oberoende koordinater",
            article=sku,
        )
        hole = {
            **through,
            "id": f"pilot-{i}",
            "width_um": diameter - 500,
            "height_um": diameter - 500,
            "depth_um": penetration + 1000,
            "through": False,
        }
        board["operations"].append(through)
        cleat["operations"].append(hole)
        joints.append(
            dict(
                id=f"cross-{i}",
                kind="screw",
                a={"part": board["id"], "operation": "pass"},
                b={"part": "cleat", "operation": hole["id"]},
                article=sku,
                diameter_um=diameter,
                length_um=thickness + penetration,
                source="Arbetsprov",
            )
        )
    return {
        "schema_version": "custombuild.measured-assembly.v1",
        "title": "Korsande skruvar",
        "source": "Två skruvar i samma tvärstycke",
        "parts": [cleat, side, back],
        "joints": joints,
    }


def exercise_measured(client, case, evidence):
    query = frame_file(**case.order.inputs)
    url = "/v1/measured-assemblies/review"
    if case.level == "impossible":
        query["parts"][0]["operations"][0]["depth_um"] = 20_000
        report = response_json(client.post(url, json=query))
        failed = [j for j in report["joints"] if j["state"] == "mismatch"]
        evidence.equal("rejection", [j["depth_reserve_um"] for j in failed], [0])
        assert failed[0]["reason"].startswith("Tappen bottnar")
    else:
        report = response_json(client.post(url, json=query))
        expected_joints = len(query["joints"])
        evidence.equal(
            "paired_measured_joints",
            sum(j["state"] == "aligned" for j in report["joints"]),
            expected_joints,
        )
        assert all(
            j["depth_reserve_um"] == 1125 and j["offset_um"] == [0, 0, 0] for j in report["joints"]
        )
        assert not report["issues"] and not report["overlaps"] and not report["overlaps_truncated"]
        if case.level in {"hard", "very-hard"}:
            broken = deepcopy(query)
            broken["parts"][0]["operations"][0]["v_um"] += 1
            wrong = response_json(client.post(url, json=broken))
            assert sum(j["state"] == "mismatch" for j in wrong["joints"]) == 1
            assert wrong["overlaps"] and wrong["assembly_sha256"] != report["assembly_sha256"]
            evidence.note("offset_rejected_um", 1)
            assert response_json(client.post(url, json=query)) == report
        if case.level == "very-hard":
            evidence.equal("inspected_parts", len(report["cells"]), 54)
            for p in report["assembly"]["parts"]:
                volume = sum(
                    prod(cell[i + 3] - cell[i] for i in range(3))
                    for cell in report["cells"][p["id"]]
                )
                assert 0 < volume < prod(p["size_um"])
            # Independent rear opening on the same API, shortened by one micrometre.
            rear = member("back", [600_000, 6_000, 800_000], [0, 300_000, 0])
            with_back = {
                **query,
                "parts": [*query["parts"], rear],
                "backing": {
                    "part_ids": ["back"],
                    "openings": [
                        {
                            "id": "bay",
                            "label": "Rygg",
                            "target": "covered",
                            "low_um": [0, 300_000, 0],
                            "high_um": [600_000, 306_000, 800_000],
                        }
                    ],
                },
            }
            complete = response_json(client.post(url, json=with_back))["back_coverage"][0]
            assert complete["state"] == "met" and complete["covered_area_um2"] == 480_000_000_000
            rear["size_um"][2] -= 1
            rear["blank_lbt_um"][0] -= 1
            short = response_json(client.post(url, json=with_back))["back_coverage"][0]
            assert short["state"] == "mismatch"
            evidence.equal(
                "rear_gap_um2", short["required_area_um2"] - short["covered_area_um2"], 600_000
            )
            rear["size_um"][2] += 1
            rear["blank_lbt_um"][0] += 1
            assert response_json(client.post(url, json=with_back))["back_coverage"][0] == complete
            hardware = crossing_screw_work()
            crossed = response_json(client.post(url, json=hardware))
            evidence.equal("crossing_fastener_pairs", len(crossed["fastener_interferences"]), 1)
            assert all(j["state"] == "mismatch" for j in crossed["joints"])
            # Move both the entrance and its receiver. Moving just the entrance cannot pass.
            hardware["parts"][2]["operations"][0]["v_um"] += 6_000
            single = response_json(client.post(url, json=hardware))
            assert single["joints"][1]["state"] == "mismatch"
            hardware["parts"][0]["operations"][1]["v_um"] += 6_000
            separate = response_json(client.post(url, json=hardware))
            assert not separate["fastener_interferences"] and all(
                j["state"] == "aligned" for j in separate["joints"]
            )
    assert report["physical_cutting_authorized"] is False
