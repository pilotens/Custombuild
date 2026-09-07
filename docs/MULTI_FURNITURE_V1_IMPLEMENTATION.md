# Möbeltyper och utbytbara profiler

Arbetsytan `/furniture` har en gemensam versionshanterad modell för hyllsystem,
gavelbord och byråer. Den nås från startsidan. Befintliga projekt fortsätter i sin
ursprungliga arbetsyta; deras utkast skrivs inte över av den nya modellen.

## Vad som fungerar

| Del | Implementerat beteende |
| --- | --- |
| Design | Heltalsmått i µm, stabila delidentiteter, yttermått och familjespecifika konstruktionsparametrar. |
| Hyllsystem | Befintlig konstruktionsmotor, fogar och regelscreening återanvänds. |
| Bord | Skiva, två gavlar och två sargar. Separat beräkning av nedböjning och böjspänning. |
| Byrå | Stomme samt sex trädelar per låda, frontspel, lådsidespel och sammanhängande utdragsgrupper. |
| Material | Versionsbundna MDF-/björkplywoodprofiler, uppmätt tjocklek och valfri batchidentitet. Inget automatiskt ersättningsmaterial. |
| Beslag | Utbytbara dimensionsprofiler. Tillverkarens beslag, kapacitet och hålbilder är fortfarande obligatoriska före tillverkning. |
| Verkstad | Separat profil med råskivemått och fiberriktning; kontroll av maskinområde och varje dels råmått. Referensprofiler är uttryckligen okvalificerade. |
| Profilbyte | Konsekvensjämförelse före tillämpning. Material/beslag räknar om geometrin. Maskinbyte behåller möbeldesignen och ogiltigförklarar berörd tillverkningsgranskning. |
| Revisioner | Serverlagring med konfliktkontroll och oföränderlig revisionshistorik. Återöppning av tidigare revision skapar en ny revision vid sparning. |
| Export | Beställning av exakt sparad revision via transaktionell kö. CAD-kärnan kontrollerar verkliga solider, kollisioner och formatåterläsning. |

Granskningspaketet innehåller STEP, GLB, DXF/SVG för delarnas båda sidor,
delritningar i PDF, BOM, kaplista, rörelsegrupper, modell, profiler och
kontrollrapport. Ett manifest binder varje fil med SHA-256. Exportstatusen binds
till organisation, projekt, revision, design och profilval. Paketet är tillgängligt
i en timme; en sparad revision kan exporteras på nytt.

Paketet har formatet `custombuild.furniture-review.v1`. Det innehåller ingen
skärande CAM och kan inte användas som en godkänd produktionsversion. Bordets och
byråns delritningar markerar att beslag/hålbilder/infästningar inte är verifierade.
Den befintliga separat verifierade CAM-kandidatkedjan för Hyllsystem behåller sina
krav på retention, material, maskin, verktyg, uppspänning och verkstadsacceptans.

## Driftsättning och verifiering

Kör ordinarie migrering till `0021_furniture_review_reads` före API/worker.
Migreringen ger API:t SELECT på tenant-avgränsad historik och köbeställningar.
API:t får fortsatt inte ändra/radera auditposter eller ändra köleveransstatus.
PostgreSQL FORCE RLS och de befintliga rollkontrollerna gäller även dessa läsvägar.
Ingen ny extern tjänst behövs; exporten använder befintlig Redis, Celery och CAD-worker.

API-ytan är `/v1/furniture/catalog`, `/preview`, `/profile-change` samt
`/projects/{id}/draft`, `/history`, `/exports` och `/exports/{job_id}` under samma
prefix. OpenAPI beskriver inmatningens versionsbundna scheman.

Regressionerna omfattar faktisk hyllhöjd vid tippscreening, unika hyllrader,
familjernas geometri, renderade delmått mot CAD-underlaget, profilbyten, tillåtna mått, CAD-export, revisionskonflikter,
tenantavgränsning, gamla/nya utkast och exporternas identitet. Webbläsartestet
`e2e/furniture-live.spec.ts` kör hela kedjan mot Compose: alla tre familjer,
tjockleksbyte, sparning, återöppning och nedladdning från den riktiga CAD-workern.

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
