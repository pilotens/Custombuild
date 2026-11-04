"""Version-controlled work orders and independent workshop acceptance criteria."""

from dataclasses import dataclass, field
from typing import Any

LEVELS = {
    "easy": "lätt",
    "medium": "medel",
    "hard": "svårt",
    "very-hard": "väldigt-svårt",
    "impossible": "omöjligt",
}


@dataclass(frozen=True)
class Order:
    brief: str
    expected: str
    inputs: dict[str, Any] = field(default_factory=dict)
    # For invalid geometry the API must reject this specific cause.
    rejection: str = ""


@dataclass(frozen=True)
class Case:
    product: str
    level: str
    order: Order

    @property
    def id(self):
        return f"{self.product}-{self.level}"


# All dimensions in the furniture API are integer micrometres.
FURNITURE = {
    "panel-table": (
        Order("Standardbord med två gavlar", "Fem delar, två gavlar och två sargar mellan dem."),
        Order(
            "Flytta in gavlarna",
            "Båda sarglängderna minskar med dubbla indraget.",
            {"width_um": 1_350_000, "end_inset_um": 125_000},
        ),
        Order(
            "En mikrometer över minsta spann",
            "Båda sargarna är exakt 250 001 µm.",
            {"width_um": 400_001, "end_inset_um": 57_000},
        ),
        Order(
            "Asymmetriska mått till verkstaden",
            "STEP, kaplista och DXF beskriver fem delar.",
            {
                "width_um": 1_107_123,
                "depth_um": 613_239,
                "height_um": 731_019,
                "end_inset_um": 90_007,
                "stretcher_height_um": 113_601,
            },
        ),
        Order(
            "Två mikrometer för kort sarg",
            "Avvisa både förhandsvisning och sparande.",
            {"width_um": 400_000, "end_inset_um": 57_001},
            "between the end supports",
        ),
    ),
    "dining-rectangle": (
        Order("Matbord för fyra ben", "Nio delar och tjugo parade skruvförband."),
        Order(
            "Bredare ben och större indrag",
            "Sargerna möter benens insidor.",
            {"leg_section_um": 68_000, "end_inset_um": 130_000},
        ),
        Order(
            "Lång skiva med hög last",
            "Nedböjningen överstiger regelns tillåtna värde.",
            {"width_um": 2_450_000, "depth_um": 850_000, "top_load_n": 1_300},
        ),
        Order(
            "Export med brutna millimetermått",
            "STEP, kaplista och båda borrdelarna stämmer.",
            {
                "width_um": 1_845_127,
                "depth_um": 884_311,
                "height_um": 737_019,
                "leg_section_um": 57_003,
                "end_inset_um": 93_007,
            },
        ),
        Order(
            "Sarg en mikrometer för kort",
            "Avvisa ramen med mindre än 100 mm fri sarg.",
            {"width_um": 389_999},
            "ryms inte inom de valda måtten",
        ),
    ),
    "dining-round": (
        Order("Runt matbord Ø 1 200 mm", "Cirkelskiva med fyra ben inom dess periferi."),
        Order(
            "Runt bord med större diameter",
            "Båda axlarna mäter 1 480 mm.",
            {"width_um": 1_480_000, "depth_um": 1_480_000, "end_inset_um": 110_000},
        ),
        Order(
            "Kompakt cirkel och grova ben",
            "Alla benhörn ryms och överhänget kräver granskning.",
            {
                "width_um": 760_000,
                "depth_um": 760_000,
                "leg_section_um": 71_000,
                "end_inset_um": 50_000,
            },
        ),
        Order(
            "Udda diameter till STEP och DXF",
            "Konturen i DXF har radien 633,5005 mm.",
            {"width_um": 1_267_001, "depth_um": 1_267_001, "leg_section_um": 53_007},
        ),
        Order(
            "Diametrarna skiljer en mikrometer",
            "Avvisa den ovala beställningen.",
            {"width_um": 1_200_001, "depth_um": 1_200_000},
            "samma diameter",
        ),
    ),
    "chair": (
        Order("Matstol med lutande rygg", "Tio delar, 24 förband och fyra försänkningar i sitsen."),
        Order(
            "Kort sits med rak rygg",
            "Sittdjupet ändras självständigt från stolens yttermått.",
            {"seat_depth_um": 395_000, "back_angle_mdeg": 0, "seat_height_um": 420_000},
        ),
        Order(
            "Hög stol med kraftigare lutning och last",
            "Ryggvinkel, tippmoment och provlast.",
            {
                "height_um": 929_000,
                "depth_um": 568_000,
                "seat_depth_um": 431_000,
                "leg_section_um": 65_000,
                "back_angle_mdeg": 14_000,
                "seat_load_n": 1_340,
            },
        ),
        Order(
            "Sned rygg och udda sittdjup i export",
            "STEP-djup 562,037 mm, sits 414,521 mm.",
            {
                "width_um": 488_505,
                "depth_um": 562_037,
                "height_um": 929_219,
                "seat_depth_um": 414_521,
                "leg_section_um": 65_001,
                "back_angle_mdeg": 14_333,
            },
        ),
        Order(
            "Sits en mikrometer in i bakbenen",
            "Avvisa sittdjupet med oförändrat utkast.",
            {"seat_depth_um": 450_001},
            "Kontrollera sittdjupet",
        ),
    ),
    "dresser": (
        Order(
            "En låda på 350 mm skena",
            "En rörlig sexdelad låda med 355 mm slag.",
            {"drawer_count": 1, "width_um": 520_000, "runner": "9114273"},
        ),
        Order(
            "Tre fronter med restmått",
            "Alla fria fronter och mellanrum summerar till innerhöjd.",
            {
                "width_um": 550_000,
                "height_um": 900_001,
                "drawer_count": 3,
                "gap_basis": "clear_reveal",
                "row_front_heights_um": [190_000],
            },
        ),
        Order(
            "Sex lådor, två kolumner och hög last",
            "Skenhål och överlast per låda redovisas.",
            {
                "width_um": 1_020_000,
                "height_um": 1_150_000,
                "drawer_count": 6,
                "drawer_columns": 2,
                "gap_basis": "clear_reveal",
                "row_front_heights_um": [230_000, 301_000],
                "drawer_load_n": 400,
            },
        ),
        Order(
            "Två kolumner med olika fronthöjder",
            "31 delar, fyrsidiga skenhål och rätt kaplista.",
            {
                "width_um": 1_001_123,
                "height_um": 912_037,
                "drawer_count": 4,
                "drawer_columns": 2,
                "row_front_heights_um": [258_117],
                "gap_basis": "clear_reveal",
                "front_gap_um": 3_125,
            },
        ),
        Order(
            "450 mm skena i 450 mm yttermått",
            "Skenan får inte plats bakom fronten.",
            {"depth_um": 450_000},
            "profile does not fit",
        ),
    ),
    "shelving": (
        Order(
            "En hylla i ett fack",
            "Sex delar inklusive det infällda bakstycket.",
            {"shelf_count": 1, "divider_count": 0},
        ),
        Order(
            "Två fack med sockel",
            "Fyra hyllplan, två bakfält och en golvsockel.",
            {"shelf_count": 2, "divider_count": 1, "plinth_height_um": 85_000},
        ),
        Order(
            "Precis vid exportens delgräns",
            "128 delar godtas i exportkön.",
            {"width_um": 2_600_000, "height_um": 3_000_000, "shelf_count": 23, "divider_count": 4},
        ),
        Order(
            "Ojämna fack och valda hyllhöjder",
            "Geometri, mått och delidentiteter genom export.",
            {
                "width_um": 981_123,
                "height_um": 1_382_037,
                "depth_um": 324_137,
                "shelf_count": 2,
                "divider_count": 1,
                "plinth_height_um": 83_007,
                "bay_width_ratios_ppm": [350_000, 650_000],
                "shelf_height_ratios_ppm": [290_000, 730_000],
            },
        ),
        Order(
            "En del över exportens delgräns",
            "129 delar kan granskas men exporten stoppas.",
            {
                "width_um": 2_600_000,
                "height_um": 3_000_000,
                "shelf_count": 23,
                "divider_count": 4,
                "plinth_height_um": 85_000,
            },
        ),
    ),
}

FAMILIES = {
    "panel-table": "table",
    "dining-rectangle": "dining_table",
    "dining-round": "dining_table",
    "chair": "chair",
    "dresser": "chest_of_drawers",
    "shelving": "shelving",
}

WALNUT = (
    Order(
        "Två fullhöga valnötsmoduler",
        "2×1, 20 kaprader och för korta råämnen.",
        {"columns": 2, "rows": 1, "gap_um": 0, "shelf_count_per_row": [4]},
    ),
    Order(
        "Dela även på höjden",
        "2×2, 30 delar. Valnöten ryms men bakfältens fiberriktning stoppar råformatet.",
        {
            "columns": 2,
            "rows": 2,
            "gap_um": 0,
            "shelf_count_per_row": [2, 2],
            "plinth_placement": "floor_row_only",
        },
    ),
    Order(
        "Tre kolumner med 3,001 mm fog",
        "Sex moduler, 69 kaprader och exakt fördelad rest.",
        {
            "columns": 3,
            "rows": 2,
            "gap_um": 3_001,
            "shelf_count_per_row": [2, 2],
            "divider_count_per_module": 1,
            "plinth_placement": "floor_row_only",
        },
    ),
    Order(
        "Kundvägg med fyra självständiga utkast",
        "Fyra moduler, 62 kaprader, läge och fiberaxel.",
        {
            "columns": 2,
            "rows": 2,
            "gap_um": 0,
            "shelf_count_per_row": [2, 2],
            "divider_count_per_module": 2,
            "plinth_placement": "floor_row_only",
        },
    ),
    Order(
        "Stapla socklar utan placeringsbeslut",
        "Ingen modul eller kaplista får skapas.",
        {"columns": 2, "rows": 2, "gap_um": 0, "shelf_count_per_row": [2, 2]},
    ),
)


@dataclass(frozen=True)
class StudioProduct:
    name: str
    width: int
    height: int
    depth: int
    base: int = 0
    plinth: bool = True
    back: bool = True


# Customer dimensions in mm. The templates have different design and release boundaries.
STUDIO = {
    "shelving": StudioProduct("Hyllsystem", 1_350, 1_890, 315),
    "wall-library": StudioProduct("Väggbibliotek", 2_400, 2_400, 320, base=680),
    "sideboard": StudioProduct("Skänk", 2_000, 1_100, 360, base=540),
    "room-divider": StudioProduct("Rumsavdelare", 1_850, 1_970, 420, back=False),
    "hanging-shelf": StudioProduct("Hänghylla", 915, 1_130, 275, plinth=False),
    "cupboard": StudioProduct("Förvaringsvägg", 1_800, 2_200, 400, base=1_000),
}


def studio_orders(template, product):
    baseline = {"divider_count": 0, "shelf_count": 1}
    custom = {
        "divider_count": 1,
        "shelf_count": 2,
        "bay_width_ratios": [0.35, 0.65],
        "shelf_height_ratios": [0.26, 0.71],
    }
    return (
        Order(
            f"{product.name} med ett fack", "Räkna hyllplan, bakstycken, sockel och skåp.", baseline
        ),
        Order(
            f"{product.name} med två fack",
            "Två lika breda fack och två rader.",
            {"divider_count": 1, "shelf_count": 2},
        ),
        Order(
            f"{product.name} med fördelningen 35/65",
            "Mät båda fackens bredd och hyllornas läge.",
            custom,
        ),
        Order(
            f"Ändra en sparad {product.name.lower()} med 7 mm",
            "Rätt omsparad bredd, gammal skrivning stoppad och rätt mall kvar efter återöppning.",
            custom,
        ),
        Order(
            f"Omöjlig beställning för {product.name.lower()}",
            "Avvisa för små hyllöppningar utan att ändra utkastet."
            if template == "shelving"
            else "Stoppa produktionsrevisionen med mallens identitet och konkreta begränsning.",
            baseline,
        ),
    )


MEASURED = (
    Order(
        "En måttsatt blind tapp",
        "Centrum sammanfaller och 1,125 mm djupreserv återstår.",
        {"two_ended": False},
    ),
    Order("En regel i två motstående stolpar", "Båda ändarna når rätt skuldra.", {"levels": 1}),
    Order(
        "Tre rader med en avsiktligt flyttad bearbetning",
        "En mikrometers förskjutning upptäcks och en korrigerad omkörning är identisk.",
        {"levels": 3},
    ),
    Order(
        "Sex ramar med sju tvärstycken vardera",
        "54 bearbetade delar, 84 matchande fogar och inga materialöverlapp.",
        {"frames": 6, "levels": 7},
    ),
    Order(
        "Tapp som bottnar i sitt blindhål",
        "Fogkontrollen stoppar den aktuella tappen med noll djupreserv.",
        {"two_ended": False},
    ),
)


def all_cases():
    orders = {
        **FURNITURE,
        "walnut-modules": WALNUT,
        "measured-frame": MEASURED,
        "reference-trestle": (
            Order(
                "Bockbord med individuella referensdelar",
                "13 delar, nio formprofiler och uttryckligt ofullständiga förband.",
            ),
            Order(
                "Lutande ben och rundad skiva i CAD",
                "Analytiska volymer och 78 kontrollerade delpar utan materialöverlapp.",
            ),
            Order(
                "Flytta en tvärbalk in i benet",
                "Exakt CAD hittar kollisionen; rättad position kontrolleras igen.",
            ),
            Order(
                "13 profiler genom STEP och råformat",
                "Varje STEP återimporteras; ett råämne 1 µm för kort avvisas och rättas.",
            ),
            Order(
                "Omöjlig hörnradie i bockbordets skiva",
                "En radie 1 µm över halva bredden avvisas; originalet går att läsa igen.",
            ),
        ),
        "constructed-trestle": (
            Order(
                "Bockbord B1 med automatiska förband",
                "13 delar, 29 kopplade förband och 79 bearbetningar skapas.",
            ),
            Order(
                "Stor skiva med högre centrisk provlast",
                "Skivtjockleken ökas automatiskt; CAD verifierar varje förband.",
            ),
            Order(
                "Felmonterat ben i bearbetad konstruktion",
                "CAD stoppar kollisionen; rättat arbetsprov granskas igen.",
            ),
            Order(
                "Borrningar genom PDF, DXF och bearbetad STEP",
                "78 DXF-vyer, 13 återimporterade STEP-delar och fem bevarade pilotborrningar.",
            ),
            Order(
                "Hundratusen newton och bord utan bredd",
                "Omöjliga indata stoppas utan tyst klippning; giltigt arbetsprov kan återskapas.",
            ),
        ),
        **{f"studio-{key}": studio_orders(key, product) for key, product in STUDIO.items()},
    }
    return [
        Case(product, level, order)
        for product, items in orders.items()
        for level, order in zip(LEVELS, items, strict=True)
    ]
