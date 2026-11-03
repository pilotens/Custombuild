"""Turn the acceptance run's own JUnit evidence into a Swedish workshop protocol."""

import argparse
import json
from pathlib import Path
from xml.etree import ElementTree

LEVELS = {
    "easy": "Lätt",
    "medium": "Medel",
    "hard": "Svårt",
    "very-hard": "Väldigt svårt",
    "impossible": "Omöjligt",
}
MEASURES = {
    "inspected_parts": "Granskade enskilda delar",
    "verified_face_drawings": "Sidritningar jämförda med export",
    "parts_joints": "Delar/förband",
    "front_span_um": "Fri sarg (µm)",
    "top_width_depth_elevation_um": "Skiva B/D/nivå (µm)",
    "circular_outline": "Cirkelskiva",
    "deflection_screen": "Nedböjning",
    "rear_edge_um": "Stoldjup (µm)",
    "back_angle_mdeg": "Ryggvinkel (tusendels grader)",
    "parts_groups": "Delar/lådor",
    "runner_travel_um": "Skenans slag (µm)",
    "overload": "Lastgräns",
    "carcass_um": "Stomme B/D/H (µm)",
    "shelves_backs_plinth": "Hyllplan/bakstycken/socklar",
    "parts_at_export_limit": "Delar vid export",
    "cabinet_fronts": "Skåpfronter",
    "shelf_centres_um": "Hyllcentrum över golv (µm)",
    "module_widths_um": "Modulbredder (µm)",
    "module_heights_um": "Modulhöjder (µm)",
    "stock_fit": "Alla råämnen ryms",
    "cut_rows": "Kaprader",
    "rear_gap_um2": "Öppnad yta vid 1 µm för kort rygg (µm²)",
    "paired_measured_joints": "Passande ritningsförband",
    "offset_rejected_um": "Upptäckt förskjutning (µm)",
    "STEP_mm": "Uppmätt STEP B/D/H (mm)",
    "STEP_solids": "STEP-solider",
    "stale_write": "Äldre sparförsök",
    "blocked_plan": "Spärrad modulplan",
    "rejection": "Avslag",
    "restriction": "Mallens begränsning",
}


def cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def display(value):
    if isinstance(value, list):
        return " / ".join(display(item) for item in value)
    if isinstance(value, bool):
        return "ja" if value else "nej"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".").replace(".", ",")
    if isinstance(value, dict):
        return str(value.get("message", value))
    return str(value)


def protocol(source, source_id):
    # This input is our own locally generated pytest report.
    root = ElementTree.parse(source)  # noqa: S314
    cases = root.findall(".//testcase")
    suite = root.find(".//testsuite")
    started = suite.get("timestamp", "") if suite is not None else ""
    rows = []
    counts = {"Godkänt": 0, "Rätt spärr": 0, "Underkänt": 0, "Ej kört": 0}
    complete = bool(cases)
    for case in cases:
        properties = {
            p.get("name"): p.get("value", "") for p in case.findall("properties/property")
        }
        errors = case.findall("failure") + case.findall("error")
        skipped = case.find("skipped") is not None
        if errors or skipped:
            complete = False
        if "product" not in properties:
            if errors or skipped:
                rows.append(f"**Katalog- eller körfel** {cell(case.get('name'))}")
            continue
        level = properties["level"]
        status = (
            "Underkänt"
            if errors
            else "Ej kört"
            if skipped
            else "Rätt spärr"
            if level == "impossible"
            else "Godkänt"
        )
        counts[status] += 1
        readings = []
        for key, label in MEASURES.items():
            if key not in properties:
                continue
            value = json.loads(properties[key])
            actual = value.get("actual", value) if isinstance(value, dict) else value
            readings.append(f"{label} {display(actual)}")
        readings.extend(e.get("message", "Fel") for e in errors)
        rows.append(
            "| "
            + " | ".join(
                cell(v)
                for v in (
                    properties["product"],
                    LEVELS[level],
                    properties["order"],
                    properties["acceptance"],
                    status,
                    " · ".join(readings),
                )
            )
            + " |"
        )
    total = sum(counts.values())
    summary = " · ".join(f"{n} {label.lower()}" for label, n in counts.items())
    lines = [
        "# Arbetsprotokoll för möbelprodukterna",
        "",
        f"**{total} arbetsprov** · {summary}",
        "",
        f"Körning {started} · Kodversion `{source_id}`",
        "",
        "Måtten kommer från produktens API och de genererade filerna. Rätt spärr betyder att "
        "appen stoppade den angivna beställningen av rätt skäl. Godkänt gäller "
        "programutfallet. Fysisk provning och tillverkningsfrigivning hanteras separat.",
        "",
        "| Produkt-ID | Nivå | Beställning | Godkännandekrav | Utfall | Observerat |",
        "|---|---|---|---|---|---|",
        *rows,
    ]
    return "\n".join(lines) + "\n", complete and total > 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--source", default="okänd")
    args = parser.parse_args()
    report, passed = protocol(args.junit, args.source)
    args.report.write_text(report, encoding="utf-8")
    print(f"Arbetsprotokoll: {args.report}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
