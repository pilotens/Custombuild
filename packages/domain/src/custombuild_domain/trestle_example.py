"""Dimensioned geometry study, with explicitly unverified hidden construction."""

from typing import Any

from .measured_assembly import MeasuredAssembly

SOURCE = (
    "Geometriskt arbetsprov, antagna mått. "
    "Ingen måttsatt originalritning eller verifierade förband."
)


def trestle_example() -> MeasuredAssembly:
    parts: list[dict[str, Any]] = []

    def part(
        key: str,
        name: str,
        size: tuple[int, int, int],
        origin: tuple[int, int, int],
        blank: tuple[int, int, int],
        *,
        profile: dict[str, Any] | None = None,
        note: str = "",
    ) -> None:
        parts.append(
            dict(
                id=key,
                code=key,
                name=name,
                size_um=[v * 1000 for v in size],
                origin_um=[v * 1000 for v in origin],
                blank_lbt_um=[v * 1000 for v in blank],
                material="Träslag, kvalitet och fuktkvot ej bekräftade",
                note=note + " Råmått är provets ämnesbehov, inte en verifierad kapbeställning.",
                profile=profile,
            )
        )

    def polygon(axes: str, points: list[tuple[int, int]]) -> dict[str, Any]:
        return dict(
            kind="convex_prism",
            axes=axes,
            points_um=[[v * 1000 for v in point] for point in points],
            source=SOURCE,
        )

    part(
        "TOP",
        "Bordsskiva",
        (2200, 950, 32),
        (0, 0, 718),
        (2200, 950, 32),
        profile=dict(kind="rounded_rectangle", axes="xy", radius_um=60000, source=SOURCE),
        note="Prov: hörnradie 60 mm och skivtjocklek 32 mm. Kantens rundning är inte måttsatt.",
    )
    for end, x in (("L", 250), ("R", 1880)):
        part(
            f"LEG-{end}-F",
            "Främre lutande ben",
            (70, 150, 640),
            (x, 105, 0),
            (655, 70, 70),
            profile=polygon("yz", [(0, 0), (70, 0), (150, 640), (80, 640)]),
            note="Benets långriktning lutar 80 mm över 640 mm höjd. Ändarna är vågräta.",
        )
        part(
            f"LEG-{end}-B",
            "Bakre lutande ben",
            (70, 150, 640),
            (x, 695, 0),
            (655, 70, 70),
            profile=polygon("yz", [(80, 0), (150, 0), (70, 640), (0, 640)]),
            note="Spegelvänd benprofil. Inga dolda hål eller tappar är antagna.",
        )
        part(
            f"CROSS-{end}-TOP",
            "Övre tvärbalk",
            (70, 670, 78),
            (x, 140, 640),
            (670, 78, 70),
            note="Separat bockbalk under skivan; kontakten är inte ett färdigt förband.",
        )
        part(
            f"CROSS-{end}-LOW",
            "Nedre tvärbalk",
            (70, 560, 80),
            (x, 195, 160),
            (560, 80, 70),
            profile=polygon("yz", [(0, 0), (560, 0), (550, 80), (10, 80)]),
            note="Båda ändarna följer benens lutning. Fogtyp och tolerans återstår.",
        )
    part(
        "TIE-LOW",
        "Lång nedre slå",
        (1560, 50, 80),
        (320, 450, 160),
        (1560, 80, 50),
        note="Slutar mellan bockarnas nedre tvärbalkar.",
    )
    part(
        "TIE-TOP",
        "Föreslagen övre längsbalk",
        (1560, 50, 78),
        (320, 450, 640),
        (1560, 78, 50),
        note="Konstruktionsantagande för strävornas övre anslutning; dold i referensbilden.",
    )
    part(
        "BRACE-L",
        "Vänster snedsträva",
        (430, 40, 400),
        (370, 455, 240),
        (585, 61, 40),
        profile=polygon("xz", [(0, 0), (80, 0), (430, 400), (350, 400)]),
        note="Vågräta ändsnitt. Långriktning 350/400; sammanfogning saknas.",
    )
    part(
        "BRACE-R",
        "Höger snedsträva",
        (430, 40, 400),
        (1400, 455, 240),
        (585, 61, 40),
        profile=polygon("xz", [(350, 0), (430, 0), (80, 400), (0, 400)]),
        note="Spegelvänd sträva. Ingen skruv eller tapp har gissats från fotot.",
    )
    return MeasuredAssembly.model_validate(
        dict(
            schema_version="custombuild.measured-assembly.v1",
            title="Bockbord · geometriskt arbetsprov 2200 × 950 × 750 mm",
            source=SOURCE,
            parts=parts,
            open_items=[
                "Alla mått är provmått, inte mått som har kunnat avläsas ur fotografiet.",
                "13 föreslagna delar. Den övre längsbalkens existens och dolda "
                "anslutningar måste bekräftas.",
                "Samtliga förband och borrningar är ospecificerade. Kontakt innebär "
                "inte att delarna hålls ihop.",
                "Skivans infästning behöver projekteras för det valda materialets rörelser.",
                "Bärförmåga, sidostabilitet, tippning, foghållfasthet och "
                "användningslast är ej verifierade.",
                "Råformat, träkvalitet, fiberriktning och tillverkningsmarginaler "
                "behöver bekräftas per del.",
                "Rum, passage och monteringsordning är inte uppmätta. Ingen "
                "beställning eller tillverkning är frisläppt.",
            ],
        )
    )
