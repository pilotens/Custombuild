# Custombuild — samlad produktvision, readinessanalys och arbetslista

Datum: 2026-08-14<br>
Status: levande masterlista för segmenterat genomförande<br>
Källor: den utvecklade produktvisionen, den externa granskningen av leveranspaketet, tidigare frontend-/geometri-/driftrevisioner samt aktuell käll- och runtimekontroll.

> **Policyuppdatering 2026-08-17:** [Adhesive-free joining policy](ADHESIVE_FREE_JOINING_POLICY.md)
> är styrande. Alla äldre alternativ med lim eller kemiskt bunden fogning är
> indragna. Torrt självlåsande montage prioriteras; annars krävs explicit
> demonterbar mekanisk infästning.

> **Genomförandeuppdatering 2026-08-17:** P0.10 skiljer nu strikt mellan ett
> nedladdningsbart design-review-paket och CAM. De faktiska standarderna kan ge
> `READY_FOR_DESIGN_REVIEW` med CAM `BLOCKED` och exakt
> `STOCK_PROFILE_MISSING` utan att lagerformat eller maskinprofil ändras.
> `DFM-GRAIN-001` är backend-auktoritativt och stoppar nesting/CAM för riktade
> material utan exakt X/Y-bindning; dokument eller kvittens kan inte lösa det.

## 1. Produktlöftet som styr allt arbete

Custombuild ska vara en visuell möbelstudio:

> Jag ser möbeln. Jag formar möbeln. Custombuild ser till att den går att bygga.

Det löftet består av två lika viktiga delar:

1. Användaren arbetar direkt med en stor, persistent och begriplig modell i `Explore`, `Studio`, `Check` och `Build`.
2. Systemet får aldrig kalla något produktionsklart innan den auktoritativa geometri-, regel-, CAD- och tillverkningskedjan faktiskt har verifierat det.

Förenkling får alltså ske i gränssnittet, men aldrig genom falsk precision eller genom att dölja ett säkerhetsbeslut.

## 2. Verifierad baslinje

- Källmanifest vid arbetsstart: `14999b5ef207c62dcab9c2a35be6f29bdfc116fc2ef3a3a22c805001a04a24d8`.
- Git HEAD: `aabf89bfedd6abbb0cc6b701767f87fc57411d21`.
- Arbetskopian innehåller 302 ändrade eller nya sökvägar. De får inte återställas eller skrivas över som om de vore disponibla engångsfiler.
- Produktionsmiljön har sju av sju långlivade tjänster uppe och friska, webb svarar 200 och API readiness rapporterar databas, Redis, objektlagring och regelmotor som `ok`.
- Ingen produktionsdeployment ingår i arbetssegmenten utan ett separat, uttryckligt beslut och full acceptansgrind.
- Fysisk kapning är och ska förbli avstängd: `physical_cutting_authorized=false`.
- Det tidigare felet med djupare underskåp och den rapporterade gruppen med 24 geometriöverlappar är korrigerat i aktuell källkod och testat.
- Endast `shelving` och `wall-library` är klassade som screenade. `sideboard`, `room-divider`, `hanging-shelf` och `cupboard` är konceptmodeller tills deras konstruktion, beslag och stabilitet har modellerats och verifierats.

## 3. Tydliga mognadsnivåer

Ordet “fullgod” delas upp i fem kontrollerbara nivåer.

### Nivå 1 — visuell möbelstudio

Användaren kan välja en verklig parametrisk startmodell, ändra den direkt i 3D, använda numerisk precision, ångra/göra om och behålla modell, kamera och selection mellan arbetslägen.

### Nivå 2 — verifierad designgranskning

Backend har kontrollerat aktuell designversion. Paketet är spårbart och lämpligt för teknisk granskning, men får inte beskrivas som frisläppt för fysisk tillverkning.

### Nivå 3 — fullgott verkstadsunderlag

Geometri, fogar, kollisioner, enheter, BOM, kantband, inköp, QA, etiketter, setup-plan och monteringsdata är kompletta och internt konsekventa. Alla externa material-, beslag-, infästnings- och maskinval är bundna till verifierade katalogposter.

### Nivå 4 — fysisk CNC-release

En namngiven ansvarig har godkänt material, beslag, vägginfästning, hållande fogar, verktyg, fixtur, WCS, registrering, prototyp-/lastprov och maskinspecifik postprocessor. Först då får fysisk kapning auktoriseras.

### Nivå 5 — publik produktionsdrift

TLS, riktig OIDC, hemlighetshantering, central loggning, spårning, larm, offsite-backup, restore drills, signerade images och digestbaserad promotion är driftsatta och övade.

## 4. Produktvisionens nuläge

| Område | Nuläge | Bedömning |
|---|---|---|
| Fyra lägen | `Utforska`, `Studio`, `Kontroll`, `Underlag` finns | Till stor del genomfört |
| Persistent modell | Samma canvas överlever lägesbyten | Genomfört |
| Explore-startvägar | Design, Custombuild-förslag, bild och tom start finns | Delvis; AI är inte en verklig intentionstjänst |
| Direkt måttändring | Yttermått, hyllor och avdelare har verkliga transaktioner | Delvis genomfört |
| Drag-and-drop | Semantisk slot-baserad placering finns | Delvis; inte generell 3D-snap |
| Kontextinspektör | Möbler och vissa fysiska delar kan redigeras | Delvis; zon, kant, yta och infästning saknas |
| Realtime/local + server | Lokal preview, debounce, abort och auktoritativ serverpreview finns | Stark grund |
| Undo/redo | Modelltransaktioner finns | Genomfört för centrala flöden |
| Intelligenta zoner | Saknar domän- och API-kontrakt | Inte implementerat |
| AI för intention | Bildimport är försiktigt avgränsad; fri text-AI saknas | Inte fullständigt implementerat |
| Visuell validering | Berörda delar kan fokuseras och strukturändringar har jämförbar 3D-ghost | Implementerat och statiskt verifierat; runtime-E2E återstår som acceptans |
| X-ray | Fulla infästningar/features kan inte visas auktoritativt | Inte implementerat |
| Exploderad vy | Enkel visuell separation och AssemblyGraph-data finns | Delvis |
| Monteringsläge | PDF/monteringsdata finns men inte en full interaktiv guide | Delvis |
| Build-pipeline | Version, validering, godkännande och design-review-ZIP finns | Fungerar fail-closed |
| Fabrik/inköp/montör | En verifierad ZIP finns; mottagaranpassade paket är ofullständiga | Delvis |
| Materialvisualisering | Två verkliga material-ID:n stöds visuellt | Begränsat; inga fria ytskikt/per-delmaterial |
| Projekt/revision | Draft, immutable versioner och listning finns | UI och restore/fork är ofullständiga |
| Mobil | Granskning och grundläggande redigering fungerar | Full precisionseditor är inte definierad |

## 5. Kanonisk arbetslista

Varje punkt är ett eget segment. Ett segment räknas inte som klart förrän dess acceptanstest är grönt och ändrade filer är dokumenterade.

### P0 — säkerhet, sanning och deterministisk leverans

#### P0.1 Sanningsenlig Build-vy

- Läs och visa backendens `design_review_ready`, `physical_cutting_authorized` och saknade verkstadskrav.
- Ersätt oqualificerade uttryck som “Godkänt”, “produktionsklar” och “tillverkningsfiler redo” med korrekt design-review-språk när fysisk release är falsk.
- Behåll nedladdningen, men märk paketet som designgranskningspaket.
- Visa revision, manifesthash och tillgängliga artifactdata utan att återinföra dokumentuppladdningskrav.

Acceptans: `physical_cutting_authorized=false` får aldrig presenteras eller uppfattas som fysisk produktionsrelease.

#### P0.2 Generell assembly-kollisionsgrind

- Kontrollera exakt positiv solidvolym mellan varje delpar i den auktoritativa CAD-kedjan.
- Tillåt endast volymöverlapp som motsvarar en deklarerad och geometriskt verifierad fog/feature.
- Tillåt nollvolymkontakt.
- Rapportera del-ID, volym och bounding box deterministiskt.
- Kör alla sex mallar genom geometrigrinden, utan att uppgradera konceptmallarnas capability.

Acceptans: injicerad front-/botten-/sockelkollision blockerar; giltiga screenade modeller passerar; resultatet är reproducerbart.

#### P0.3 DXF-enheter

- Skriv `$INSUNITS=4` och `$MEASUREMENT=1`.
- Kontrollera enhet och bounds med en oberoende parser.

Acceptans: varje DXF deklarerar millimeter och ett känt 300 mm-mått återläses som 300 mm.

#### P0.4 GLB i meter

- Konvertera positioner och accessor-bounds från millimeter till meter.
- Konvertera volymmått konsekvent.
- Lägg ett oberoende loader-/parser-test.

Acceptans: 300 mm blir 0,300 m och inga gamla tester cementerar millimeter i glTF.

#### P0.5 Kantbandskedjan

- Bevara tjocklek och orientering genom domän, adapter, BOM och export.
- Mappa globalt synliga kanter till rätt lokala kant för varje panelorientering.
- Blockera saknat katalogmaterial/SKU i stället för att hitta på det.

Acceptans: varje deklarerad kant landar på rätt fysisk kant och råmåttsaritmetiken är verifierad.

#### P0.6 Fryst DesignSpec och provenance

- Lägg checksummebunden `design-spec.json` och resultatsammanfattning i leveransen.
- Bind design-, engine-, regel-, capability- och paketschemaversioner entydigt.
- Döp om tvetydiga `template_version`-fält.

Acceptans: leveransen kan rekonstruera exakt ingångsspec och verifiera dess hash.

#### P0.7 BOM, inköp, etiketter och QA

- Behåll instansbaserad cut list men komplettera med grupperad BOM.
- Skapa inköpslista från faktisk nesting: använda skivor × skivformat, inte summan av delarnas blankyta.
- Ge varje placement en etikett och ett entydigt QR-/ID-spår.
- Skapa maskinläsbart QA-protokoll som täcker alla delar, operationer och toleranser med tomma mät-/resultatfält.

Acceptans: grupperade totalsummor stämmer mot instanser, inköpsyta stämmer mot nesting och inget QA-resultat är förifyllt som godkänt.

#### P0.8 Monteringssäkerhet och startinformation

- Beräkna vikt och dimension för hela rörliga monteringsgrupper, inte bara enskilda delar.
- Ange minsta antal personer och blockera osäker konsumentmanual över tröskel.
- Lägg `START_HERE`, verktygsförteckning, säkerhetsgränser och tydlig status för ej specificerade jiggar/infästningar.

Acceptans: tung grupp kan inte passera med enbart per-delsvarning och manualen börjar inte direkt med råa XYZ-kommandon.

#### P0.9 Maskinneutral setup-plan

- Säkerställ B-sida före A-sida där ordningen kräver det.
- Säkerställ genomgående konturer sist globalt, inte bara inom en setup.
- Blockera tvåsidig bearbetning när registreringspunkter saknas.

Acceptans: planen är semantiskt säker men påstår inte att WCS, pinnar, fixtur eller postprocessor är verifierade.

#### P0.10 Full releasegrind för screenade mallar

- Kör domän-, geometri-, CAD-, CAM-, dokument-, API-, frontend- och liveflöden i en fryst ephemeral miljö.
- Verifiera package hash, artifact inventory och fail-closed-status.
- Redovisa en separat reproducerbar engine-smoke med explicit materialprofil, och verifiera dessutom actual-default-flödenas stocklösa review-paket utan syntetisk materialdata.
- Kör även de verkliga UI-standarderna för `shelving` och `wall-library` genom CAD- och manufacturinggrinden och bevara den exakta blockeraren.
- Behåll `physical_cutting_authorized=false`.

Acceptans: båda actual-default-standarderna skapar ett lyckat, strikt stocklöst granskningspaket med `READY_FOR_DESIGN_REVIEW`, CAM `BLOCKED`, exakt singleton `STOCK_PROFILE_MISSING`, noll nesting/CAM och oförändrad vald stock-/maskinkontext. Råämnesbehovet bevaras och `physical_cutting_authorized=false`. Detta är design-review PASS, inte CAM- eller verkstads-PASS.

### P1 — en fullgod visuell möbelstudio med befintliga kontrakt

#### P1.1 Navigation utan dead ends

- Varje menyval ska öppna verkligt innehåll eller tas bort.
- Aktiv markering ska motsvara synligt läge.
- Lägg skip-länk, korrekt `aria-current` och fokusflytt till ny lägesrubrik.

#### P1.2 Exponera redan verkliga modellparametrar

- Underskåpshöjd och modulantal.
- Planerad last och förstärkningsläge.
- Individuella fackbredder och hyllplaceringar.
- Progressiv sektion för verkliga avancerade egenskaper.

Ingen kontroll för beslag, ytskikt eller produktion får visas som auktoritativ innan motsvarande kontrakt finns.

#### P1.3 Förbättra direktmanipulation

- Visa två intilliggande fackmått vid dividerdrag.
- Visa mått över/under vid hyllflytt.
- Ge varje pointeroperation ett numeriskt och tangentbordsstyrt alternativ.
- Visa ghost preview och förklaring innan automatiska strukturändringar accepteras.

#### P1.4 Kontextuell selection

- Hel möbel, modul, fysisk del och befintliga semantiska komponenter ska ha tydlig kontext.
- Egenskapspanelen ska inte ersätta hela arbetsflödet när en del väljs.
- Bevara selection, kamera och modellidentitet mellan Studio och Check.

#### P1.5 Visuell validering

- Förankra problem till berörda delar.
- Visa orsak, aktuellt värde och verifierad gräns.
- Förhandsgranska stödd autofix och genomför den som en enda ångringsbar transaktion.
- Visa den föreslagna strukturändringen som en jämförbar 3D-ghost före separat bekräftelse.

Acceptans: implementationen och dess statiska verifiering är gröna; kvarvarande acceptans är runtime-E2E för den verkliga WebGL-interaktionen.

#### P1.6 URL, reload och browserhistorik

- Synka projekt och arbetsläge till URL.
- Stöd deep link, reload och back/forward.
- Falla säkert tillbaka när projektet saknas eller inte är åtkomligt.

#### P1.7 Read-only versionshistorik

- Visa verklig draftrevision, immutable versioner och status från servern.
- Skilj utkast, fryst designrevision, designvaliderad och fysisk release.
- Lägg inte till restore/fork innan backend stöder det.

#### P1.8 Tillgänglighetsmatris

- Axe i alla fyra lägen och centrala delstater.
- Tangentbordsresa från Explore till nedladdat design-review-paket.
- 400 % reflow, forced colors, reduced motion och mobil.
- Ingen dragoperation utan alternativ kontroll.

#### P1.9 Visuell regression

- Deterministiska `toHaveScreenshot`-baselines för Explore, Studio, Check och Build.
- Desktop och mobil.
- Layoutmått för bibliotek, canvas, inspector och bottom sheet.

#### P1.10 Prestandabaslinje

- Mät normal 5×5-modell och maximal stödd modell.
- Mät dragning, serverpreview, selection och lägesbyte.
- Sätt regressionsgränser innan eventuell worker-/stateomskrivning.
- Gata den fulla kanoniska frontendgränsen `6000 × 4000 × 1200 mm`, 40 hyllor, 16 avdelare/17 fack, 17 underskåp och 752 delar i verklig WebGL-runtime.

#### P1.11 Kod- och CSS-städning sist

- Bryt ut projekt/session, preview, kommandohistorik och navigation ur den stora workspace-komponenten.
- Ta endast bort komponenter och CSS som bevisats oanvända efter beteende- och bildgrindar.
- Redovisa mätbar bundle-/CSS-minskning.

#### P1.12 Fail-closed projektpersistens

- Låt strikt `spec_json` vara ensam auktoritet för produktionspåverkande geometri.
- Begränsa `workspace_spec` till ett versionerat och strikt validerat V1-intent utan duplicerade produktionsfält.
- Avvisa fel projekt, ogiltiga svar, orimliga resursmängder, dubbla del-ID:n och geometri utanför modellens orienterade AABB före första resolve/render.
- Bevara korrupt lokal rådata i karantän och tillåt lokal fallback endast vid ett uttryckligen identifierat transportfel.

#### P1.13 Versionerat designkontrakt och domänförsvar

- Lås den publicerade designytan i ett fingerprintbundet kontrakt som skiljer designbarhet från tillverkningsfrisläppning.
- Verkställ samma statiska gränser och möbelfamiljsinvarianter i domänen, API-normaliseringen och workerns läsning av frysta revisioner.
- Låt regelmotorn stanna olöst vid ett nått strukturellt tak i stället för att föreslå eller skapa ett värde utanför kontraktet.
- Vidga först Studio, direktmanipulation, Explore och referensflödet efter att den fulla kontraktsgränsen har klarat motor- och WebGL-prestandagrinden.

### P2 — domänutvidgningar som kräver produktbeslut

Följande hör till visionen men får inte simuleras enbart i frontend:

1. Intelligenta zoner och deterministisk zon-till-assembly-kompilering.
2. Dörrar, dubbeldörrar, lådor, vitrindörrar, klädstänger, kabelzoner och belysning.
3. Per-delmaterial, ytskikt, faner, lack, metall, materialriktning och kantprofil.
4. Verkliga gångjärn, montageplattor, lådskenor, skruv, dymling, låsbeslag, sockelfästen och väggankare.
5. Full X-ray från samma `ManufacturingFeature`- och AssemblyGraph-data som produktionen.
6. Interaktiv monteringsguide och monteringsanimationer härledda ur verkliga beroenden.
7. Riktiga mottagarpaket för fabrik, inköp och montör.
8. Text- och bild-AI som endast producerar strukturerad intention och aldrig produktionsgeometri.
9. Revision restore/fork, projektdelning, arkivering och duplicering.

Varje punkt behöver först ett versionsatt domän-/API-kontrakt, migrationsplan och säkerhetsägare.

### P3 — fysisk release och publik drift

Detta arbete kräver externa system och ansvariga människor:

- material- och lastklass med leverantörsbevis
- beslag, infästning och hållande fogsystem
- maskin, verktyg, WCS, pinnar, clamps, spoilboard och tabs
- maskinspecifik postprocessor och provkörning
- prototyp, belastningsprov och namngivet konstruktörsgodkännande
- TLS, extern OIDC, secrets manager, observability, larm och incidentrutiner
- offsite/encrypted backup och dokumenterad restore drill
- registry, image-signering, SBOM/provenance och digestpromotion

## 6. Genomföranderegler

1. Små segment: ett sammanhängande kontrakt per patchserie.
2. Läs före edit och bevara all befintlig användarkod i den smutsiga arbetskopian.
3. Test först eller samtidigt för säkerhetsinvarianter.
4. Fokuserade tester efter varje segment; full svit först när segmentet är stabilt.
5. Ingen permanent testmiljö krävs. Använd ephemeral containrar och lämna dem inte kvar.
6. Ingen produktionsdeployment, databasmutation eller extern meddelandeåtgärd utan separat uttryckligt godkännande.
7. Inga fabricerade beslag, material, priser, kapacitetstal eller maskindata.
8. Fysisk kapning förblir avstängd även när ett tekniskt segment blir grönt.
9. Varje segment rapporterar filer, testresultat, kända begränsningar och nästa säkra steg.

## 7. Referensresor som slutacceptans

### Resa A — välj en design

Välj en verklig parametrisk design, öppna den direkt i Studio, ändra yttermått och komposition, kontrollera modellen och skapa ett tydligt avgränsat design-review-paket.

### Resa B — börja tomt

Öppna en minimal fungerande stomme, lägg till hyllplan och avdelare via modell eller tangentbord, justera exakta mått, ångra och återuppta efter reload.

### Resa C — förstå och lösa ett problem

Öppna Check, välj ett problem i modellen, jämför stödda lösningar, förhandsgranska ändringen, acceptera den som en transaktion och återgå till samma kamera/selection.

### Resa D — säker leverans

Skapa en immutable designrevision, verifiera artifactinventering och hash, visa uttryckligen att fysisk kapning inte är auktoriserad och ladda ned ett reproducerbart granskningspaket.

## 8. Pågående ordning

Arbetet startar med P0.1–P0.4 eftersom de kombinerar hög säkerhetsnytta med tydliga, testbara kontrakt. Därefter följer provenance, BOM/QA/manual/setup och först sedan de bredare produktsegmenten i P1. P2 och P3 startas inte utan de uttryckliga beslut som listas ovan.

Detaljerad nulägesanalys finns i `docs/FULL_APPLICATION_READINESS_2026-08-14.md`. Den tidigare segmentlistan finns i `docs/OVERNIGHT_EXECUTION_PLAN_2026-08-14.md`; detta dokument är den kanoniska sammanslagna listan.

## 9. Genomförandelogg

Senast uppdaterad: 2026-08-17. Statusen nedan beskriver arbetskopian; synkroniserad lokal deployment och liveacceptans återstår tills den frysta källgrinden är grön.

| Segment | Status | Verifierad leverans och kvarvarande gräns |
|---|---|---|
| P0.1 | Genomfört | Build visar designgranskning, manifest, artifacts och externa krav fail-closed. `custombuild.workshop-readiness.v2` kräver exakt sex mjukvarukrav, fjorton externa verkstadskrav samt villkorat kantbandskrav, säkra scope-värden och `physical_cutting_authorized=false`; ofullständiga eller motsägelsefulla payloads avvisas i API och webb. API:t binder dessutom jobbets readiness bytekanoniskt och semantiskt till det storleksbegränsat lästa readinessobjektet och dess exakta manifest-v5-post. Fysisk kapning visas permanent som ej frisläppt. |
| P0.2 | Genomfört | Exakt OpenCascade-kollisionsgrind blockerar odeklarerad positiv solidvolym och binder till verifierade fogar. |
| P0.3 | Genomfört | DXF är AC1027, deklarerar millimeter och återläses med oberoende `ezdxf`-kontroll. |
| P0.4 | Genomfört | GLB lagrar positioner, bounds och volym i meter/m³ och verifieras av en separat loader. |
| P0.5 | Säkert delmål genomfört | Lokal kant, orientering och tjocklek bevaras. Saknat SKU, mekanisk fästmetod och godkänd råmåttskompensation är externa krav och blockerar fysisk release. |
| P0.6 | Genomfört | Paketet innehåller checksummebunden DesignSpec/resultatsammanfattning och entydiga domän-, capability- och paketschemaversioner. Liveacceptansen kräver nu manifest v5, samtliga context-hashfält och en obruten queued-jobb → completed-jobb → resultat → manifest-kedja. |
| P0.7 | Genomfört med villkorad stockdel | Instans-BOM och grupperad BOM finns alltid i granskningspaketet. Nestingbaserat inköpsunderlag, placementbundna etiketter och operationsbaserad QA finns endast när exakt stock-, grain- och CAM-kontext har passerat; de utelämnas avsiktligt i stocklösa/grain-blockerade paket. |
| P0.8 | Genomfört för designgranskning | Manualen börjar med `START_HERE`, redovisar gruppvikt/-mått, minsta personantal, verktyg och olösta jiggar. Kund-/fysisk montering är fortsatt ej auktoriserad. |
| P0.9 | Genomfört när CAM-förutsättningarna finns | Stockurval körs först, därefter strukturerad grain-bindning och sedan tvåsidig registrering. Först när dessa passerar planeras B-sida före A-sida och genomgående kontur sist. Ingen koordinat, WCS, fiberaxel eller fixtur härleds. |
| P0.10 | Design-review PASS; CAM BLOCKED utan matchande stock | En separat MDF-engine-smoke passerar. De faktiska standarderna skapar ett strikt stocklöst granskningspaket med `READY_FOR_DESIGN_REVIEW` och exakt `STOCK_PROFILE_MISSING`. CAD, fryst design, rå DFM och råämnesbehov bevaras; stockinköp, nesting och alla CAM-beroende artefakter saknas avsiktligt. Vald stock- och maskinprofil muteras inte och `physical_cutting_authorized=false`. |
| P1.1 | Genomfört | Endast de fyra verkliga produktlägena återstår; skip-länk, `aria-current` och kontrollerad fokusflytt är browsertestade. |
| P1.2 | Genomfört | Studio exponerar befintliga underskåps-, last-, förstärknings-, fack- och hyllparametrar utan nya domänpåståenden. |
| P1.3 | Genomfört | Dragning visar faktisk position och fria mått mot verkliga grannytor; saknad motyta markeras som ej beräkningsbar. |
| P1.4 | Genomfört | Möbel- och delkontext kan växlas utan att vald fysisk del, kamera eller isolate försvinner; explicit avmarkering rensar valet. |
| P1.5 | Genomfört | Jämförbar ghost för strukturändringar är implementerad och verifierad med 391/391 tester, full TypeScript/ESLint, oberoende reviewer-PASS och 2/2 Chromiumfall i verklig produktionsbuild. WebGL och SVG visar samma `BASE-SUPPORT-001`-förslag; avbryt muterar inget, tangentbordsbekräftelse ger en sparning/undo-transaktion och selection, frontvy, canvas och modellrot bevaras. |
| P1.6 | Genomfört | URL bär endast tillgängligt projekt och canonical produktläge; deep link/reload/back/forward är sanerade utan fokusstöld eller historikloop. |
| P1.7 | Genomfört | Underlag visar read-only serverhistorik, immutable revisioner och fail-closed fysisk status. Hash- och revisionskonflikter hämtar nu ny preview/serverrevision, rensar stale downstream-state och kräver ett nytt uttryckligt klick utan automatisk version eller sparning; restore/fork/compare saknas avsiktligt. |
| P1.8 | Genomfört | Axe 2.2 AA utan undantag, tangentbordsresa, 320 px reflow, forced colors, reduced motion och alternativa kontroller är verifierade. |
| P1.9 | Genomfört | Åtta deterministiska baselines täcker Utforska, Studio, Kontroll och Underlag i desktop/mobil. Två separata jämförelsepass är pixelrena; mobil toolbar, komponentpalett, toast och beständig WebGL-canvas är samtidigt regressionsskyddade. |
| P1.10 | Genomfört | Den verkliga WebGL-grinden passerar hela den kanoniska frontendgränsen `6000 × 4000 × 1200 mm`, 40 hyllor, 16 avdelare/17 fack, 17 underskåp och 752 delar. Mätvärden: cold `2428.2 ms`, orbit p95 `19.1 ms`/max `20.5 ms`, selection p95 `419 ms`, lägesbyte `399.6 ms`, transparens `849.8 ms`, long task `447 ms` och 0 context loss/errors. |
| P1.11 | Säkert delmål genomfört | Fyra bevisat oanvända panel-/configuratorfiler och deras tester har tagits bort, totalt 102 509 filsystembyte. Workspace-uppdelning och slutlig CSS-rensning återstår efter kontrakts- och bildgrindar. |
| P1.12a | Genomfört | Frontend hydratiserar endast strikt server-spec, binder svaret till begärt projekt, begränsar alla preview-resurser, verifierar unika server-ID:n och full orienterad AABB samt karantänsätter ogiltiga lokala utkast utan autosave. |
| P1.12b | Genomfört | API:t accepterar och lagrar endast strikt `custombuild.workspace-intent.v1`; explicit legacy `1.0` migreras via exakt whitelist och måste matcha den validerade produktionsspecen. OpenAPI exponerar endast V1. |
| P1.13a | Genomfört | `custombuild.design-constraints.v1` låser publicerad envelope, dynamiska/familjebundna invarianter och ett deterministiskt SHA-256-fingerprint. Kontraktet deklarerar uttryckligen `physical_cutting_authorized=false`. |
| P1.13b | Genomfört | Domänen verkställer kontraktets statiska tak och möbelfamiljer; API:t återvaliderar efter korrigering, regelmotorn går aldrig över 16 avdelare och workern blockerar felaktiga frysta revisioner terminalt före build eller objektsskrivning. |
| P1.13c | Genomfört | Aktiva gränser i Studio, direktmanipulation, autofix, planering, mallval, referensimport och semantiska tillägg använder det kanoniska kontraktet. Verklig WebGL passerar `6000 × 4000 × 1200 mm`, 40 hyllor, 16 avdelare/17 fack, 17 underskåp och 752 delar; bildtolkningens lägre inference-tak är fortsatt tydligt separata. |

**Historisk checkpoint, ersatt av uppdateringen 2026-08-17:** Sammanslagen checkpoint efter P0.1–P0.9: 254 Python 3.13-domän/CAD/manufacturing/API-tester godkända och en avsiktlig tillgänglighetsgren hoppades över. P0.10 hade då en separat reproducerbar MDF-engine-smoke PASS, medan de verkliga UI-standarderna endast hade CAD PASS och manufacturing BLOCK på `STOCK_PROFILE_MISSING`; fysisk release var fortsatt falsk. Frontendens ghost-implementation verifierades med reviewer-PASS, produktionsbuild och verkliga Chromiumfall. Efterföljande testantal och source-manifesthashar i detta stycke bevaras som historisk evidens och ska inte användas som aktuell releaseidentitet.

Readiness-v2-checkpointen passerar 144/144 backend-, API- och workerregressioner, strikt Mypy/Ruff och en oberoende slutgranskning. Webbparsern och dess tester är promoterade byte-identiskt från den granskade skuggkopian, men full TypeScript/Vitest/Chromium efter promotion återstår tills Docker-data har flyttats från den fulla C:-disken och en verifierbar frontendmiljö kan startas. Detta är en kvarvarande QA-grind, inte ett tillverkningsgodkännande.

Den publika liveacceptansen kräver dessutom `validation/workshop-readiness.json`, verifierar full v2-semantik och binder payloaden bytekanoniskt till jobbresultatet; manifestdriften från v1 till v5 är stängd med kontraktstester. API:t kräver nu exakt `authoritative_geometry is True` och DFM-status `PASS|WARNING`, även vid release, och evidens-ID:n canonicaliseras före context-hashning så omvänd ordning återanvänder samma jobb.

Den lagrade readinesskedjan verifieras med en enda bounded GET vardera för readiness (64 KiB) och manifest (8 MiB), strikt canonical JSON, exakt `application/json`, v1/v2-validering, manifest-v5-kontext och oförändrad 409/503-semantik. Den fristående release-readinessrapporten har samtidigt en AST-baserad `PRODUCTION_SEMANTIC_CONTRACT` som blockerar manifest-, readiness-, worker-, API- och liveacceptansdrift. Kontrollen är bunden till verkliga returvärden och dominerande guards, inte kommentarer eller säkra skendictar.

Följande är fortfarande externa stoppvillkor och får inte markeras som lösta av autonom kod: material-/lastklass, beslag och väggankare, verifierat torrt självlåsande eller mekaniskt hållande förbandssystem, maskin/verktyg/WCS/fixtur/registrering för verkligt jobb, prototyp-/lastprov samt namngivet operatörs- och konstruktörsgodkännande. `physical_cutting_authorized=false` gäller fortsatt.
