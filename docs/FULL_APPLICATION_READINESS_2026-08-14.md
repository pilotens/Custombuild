# Full applikationsanalys och väg till fullgod status

Datum: 2026-08-14<br>
Status: besluts- och arbetsunderlag, källans nuläge uppdaterat 2026-08-17; lokal synkroniserad deployment/liveacceptans återstår
Omfattning: frontend, domänmodell, CAD, tillverkningsunderlag, API, drift, säkerhet och release

> **Policyuppdatering 2026-08-17:** [Adhesive-free joining policy](ADHESIVE_FREE_JOINING_POLICY.md)
> är styrande. Lim, tätningsmedel och andra kemiskt bundna hållmetoder är inte
> tillåtna. Torrt självlåsande montage prioriteras; annars krävs explicit
> demonterbar mekanisk infästning.

> **Verifierad underlagsuppdatering 2026-08-17:** De verkliga UI-standarderna
> kan nu slutföra ett strikt granskningspaket även när vald lagerprofil inte
> rymmer råämnet. Jobbet blir lyckat för designgranskning med
> `package_status=READY_FOR_DESIGN_REVIEW`, `cam_status=BLOCKED`, exakt
> `blocker_codes=[STOCK_PROFILE_MISSING]` och
> `physical_cutting_authorized=false`. Råämnesbehov, fryst DesignSpec, CAD och
> rå DFM bevaras; stockinköp, nesting, operationer, verktyg, setupblad, backplot
> och maskinkod utelämnas. Ingen stock- eller maskinprofil ändras automatiskt.
> För riktade skivmaterial har `DFM-GRAIN-001` samma review-only-konsekvens tills
> en exakt strukturerad X/Y-axel är bunden; ett dokument eller en kvittens kan
> inte verifiera den. Äldre formuleringar nedan om total paketblockering och
> storformatsreparation är historiska där de motsäger denna uppdatering.

## 1. Sammanfattning

Custombuild är i dag en fungerande och ovanligt väl spärrad lokal produktionskandidat för parametrisk design och intern designgranskning. Den är inte en färdig Internetprodukt och den genererar inte ett paket som får användas för fysisk kapning.

Det är viktigt att skilja på fem olika mål:

| Nivå | Nuläge | Bedömning |
| --- | --- | --- |
| Redigera och spara en parametrisk möbel | Fungerar | Stark grund, men produktflöde och avancerade kontroller behöver kompletteras |
| Skapa ett spårbart design-review-paket | Fungerar även utan matchande lagerprofil | Paketet bevarar fryst design, CAD, rå DFM och råämnesbehov men markerar CAM blockerat och utelämnar all stock-/nesting-/CAM-beroende evidens |
| Lämna ett komplett verkstadsunderlag | Inte fullgott | Beslag, kantband, montering, QA och inköpsunderlag är ofullständiga |
| Generera och frisläppa skärande CNC-program | Avsiktligt avstängt | Ska förbli avstängt tills verklig maskin, verktyg, material och fysisk verifiering finns |
| Driva tjänsten externt som SaaS | Inte driftsatt | Lokal kandidat finns; TLS, OIDC, secrets, offsite-backup och alerting återstår på plattformen |

Den tidigare externa slutsatsen “stoppa fysisk tillverkning” är därför fortfarande riktig. Däremot är flera av rapportens specifika konstruktionsfel redan lösta i den aktuella versionen. Rapporten får inte återanvändas som om alla dess detaljfynd fortfarande gällde.

## 2. Verifierad aktuell baslinje

Analysen byggde ursprungligen på aktuell källkod, körande lokal produktionskandidat och det då senaste verkliga 228-filerspaketet. Hasharna och inventeringen nedan bevaras som historisk evidens; den senare verifierade checkpointen redovisas separat och ersätter paketets gamla detaljfynd som nulägesstatus.

- Git HEAD: `aabf89bfedd6abbb0cc6b701767f87fc57411d21`
- Källmanifest: `14999b5ef207c62dcab9c2a35be6f29bdfc116fc2ef3a3a22c805001a04a24d8`
- Körande webbversion: `0.5.1-local.14999b5ef207`
- Produktionsmiljö: 7 av 7 långlivade tjänster `healthy`, omstarter 0
- Webb: HTTP 200 på port 3000
- API `/ready`: databas, Redis, objektlagring och regelmotor `ok`
- Senaste granskade jobb: `3609582e-5a5e-4865-9f3b-344a0652b29d`
- Senaste paketets bundle-SHA: `1457752e65199bc71fef4a0fcf71bad42407c2053add85b9d7e19893bba44898`
- Senaste manifest-SHA: `9b71c36fcc8752043c1ae283f7e3c8907d8c3eae2fc149ee6ae452bd389fa752`
- Paket: 227 payloadfiler plus `manifest.json`

Direktinspektion av det historiska 228-filerspaketet visade bland annat:

- `release_scope=design_review`
- `machine_use=validation_only`
- `physical_cutting_authorized=false`
- 49 BOM-rader och 49 cut-list-rader
- 98 DXF-filer
- 0 rader i beslaglistan
- paketet saknade då fryst `DesignSpec`, `START_HERE` och parametrisk provenance
- GLB hade då millimeterstora råkoordinater och DXF saknade enhetsdeklaration

Senare verifiering har löst just dessa paketfynd: DXF är AC1027 med `$INSUNITS=4` och `$MEASUREMENT=1` och återläses som millimeter med oberoende `ezdxf`; GLB lagrar positioner, bounds och volym i meter/m³ och kontrolleras med separat loader; paketet innehåller checksummebunden fryst DesignSpec/resultatsammanfattning och provenance/rekonstruktionskedja; manualen börjar med `START_HERE`.

Aktuell P0.10-status hålls isär i två spår. En separat reproducerbar MDF-engine-smoke är PASS. De verkliga UI-standarderna `shelving` och `wall-library` passerar CAD. När baksidan med `birch-plywood-6` saknar matchande lagerprofil skapas nu ett lyckat, nedladdningsbart men stocklöst granskningspaket med exakt `STOCK_PROFILE_MISSING`; CAM, stockinköp och fysisk release är fortsatt blockerade. Deras exakta råämnesbehov är `2376.266 × 2296.266 mm` respektive `2376.266 × 1634.066 mm`, och den valda standardkontexten muteras inte.

Den historiska strikta releasekontrollen med `scripts/release_readiness.py --require-clean` gav:

- `software_release_ready=false`
- `commercial_release_ready=false`
- `physical_machine_release_ready=false`
- enda programvarurelease-blockeraren i kontrollen är 300 ej committade sökvägar
- kommersiellt godkännande och fysisk maskinauktorisering kräver extern evidens

## 3. Delta mot den bifogade externa rapporten

Den bifogade rapporten bevaras här i normaliserad form tillsammans med dagens status.

| Ursprungligt fynd | Status i aktuell version | Kvarstående arbete |
| --- | --- | --- |
| 24 solida kollisioner mellan fronter, bottnar, topp, sockel och underskåpssidor | Löst för det rapporterade felet | Lägg en generell B-rep/OCC-kollisionskontroll i runtime så samma felklass aldrig kan återkomma |
| Underskåp djupare än möbelns angivna djup | Löst | Basdjup måste fortsätta vara exakt lika med stomdjup i frontend, domän och API |
| Front- och sockelkoordinater inne i stommen | Löst | Behåll aktuella geometriinvarianter och tvärmotoriska regressionsprov |
| Ingen global assembly-clash-kontroll | Löst | Exakt OpenCascade-grind blockerar odeklarerad positiv solidvolym och binder till verifierade fogar |
| Ingen verklig front-, sockel- eller vägginfästning | Kvarstår, men spärras ärligt | Kräver val av gångjärn, montageplatta, sockelfäste, väggtyp och ankarsystem |
| Ingen verifierad permanent hållning för DADO/RABBET | Kvarstår | Kräver verifierat torrt självlåsande montage eller skruv, plugg eller låsbeslag samt provad förbandsklass |
| 105 kg monteringsgrupp utan grupplyftanalys | Löst för designgranskning | Manualen redovisar gruppvikt/-mått och minsta personantal; kund-/fysisk montering kräver fortfarande extern auktorisering |
| Odefinierad panel-positioning-jig | Kvarstår | Antingen generera/verifiera jiggen eller ta bort kravet och ersätt med verklig metod |
| `.ngc` är bara luftkörning | Korrekt och avsiktligt | Ska inte “fixas” genom att skapa skärande kod utan verklig maskin- och verkstadsverifiering |
| Tvåsidig bearbetning saknar full fysisk strategi | Maskinneutral del löst; fysisk del kvarstår | B-sida planeras före A-sida, genomgående kontur sist och tvåsidigt kräver registrering; verkliga WCS, pinnar och klämning kräver leverantör |
| DXF saknar enhetsdeklaration | Löst | AC1027-filer deklarerar `$INSUNITS=4` och `$MEASUREMENT=1`; millimeter verifieras med oberoende `ezdxf` |
| GLB är 1000 gånger för stor i standardvisare | Löst | Positioner, bounds och volym exporteras i meter/m³ och verifieras med separat loader |
| Ingen STEP round-trip | Löst internt, delvis externt | CadQuery/OCC-import kontrollerar namn, antal, bounds och volym; separat FreeCAD-kontroll är fortfarande valfri och svagare |
| Kantskyddskedja ofullständig och framkant felmappad | Säkert delmål löst | Lokal kant, orientering och tjocklek bevaras; SKU, mekanisk fästmetod och godkänd råmåttskompensation är externa krav som fortsatt blockerar fysisk release |
| BOM visar 49 rader med quantity 1 | Löst | Instansspårbar cut-list kompletteras av grupperad BOM |
| `raw_area_m2` misstolkas som inköpsyta | Löst | Separat inköpsunderlag härleds från verklig nesting |
| QA-protokoll har förifylld “OK” utan mätvärden | Löst | Tomt maskinläsbart QA-protokoll täcker delar, operationer och toleranser utan fabricerade resultat |
| Etiketter saknar revision, hash, material, fiber, sida och nesting | Löst | Etiketterna är placementbundna och maskinläsbart spårbara |
| Manualen är intern och teknisk | Löst för designgranskning | Manualen börjar med `START_HERE` och redovisar gruppdata, personantal, verktyg och olösta jiggar; fysisk montering är inte auktoriserad |
| Lastfallet motsvarar cirka 6,4 kg per panel i femfacksfallet | Kvarstår som screening | Inför explicit lastfall/lastklass; certifierad klass kräver material- och prototypdata |
| `template_version` 1.1.0 och 1.0.0 i samma manifest | Löst | Domän-, capability- och paketschemaversioner är nu entydigt namngivna och bundna |
| Placeholder-identitet godkänner varningar | Löst | Aktuell API-bindning använder verklig principal, tidpunkt, skäl och serverägd snapshot |
| `source_provenance=null` | Löst | Paketet innehåller checksummebunden fryst DesignSpec, resultatsammanfattning och verifierbar provenance/rekonstruktionskedja även för parametriska modeller |

## 4. Nuläge per delsystem

### 4.1 Produkt och frontend

Styrkor:

- Fyra begripliga arbetslägen: Utforska, Studio, Kontroll och Underlag.
- Samma modell förblir monterad mellan Studio, Kontroll och Underlag.
- Parametriska mått, fack, hyllor, material, undo/redo, serverpreview, konfliktkontroll och autosave är verkliga funktioner.
- Delval, vertikal hyllflytt, horisontell avdelarflytt och semantiska tillägg har riktiga domänkopplingar.
- Underlag använder sanningsenligt designgranskningsspråk och visar immutable serverhistorik samt fail-closed fysisk status.
- Underlag återhämtar deterministiskt ny designhash eller aktuell serverrevision efter 409-konflikt, rensar stale jobb/artefakter och kräver ett nytt uttryckligt användarklick utan automatisk versionsskapning eller sparning.
- Navigation, canonical URL/deep links, browserhistorik, 320 px reflow, forced colors, reduced motion, alternativa kontroller och åtta deterministiska desktop-/mobilbaselines är verifierade.
- Strukturändringar har en jämförbar ghost. Grinden är PASS med 391/391 tester, full TypeScript/ESLint, oberoende reviewer-PASS, aktuell produktionsbuild och 2/2 Chromiumfall i verklig WebGL respektive SVG-fallback. Bildanalysen verifierar att hela modellen och båda jämförelsefärgerna syns; avbryt lämnar lagring/historik/API orörda och bekräftelse blir en ångringsbar transaktion.
- Den verkliga WebGL-grinden är PASS för den fulla kanoniska frontendgränsen `6000 × 4000 × 1200 mm`, 40 hyllor, 16 avdelare/17 fack, 17 underskåp och 752 delar: cold `2428.2 ms`, orbit p95 `19.1 ms`/max `20.5 ms`, selection p95 `419 ms`, lägesbyte `399.6 ms`, transparens `849.8 ms`, long task `447 ms` och 0 context loss/errors.

Kvarstående gap:

- Restore/fork/compare saknar fortfarande backendkontrakt och visas därför inte som fungerande produktfunktioner.
- De aktiva frontendgränserna är harmoniserade med det kanoniska kontraktet. Bildtolkningens lägre inference-only-tak är avsiktliga heuristiker och förblir separata.
- Den verkliga 3D-modellen använder enkla färger/roughness, inte produktionssanna texturer eller per-delmaterial.
- Fyra av sex grundmodeller är avsiktligt konceptmodeller:
  - screenade: `shelving`, `wall-library`
  - koncept/blockerade: `sideboard`, `room-divider`, `hanging-shelf`, `cupboard`

Produktens nuvarande språk bör alltså säga “designgranskning klar” och inte “tillverkningsklar”.

### 4.2 Domänmodell och geometri

Styrkor:

- Måtten är heltalsbaserade i mikrometer i domänen.
- Identiteter, DesignSpec-hashar, fogar, features och AssemblyGraph är deterministiska.
- Basdjupet är nu strikt lika med möbeldjupet.
- De rapporterade front-/botten-/sockelkollisionerna är korrigerade.
- Tester täcker envelope, 17 moduler och odeklarerade överlapp för den reparerade basgeometrin.

Gap:

- Dörrar, lådor, verkliga gångjärn, montageplattor, väggbeslag, sockelfästen och belysning saknar domänmodell.
- Endast ett globalt material kan anges; per-delmaterial, frontfinish, kantband och fiberriktning är inte ett fullständigt produktkontrakt.
- 4200 mm kontinuerliga delar kräver en verklig leverantörsbunden passande lagerprofil eller torr självlåsande/demonterbar mekanisk segmentering. Systemet får aldrig ersätta lager eller maskin automatiskt; utan ett sådant beslut förblir CAM blockerat.
- UI-standardernas `birch-plywood-6`-baksida saknar aktuell lagerprofil. CAD och det stocklösa granskningspaketet kan färdigställas, men stockinköp, nesting och CAM förblir fail-closed.

### 4.3 CAD och interoperabilitet

Styrkor:

- STEP är auktoritativ CadQuery/OpenCascade-geometri.
- Varje del måste vara en giltig solid.
- STEP återimporteras och kontrolleras mot delnamn, delantal, bounds och volym.
- En obligatorisk exakt OpenCascade-grind blockerar odeklarerad positiv solidvolym i assemblyn.
- DXF deklarerar millimeter och verifieras med oberoende `ezdxf`; GLB lagrar meter/m³ och verifieras med separat loader.
- Placeholder-CAD blockeras.

Gap:

- FreeCAD är en valfri derivata och verifierar ännu inte hela modellen som oberoende kernelbevis.
- CAD passerar för båda faktiska UI-standarderna, men detta får inte beskrivas som en manufacturing-PASS när lagerprofilen för baksidan saknas.

### 4.4 Tillverkningsplan, BOM, QA och manual

Styrkor:

- Parts, A/B-DXF och manifest är starkt korsrefererade. Operationer och nesting finns endast när exakta stock-, grain- och övriga CAM-förutsättningar är uppfyllda.
- Nesting har bounds-, grain-, keepout- och överlappskontroller.
- Maskinprogrammet är ärligt klassat som `VALIDATION_DRY_RUN`.
- `custombuild.workshop-readiness.v2` kräver exakt sex mjukvarukrav, fjorton externa verkstadskrav samt villkorat kantbandskrav. Scope är låst till `design_review`/`validation_only`, fysisk kapning är alltid falsk och ofullständiga, duplicerade eller motsägelsefulla bevis avvisas i API, worker och webb.
- Fiberriktning har en backend-auktoritativ källa. Katalogdeklarerat icke-riktat material får status ej tillämpligt. Riktat material utan strukturerad X/Y-bindning ger exakt `DFM-GRAIN-001`, stoppar före nesting och kan endast följa med som olöst blockerare i ett review-only-paket. Ogenomskinlig `material_grain`-evidens eller en designkvittens får aldrig göra kravet verifierat.
- Liveacceptansen kräver manifest v4 och den checksummebundna readiness-v2-artefakten, jämför den med jobbresultatet och verifierar samma generation-context genom jobb, resultat och manifest. API:t accepterar endast exakt auktoritativ geometri samt DFM `PASS|WARNING`; ordningen på unika externa evidens-ID:n kan inte längre skapa duplicerade jobb.
- API:t läser readiness och manifest med fasta storlekstak och binder rå canonical JSON, normaliserad v1/v2-semantik, mediatyp, hash, storlek och manifest-v4-kontext innan CAM eller release. Den statiska release-readinessrapporten AST-verifierar samtidigt de verkliga producerande returvärdena och releaseguarderna, så stale eller död säkerhetskod inte kan ge ett falskt grönt programvarubesked.
- B-sida planeras före A-sida, genomgående kontur sist och tvåsidig bearbetning kräver explicit caller-bunden registrering.
- Grupperad BOM, nestingbaserat inköpsunderlag, placementbundna etiketter och tomt QA-protokoll kompletterar instansspårbarheten.
- Manualen börjar med `START_HERE` och redovisar gruppvikt/-mått, personantal, verktyg och olösta jiggar.

Gap:

- Readiness-v2-backendens 144 regressioner, strikt Mypy/Ruff och oberoende granskning är gröna. Den promoterade webbparsern är bytegranskad, men full TypeScript/Vitest/Chromium efter promotion väntar på kontrollerat Docker-lagringsunderhåll eftersom C:-disken inte rymmer en verifierbar frontendkörning.
- Inga verkliga registreringspunkter, WCS, klämzoner, spoilboarddata eller hållflikar finns.
- Kantskyddskedjan bevarar lokal kant, orientering och tjocklek, men SKU, verifierad mekanisk fästmetod, färg och godkänd råmåttskompensation kräver externa katalogbeslut.
- Beslaglistan är tom därför att verkliga beslag inte är modellerade.
- Standardbaksidan `birch-plywood-6` saknar en godkänd lagerprofil; systemet blockerar korrekt i stället för att fabricera skivformat.

### 4.5 API, data och säkerhet

Styrkor:

- Tenant/RLS, optimistic concurrency, immutable versioner, hashbundna artefakter och innehållsadresserad lagring finns.
- OIDC/PKCE, rate limiting, CORS, CSP, request-ID, readiness och säkra uppladdningar är implementerade eller fail-closed.
- API, worker och scheduler har separerade roller och hälsoindikatorer.
- Extern produktionsoverlay validerar HTTPS, OIDC, secrets och proxygränser.
- Parametriska paket innehåller checksummebunden fryst DesignSpec/resultatsammanfattning samt verifierbar provenance och rekonstruktionskedja.

Gap:

- Den körande miljön är lokal development-auth bakom loopback, inte Internetproduktion.
- Val av riktig IdP, domän, TLS-proxy, secret manager, loggplattform, tracing-backend och alertmottagare är externa driftbeslut.

### 4.6 Drift, backup och release

Styrkor:

- Sju produktionstjänster är healthy och capability-dropped/read-only där det ska vara så.
- CI har Ruff, mypy, pytest, TypeScript, ESLint, Playwright, image builds, SBOM och vulnerability gate.
- Källmanifest och OCI-labels gör byggkontexten spårbar.
- En aktuell koordinerad v2-backup finns i ett externt backupmål, exempelvis `<backup-root>/prod-base-geometry-20260814T151539`, med databasdump, objektarkiv och checksummad inventering.
- Testmiljön är borttagen; källkopian och testbevisen finns kvar.

Gap:

- Arbetskopian har 300 ej committade sökvägar och är därför inte en reproducerbar release trots att samma källmanifest är deployat lokalt.
- Den aktuella backupen ligger på samma fysiska dator och saknar schemalagd krypterad offsite-replikering.
- Ingen färsk `restore-drill.json` ligger bredvid den senaste backupen.
- Alertleverans och dashboards finns inte i den lokala Compose-miljön.
- Det äldre `prod/`-trädet finns kvar som historisk spegel och ökar risken för fel releasekälla.
- Docker har gott om återvinningsbar cache/image-data, men volymer och rollback-images får inte bredprunas.

## 5. Vad “fullgod” bör betyda

### 5.1 Fullgod design-review-produkt

Kan nås utan skärande CNC-kod när följande är sant:

1. Underlagsvyn är helt sanningsenlig om release scope.
2. Alla screenade grundmodeller passerar generellt assembly-clash-gat.
3. DXF och GLB följer externa standardenheter.
4. Paketet innehåller DesignSpec, tydliga versionsfält och full rekonstruktionskedja.
5. BOM, skivinköp, kantband, QA, etiketter och manual är begripliga och maskinläsbara.
6. Produkt-, a11y-, visuella och performance-gates är repeterbara.
7. Koden finns i en ren granskad commit med komplett CI-evidens.

Punkterna om sanningsenligt Underlag, generell kollisionsgrind, DXF-/GLB-enheter, fryst DesignSpec/provenance, maskinläsbara paketartefakter, ghostens runtime-E2E och den fulla WebGL-prestandagrinden är genomförda i källan. De faktiska UI-standarderna kan nu ge ett reproducerbart stocklöst granskningspaket när `birch-plywood-6` saknar lagerprofil. `STOCK_PROFILE_MISSING` är fortfarande olöst och blockerar stockinköp, nesting och CAM; det blockerar inte längre hämtning av ärligt avgränsad review-evidens.

Design-review-grinden stoppar dessutom före CAD om canonical konstruktionsregler ger `BLOCK`. Manufacturing utvärderar samtliga stockslags layout/DFM före CAM och bevarar separata instansfel, så senare DFM-problem inte maskeras av registreringsfel och flera fysiska exemplar inte kollapsar till ett problem.

### 5.2 Fullgott verkstadsunderlag

Kräver dessutom:

1. Verkliga beslag och borrbilder.
2. Verkligt torrt självlåsande eller demonterbart mekaniskt förbandssystem.
3. Verkligt väggankare och installationskontrakt.
4. Transport- och monteringsmoduler med godkänd vikt/sekvens.
5. Leverantörskatalog för material, kantband och toleranser.
6. General arrangement, inköpslista och rollseparerade paket.

### 5.3 Fysisk CNC-release

Kräver extern fysisk evidens och ska inte automatiseras fram av en nattkörning:

- maskinprofil, WCS och travel limits
- mätta verktyg, hållare, runout och stick-out
- materialbatch och uppmätt tjocklek
- fogkuponger och toleransprov
- oberoende toolpath/material-removal-jämförelse
- övervakad air-cut
- referensdel
- komplett prototyp och lastprov
- namngiven CNC-operatör och möbelkonstruktör

`physical_cutting_authorized` ska förbli `false` tills ett separat granskat kontrakt kan binda dessa bevis.

### 5.4 Internetproduktion

Kräver utanför repot:

- domän och TLS-terminering
- vald OIDC-provider och klientregistrering
- secret manager och rotationsrutiner
- krypterad, versionerad offsite-backup
- schemalagd restore-drill
- central loggning, tracing, dashboards och larm
- registry, image-signering och verifierad digestpromotion
- kommersiell/licensmässig releaseägarattest

## 6. Tidigare identifierade helhetsgap som ska bevaras

Följande tidigare slutsatser ingår fortsatt i planen och får inte tappas bort:

- verkliga komponenter: dörrar, lådor, gångjärn, skenor, handtag och ankare
- per-delmaterial, tvåton, ytskikt, fiberriktning och kantband
- segmentering och verifierade skarvar för långa delar
- extern drift med TLS/OIDC/secrets/monitorering
- ren commit- och releasehygien
- djupare direktmanipulation utan att skapa skenfunktioner
- bättre materialvisualisering/PBR i den verkliga modellen
- revisionsjämförelse och restore/fork
- mobil strategi: full editor eller uttalad granskningsvy
- krypterad offsite-backup och automatisk restore-verifiering
- CI-baserad tillfällig testmiljö nu när den permanenta teststacken är borttagen

## 7. Beslut som måste tas av produktägare, verkstad eller leverantör

Följande får inte fyllas i med antaganden:

1. Är slutmålet endast design-review, verkstadsunderlag eller fysisk CNC-release?
2. Vilket gångjärn, montageplatta, lådskena, sockelfäste och väggankare ska stödjas först?
3. Vilket torrt självlåsande, skruv-, plugg- eller låsbeslagssystem ger verifierad hållning utan lim?
4. Vilken maxvikt, modulstorlek, bemanning och lyftutrustning gäller?
5. Vilka material-, kantbands- och leverantörs-SKU:er är godkända?
6. Vilken maskin, postprocessor, WCS, verktygsuppsättning, registrering och klämstrategi gäller?
7. Vilken av de fyra konceptmallarna ska få ett verkligt produktionskontrakt först?
8. Ska mobil vara en komplett editor eller främst en gransknings- och statusyta?
9. Vilka roller och filer ska ingå i kund-, verkstads- och installatörspaket?
10. Vilken IdP, hostingplattform, domän, loggplattform och backupdestination ska användas?
11. Ska UI-standardernas baksida behålla `birch-plywood-6` och få en leverantörsverifierad lagerprofil som rymmer råämnena `2376.266 × 2296.266 mm` och `2376.266 × 1634.066 mm`, eller ska standardmaterialet ändras genom ett separat produktbeslut till en redan verifierad profil?

## 8. Rekommenderad prioritering

1. Fatta och dokumentera det externa materialbeslutet för UI-standardernas `birch-plywood-6`-baksida; lägg inte in ett påhittat skivformat för att få grinden grön.
2. Slutför externa SKU-/mekanisk-fästmetod-/råmåttsbeslut för kantskydd utan att ändra fail-closed fysisk status.
3. Ta beslut om verkliga beslag, fogsystem, väggankare, maskin, WCS, verktyg och fixtur.
4. Bygg prototyp-/lastprov och namngivna operatörs-/konstruktörsgodkännanden innan någon fysisk release övervägs.
5. Frys arbetet i ren granskad commit och bygg komplett CI-evidens.
6. Slutför extern drift med TLS/OIDC/secrets, observability och offsite restore-verifiering.

Den detaljerade, körbara segmenteringen finns i `docs/OVERNIGHT_EXECUTION_PLAN_2026-08-14.md`.
