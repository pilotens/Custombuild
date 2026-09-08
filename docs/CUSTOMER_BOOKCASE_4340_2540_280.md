# Kundbokhylla: 4340 × 2540 × 280 mm

## Fastställda uppgifter

Kundens uppgift 2026-09-08 är **434 cm längd inklusive list, 254 cm höjd och
28 cm djup**. Äldre mått 4350 × 2480 × 397 mm gäller inte detta arbetsunderlag.
Samma produkt ska användas för andra kundmått, hyllor, bord och byråer.
Verkstad och materialbatch är inte valda. Konstruktionen ska vara limfri.

[Arbetsfilen](../examples/furniture/bookcase-4340x2540x280.json) kan öppnas med
**Läs arbetsfil (JSON)** på `/furniture` och sparas som ett nytt projekt. Kundens
exakta uppgifter ligger i `design.installation`. Alla sex reserverade utrymmen
är okända (`null`); inga listbredder eller montagespel har hittats på.

Filens CAD-stomme är tills vidare en illustration av hela den uppmätta volymen.
De fyra hyllraderna, en avdelare, 200 N per hyllplan och plywoodprofilerna är
**arbetsytans standardvärden, inte en godkänd kundkonstruktion eller materialorder**.
Listen ingår inte som en tillverkad del. Den här filen är inte ett tillstånd
att skära material eller beställa det dyra tillverkningsprovet.

## Implementerad måttkedja

1. Kundens yttermått sparas separat från de stomdelar som styr CAD och kaplista.
2. Utrymme på vänster/höger, ovanför/under och framför/bakom anges explicit.
   Tomt är okänt. Värdet 0 betyder att inget utrymme reserveras på den sidan.
3. **Räkna om stommen från kundmåtten** drar av utrymmena med heltalsnoggrannhet
   i µm. En ändring av kundmåttet ersätter inte automatiskt stommen.
4. Servern kontrollerar att stommåtten och kundmåtten stämmer överens. En
   ofullständig måttkedja kan sparas och CAD-granskas men inte beredas för produktion.
5. Markeringen **Längden inkluderar list** innebär fortsatt blockering tills
   listens geometri och mekaniska infästning faktiskt är implementerade.
   Att fylla reserverat utrymme eller skriva 0 kvalificerar ingen listkonstruktion.
6. Kundmått ingår i designens identitet och revisionshistorik. Ett profilbyte får
   inte ändra dem. Den frysta produktionskällan och paketläsaren upprepar kontrollen.

Hyllfackens breddproportioner och hyllcentrens höjdproportioner kan ändras i
**Fack, hyllhöjder och rygg**. De når produktionsmotorn utan att ersättas av
standardindelning. Fackproportioner summerar till exakt 100 %. Hyllcentrum
anges relativt den fria hyllzonen. Sockel, rygg och fasta/flyttbara hyllor har
separata konstruktionskontroller; ett synligt val innebär inte fysisk kvalificering.

## Vad verkstaden kan granska nu

Granskningspaketet innehåller verkliga STEP/GLB-modeller, DXF/SVG för varje dels
båda sidor, delritningar, BOM och kaplista. Verkstadsunderlaget binder samma
designhash och visar exakta råformat, fiberriktning och materialgrupper.
Delritningar med olösta kundmått/listuppgifter är uttryckligen preliminära.

Odelade delar i den här storleken ryms inte på vanliga 2440 mm långa råskivor.
Även höjden 2540 mm kräver kontroll av råformat och maskinområde. Andra skivformat
och olika fiberriktningar för stomme och rygg måste stämmas av per material i
den verkliga beredningen. En modulindelning är en ändring av konstruktionen och
kräver modellerade fogar, montering och bärighetskontroll; appen lägger inte in
dolda skarvar för att få nesting att gå igenom.

## Kvar innan just denna möbel kan frisläppas

| Saknad uppgift eller funktion | Vad som behöver fastställas |
| --- | --- |
| Godkänd konstruktion | Skiss/ritning med fackindelning, hyllhöjder, eventuella underskåp och rygg. Dagens standardlayout är inte kundens beslut. |
| List och montage | Vad listmåttet avser, listens placering, tvärsnitt och längder samt mekanisk infästning. Det kräver nya modellerade listdelar och förband, inte bara en inställning. |
| Långa delar | Faktiska tillgängliga råformat och maskinområde eller en uttryckligen vald, dimensionerad modulkonstruktion. |
| Material | Verkligt synligt/dolt material, batch, tjocklek och underbyggda egenskaper. Katalogens MDF/björkplywood är screeningprofiler; ek är inte implementerad som kvalificerad ersättning. |
| Last och förankring | Böckernas dimensioner/last, belastningsfördelning, vägg/golv och verifierade infästningar. |
| Förband och CAM | Kvalificerad limfri retention, maskin, verktyg, uppspänning, tvåsidig registrering och verifierad postprocessor. |
| Fysiskt prov | Ett uppmätt fog-/beslagsprov och överenskomna acceptansmått före komplett tillverkningsprov. |

Bord och byråer använder samma kundmått, profilhantering och CAD-/formatunderlag.
Deras nuvarande beslag är layoutprofiler. Familjernas faktiska hålbilder,
kompletta förband och monteringsförlopp behöver fortfarande implementeras mot
ett dokumenterat beslagssystem. De kan inte frisläppas genom hyllkompilatorn.

## Verifiering i koden

- Kundmått överlever API-sparning, återöppning och revisionshistorik.
- Okända mått, avvikande stomme och omodellerad list kan inte skapa en
  produktionskälla, inte heller genom att skicka en direkt källpayload.
- Oregelbundna hyllor/fack och rygg-/sockel-/hyllbärarval överförs utan
  ändring av delar, bearbetningsfeatures, fogar eller monteringsgraf.
- Materialgruppernas råmått kommer från samma anpassade delar som CAD-exporten.
  Kantmarginal kan underkänna ett format som bara passar nominellt.
- Verklig CAD-export av detta stora kundfall och samtliga tre familjer verifieras.
  Den nya webbläsarregressionen importerar kundfilen, sparar, laddar om och
  kontrollerar kontrollsummorna och kundmåtten i den riktiga CAD-workerns ZIP.

Godkända programvarutester är inte bevis på fysisk passning, bärighet eller
att denna kundmöbel kan frisläppas. Status ska bedömas mot punkterna ovan.
