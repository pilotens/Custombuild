# Slutleveranslogg · bokhylla

## Låst första flöde

Den sparade hyllfamiljen på `/furniture?family=shelving` använder bokhyllekompilatorn. Första konstruktionsprovet avser rektangulär stomme med fasta hyllplan i spår och infälld spårmonterad rygg. Bredd, höjd, djup, fackandelar, hyllnivåer och jämnt utbredd nyttolast är parametrar. Ingen körbar CAM eller vald maskin behövs för granskningspaketet. Andra editorer och tidigare sparade projekt har egna ingångar.

Redigeringsgränserna i den befintliga modellen är B 250–6000, H 300–4000, D 100–1200 mm, 0–40 hyllrader, 0–16 avdelare, upp till 5000 N per hel rad. Dessutom kontrolleras bl.a. fria öppningar, överlapp, hyllavstånd, materialets faktiska tjocklek och högst 128 delar per export. Gränserna är programvarans indatadomän. Tillåten last beror på spann, material och upplag. Inget generellt fysiskt lastintervall är kvalificerat.

Stommen i referensen är nominellt 18 mm massiv amerikansk svartvalnöt. Sekundärt ryggmaterial är modellens 6 mm björkplywood tills kundens val fastställs. 4340 × 2540 × 280 mm är kundens yttermått inklusive list i bredd. 90 × 20 mm är listprofilen. Placering och sex reserverade utrymmen saknas. Underlaget innehåller ingen fysisk list. Exemplet `walnut-shelving-4340x2540x280.json` är ett separat scenario med tre väggar och antagna avdrag. Det representerar inte dessa okända mått. Luckor, underskåp, modulfogar, justerbara hyllbärare och alternativa rygg- eller sockellösningar ingår inte i första kvalificeringen.

## Verifiering och nästa arbete · 2026-11-07

Utgångspunkt: PR #26, `19d0d66`. Senaste arbete finns på `fix/bookcase-workshop-handoff`; använd `git log -1` och kontrollerna för exakt SHA. Huvudflödet reproducerades i byggd Chromium-klient, FastAPI och CAD-worker med SQLite och testobjektlager. Den hämtade referensen hade björkplywoodstomme och en monteringsöversikt som hänvisade vidare till separat beredning. Den rättade exporten använder valnöt och skickar ut monteringsgrafens steg och maskinoberoende bearbetningsdata. Gamla färdiga exportjobb återanvänds inte för den nya paketrevisionen.

Nivå A och B är öppna. Prioritet är mekanisk låsning och kontroll av åtkomst, materialrörelse, långt råvirke/modulval och en verifierad lastväg. Kvalificera först en uttrycklig förbandsvariant med geometri och borrbilder. Knyt provresultat till dess material, spann, last och montagesätt. Återanvänd dem sedan för order inom styrkta gränser. Blank provruta innebär okänt. Inga fysiska prov är gjorda.
