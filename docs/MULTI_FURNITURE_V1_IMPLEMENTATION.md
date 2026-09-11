# Möbeltyper och utbytbara profiler

Arbetsytan `/furniture` har en gemensam versionshanterad modell för hyllsystem,
gavelbord och byråer. Den nås från startsidan. Befintliga projekt fortsätter i sin
ursprungliga arbetsyta; deras utkast skrivs inte över av den nya modellen.

## Vad som fungerar

| Del | Implementerat beteende |
| --- | --- |
| Design | Heltalsmått i µm, stabila delidentiteter, yttermått och familjespecifika konstruktionsparametrar. |
| Hyllsystem | Befintlig konstruktionsmotor, fogar och regelscreening återanvänds. Olikstora fack, individuella hyllhöjder, fasta/flyttbara hyllor, ryggval och sockel följer med till beredningen. |
| Kundmått | Separata uppmätta yttermått och sex explicita reserverade utrymmen. Okänt är inte noll. Stommen räknas om efter användarens val. Ofullständig måttkedja eller omodellerad list blockerar beredningen även på servern och i paketläsaren. |
| Bord | Skiva, två gavlar och två sargar. Separat beräkning av nedböjning och böjspänning. |
| Byrå | Stomme samt sex trädelar per låda, frontspel, lådsidespel och sammanhängande utdragsgrupper. |
| Material | Versionsbundna MDF-/björkplywoodprofiler, uppmätt tjocklek och valfri batchidentitet. Inget automatiskt ersättningsmaterial. |
| Beslag | Utbytbara dimensionsprofiler. Tillverkarens beslag, kapacitet och hålbilder är fortfarande obligatoriska före tillverkning. |
| Verkstad | Separat profil med råskivemått, kantmarginal och fiberriktning; kontroll av maskinområde och varje dels råmått. Materialgrupperat verkstadsunderlag anger nödvändiga råformat. Referensprofiler är uttryckligen okvalificerade. |
| Arbetsfil | Export/import av versionsbunden JSON. Import kontrolleras av servern och blir ett nytt osparat projekt; en tidigare projektidentitet återanvänds inte. |
| Profilbyte | Konsekvensjämförelse före tillämpning. Material/beslag räknar om geometrin. Maskinbyte behåller möbeldesignen och ogiltigförklarar berörd tillverkningsgranskning. |
| Revisioner | Serverlagring med konfliktkontroll och oföränderlig revisionshistorik. Återöppning av tidigare revision skapar en ny revision vid sparning. |
| Hyllbärighet | **Föreslå fackindelning** prövar fler jämnt fördelade fack i den ordinarie beräkningsmotorn. Samma yttermått, material och angivna nyttiga last behålls. Förslaget visar före/efter, fri fackbredd, ändrad egenvikt och kvarstående krav; det tillämpas först efter användarens val. |
| Beredning av hyllsystem | Den sparade möbelrevisionen kan öppnas direkt i det befintliga flödet för råmaterial, foggranskning, nesting, bearbetningsoperationer och maskinbunden CAM-kandidat. |
| Export | Beställning av exakt sparad revision via transaktionell kö. CAD-kärnan kontrollerar verkliga solider, kollisioner och formatåterläsning. |

Granskningspaketet innehåller STEP, GLB, DXF/SVG för delarnas båda sidor,
delritningar i PDF, BOM, kaplista, rörelsegrupper, modell, profiler och
kontrollrapport. Ett manifest binder varje fil med SHA-256. Exportstatusen binds
till organisation, projekt, revision, design och profilval. Paketet är tillgängligt
i en timme; en sparad revision kan exporteras på nytt.

`manufacturing/workshop-handoff.json` innehåller kundmått, skillnaden mot den
genererade stommen och råmått för varje CAD-del, grupperat efter material/version
och faktisk tjocklek. Formatgränser längs/tvärs fibern är nödvändiga villkor för
enskilda råämnen. Kantmarginal, verktygsutrymme och nestingspill tillkommer;
råämnenas summerade area är ingen beställningskvantitet. Delar delas eller skarvas
aldrig utan en ny modellerad konstruktion med verifierade förband.

Det nya kundfallet och kvarvarande konstruktionsbeslut dokumenteras i
[Kundmått 4340 × 2540 × 280](CUSTOMER_BOOKCASE_4340_2540_280.md).

Paketet har formatet `custombuild.furniture-review.v1`. Det innehåller ingen
skärande CAM och kan inte användas som en godkänd produktionsversion. Bordets och
byråns delritningar markerar att beslag/hålbilder/infästningar inte är verifierade.
Den befintliga separat verifierade CAM-kandidatkedjan för Hyllsystem behåller sina
krav på retention, material, maskin, verktyg, uppspänning och verkstadsacceptans.

## Från möbelarbetsytan till tillverkningsberedning

1. Välj **Hyllsystem**, ange möbeln och välj en planeringsprofil. Spara revisionen.
   Planeringsprofilen binder inte någon verklig verkstad eller tillgängliga råskivor.
2. Öppna **Förbered tillverkning**. Servern överför exakt samma geometri, fogar,
   skivtjocklekar, hyllplaceringar och belastningar till produktionsmotorn.
   Om överföringen ändrar någon konstruktionsparameter avvisas den.
3. Ange verkliga råskivor för stomme och bakstycke i beredningen: material/version,
   uppmätt tjocklek, format, antal, fiberriktning, marginaler och hinder. Ange
   verkstadens registrering för de delar som måste bearbetas från två sidor.
   Inga sådana uppgifter skapas från planeringsprofilens ungefärliga skivformat.
4. Hämta förfrågan om fogkvalificering och bind den externt verifierade
   retentionsevidensen genom den befintliga signaturkontrollen. Granska
   konstruktionskraven och spara en tillverkningsrevision.
5. Skapa och granska paketet. När de berörda kraven är uppfyllda innehåller det
   även nesting och operationer. Saknade krav redovisas; ett paket utan CAM
   marknadsförs inte som körklar maskinkod.
6. Verkstadsoperatören fyller i och validerar den verkliga maskinprofilen enligt
   [CAM-handoff](CAM_CANDIDATE_WORKSHOP_HANDOFF.md). Den profilen krävs innan
   den körbara CAM-kandidaten kan genereras. Nuvarande körbara postprocessor har
   ett uttryckligen avgränsat LinuxCNC-kontrakt; andra styrsystem kräver en
   implementerad och verifierad postprocessor.

Tillverkningsrevisionen sparar en oföränderlig `source_furniture` med ursprunglig
möbelrevision, hela modellen, profilerna och kontrollsummor. Den följer med i
`design/furniture-source.json` i det fullständiga paketet och kontrolleras mot
den frysta konstruktionsmodellen av paketläsaren. Möbelrevisionens och
tillverkningsrevisionens nummer är separata och båda behålls.

En ny sparning i möbelarbetsytan gör härledda tillverkningsrevisioner inaktuella
och avbryter deras köade/pågående jobb. Det gäller även ett rent batchbyte.
Historiska revisioner skrivs inte över. Vid återöppning återställs den senast
sparade beredningen endast när den hör till exakt samma möbel och profiler.
Osparade beredningsändringar varnas för när man återgår till möbeln.

En oförändrad sparning behåller den befintliga revisionen, historiken och
tillverkningsberedningen när även den aktuella regel-/profilbedömningen är
oförändrad. Konfliktkontrollen gäller fortfarande. Ändrade indata eller ändrad
beräkningsbedömning skapar en ny revision och gör tidigare beredning inaktuell.
Påbörjade profilbyten behöver tillämpas eller återställas före sparning och
export. Navigation varnar innan ett ej tillämpat profilförslag lämnas.

Fackförslaget hämtas med `POST /v1/furniture/shelf-bay-suggestion`. Det är en
läsande beräkning och sparar inte projektet. Sökningen är deterministisk och
begränsad till 16 avdelare. Numerisk PASS för nedböjning, böjspänning och lokal
upplagsbärighet följer den ordinarie regelmotorns varningsreserv. DADO-fogens
retentionsvarning finns kvar även när den numeriska upplagskontrollen passerar.
Egenvikten räknas om efter varje konstruktionsändring. Egna fackproportioner
ersätts inte; de måste först ändras till jämn fördelning av användaren.
Genomgående topp-, botten- och ryggdelar delas inte i moduler av fackförslaget.

### Råformat per material och tjocklek

**Eget råformat** låter stomme, rygg och lådbottnar ha olika skivformat,
fiberriktning och kantmarginal. Ett format binds till exakt material-ID,
katalogversion och uppmätt tjocklek. Material som saknar eget format använder
det gemensamma formatet. Tom fiberaxel i ett eget format betyder okänd; den
ärvs inte från det gemensamma formatet. Byte av maskin behåller råformaten.

**Råformat per material** visar tillåtna delrotationer, minsta skivformat och
hur många millimeter som saknas i X respektive Y. Bredd är maskinens X-led och
höjd är Y-led. Minsta format inkluderar kantmarginalen och beräknas så att varje
del ryms **enskilt**. Flera alternativ kan visas för riktningslöst material.
Det är inte ett nestingresultat, antal skivor eller en beställningskvantitet.
Verktygsutrymme och uppspänning tillkommer. En vald skiva som överskrider
maskinens arbetsområde markeras även om alla delar ryms på skivan.

Arbetsfilens `manufacturing.material_stocks` innehåller de separata formaten.
Varje rad anger `material_id`, `material_version`, `measured_thickness_um`,
`stock_width_um`, `stock_height_um`, `stock_grain_axis` och `edge_margin_um`.
Dubbla bindningar avvisas. Ett format som inte längre motsvarar någon genererad
del markeras med `MATERIAL_STOCK_NOT_USED` och behöver tas bort eller väljas om.
Det återanvänds inte för en annan tjocklek eller materialversion.

Planen sparas i revisionen och följer med i `stock_plan` i
`manufacturing/workshop-handoff.json`, bunden till samma CAD-design och manifest.
Ändrade format gör tidigare tillverkningsberedning inaktuell. Äldre arbetsfiler
utan `material_stocks` behåller sina gemensamma val och kan fortfarande läsas.
Planeringsversion `furniture-profile-planning-1.1.0` räknar om tidigare bedömningar;
en ny bedömning kan kräva en ny revision även när själva råmåtten är oförändrade.
Inga skivantal, lagerposter eller kvalificerade maskininställningar skapas från
planen när hyllsystemet öppnas i produktionsberedningen.

Nesting använder nu `deterministic-bottom-left-v2`. Kvadratiska delar kan vridas
90° för att följa råskivans fiberriktning, och rotationen följer med till
operationernas koordinater. Tidigare frysta generationsunderlag måste räknas om
mot den nya algoritmidentiteten; den tidigare versionens resultat omtolkas inte.

Bord och byråer kan fortsatt formges och CAD-exporteras, men kan inte skickas
genom hyllsystemets produktionsmotor. De saknar ännu sina egna kvalificerade
beslag, hålbilder och kompletta monteringsförband.

## Driftsättning och verifiering

Kör ordinarie migrering till `0021_furniture_review_reads` före API/worker.
Migreringen ger API:t SELECT på tenant-avgränsad historik och köbeställningar.
API:t får fortsatt inte ändra/radera auditposter eller ändra köleveransstatus.
PostgreSQL FORCE RLS och de befintliga rollkontrollerna gäller även dessa läsvägar.
Ingen ny extern tjänst behövs; exporten använder befintlig Redis, Celery och CAD-worker.

Beredningskopplingen kräver ingen ytterligare databasmigrering. Deploya API och
worker tillsammans: produktionspipeline `1.12.0` ändrar paketkontrollen och
tidigare generationsjobb måste genereras om mot den aktuella implementationen.

API-ytan är `/v1/furniture/catalog`, `/preview`, `/profile-change` samt
`/projects/{id}/draft`, `/history`, `/exports` och `/exports/{job_id}` under samma
prefix. OpenAPI beskriver inmatningens versionsbundna scheman.

`POST /v1/furniture/projects/{id}/production-preview` öppnar en exakt sparad
möbelrevision. `POST /v1/projects/{id}/versions` kräver dess serverkontrollerade
`source_furniture` när projektet använder möbelarbetsytan. En saknad, gammal eller
förändrad koppling kan inte skapa en tillverkningsrevision.

Regressionerna omfattar faktisk hyllhöjd vid tippscreening, unika hyllrader,
familjernas geometri, renderade delmått mot CAD-underlaget, profilbyten, tillåtna mått, CAD-export, revisionskonflikter,
tenantavgränsning, gamla/nya utkast och exporternas identitet. Webbläsartestet
`e2e/furniture-live.spec.ts` kör hela kedjan mot Compose: alla tre familjer,
tjockleksbyte, sparning, återöppning och nedladdning från den riktiga CAD-workern.
För hyllsystem fortsätter det till en sparad tillverkningsrevision, fullständigt
CAD-paket med verifierad möbelkälla samt återöppning på dator och mobil.
Live-proven behåller API:ts ordinarie gräns på 180 anrop per 60 sekunder.
Inför varje fristående scenario får föregående scenarios anrop löpa ut under
61 sekunder; själva användarflödet körs utan anropsbroms eller dolda omförsök.
Det hindrar att testsvitens gemensamma IP-adress gör separata prov beroende av
varandras anropsbudget. HTTP-fel under möbelflödet underkänner provet.
Integrationsprovet `test_signed_retention_executable_cam_release_and_historical_download_are_bound_end_to_end`
kör dessutom båda arbetsytorna genom signerad foggranskning, nesting, operationer,
maskinbunden CAM-kandidat och oföränderlig historisk nedladdning. Dess intyg,
maskinprofil och materialunderlag är testdata och kvalificerar ingen verklig verkstad.

## Återstående villkor för ett kostsamt fysiskt prov

1. Välj en första konstruktion och dokumenterade, demonterbara beslag. Lägg in
   exakta hålbilder, monteringskrav och verifierad kapacitet i en ny katalogversion.
2. Bind verklig materialbatch, uppmätt tjocklek och konstruktionsunderlag. MDF och
   björkplywood i planeringskatalogen är indikativa; ek är ännu inte kvalificerad.
3. Bind verkstadens verkliga maskin, verktyg, uppspänning och postprocessor. Validera
   nesting, verktygsåtkomst, kollisionsfri bearbetning och maskinkoden där.
4. Verifiera passning med ett litet fog-/beslagsprov och överenskomna toleranser.
   Frisläpp först därefter en komplett möbel för första tillverkningsprov.

En godkänd CAD-geometri bevisar inte bärighet, stabilitet eller att en viss
verkstad kan tillverka möbeln. Programmet redovisar dessa som separata villkor.
