# Planera långa och höga hyllor som separata moduler

I möbelstudion kan **Dela upp möbeln i stommoduler** beräkna ett uttryckligt rutnät av separata
stommar. Varje stomme får egna sidstycken, topp, botten och hyllplan från samma
geometrimotor som övriga hyllsystem. Förslaget hjälper dig att granska alternativ
till alltför långa odelade delar.

## Användning

1. Öppna hyllans arbetsfil och ange dess mått, material och last.
2. Välj antal kolumner och rader. Ange mellanrummet uttryckligen, även när det är
   0 mm. Ange antal hyllplan för varje rad, räknat nedifrån, och antal avdelare
   i varje modul.
3. Beräkna förslaget. Kontrollera rutnätet, modulernas mått, vikt, bärighet och
   vilka delar som ryms inom valda råformat. Att delar ryms enskilt är inte en
   verifierad nesting eller maskinberedning.
4. Läs monteringskraven. Ladda ned hela planen för sammanhang och spårbarhet.
   Enskilda arbetsfiler kan öppnas i möbelstudion som nya projekt för fortsatt
   granskning. Källprojektet ändras inte när planen beräknas eller laddas ned.

Ett exempel med stomvolymen **4340 × 2540 × 280 mm**, fem kolumner, två rader
utan mellanrum och två hyllplan i varje rad ger tio stomutkast om
**868 × 1270 × 280 mm**. Detta illustrerar rutnätsberäkningen. Kundfilens
stomvolym, last, material och installation behöver fortfarande fastställas;
exemplet är ingen vald eller frisläppt kundkonstruktion.

## Vad beräkningen bevarar

- Hela källarbetsfilen, materialvalen och installationskraven följer med planen.
  Varje moduls arbetsfil behåller källans installationskrav. Okänd frigång eller
  olöst list får inte försvinna genom uppdelningen.
- Summan av modulmått och mellanrum stämmer exakt med stommåtten i heltals-µm.
  Eventuella enhetsrester fördelas deterministiskt.
- Angiven last per hyllrad fördelas efter modulbredd och avrundas uppåt. N/m
  bevaras när den lastmodellen används. Ett förslag som sänker den sammanlagda
  källasten genom mellanrum nekas; lasten ändras inte i bakgrunden.
- Planens identitet binder källans arbetsfil, design, beroenden och rutnätsval.
  Varje modul har egen designidentitet och omräknad geometri, vikt och kontroll.

Egna fackproportioner och hyllhöjder omvandlas inte automatiskt till jämna
moduler. Staplad sockel behöver också en uttrycklig konstruktion. Sådana fall
nekas när en korrekt överföring saknas. Förslaget får inte minska det sammanlagda
antalet hyllrader. Sökrymden begränsas till 16 moduler och 512 beräknade delar.

## Vad som återstår för tillverkning

Förband mellan moduler, deras hålbilder, distanser, väggförankring och
monteringsordning är ännu inte modellerade eller kvalificerade av rutnätet.
Vid flera rader måste även övre modulers egenvikt och nyttiga last föras genom
verifierade upplag till underlaget. En modul som klarar sin hyllscreening har
inte därmed visats bära modulerna ovanför.

Delutkastens bevarade kundinstallation kommer normalt att avvika från deras
nya mått. Detta är ett synligt produktionshinder: den nya modulgruppen behöver
sin egen granskade installations- och monteringslösning. Ingen produktion,
fogkvalificering, verkstadsacceptans eller CAM-frisläppning ärvs av planen.

## Gränssnitt

`POST /v1/furniture/module-plan` tar `workspace` och `grid`. Rutnätet kräver
`columns`, `rows`, `gap_um` och `shelf_count_per_row` (nedifrån upp). Valfritt
`divider_count_per_module` är 0 om det inte anges. Anropet är autentiserat och
skriver inga projekt eller revisioner. Alla moduler måste kunna beräknas för
att ett exporterbart förslag ska returneras; delresultat exporteras inte.

En giltig plan kan ha `can_export_drafts: true` samtidigt som konkreta
monterings- och installationskrav återstår. `production_qualified` och
`physical_cutting_authorized` är alltid `false`.
