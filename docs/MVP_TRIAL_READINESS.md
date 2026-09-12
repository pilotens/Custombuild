# MVP och skarpt tillverkningsprov

Ett lyckat programvarubygge räcker inte för att beställa ett tillverkningsprov
för cirka 20 000 kr. Appen kan skapa parametriskt designunderlag och bereda en
körbar CAM-kandidat för den stödda hyllfamiljen. Den här revisionen innehåller
ingen verifierad fysisk körning, fogprovning eller godkänd komplett möbel.

## Rekommenderad MVP-gräns

Den första hela leveransen bör vara **en hyllkonstruktion med verifierade
limfria förband, dokumenterat material och en accepterad verkstadsberedning**.
Kundmått är parametrar, inte en låst standardstorlek. Material- eller
verkstadsbyte behåller kundavsikten men kräver ny beräkning och granskning av
berörda tillverkningsunderlag. Verkstaden behöver inte väljas för att börja
formge; den verkliga maskinen och dess uppgifter behövs för körbar CAM.

Bord och byråer har parametrisk geometri och CAD-export. Deras beslag är
layoutprofiler utan tillverkarens kompletta hålbilder och kvalificerade
infästningar. De har ännu inte samma kompletta tillverkningsflöde som hyllan.

## Det viktigaste före ett betalt fullskaligt prov

| Prioritet | Kvarvarande hinder | Klar när |
| --- | --- | --- |
| 1 | Kundens verkliga måttkedja | Listens funktion och placering samt alla sex montageutrymmen är fastställda; CAD-stommen följer dessa uppgifter. |
| 2 | Konstruktion och råformat | Inga blockerande konstruktionsregler återstår; varje del ryms i verkligt råformat och arbetsområde, eller en ny modulkonstruktion är modellerad och verifierad. |
| 3 | Material och limfria fogar | Verkliga material/batcher, tjockleksvariation, egenskaper och mekanisk låsning stöds av dokumentation och uppmätta fogprov. |
| 4 | Verkstadens CAM | Aktuell revision binds till accepterad maskin, verktyg, uppspänning, sidregistrering, recept och postprocessor; det kompletta paketet verifieras oberoende. |
| 5 | Montering och acceptans | Monteringsordning, transport och resning fungerar; mätpunkter, lastfall och godkännandegränser är avtalade före provet. |
| 6 | Stegvis verkligt prov | Fogprov och representativ referensdel är godkända före komplett montering och avtalad slutprovning. Avvikelse stoppar nästa kostnadssteg. |

Exemplet **4340 × 2540 × 280 mm och list 90 × 20 mm** är en enskild kunds
uppgifter. Det får inte bli globala standardvärden. Listens funktion och
montageutrymmen i den filen är fortfarande okända. Modellens standardlayout,
200 N per hel hyllrad och screeningmaterial är inte kundens godkända beslut.
Fler hyllfack kortar fria hyllspann men delar inte automatiskt topp, botten
eller sidor. En dold skarv är ingen godtagbar lösning på ett formatproblem.

## Revisionsbunden beredning och mätning

`preview.trial_readiness` och `workshop_handoff.trial_readiness` innehåller samma
rapport med tio konkreta kontroller. Rapporten binder designhash, revision och
hela arbetsfilens hash, inklusive tillverkningsval. En egen rapporthash binder
status, underlag och åtgärder. Ändrade mått, material, revision eller råformat
ger en ny rapport. Den första blockerande kontrollen visas som nästa åtgärd.

`checked` betyder en kontrollerad del av modellunderlaget. `blocked` betyder
att design eller beredningsuppgifter behöver ändras. `requires_evidence`
betyder att verkliga uppgifter eller prov återstår. Rapporten godkänner aldrig
skärning: `production_qualified=false` och `physical_cutting_authorized=false`
gäller även när alla beräknade kontroller passerar. Ett inskrivet batchnamn
eller en referensmaskin kan inte ersätta verifierad fysisk dokumentation.

Granskningspaketets checksummeförteckning omfattar också:

- `inspection/first-article-checks.csv`: delmått och features i samma lokala
  koordinater och på samma sida som DXF-underlaget.
- `inspection/assembly-checks.csv`: stommens bredd, höjd och djup samt kontroll
  av diagonaler, planhet, fogar, monteringsordning, transport, förankring,
  avtalat belastningsprov och kundmått/list.
- `inspection/trial-readiness.md`: läsbar kopia av den revisionsbundna rapporten.

Stommått inkluderar inte list eller montageutrymme. Överenskomna toleranser,
hela möbelns godkännandegränser, mätvärden, resultat och kontrollant är tomma
tills de faktiskt har avtalats respektive uppmätts. Modellens featuretoleranser
är inte automatiskt acceptansgränser för hela möbeln.

## Utkast och återställning

Designarbetsytans lokala säkerhetskopia omfattar arbetsfil, projektnamn och
revision, ännu ogiltiga måttfält samt ej tillämpade förslag till material-,
batch-, maskin- och råformatsbyten. Servern kontrollerar arbetsfil och
profilförslag före återställning; ogiltiga råfält måste rättas innan de kan
tillämpas. Kopian skiljs per API, organisation och användare. En förändrad
kopia från en annan flik skrivs inte över utan att konflikten hanteras.
Båda möbelvyerna samordnar skrivning och borttagning med webbläsarens exklusiva
lås. Föråldrade väntande ändringar avbryts. Om låsstöd saknas eller nekas visas
ett lagringsfel; användaren kan fortsätta redigera och hämta en återställningsfil.
Skadade kopior kan laddas ned för återställning och export fungerar även
när API-granskningen inte är tillgänglig.

Produktionsberedningens säkerhetskopia omfattar tillämpade beredningsval samt
ofullständiga råfält för leverantörsprofiler, råmått, zoner och registreringspinnar.
Den binds till konto, API och exakt designkälla, som kontrolleras på nytt mot
servern. Råfält återställs som okontrollerade och måste klara formulärets
validering innan de tillämpas. Godkännanden och sparade giltighetsflaggor återställs
aldrig. Samma kopia kan laddas ned; filer över 256 KiB avvisas utan trunkering,
och lagringsfel eller konflikter mellan flikar visas uttryckligen.

## CAM och den fysiska verkstadskedjan

Rasterns raka fick- och spårväggar kräver obruten avverkning vid varje djup.
Den aktuella CAM-rättningen kompletterar rasterbanorna med en väggpassering;
oberoende verifiering kontrollerar sammanhängande faktiska skärsegment längs
samtliga väggar. Äldre kandidater från versionen före rättningen måste
genereras och granskas igen med aktuell programvaruidentitet.

CAM-kandidaten är en separat maskinkörbar leverans. Designpaketets STEP/DXF är
inte CAM. Den stödda produktionsprofilen är begränsad till den explicit
verifierade LinuxCNC-konfigurationen med tre linjära XYZ-leder; andra styrsystem
och maskintopologier kräver eget implementerat och verifierat stöd.

En fryst CAM-release ger inte fysisk körbehörighet. Appens workshop-endpoint
är fortfarande blockerad; de befintliga signerade modellerna för fysisk
provningskedja är inte anslutna till kandidatreleasen. Dessutom avser deras
aktuella verifiering en uppspänning per kedja. Ett paket med flera skivor och
A/B-sidor får inte återanvända samma provbevis för olika uppspänningar.
Verkstaden kan använda en separat, dokumenterad fysisk frisläppningsrutin för
MVP:n. Appen ska då tydligt behålla denna gräns. Se
[CAM_CANDIDATE_WORKSHOP_HANDOFF.md](CAM_CANDIDATE_WORKSHOP_HANDOFF.md).

## Vad mjukvaruverifieringen måste omfatta

Riktiga CAD-exporter, geometriska gränsfall, negativa CAM-mutationer,
revisionsbindning, isolering mellan kunder och checksummeverifierade nedladdningar
behövs. UI-flödet ska provas genom riktig API och worker: ändra mått, spara,
öppna igen, granska, byta profil och läsa ut samma identiteter ur paketet.

Det längre Compose-provet avslöjade att schemaläggningen kunde stanna efter
cirka 22 minuter medan workrarna fortfarande rapporterade god hälsa. Därför
övervakas nu den verkliga schemaläggarprocessen. En avslutad process eller en
för gammal schemaläggningsfil leder till kontrollerad omstart; filkontrollen
behåller hälsokontrollens gränser.
CI framtvingar även ett processstopp och kräver återhämtning tillsammans med
det fullständiga genererings- och nedladdningsflödet. Separata tjänsteloggar och
omstartsräknare gör återkommande stopp synliga. Den ursprungliga interna
stopporsaken är ännu inte fastställd; återkommande omstarter ska utredas.

`scripts/live_acceptance.py` rapporterar uttryckligen
`acceptance_scope=development_design_review`, `executable_cam_exercised=false`
och `physical_trial_verified=false`. Ett grönt utvecklingstest bevisar därför
inte i sig positiv produktions-CAM eller en fysisk körning.

Ett separat integrationstest kör redan hela den positiva kedjan: API skapar
genereringsjobbet, den verkliga workern genererar CAD och CAM-kandidat, paketet
verifieras, CAM godkänns och en oföränderlig release skapas. Det omfattar både
äldre designinmatning och möbelstudions sparade källa. Testet heter
`test_signed_retention_executable_cam_release_and_historical_download_are_bound_end_to_end`
i `tests/integration/test_api_design_flow.py`. Olika användare begär generering,
godkänner design, granskar CAM och frisläpper paketet. Samma designgranskares
CAM-godkännande och operatörens försök att granska avvisas. Testet använder syntetiska
CI-profiler och signerade provuppgifter, SQLite, minnesbaserad objektlagring,
direkt workeranrop och utvecklingstoken. Det provar mjukvarans verkliga
genererings- och kontrollkod men inte en extern driftsmiljö eller fysisk maskin.
Den separata spärren mot att godkänna en `TEST_ONLY`-kandidat gäller fortfarande.

Inför extern drift krävs även verifierad HTTPS/OIDC, skilda behöriga granskare,
container- och miljökontrakt samt samma positiva kandidatflöde genom kö,
objektlagring och databas i den faktiska driftsmiljön. Verklig
verkstadskvalificering måste styrkas separat från syntetiska CI-underlag.

Programvarutester och fysisk kvalificering ska redovisas separat. Inget
testantal, genomsnittsbetyg eller automatiskt grönt statusfält ersätter de
saknade underlagen i tabellen ovan.
