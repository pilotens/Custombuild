# Segmenterad nattplan för Custombuild

Datum: 2026-08-14
Syfte: ett autonomt arbetsprogram som kan köras sekventiellt över natten utan att uppfinna verkstadsdata eller aktivera fysisk tillverkning

> **Policyuppdatering 2026-08-17:** [Adhesive-free joining policy](ADHESIVE_FREE_JOINING_POLICY.md)
> är styrande. Tidigare limalternativ i denna historiska plan är indragna.

> **Genomförandeuppdatering 2026-08-17:** A7/A8-artefakter som kräver verklig
> stock, placement eller operationer är villkorade. Vid
> `STOCK_PROFILE_MISSING` eller `DFM-GRAIN-001` innehåller granskningspaketet
> fryst design, CAD, rå DFM och delbaserade behov, men utelämnar stockinköp,
> nesting, placementindex, operationer, setup, backplot, maskinkod och
> operationsbaserad QA. Ingen lagerprofil, maskin eller fiberaxel fabriceras.

## 1. Grundregler

Varje segment är litet, granskningsbart och avslutas med eget testgat innan nästa börjar. Hela kön kan köras i en sammanhängande nattkörning, men den ska stoppa på första oförklarade säkerhets-, data- eller paritetsfel.

Nattkörningen får:

- ändra källkod, tester och dokumentation inom definierat scope
- bygga tillfälliga testimages och en isolerad, icke-persistent QA-miljö
- generera lokala testartefakter och granskningsbevis
- köra fulla statiska tester, integrationstester och browsertester
- skapa en slutrapport och exakt källmanifest

Nattkörningen får inte utan nytt uttryckligt godkännande:

- deploya till den körande produktionsmiljön
- migrera eller skriva produktionsdata
- radera backup, produktionsvolymer eller rollback-images
- skapa skärande G-code
- sätta `physical_cutting_authorized=true`
- välja gångjärn, ankare, mekaniska fästdon, maskin, verktyg eller leverantörs-SKU på egen hand
- committa, pusha eller öppna PR

## 2. Körmodell

För varje segment:

1. Frys aktuell källhash och berörda filer.
2. Lägg först ett felande regressionstest där det är möjligt.
3. Gör minsta sammanhängande implementation.
4. Kör fokuserade tester, typkontroll, lint och diff-check.
5. Spara segmentets resultat i nattens evidenskatalog.
6. Fortsätt bara om resultatet är grönt eller en avvikelse är bevisat orelaterad.

Slutgaten använder en ny tillfällig QA-stack eftersom den permanenta testmiljön är borttagen. Den körande produktionen används endast för read-only baslinje och berörs inte.

## 3. Nattpaket A — säkerhet och leveranssanning

Detta paket bör vara första nattkörningen.

### A0. Baslinje och isolering

Utfall:

- dokumentera Git HEAD, source manifest, lock-hashar och aktuell produktionshälsa
- verifiera den senaste backupens manifest och payloadhashar read-only
- skapa en unik, tillfällig Compose-identitet och temporära volymer för QA
- kontrollera ledigt diskutrymme före byggstart

Acceptans:

- produktionsmiljön förblir 7 av 7 healthy och omstart 0
- QA kan inte nå eller montera produktionsvolymer
- source manifest ändras endast när ett avsett segment ändrar källan
- backup eller rollback-resurser modifieras inte

### A1. Sanningsenlig Underlag-vy

Berör främst:

- `apps/web/components/production-workflow.tsx`
- `apps/web/components/production-drawer.tsx`
- `apps/web/components/workspace-navigation.tsx`
- komponent- och E2E-tester

Ändring:

- visa `design_review_ready`, `physical_cutting_authorized` och saknade workshopkrav
- ersätt oqualificerat `Godkänt` med `Designgranskning klar`
- visa permanent `Ej frisläppt för fysisk kapning` när flaggan är falsk
- märk ZIP som design-review-paket
- visa manifestidentitet, checksumma, storlek och roll utan att återinföra bevisuppladdningsformulär

Acceptans:

- UI får aldrig säga eller antyda “tillverkningsklar” när backend returnerar `false`
- download fungerar fortfarande
- konceptmodeller kan inte skapa produktionsrevision
- skärmläsare får samma statusinformation som visuell användare

### A2. Generell assembly-clash-gate

Berör främst:

- `cad/src/custombuild_cad/adapter.py`
- domän-/manufacturing-pipeline
- CAD-, domän- och integrationstester

Ändring:

- kör exakt parvis OCC/B-rep-intersektion över den placerade assemblyn
- tillåt bara volymöverlapp som motsvarar en explicit modellerad fog och dess verifierade skärgeometri
- rapportera part-ID, fog-ID, överlappsvolym och bounds
- blockera jobbet före artefaktpaketering vid odeklarerad kollision

Acceptans:

- återinförda gamla front-/botten-/sockelkollisioner blockeras
- alla sex mallars geometri och ett 17-modulsfall passerar envelope-test
- endast de två screenade mallarna kan gå vidare till revision/underlag
- ingen broad allowlist eller numerisk specialregel döljer verkliga överlapp

### A3. DXF-enheter

Berör främst:

- `packages/manufacturing/src/custombuild_manufacturing/exporters.py`
- export- och bundle-tester

Ändring:

- lägg `$INSUNITS=4`
- lägg `$MEASUREMENT=1`
- behåll millimeterkoordinater och deterministisk byteordning

Acceptans:

- varje DXF deklarerar millimeter
- en oberoende DXF-parser läser rätt bounds och lager
- A/B-filerna fortsätter ha samma feature-identitet
- deterministiskt paket ger samma bytes vid omkörning

### A4. GLB i meter

Berör främst:

- `cad/src/custombuild_cad/adapter.py`
- GLB-parser/validator och CAD-tester

Ändring:

- konvertera POSITION-data och accessor-bounds från mm till meter
- uppdatera volymkontroll med faktor `1e9`
- behåll delnamn och nollställ onödiga nodtransforms

Acceptans:

- en 300 mm dimension mäts som 0,300 m i oberoende glTF-loader
- en 4200 mm möbel mäts som 4,200 m
- meshantal, namn, bounds och volym matchar STEP inom definierad tolerans
- ingen visuell klient behöver kompensera med en dold faktor

### A5. Kantbandsgeometri och databevarande

Berör främst:

- domänens kantbandsmodell
- `packages/manufacturing/src/custombuild_manufacturing/adapters.py`
- `packages/manufacturing/src/custombuild_manufacturing/exporters.py`
- DFM- och exporttester

Ändring:

- mappa global kant till panelens lokala U/V-axlar
- bevara kantbandstjocklek genom adapter och manifest
- blockera en kant som sammanfaller med tjockleksnormal eller inte kan mappas
- skapa explicit `MISSING_CATALOG_EVIDENCE` för SKU/färg/mekanisk fästmetod i stället för att hitta på värden

Acceptans:

- FRONT på en vertikal sida hamnar på den verkliga vertikala framkanten
- alla roller täcks av tabelltester och roterade paneler
- summerad längd kan härledas från exporterad geometri
- ingen råmåttskompensation görs utan ett beslutat tillverkningskontrakt

### A6. Fryst DesignSpec och tydliga versioner

Berör främst:

- worker-paketering
- produktionsmanifest/schema
- API- och bundle-tester

Ändring:

- lägg checksummebunden `design/design-spec.json`
- lägg normaliserad `design/resolved-summary.json`
- bevisa att DesignSpec återger samma designhash
- särskilj `domain_template_version`, `product_template_version` och `capability_registry_version`
- behåll referensbildsproveniens separat när den finns

Acceptans:

- varje paket kan rekonstruera exakt designhash utan databasåtkomst
- `source_provenance` är inte längre den enda källidentiteten
- versioner kan inte misstolkas som en konflikt mellan 1.1.0 och 1.0.0
- manifest- och API-schema migreras fail-closed

### A7. Grupperad BOM och verklig skivinköpslista

Berör främst:

- manufacturing-exporters
- worker-dokument
- bundle-tester

Ändring:

- behåll instansunik cut-list
- skapa grupperad BOM via geometri/material/feature/edge-band-fingerprint
- skapa `stock-purchase.csv` från använda nestingark och verkliga stockprofiler
- särskilj finished area, blank area och purchased sheet area

Acceptans:

- grupperad mängdsumma är exakt lika med antal instanser
- varje cut-list-instans pekar på en BOM-grupp
- inköpsytan är summan av använda hela skivor, inte delarnas area
- material-, tjockleks-, grain- och kantbandsvarianter grupperas aldrig ihop av misstag

### A8. QA-plan och etiketter

Berör främst:

- `services/worker/custombuild_worker/documents.py`
- nya CSV/JSON-artefakter
- PDF- och bundle-tester

Ändring:

- QA-plan per del och kritisk feature/operation
- fält för nominellt, tolerans, metod/instrument, mätvärde, operatör, datum, resultat och avvikelse
- inga förifyllda PASS/OK
- etikett per placerad instans med revision, designhash, material, grain, sida, sheet och nestingposition
- QR innehåller checksummebunden instansidentitet, inte bara part-ID

Acceptans:

- varje tillverkad instans får exakt en etikett
- varje operation och kritisk feature finns i QA-planen
- långa ID:n förblir entydiga i BOM, QA och etikett
- PDF renderas utan klippning och CSV/JSON är den maskinläsbara sanningen

### A9. Monteringssäkerhet och `START_HERE`

Berör främst:

- AssemblyGraph-analys
- `services/worker/custombuild_worker/documents.py`
- manual- och bundle-tester

Ändring:

- beräkna total vikt och bounds för varje rörlig grupp, inte bara enskilda delar
- blockera kundmanual om gruppen överskrider konservativt definierad gräns
- ange rekommenderat personantal som screening, aldrig certifiering
- gör panel-jig till explicit katalogkrav eller ta bort det ospecificerade verktygsnamnet
- skapa `START_HERE.pdf` med scope, inventering, arbetsyta, risker, roller och tydlig design-review-status
- ersätt X/Y/Z och IN/FIX med kundnära språk, med tekniska detaljer i separat verkstadsbilaga

Acceptans:

- 105 kg-gruppen kan inte passera obemärkt
- varje namngivet verktyg/jigg finns i katalog/BOM eller markeras som blockerad
- manualen börjar med översikt och inventering
- fysisk tillverkning är fortsatt avstängd

### A10. Maskinneutral setup-ordning

Berör främst:

- `packages/manufacturing/src/custombuild_manufacturing/operations.py`
- DFM och setup-tester

Ändring:

- B-sida före A-sida när båda behövs
- genomgående konturer sist globalt för samma del
- blockera tvåsidig setup utan explicit registreringsstrategi
- exponera saknade WCS/pinnar/klämning som externa krav

Acceptans:

- inga konturer frigör delen före återstående sidbearbetning
- tvåsidig del utan verifierad registrering får inte beskrivas som körbar
- valideringsprogrammet är fortfarande safe-Z, G0 och M5
- ingen leverantörsspecifik skärkod skapas

### A11. Slutgat för nattpaket A

Kör minst:

- full Ruff och mypy
- full pytest inklusive CAD/manufacturing/API/integration
- frontend TypeScript och ESLint
- full Vitest
- Playwright för Utforska, Studio, Kontroll och Underlag
- generera och inspektera paket för `shelving` och `wall-library`
- geometri-only regressionsprov för alla sex mallar
- manifest-/ZIP-reproducerbarhet
- PDF-rendering och text-/encodingkontroll
- source manifest, diff-check och release-readiness

Stop-gat:

- ingen deploy om någon regression, odeklarerad kollision, standardenhetsavvikelse, manifestdrift eller falsk tillverkningsstatus finns
- slutrapporten ska lista exakta testresultat, artefakthashar och kvarstående externa krav

## 4. Nattpaket B — produktkvalitet

Detta kan köas efter paket A i samma långa körning, men bör inte blandas in i säkerhetsfixarna. Varje del är fortsatt separat.

### B1. Navigation utan dead ends

- mappa varje global knapp till verkligt innehåll eller ta bort den
- korrekt `aria-current`
- fungerande Hjälp och Inställningar i alla lägen
- skip-länk och fokus på ny lägesrubrik

### B2. Exponera redan verkliga parametrar i Studio

- underskåpshöjd och modulantal
- lastvärde
- auto/manuell förstärkning
- individuella fackbredder och hyllplaceringar
- inga kontroller för omodellerade beslag eller tillverkning

### B3. URL, reload och browserhistorik

- projekt, läge och Utforska-undervy i URL
- säkra deep-links
- back/forward och reload utan tappat arbete

### B4. Read-only revisionshistorik

- visa verkligt draftrevisionsnummer och synkstatus
- använd befintligt `listVersions()`
- skilj utkast, fryst revision, designvaliderad och released
- inga falska restore-knappar innan restore/fork-API finns

### B5. Full a11y-matris

- axe i alla lägen och viktiga delstater
- komplett tangentbordsresa
- 400 procent reflow, forced colors och reduced motion
- tangentbordsalternativ till all dragning
- stabilt fokus runt canvas och lägesbyte

### B6. Verkliga visuella regressioner

- `toHaveScreenshot`-baselines, inte bara sparade PNG-filer
- desktop och mobil för alla fyra lägen
- deterministiska masker endast för genuint dynamisk canvasdata

### B7. Prestandabaslinje

- normal 5 × 5-modell
- maximal 17-facks/30-hyllorsmodell
- mått-, hyll- och avdelardragning
- serverpreview under tät interaktion
- inga arkitekturomskrivningar utan profileringsbevis

### B8. Kod- och CSS-städning sist

- dela `custombuild-workspace.tsx`, `production-workflow.tsx` och `design-engine.ts`
- flytta relevanta äldre kontroller till Studio
- radera endast bevisat död kod och CSS
- kräv samma visuella baselines och mätbar bundle-/CSS-minskning

## 5. Arbete som inte får ingå i en autonom nattkörning

Följande kräver externa beslut eller fysisk verifiering:

- val av gångjärn, montageplatta, lådskena, handtag och borrmönster
- sockelinfästning och väggankare för en känd väggtyp
- torrt självlåsande montage, skruv, plugg, låsbeslag eller annat verifierat mekaniskt förbandssystem
- kantskydds-SKU, färg, mekanisk fästmetod och faktisk råmåttskompensation
- certifierad material- och lastklass
- transportmoduler, maxvikt, personantal och lyfthjälpmedel som arbetsmiljöbeslut
- verklig CNC-maskin, postprocessor, verktyg, WCS, pinnar, clamps, spoilboard och tabs
- referensdel, fogkuponger, prototyp och lastprov
- namngiven operatörs- och konstruktörsattest
- val av hosting, IdP, domän, certifikat, secret manager och observabilityplattform
- beslut om vilken konceptmodell som ska få full domän först

## 6. Förväntat resultat efter båda nattpaketen

Om A och B passerar helt får Custombuild en betydligt starkare och sanningsenlig design-review-produkt:

- återkommande geometri- och kollisionsfel blockeras generellt
- externa format har rätt enheter
- paketet kan rekonstrueras från fryst DesignSpec
- BOM, skivinköp, QA, etiketter och manual är praktiskt användbara
- Underlag överdriver inte säkerhet eller tillverkningsstatus
- frontendflödet är tydligare, mer tillgängligt och regressionsskyddat
- releasekandidaten kan frysas och granskas reproducerbart

Det gör fortfarande inte paketet fysiskt tillverkningsauktoriserat. Nästa nivå kräver de uttryckliga produkt-, leverantörs- och verkstadsbesluten i avsnitt 5.
