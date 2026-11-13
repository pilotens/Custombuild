# Frontendarkitektur för den sammanhängande designarbetsytan

## Nuvarande grund

Webbappen är en Next.js 16/React 19-applikation med två arbetsytor:

- `/`: `CustombuildWorkspace` äger bokhylleutkast, lokal undo/redo, projektval,
  serverpreview och de fyra produktlägena Utforska, Studio, Kontroll och Underlag.
- `/furniture`: `FurnitureWorkspacePage` använder `FurnitureWorkspace` för bord,
  byråer och hyllor med separata material-, beslag-, tillverknings- och lagerprofiler.
  Den har egna draft-, historik- och exportkontrakt under `/v1/furniture`.

Three.js renderas via React Three Fiber och Drei. Den monterade modellcanvasen på `/`
bevaras mellan produktlägen så att kamera och selection inte tappas. De två routerna
är inte utbytbara serialiseringar av samma utkast; befintliga projekt får inte skrivas
över vid ett routebyte.

`DesignSpec` är den gemensamma redigerbara designmodellen. `ResolvedDesign` är en beräknad vy av
den modellen och märks med `source: "local" | "server-preview"`. UI-state är medvetet separat:
`WorkspaceUiState` version 2 innehåller `mode` (`explore`, `studio`, `check`, `build`),
vy, selection och panelstatus. Den får aldrig spridas in i `DesignSpec`. Äldre sexstegs-
och guided/free-state läses endast genom den explicita migreringen i
`workspace-ui-state.ts`; de är inte aktuella navigationsmodeller.

## Lokal persistens

`workspace-draft-storage.ts` skriver nya snapshots under det identitets- och projektskopade
prefixet `custombuild:workspace:v3`. En snapshot innehåller:

- `spec`, `templateId`, `workspaceSelected` och planeringsbrief,
- en sanerad `uiState`,
- lokalt `updatedAt`.

Läsaren söker först v3 och därefter det gamla v2-prefixet. Ett giltigt v2-utkast läses som v3
med säkra UI-standardvärden. Ogiltiga UI-fält återställs individuellt; ett användbart designutkast
kastas alltså inte bort bara för att presentationsstate är trasig. Inloggad state är fortsatt
isolerad per organisation, användare och projekt. Den anonyma arbetsytan har ett separat scope.

Integrations-API:

- `readWorkspaceDraft(storage, identity, projectId)` returnerar `WorkspaceDraftSnapshot | undefined`.
- `writeWorkspaceDraft(storage, identity, projectId, snapshot)` tar `WorkspaceDraftWriteInput`;
  `uiState` är valfri under övergången och fylls då med standardvärden.
- `sanitizeWorkspaceUiState(value)` är gränsen för JSON från lagring.
- `DEFAULT_WORKSPACE_UI_STATE` är en fullständig säker startstate.

## Preview och produktionssanning

Redigering på `/` passerar `planWorkspaceDesignTransaction` innan modellen går till
historik, lokal lagring eller serverpreview. Samma gräns används för fält, dragning,
komponenter och startmallar. Planeraren använder befintliga regeltrösklar: den lägger
till bärande avdelare, samordnar underskåpsstöden och beräknar tillräckligt skivantal.
Den sänker inte lasten eller ändrar önskade yttermått för att få ett godkänt resultat.
Om ingen fungerande konstruktion finns inom modellens gränser avvisas ändringen;
föregående modell och historik behålls. Nödvändiga stöd och bakstycken är låsta i UI.

Automatiskt skivantal är planerat materialbehov, aldrig verifierat lager. Exakta
workshop-profiler och uttryckligt valda antal ökas inte automatiskt. En lokalt fungerande
design innebär inte att infästning, beslag, foglåsning eller produktion är godkända.
Äldre utkast repareras när det kan göras utan att kasta bort designintent; utkast som
inte kan repareras behålls för inspektion med sina verkliga regelutfall. Undo/redo får
inte återinföra en konstruktion som inte längre passerar redigeringsgränsen.

Kvarstående begränsning: denna redigeringsgräns delar inte automatiskt möbelns
genomgående topp, botten och sockel i byggbara sektioner. Ett väggbibliotek på
4 300 mm stoppas därför med standardprofilens arbetsområde och skivformat, även
om hyllspannen har tillräckliga stöd. Automatisk sektionsindelning med modellerade
sammanfogningar och motsvarande servergeometri, ritningar och kaplistor återstår.
Ingen komplett dokumentuppsättning för den konstruktionen har genererats eller
verifierats. Uppdateringen är inte ett färdigt tillverkningsstöd.

Lokal `resolveDesign` ger omedelbar visuell feedback. Den är aldrig produktionsauktoritativ.
`POST /v1/designs/preview` och `/autofix` returnerar serverns geometri, designhash och regler.
Frontend får bara skapa en produktionsrevision när aktuell `ResolvedDesign.source` är
`server-preview` och dess hash motsvarar den redigerade modellen.

Produktionsfiler skapas endast av serverjobbet från en fryst revision. Paketets manifest och
workshop-readiness behåller `physical_cutting_authorized: false`; leveransen är ett
design-review-underlag, inte ett automatiskt kapptillstånd.

## Verifiering och underlag i gränssnittet

Kontroll och Underlag är två av de fyra produktlägena på `/`. De presenteras som
projektytor bredvid samma modell, inte som en separat guide eller release-dialog.
Navigationens tillgängliga namn är `Produktlägen`. Underlag är en inbäddad yta;
redesignens visuella tester säkerställer att ingen `Skapa underlag`-dialog öppnas.

Användarstatus översätts konsekvent till `Godkänt`, `Behöver beslut` och `Måste lösas`, alltid med
både ikon och text. Regel-ID, version, beräkningsspår, antaganden och diagnostik ligger i
stängda `Tekniska detaljer`. En varning är informativ tills användaren bekräftar den enda
checkboxen. Ett `BLOCK` stoppar fortsatt skapande. Efter lyckad servergenerering visas ZIP-filen
direkt i en underlagskortlik yta, utan evidence-upload, CAM-godkännande eller release-steg.

DOM-kontraktet använder namngiven produktnavigation, en statusregion för verifieringen
och `role="alert"` för krav som måste lösas. Checkboxens label och knapparnas synliga
namn är stabila test- och tillgänglighetskontrakt. Canvasidentitet, modellrot och
renderingsrevision verifieras över lägesbyten i `redesign-visual-evidence.spec.ts`.

## Backendkontrakt som måste bevaras

1. Alla API-anrop är bearer-autentiserade och tenantfiltrerade. Klientens local/session storage
   får inte användas som auktoritet för organisation, roller, approvals eller release.
2. `GET/PUT /v1/projects/{id}/draft` använder `expected_draft_revision`. En 409-konflikt får inte
   skrivas över tyst. `workspace_spec` bär frontendens utökade designintent, men UI-state förblir
   lokal och skickas inte som canonical `spec`.
3. Preview/autofix tar endast `BookcasePreviewInput`. `DesignSpec`-fält som är rena UI- eller
   konceptfält filtreras av `toPreviewRequest`.
4. `POST /versions` fryser `spec`, `production_context`, template capability, source provenance,
   serverns `expected_design_hash` och `expected_current_revision`. Hash- eller revisionskonflikter
   kräver ny synkronisering.
5. `/validate` får aldrig passera en serverregel med `BLOCK`. Varningar kräver en explicit design-
   approval vars `warning_overrides` matchar exakt serverns aktuella WARNING-regler. Tomma
   `evidence_ids` är tillåtna i det förenklade användarflödet.
6. `/generate` kräver designvalidering, aktuell design-approval och exakt samma frysta
   produktionskontext. Jobbets idempotency- och context-hash får inte ersättas med klient-ID.
7. Jobbstatus och artefakter återställs från `/production-state`, `/jobs/{id}` och
   `/jobs/{id}/artifacts`. Signerade `download_url` får inte långtidslagras; de hämtas om före
   nedladdning. Klienten ska validera artefakt-ID, SHA-256, storlek och URL-form.
8. Referensbilder lagras först oföränderligt via `/imports/inspect`. Revisionens provenance måste
   fortsätta binda import-ID, bildhash och bekräftad modellfingerprint.
9. Serverägda template- och joint-capabilities får inte ersättas av klientpåståenden.
10. Release-kontraktet och CAM-approval finns kvar för spårbar låsning även om den förenklade
    UI:n i dag erbjuder direkt ZIP efter ett lyckat design-review-jobb.

## Multi-family-arbetsytan och återstående gap

`/furniture` har redan serverkatalog, profiljämförelse, hyllförslag, modulplanering,
revisionsskyddad sparning och historik. Bord och byråer har riktig granskningsgeometri
men inte kvalificerade beslag eller tillstånd till skärande CAM. Dessa funktioner ska
inte längre beskrivas som helt saknade. Läs `MULTI_FURNITURE_V1_IMPLEMENTATION.md`
för den exakta avgränsningen.

Följande behov kräver fortfarande separata kontrakt och får inte simuleras i UI:

- projektuppdatering/arkivering/radering/duplicering, favoritmallar och senaste exporter,
- serverpersistens av planeringsbrief, rumsbegränsningar, hinder, toleranser, designriktning,
  anteckningar och delning,
- batchad interaktionsmutation med `clientMutationId` och explicit basrevision för en enda
  backendmutation vid avslutad dragning,
- fullständig revisionsjämförelse och återställning/fork av historisk immutable revision,
- kommersiella priser, ytkatalog och live-lagerstatus (inte den befintliga materialkatalogen),
- flera servergenererade koncept med jämförbara material-, last- och komplexitetsmått,
- kvalificering av dörrar/lådor/beslag för produktion samt belysning och egna delar,
- artefaktmetadata/preview för individuella leveranser och en endpoint som kan visa den faktiska
  versionsbundna monteringsmanualen utan att först ladda ned hela ZIP-paketet,
- serverpersistens av kamera/paneler om exakt återupptagning ska fungera mellan enheter (v3-state
  löser endast samma webbläsare).

Tills dessa kontrakt finns ska frontend visa ärliga tom- eller koncepttillstånd och inte skapa
parallella, produktionsliknande lokalfiler.

## Utvecklingsriktning

Nya möbelfamiljer hör hemma i multi-family-domänen. Återanvänd den gemensamma
jobb-, lagrings- och produktionskedjan; skapa inte en tredje parallell pipeline.
Bokhyllearbetsytans beprövade granskningsflöde behålls tills en explicit, testad
migrering bevarar utkast, revisioner, importprovenance och workshop-spärrar.
Bryt ut nya ansvar i fokuserade moduler i stället för att fortsätta växa
`api.py`, `production-workflow.tsx` eller den globala stilmallen.
