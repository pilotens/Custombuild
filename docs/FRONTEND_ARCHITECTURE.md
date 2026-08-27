# Frontendarkitektur för den sammanhängande designarbetsytan

## Nuvarande grund

Webbappen är en Next.js 16/React 19-applikation med en enda route (`/`). `CustombuildWorkspace`
äger i dag designutkastet, lokal undo/redo, projektval, serverpreview och editorpresentation.
Three.js renderas via React Three Fiber och Drei. En framtida uppdelning i fler vyer bör behålla
samma monterade modellcanvas inom projektarbetsytan så att kamera och selection inte tappas.

`DesignSpec` är den gemensamma redigerbara designmodellen. `ResolvedDesign` är en beräknad vy av
den modellen och märks med `source: "local" | "server-preview"`. UI-state är medvetet separat:
`WorkspaceUiState` innehåller sexstegsnavigation, guided/free-läge, vy, selection och panelstatus.
Den får aldrig spridas in i `DesignSpec`.

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

Lokal `resolveDesign` ger omedelbar visuell feedback. Den är aldrig produktionsauktoritativ.
`POST /v1/designs/preview` och `/autofix` returnerar serverns geometri, designhash och regler.
Frontend får bara skapa en produktionsrevision när aktuell `ResolvedDesign.source` är
`server-preview` och dess hash motsvarar den redigerade modellen.

Produktionsfiler skapas endast av serverjobbet från en fryst revision. Paketets manifest och
workshop-readiness behåller `physical_cutting_authorized: false`; leveransen är ett
design-review-underlag, inte ett automatiskt kapptillstånd.

## Verifiering och underlag i gränssnittet

Verifiering och underlag är steg 5 respektive 6 i samma arbetsflöde som resten av designen. De
presenteras därför som vanliga projektytor, inte som ett separat release- eller bevisflöde.
Underlagsvyn behåller projektets sexstegsnavigation och modellen kan ligga synlig bakom den
sidoliggande dialogen.

Användarstatus översätts konsekvent till `Godkänt`, `Behöver beslut` och `Måste lösas`, alltid med
både ikon och text. Regel-ID, version, beräkningsspår, antaganden och diagnostik ligger i
stängda `Tekniska detaljer`. En varning är informativ tills användaren bekräftar den enda
checkboxen. Ett `BLOCK` stoppar fortsatt skapande. Efter lyckad servergenerering visas ZIP-filen
direkt i en underlagskortlik yta, utan evidence-upload, CAM-godkännande eller release-steg.

DOM-kontraktet använder `ProjectHeader` för projektrubriken, `StepNavigation` med
`aria-label="Projektets sex steg"`, en namngiven statusregion för verifieringen, ett `role="alert"`
för krav som måste lösas och ett riktigt dialogfokus för underlagsytan. Checkboxens label och
knapparnas synliga namn är stabila test- och tillgänglighetskontrakt.

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

## Verkliga API-gap för redesignen

Följande behov finns inte i nuvarande kontrakt och bör lösas explicit, inte simuleras i UI:

- projektuppdatering/arkivering/radering/duplicering, favoritmallar och senaste exporter,
- serverpersistens av planeringsbrief, rumsbegränsningar, hinder, toleranser, designriktning,
  anteckningar och delning,
- batchad interaktionsmutation med `clientMutationId` och explicit basrevision för en enda
  backendmutation vid avslutad dragning,
- revisionsjämförelse och återställning/fork av historisk immutable revision,
- serverlistor för material/ytor, priser och lagerstatus,
- flera servergenererade koncept med jämförbara material-, last- och komplexitetsmått,
- utökad parametrisk komponentdomän för dörrar, lådor, fronter, beslag, belysning och egna delar,
- artefaktmetadata/preview för individuella leveranser och en endpoint som kan visa den faktiska
  versionsbundna monteringsmanualen utan att först ladda ned hela ZIP-paketet,
- serverpersistens av kamera/paneler om exakt återupptagning ska fungera mellan enheter (v3-state
  löser endast samma webbläsare).

Tills dessa kontrakt finns ska frontend visa ärliga tom- eller koncepttillstånd och inte skapa
parallella, produktionsliknande lokalfiler.
