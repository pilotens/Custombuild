# Slutleveranslogg · bokhylla

## 18 november 2026 · hela fackmått och fysiskt delprov · v24

Utgångspunkt är v23 på `73a696b` i [PR 28](https://github.com/pilotens/Custombuild/pull/28).
På `/furniture`, öppna sexluckorsbokhyllan och välj **Kundens yttermått och listutrymme → Använd lika fack i hela millimeter**.
Förslaget gäller jämnt uppdelade fack med känd sidmån och samstämmig installationsbredd.
Servern visar alternativ i hela och halva mm när de ger olika mindre stommar. Vid användning
fördelas överskottet mellan sidmarginalerna. Ändringen sparas som en vanlig designrevision.
Mätningar med flyttade väggreferenser måste göras om. Befintliga kundproportioner bevaras.

Prov B behåller utrymmet 4340 × 2540 × 280 mm och listen 90 × 20 mm. Sidmånen blir 31 mm
på vardera sidan och stommen 4278 × 2500 × 260 mm. Sex fria fack på 692 mm ger
30 hyllplan på 704 mm och sex luckor på 688 mm. Toppen och mellanbotten blir 4254 mm.

För fast bokhylla med J95, GN 970, infälld rygg och minst ett hyllplan ingår
`inspection/fackprov.zip` i granskningspaketet. Börja med `Provprotokoll.pdf`.
Prov B väljer fack 3 och ger sju fullstora orderdelar, 14 DXF, separat STEP samt tomt mätblad.
Utgångsordern utan måttändring väljer fack 2, en av de två bredaste luckorna.
Det numeriska orderprovet kontrollerar även att batchens minsta lucktjocklek efter avdrag
för osäkerhet uppfyller J95-kravet 18,00 mm. Övre gränsen för detta 18-mm-prov är 18,10 mm.

Kör `pytest tests/unit/test_furniture_dimensions.py tests/integration/test_bookcase_workshop_release.py tests/integration/test_cabinet_door_export.py`.
Webbprovet `sex underskåpsluckor` tillämpar B, hämtar paketet och kontrollerar en ändrad revision.
Aktuell commit och Linux-/webbkörningar finns i [PR-kontrollerna](https://github.com/pilotens/Custombuild/pull/28/checks).
Nästa fysiska åtgärd är att provmontera detta fack i en fixtur i faktisk batch, mäta spårspel,
ryggens sidoinfångning och lucksvep samt återföra mätosäkerhet och klimat i mätbladet.
Glappbudget, fullhöjdsstabilitet och kvalificeringsregister för hela möbeln kräver fortsatt provning.

## Fortsättningspunkt · fast bokhylla v23 · PR #28

V22 på `69d52eb` klarade [samtliga CI-flöden](https://github.com/pilotens/Custombuild/pull/28/checks).
I v22 visade order-API:t en order med avklippt installations-PDF som tillgänglig när
rapportens kontrollsumma stämde. Verkstadens mätblad saknade också ett fält för
mätosäkerhet, trots provanvisningen. V23 läser provrapporternas sidor före frisläppning.
Förstagångskontrollens CSV anger revision, konstruktionsgränser och plats för mätosäkerhet.
Prova felaktig och inskannad rapport med `test_customer_can_open_the_qualified_installation_report`.
Webbprovet kontrollerar bladet i de två faktiskt hämtade revisionerna.
[Verifiering och ZIP-filer för aktuellt PR-huvud](https://github.com/pilotens/Custombuild/pull/28).
Nästa externa steg är kodgranskning av PR-huvudet och de fysiska proven nedan.
Tillåtna variationer och de mekaniska provkraven följer samma regler som i v22.

## Fast bokhylla v22

Utgå från v21 på GitHub (`affd6a3`). V22 lägger till orderbundna mätblad för samtliga
J95-luckor. I kvalificerade order levereras den verifierade provrapporten.
Kontrollera PR-huvudets [körningar](https://github.com/pilotens/Custombuild/pull/28/checks)
före leverans. Liveprovet i `furniture-live.spec.ts` sparar sexluckorsordern, ändrar
till sju fack och hämtar båda revisionerna från samma kundprojekt. Båda filerna
bevaras i CI-artefakten `bookcase-workshop-trial-packages`. Om ett jobb faller,
öppna första felsteget på aktuell SHA. Starta där vid nästa pass.

**Första leveransens omfattning.** Stående, fast bokhylla med infälld plywoodrygg
och GN 970-låsning. Referensutförandet har 18 mm massiv valnöt, 6 mm björkplywood,
ingen sockel och 700 mm hög underskåpsrad. En vänsterhängd J95-lucka per fack,
660 × 18 mm i höjd/tjocklek, projekterat 2 mm spel, högst 800 mm bredd och
25 kg per lucka enligt den implementerade beslagsprofilen. Familjen kan också
generera öppna hyllor. Kundmått inklusive list är 4340 × 2540 × 280 mm (B/H/D).
Reservationerna är 30 mm per sida, 40 mm upptill och 20 mm bakåt. Dessa
är konstruktionsval. Den befintliga listen 90 × 20 mm ligger på tre väggar.
Det faktiska montagerummet och bärande underlaget bakom gipset återstår att mäta.

| Acceptansfall | Förväntat beteende och prov |
| --- | --- |
| Ändrad bredd, höjd, djup och layout | Editorns yttre inmatningsgränser är 250–6000, 300–4000 respektive 100–1200 mm, 0–40 hyllrader, 0–16 avdelare. Tillverkbar geometri, spann, beslag, last och max 128 exporterade delar begränsar kombinationerna ytterligare. Dessa gränser ger inget fysiskt kvalificerat intervall. |
| Kundens valnöt, sex till sju fack | Ny 3D-visning, ritningsrevision och nya borr- och mätrader per lucka. Sex resp. sju luckor, tolv resp. fjorton gångjärn. Historiska revisioner förblir identifierbara. |
| Kortare råämnen och uppdelning | Varje del kontrolleras mot vald fiber och format. Staplad 6 × 2-plan behåller sex luckor på golvraden. Separata modulförband och staplingslast ingår inte i denna leverans. |
| Breddintervall, batch och plats | Med isolerade provdata prövas båda ändpunkter, ett mellanvärde och en mikrometer utanför. Ändrad batch/plats, saknad rapport och återkallelse ska spärra verkstadsorder och hämtning. Se `test_bookcase_workshop_release.py`. |
| Montering och geometri | Ordinarie worker återläser STEP, DXF och dokument, kontrollerar fogar, måttkedjor och monteringsbana. `test_cabinet_door_export.py` provar även en skadad fil och en saknad mätrad. |

**Till extern provning.** Kunden anger platsmått och väggens uppbyggnad. Verkstaden
bekräftar tillgänglig batch, två obrutna råämnen om minst 4256 mm längs fibern samt
resterande delar i kaplistan. Provansvarig fastställer bruksfukt, cykelkrav,
infästningslast och verklig provfixtur. Använd orderns `inspection/provprotokoll.txt`,
`inspection/kvalificeringsprov.json`, `inspection/fogprov.csv` och för J95 även
`inspection/gangjarnsprov.txt` samt `inspection/luckprov-per-fack.csv`.
Råformatet och det längsta fria spannet ändras med ordern. Registret för verkliga
typunderlag är tomt. A kräver godkänt kundflöde och bygg/drift för samma revision.
B kräver dessutom verkligt provunderlag för de kombinationer som ska frisläppas.

## 14 november 2026 · sexluckorsordern från möbelstudion, v21

Välj **Öppna valnötsbokhylla med sex gångjärnsluckor · J95 och GN 970** i
`/furniture`. Det ger stommen 4280 × 2500 × 260 mm för rummet
4340 × 2540 × 280 mm. Den sparade referensordern ger 62 trädelar, sex
luckor, tolv J95-gångjärn och 91 GN 970-vinklar. Ryggens täckning visas per
sektion. Sex övre fack har rygg, sex underskåp är öppna bakåt. De två längsta ämnena måste vardera
rymma 4256 mm längs fibern i den valda valnötsbatchen.

I modulplanen följer skåpen golvraden och behåller beställt antal.
Ett rutnät som ändrar antalet fack returnerar varken modulutkast eller
kaplista. Stapling och förband mellan separata moduler behöver egen prövning.
Monteringsöversikten återger samma mikrometermått som kaplistan.
Manualen placerar vinkelbladen före luckorna. Varje luckblad visar
borrbilderna parvis för gavel och lucka samt hänvisar till Sugatsunes anvisning.

Beställningsstatus beräknas för sparad revision och visar orsaken vid avslag.
En betrodd profil kan ange `width_range` med två provgränser och en separat,
kontrollsummerad rapport som täcker hela breddintervallet. Systemet jämför
ändpunkternas konstruktionsidentiteter, kontrollerar båda ändpunkterna och
prövar den faktiska orderns last, spann, materialbatcher, beslag och plats.
Giltigheten kontrolleras även vid körning och hämtning. Utan intervall är
profilen bunden till exakt konstruktion. Produktregistret saknar verkliga
provrapporter. Exemplet kan hämtas som granskningspaket.

## Sexluckorsbokhyllan med provmonterade GN 970-vinklar, v20

Sexluckorsordern på 4280 × 2500 × 260 mm har prövats mot den exporterade STEP-modellen.
I v19 kunde monteringssteg 14 inte föra in en avdelare utan kollision. Den gamla
följden krävde 4282 mm höjd. V20 monterar de nedre facken först, placerar ryggarna
före avdelarna och för in avdelare och hyllor framifrån. De 55 raka stomrörelserna
ryms nominellt inom 2518 mm höjd och 248 mm framför stommen. Det återstår 22 mm
höjdmarginal mot kundens angivna 2540 mm. Kontroll av lucksvep och fixturer ingår
i det fysiska provet. Faktiska platsmått och väggens infästning saknas i referensen.

Kombinationen J95 och GN 970 är ett val i möbelredigeraren. Referensens revision 2
använder en GN 970-ALM-30-30-30-B med två DIN 96 A4 5×16 per bärande stomfog.
Alla 91 vinklars 182 pilothål är knutna till respektive motdel och måttkontrolleras
i DXF och STEP. Skruvaxlarna vid mellanbotten ligger på skilda djupstationer.
Den limfria konstruktionen har 24 separata ryggspår. Montera vinklarna när
stommen är sammanfogad och stödd, innan luckorna hakas på. En vinkel med rätt
hålbild saknar fortfarande uppmätt brukskapacitet i referensens virke.

Verkstadsordern verifierar levererade material, råformat i fiberriktning, tjocklek
och batch mot en kvalificerad profil. Massivträ kräver mätvärden för rörelsemån
över installationsplatsens fuktintervall. För J95 tillkommer en spårbar sjätte
rapport och mätresultat för varje fack avseende massa, minst 93 graders öppning,
positiv frigång inklusive osäkerhet, antal cykler enligt avsedd användning samt skruvfäste.
Trösklarna för cykler och skruvlast kommer från den betrodda provspecifikationen.
En order kan inte lägga till en profil. Ny order, arbetskö och hämtning av en äldre
fil använder samma kontroll av den exakta konstruktionen, platsen, batcherna och
rapporternas giltighet. Produktregistret är tomt. Ett godkänt automatiskt test
med temporära provdata visar att flödet fungerar. En verklig verkstadsorder för
referensen är spärrad tills användningsmiljön och egenskaperna har verifierats.


**14 november 2026 · beslagskontrollen för valnötsbokhyllan, v19.** Vid
återläsning av sexluckorsordern från `13a62b8` angav förhandsvisningen att
gångjärn återstod att välja. Samtidigt angav monteringsmanualen J95 och
verkstadsfilen hade en tom lista över beslagsprov. Regel CB-HARDWARE-001
anger nu valt gångjärn, platta och skruv samt 2 mm nominellt spel.
Verkstadsfilen anger återstående prov av passning, frigång mot grannlucka
och vägg, skruvfäste samt beständighet vid belastning och ändrad fuktkvot.
Provprotokollet länkar till den orderspecifika gångjärnsprovningen.
Beslagsvalet har en egen kontrollsumma i förhandsvisningen. En ny export
av en tidigare sparad order använder paketversion v19.

Omprov: `pytest tests/unit/test_cabinet_doors.py tests/unit/test_rule_engine.py
tests/integration/test_cabinet_door_export.py -q`. Testerna omfattar äldre
underskåp utan valt gångjärn, öppna hyllor, ändrat antal fack och överensstämmelse
mellan sparad order, förhandsvisning och nedladdad ZIP.

**14 november 2026 · gångjärnen i verkstadens förbindelselista.** Utgångspunkt
`4977f05` på main. På referensordern angav PDF-manualen tolv gångjärn,
men `assembly/connections.csv` saknade alla tolv. Exportformatet
`custombuild.shelving-workshop-review.v18` ger en rad per gångjärn.
Varje rad anger lucka, gavel, fysiska sidor, tre hål på luckan, fyra på gaveln,
en monteringsplatta och sex skruvar. Monteringsöversiktens delparstabell anger
två gångjärn per lucka. Varje luckas genomgående fingergrepp redovisas i
delritningen. Det ingår inte i gångjärnets infästning.

Workern matchar förbindelserna mot hålens återlästa DXF-lägen och mot
beslagsraden på rätt luckas monteringsblad. Testet för sex luckor går från
sparad order till nedladdad ZIP. En separat order med två olika breda fack
provar fem avsiktliga fel i listan. Utebliven eller dubblerad rad, fel gavel,
fel borrhänvisning och fel skruvantal ska stoppa utlämningen före lagring.
Omprov: `pytest tests/integration/test_cabinet_door_export.py -q`.
Fysisk typkvalificering av luckrörelse, skruvfäste och stomme återstår.

**13 november 2026 · återupptaget bygge och luckornas monteringsblad, v18.**
Fortsättningen utgår från `3a7b8fe` på PR #27 och behåller samma bokhyllefamilj och
kundmått. Den tidigare CI-körningen hade två konkreta regressionsfel: webbens
containerbygge saknade den importerade arbetsfilen för de sex gångjärnsluckorna,
och dokumenthjälparen läste `spec.parameters` även när specifikationen saknades.
Bygget kopierar nu även `bookcase-walnut-base-700.json`, och samma fil ingår i
det strikta källmanifestets byggkontrakt. Dokumenthjälparen hanterar
saknad specifikation och godtar endast den exakta J95-artikeln för luckinstruktionen.
Ett okänt eller versionslöst beslag får fortsatt inget monteringsbesked.

**Lucka och motdel på samma monteringsblad.** Den visuella genomgången av det
verkliga API-paketet hittade texten ”0 förband” på ett steg som samtidigt listade
två gångjärn. Varje sådant blad anger nu två gångjärn mellan den aktuella luckan
och dess namngivna gavel. Samma versionsbundna koppling styr beslagsraden.
Exportkontrollen läser tillbaka rätt luckas egen PDF-sida och kräver rätt motdel
och gångjärnsantal där. Den avvisar den äldre nollförbandstexten på lucksteget.
Övriga monteringssteg behåller sina kontroller. Hela hyllpaketets exportversion
är höjd till `custombuild.shelving-workshop-review.v17`, så tidigare exportjobb
inte återanvänds för det nya underlaget.

**Verifiering och fortsättningspunkt.** Dokumentregressionen omfattar saknad
specifikation, saknade parametrar, okänd gångjärnsprofil och samtliga sex luckors
monteringsblad. Kör `uv run --locked --group dev --group cad pytest
tests/unit/test_worker_documents.py tests/unit/test_cabinet_doors.py
tests/integration/test_cabinet_door_export.py -q`, sedan `make check`.
Fulla Linux-byggen och de två nedladdade kundrevisionerna verifieras i
[PR #27:s körningar](https://github.com/pilotens/Custombuild/pull/27/checks).
Ett separat prov med validerad arbetsfil, 1054,001 mm stomme och 20/80-fack gav
luckor på 196,000 respektive 796,001 mm samt 30 återlästa borrningar. Den publika
indatakontrollen avvisar redan sockel, flyttbara hyllor och utanpåliggande rygg
för denna gångjärnsprofil; dessa begränsningar har inte ändrats.

Detta rättar programbygget och monteringsunderlaget. Underlaget för fysisk
typkvalificering av luckrörelse, skruvfäste, stomme och belastning är fortfarande
det som beskrivs i v17 nedan. Inget provresultat eller produktionstillstånd har
lagts till genom denna kodändring.

**9 november 2026 · underskåp med gångjärn, v17 på PR #27.** Utgångspunkt `173de247`. I möbelstudion under Hyllsystem finns **Öppna valnötsbokhylla med sex gångjärnsluckor**. Kundens rum är 4340 × 2540 × 280 mm, listen 90 × 20 mm. Stommen i detta uttryckliga måttalternativ är 4280 × 2500 × 260 mm. Varje fack har en separat infälld vänsterhängd lucka i en 700 mm hög underskåpsrad. Ryggen är infälld i den övre hyllsektionen. Underskåpsraden är öppen bakåt. Konstruktionsvalen är fast hylla, ingen sockel, 18 mm massiv valnöt, två självstängande Sugatsune J95-C24/0T-NI och två J95-P6T-NI med sammanlagt tolv SPAX YELLOX 5 × 16 per lucka samt ett genomgående fingergrepp. Appen avvisar annan lucktjocklek, okänt beslag och luckor bredare än 800 mm. En extra avdelare räknar om hela beslagsuppsättningen. Det äldre referensutkastet på startsidan länkar till möbelstudion.

| Referens med sex fack | Digitalt kontrollerat i ordinarie export |
| --- | --- |
| Luckor | 6 st, 688,333–688,334 × 660 × 18 mm. Beräknad vikt 4,981 kg/st före borrning. |
| Borrning per lucka | Två koppar Ø40 × 15 mm med centrum 27 mm från vänsterkant och 80 mm från ändarna. Fyra skruvpiloter i luckans baksida. Åtta skruvpiloter i den vänstra gavelns högra sida med Y 60/86 mm och Z ±16 mm kring gångjärnets höjd. Grepp Ø30 genom luckan. |
| Paket | Återimport av 62 träsolider och samtliga 124 DXF-sidor. Kontroll av del-ID, mått och genomgående/blinda bearbetningar, PDF-delblad, BOM, monteringssteg och sparad orderrevision. 12 gångjärn, 12 plattor, 72 skruvar och 90 borrningar inklusive 6 grepp. |
| Felprov | Flyttad DXF-cirkel med oförändrade beskrivande fält, annan revision och borttagen platta i beslagslistan ska stoppas. Direkt API-anrop för verkstadsritning får 409 med orsak. En manipulerad borrning under workerjobbet ska ge noll uppladdade objekt. |
| Manuellt fynd och omprov | Borrbilden var rätt men monteringsbladet hade kvar texten ”FRONT EJ MONTERINGSBAR”. Det ersattes med artiklar och antal för respektive lucka. Readback kräver nu en korrekt beslagsrad per lucka och avvisar motstridig PDF-text. |

**Frisläppning.** Geometri och beslag är underlag för ett mekaniskt verkstadsprov. Ingen skarp produktion eller kvalificerad orderfamilj för de nya luckorna. Katalogens 2 mm spalt och borrbild kontrolleras i modellen. Fabrikens test av det köpta gångjärnet styrker inte utdragning hos den valda skruven i valnöten eller fri rörelse för hela luckan vid en vägg. `inspection/gangjarnsprov.txt` i varje paket anger provdelar, mätpunkter och passningskriterier. Nästa fysiska steg är att prova en komplett öppning med angränsande lucka, faktisk skruv och platta, mäta frigången upp till 93°, skruvfäste och fuktrörelse. Ett godkänt passningsprov omfattar bara de dörrmått och beslag som verkligen provats. Stommens förband, brukslast, stabilitet och montering kräver eget kvalificeringsunderlag.

[Beslagets måttritning för träluckor, s. 286](https://www.sugatsune.com/content/site-assets/201B_PDF/286_287.pdf) · [Monteringsplattans datum och monteringsmetod](https://global.sugatsune.com/eu/en/tech/genre/Series/J95_Series) · [Skruvspecifikation](https://www.spax.com/sv-sv/p/universalskruv-helgaengad-foersaenkt-huvud-krysspar-z-4cut-yellox.html)

**9 november 2026 · platsalternativ och ritningsaxlar, v16.** På PR #27 kan kunden spara den ursprungliga bokhyllan och sedan välja **Använd platsalternativ**. För referensen behålls 4340 × 2540 × 280 mm och listen 90 × 20 mm i båda orderrevisionerna. Med 30 mm sidreserv och 40 mm toppreserv blir stommen 4280 × 2500 × 260 mm. Monteringen behöver 26 mm mot vardera sidoväggen och 2518 mm i höjdled. Det ger 4 mm planerad frigång på varje sida och 22 mm inom kundens höjdmått. Fronten behöver minst 248 mm. Modellens 20 mm extra höjdmån och avrundning till tiotal millimeter är val vid dimensioneringen.

Referensens revision 2 innehåller inga verkliga vägg-, tak- eller frontmätningar. Status är därför `requires_measurement`. Originalet stannar vid `blocked` med 6 mm kollision per sida. När kunden själv fyller i ett platsmått i appen får just det måttet en källa i arbetsfilen. Äldre numeriska exempel utan sådan källa räknas inte som observerade. En lägre uppmätt takhöjd eller mindre sidavstånd ingår i förslaget. Ett otillräckligt uppmätt frontutrymme kan inte lösas med sido- och toppreserver. När ett väggdatum flyttas rensas endast den berörda mätningen. Verkstadsbeställningen kontrolleras på servern.

Gavelritningen anger exempelvis **Fiber längs V (stomaxel Z)**. Exportworkern läser tillbaka axeln på varje ritningsblad före utlämning. Kör `scripts/verify_bookcase_reference_downloads.py ORIGINAL.zip ALTERNATIV.zip` för att kontrollera de verkliga kundfilerna. Den öppna bokhyllan och den separata måttstudien med 700 mm underskåpsstomme har olika designflöden. Öppna underskåpsstudien under **Utforska → Välj en design → Väggbibliotek**. Studien visar sex fronter och sex underskåpsbottnar. Gångjärnsborrning och tillverkningsklara luckritningar saknas. Skarp frisläppning kräver fortsatt verkligt typunderlag enligt provprotokollet. [Ordinarie kundkörningar och provfiler](https://github.com/pilotens/Custombuild/pull/27/checks)


**Fortsättningspunkt 8 november 2026 · v15.** Den aktiva versionen ligger på [PR #27](https://github.com/pilotens/Custombuild/pull/27), ovanpå [#26](https://github.com/pilotens/Custombuild/pull/26). Vid jämförelsen var `main` på `e062cbb`, #26 på `19d0d66` och den tidigare v12-leveransen på `d983f78`. Första v13-rättningen ligger på [8cd0f97](https://github.com/pilotens/Custombuild/commit/8cd0f97b94887cb81c70a9b9f314bf0713381bbb). GitHubs provmerge `822a4e9` hade samma källträd. I byggd Chromium mot PostgreSQL, Redis/Celery, CAD-worker och SeaweedFS passerade samtliga 16 aktiva kundtester. Originalet hämtades som revision 1, ett nytt försök på revision 2 gav identiska bytes, revision 3 avbröts och revision 4 innehöll 27 trädelar och 54 DXF-sidor. Båda fullstora nedladdningarna återimporterades även separat från ZIP genom STEP-solidprov, DXF-entiteter och PDF-text. Den äldre körningens WebKit-prov rapporterade ett avsiktligt avbrutet förhandsanrop som ett fel. V14 kontrollerar webbläsarens faktiska avbrottssignal för den enskilda begäran. [Kundkörning för v13](https://github.com/pilotens/Custombuild/actions/runs/42498419784/job/128785692983)

**Orderfält och äldre revisioner.** På [5d0ad62](https://github.com/pilotens/Custombuild/commit/5d0ad6252ce04512159616d1e615f6ccdec86d77) fick ett direkt API-anrop utan bredd automatiskt standardbredden 900 mm. Med en syntetisk profil för det måttet kunde beställaren hämta ett verkstadspaket. Den rättade ordervägen kräver angiven bredd, höjd, djup, antal avdelare, antal hyllrader och nyttolast enligt vald lastgrund när det finns hyllor. En uttrycklig nollast per meter följer med även när ordern öppnas på nytt. Skapa och spara ger HTTP 422 och namn på de saknade fälten. När rätt order sparas binder servern uppgifterna till revisionens arbetsfil. Ett äldre granskningspaket kan hämtas. För äldre verkstadsjobb eller order som saknar den bindningen krävs att kunden öppnar och sparar sina uppgifter igen. Ett nytt verkstadsjobb kan då passera samma automatiska kontroller. [Test av korrekt order, ofullständiga anrop och äldre exporter](../tests/integration/test_bookcase_workshop_release.py)

**Direkta gränsprov.** Verklig API-inläsning, orderworker, CAD och återhämtning av granskningsfil provades lokalt med separat testobjektlager. 250 × 300 × 100 mm i 17 mm björkplywood med 5,5 mm rygg och 6000 × 4000 × 1200 mm i 19 mm MDF med 6,5 mm rygg gav fem trädelar och tio DXF-sidor vardera. En valnötsvariant 900 × 1200 × 280 mm gav elva trädelar och 22 sidor. Ett 5000 N indatafall med en hyllrad gav sex trädelar. Alla dessa order saknade platsunderlag och fick HTTP 409 vid begäran om verkstadsritningar. Första mikrometern utanför vardera gränsen för bredd, höjd, djup, skiv- och ryggtjocklek gav HTTP 422, liksom antal och nyttolast utanför intervallet. Inget av dessa prov kvalificerar en brukslast.

**Senaste verifierade kundväg.** På [e21d746](https://github.com/pilotens/Custombuild/commit/e21d74664b1875535368cc7e0da9718db910ed08) passerade 16 Chromium-fall samt Firefox och WebKit mot den byggda applikationen. Provet hämtade originalet, den ändrade revisionen och ordern efter ett avbrutet exportjobb. De två fullstora ZIP-filerna återlästes separat med solidprov, DXF och PDF. Varje fil hade 257 poster, 196 verifierade pilotrader och 56 monteringssteg. Modellbehov för alternativet är 10 720 N brukskapacitet per bärande fog och 1 259 N jämnt fördelad nyttolast per hel rad. [Kvitto med filidentiteter och aktuell omkörning](https://github.com/pilotens/Custombuild/pull/27#issuecomment-6724442124)

**Återstart av export.** Det breda API-provet upptäckte sedan ett intermittent fel i SQLite med anonym minnesdatabas. När en samtidig sessions rollback inföll efter första lagringsposten försvann en annan sessions reservation. Detta kunde återskapas vid exakt samma punkt i den normala orderworkern. API och worker använder nu separata transaktionsanslutningar till en privat minnesdatabas per motor. Under omprov hämtades orderfilen, ytterligare begäran återanvände samma jobb och en enda fil skrevs. Kör `pytest tests/integration/test_bookcase_transaction_isolation.py tests/integration/test_api_design_flow.py::test_public_custom_shelving_reaches_verified_supplier_review_package`. Ett separat API-prov jämför ett manifest där CAM-filer utelämnats med ett statusdokument som påstår att de finns. När motsägelsen kan beläggas avslutas utlämningen med HTTP 409 innan fler filer hämtas. Det ordinarie PostgreSQL-provet och de exakta verkstadsfilerna redovisas i [provkvittot på #27](https://github.com/pilotens/Custombuild/pull/27#issuecomment-6724442124).

**Fristående mätning av kundens nedladdning.** Efter Chromium laddar ordinarie CI in exakt de två ZIP-filer som webbläsaren hämtade i en separat process. [Referensprovet](../scripts/verify_bookcase_reference_downloads.py) använder externa filbibliotek och räknar alla 57 ämnens lägen och mått från beställningen. Det läser STEP, DXF, PDF, kaplista och dellista samt mäter hålbottnar, pilothålsradier, verkliga spårstopp och vinkelns båda kärnhål. Kvitton för revision 1 och 2 sparas som `reference-readback.jsonl` tillsammans med de orörda ZIP-filerna. Kör lokalt med `uv run --locked --group cad python scripts/verify_bookcase_reference_downloads.py ORIGINAL.zip ALTERNATIV.zip`. Fel i ett referenspaket fäller kundprovet. Den ordinarie workern gör frisläppningskontrollen för varje order innan någon fil publiceras.

**Typunderlagets mätvärden i v15.** I [v14](https://github.com/pilotens/Custombuild/commit/fef2496f8d91cff7c07002e04bf8d78037175c9e) kunde fem checksummeverifierade rapporter registreras utan maskinläsbara provresultat. I ett isolerat prov godtogs en rapport som angav 1 N trots att orderns modell krävde 346 N. V15 kräver provutfärdare, rapportreferens, provdatum, dimensionerande brukskapacitet i skjuvning och utdrag samt verifierad radlast, nedböjning med mätosäkerhet, horisontalkraft, tippmarginal och genomförda monteringssteg i rätt ordning. Varje siffra hör till samma registerpost som de fem rapporternas identiteter. Programmet jämför de strukturerade värdena automatiskt. I provpaketets `inspection/kvalificeringsprov.json` finns orderkraven och schemat för de verkliga resultaten. Tomma resultat ger aldrig tillverkningsklar order. De godkända underlagen återanvänds bara vid identisk konstruktion, monteringsgraf, plats, batcher och regler. [Prov av positiva order och avsiktliga fel](../tests/integration/test_bookcase_workshop_release.py)

**Två orderfel åtgärdade i v14.** Tidigare hämtade webbsidan produktionsfilen när exportjobbet blev klart. En annan flik kunde hinna spara en ny revision innan kunden klickade på den gamla filen. Nu prövas revisionen och provprofilen på servern vid varje hämtningsklick. Ändrar kunden den öppna designen under hämtningen kastas eventuella sena bytes.

Den tidigare platsrapporten hade ett gemensamt sidomått för planerad reservation. Ett separat uppmätt väggavstånd kan nu anges för vänster, höger och bakvägg. Avstånden mäts vinkelrätt från färdig stommes slutläge till väggytan bakom eventuell list. Med 30 mm reservation och 25 mm uppmätt till vänster bevaras stommens originalmått. Den beräknade marginalen är +4 mm och den uppmätta marginalen −1 mm. PDF och API visar blockerad montering. Måtten 30, 30 och 20 mm i den separata provvarianten är exempelvärden. För verkstadsutlämning krävs uppmätta avstånd till angivna väggar.

I `inspection/fogprov.csv` finns en rad per verifierat pilothål. De två raderna med samma förbands-ID anger motdel, fysisk yta, rätt DXF, U/V, djup, tolerans och monterat centrum. Gavelns och bottens första vinkelaxlar träffar exempelvis (18, 20, 36) och (36, 20, 18) mm i stommens koordinatsystem. Mätkolumnerna avser verkliga provbitar. Kundbokhyllan har 98 förband och 196 sådana rader. [Aktuell PR-körning och nedladdade provpaket](https://github.com/pilotens/Custombuild/pull/27/checks)

**Beslut för skarp drift: NO-GO.** Antal fysiskt kvalificerade konfigurationer är noll. Profilregistret i `packages/manufacturing/src/custombuild_manufacturing/bookcase_qualifications/profiles.json` är tomt. Katalogens vinkel- och skruvritningar styrker nominell geometri. Det saknas verkliga prov på materialbatcher, fogens skjuvning och utdrag, brukslast, stabilitet, förankring, hjälpmedel och montering i angiven miljö. Ett digitalt passningsprov kvalificerar ingen order för bruk.

**Återskapat fel och rättning.** På d983f78 flyttades ett verkligt DXF-pilothål 1 mm utan ändrade metadata. Den ordinarie workern skapade ZIP och kontrollsumma. V13 rekonstruerar panelantal, placering, ämnen och material från kundens sparade indelning. Den läser ritningarnas båda sidor, faktiska DXF-entiteter, håldjup, konturer, spår och PDF-operationsmått. Oberoende solidmodeller jämförs med återimporterade delar i båda STEP-filerna, inklusive varje monterad GN 970-vinkel. Kontroll av både tillagt och saknat material hittar ett flyttat hål även om volym och yttermått är oförändrade. Digitala jämförelsegränser är 0,00001 mm i begränsningslådan och 0,001 mm³ per materialdifferens. Del-ID, antal, kaplistor, skruvar, förband, steg, PDF och orderrevision kontrolleras före skrivning till objektlagret. Kontrollen gäller den fasta GN 970-profilen.

| Kundfall | Förväntat utfall |
| --- | --- |
| 4340 × 2540 × 280 mm, list 90 × 20 mm, fem avdelare, sju rader, 30 kg/m | 57 trädelar, 98 vinklar, 196 pilothål, 114 DXF-sidor, 122 spår och 94 stopp. Granskningspaket. Den stående monteringen kolliderar 6 mm med vardera sidolisten. |
| Samma kundmått, 30 mm per sida och 40 mm upptill, exempelvärden 2540 mm fri takhöjd och 600 mm framför | Separat revision. Stomme 4280 × 2500 × 260 mm. Nominell marginal 4 mm per sida, 22 mm upptill och 352 mm framtill. Saknade verkliga plats- och kvalificeringsuppgifter stoppar verkstadsutlämning. |
| Verifieringsorder 900 × 900 × 500 mm, 50 N/rad, testbatcher och testplats | Endast i testets temporära register finns fem syntetiska provunderlag. Samma API, normala CAD-jobb och lagringskontroller levererar då ett separat verkstadspaket. Återkallelse, utgång, skadat provunderlag eller ny revision stoppar den direkta nedladdningen. |
| Flyttat DXF-pilothål, öppet spår förbi ändstopp, verklig DXF från revision 1 i revision 2, flyttat STEP-pilothål | Ordinarie jobb fallerar före lagring. Status blir `failed` och direkt nedladdning nekas. |

**Kundväg och omprov.** Välj Bokhylla med verkstadsunderlag, spara revision, skapa granskningspaket eller beställ verkstadsritningar. Direkt API använder `POST /v1/furniture/projects/{id}/exports` med sparad revision, designhash och `purpose=workshop_drawings`. Produktionsritningar har egen filidentitet, monteringsanvisning och versionsbundet platsunderlag. Servern kontrollerar profilens giltighet vid beställning, generering, färdigställande och utlämning. Behörigheter, lagringsbokföring och versionslåsning gäller båda varianterna. Det maskinspecifika CAM-flödet är separat. Kör `pytest tests/integration/test_bookcase_workshop_release.py tests/integration/test_furniture_workspace.py tests/integration/test_shelving_workshop_trial.py tests/unit/test_bookcase_angles.py tests/unit/test_bookcase_site.py`. Det byggda Chromium-fallet i `apps/web/e2e/furniture-live.spec.ts` ändrar fack och nyttolast och hämtar de två fullstora revisionerna. Det upprepar hämtningen av revision 2 byte för byte, avbryter ett verkligt jobb för revision 3 genom att spara revision 4 och kontrollerar det regenererade paketets delantal och revisionsidentitet. I lyckad Linux-CI innehåller artefakten `bookcase-workshop-trial-packages` båda nedladdningarna. Varje ZIP innehåller `inspection/geometry-readback.json`, `inspection/groove-fits.json` och `inspection/provprotokoll.txt`.

**Nästa externa prov.** Alternativet kan användas som måttsatt underlag för passningsprov i verkstad. Registrera faktiskt material, batcher, plats, fuktmiljö och tillgängligt obrutet råformat. Generera om för de uppmätta värdena. Verifiera passning, stopp, verktygsåtkomst och alla monteringssteg i säkrad uppställning. Belastningsprov kräver provledarens fixtur, varaktighet, excentricitet, miljö och godkännandekriterier. Alternativets beräknade behov är 10 720 N brukskapacitet i skjuvning per bärande fog enligt nuvarande konservativa regel. Kontrollera även utdrag och högst 3,0 mm nedböjning under den föreskrivna radlasten. Foga verkliga daterade rapporter för material, fog, brukslast, stabilitet och plats/monteringsmetod till en repogranskad profil. Varje PDF har SHA-256. Profilen gäller en organisation och exakt provad konstruktion inklusive batcher, plats, last och regelversioner. Nytt kund-ID eller revisionsnummer kan återanvända provet. Inga ytterligare måttintervall följer av ett enskilt prov.

**Återuppta här.** Följ v15-kundkörningen och det slutliga provkvittot på #27. Nästa fysiska steg är att mäta plats och provbitar enligt fogprotokollet. Knyt ett godkänt underlag till den exakta profilen och kör sedan samma positiva och negativa kunder via API och webben. Historik från tidigare versioner följer nedan.

**Verifierat med programkoden i `97d6567`.** Från startsidan laddade Chromium ned tre v12-revisioner för valnötsfallet. Originalet gav blockerad montering, måttalternativet nominell frigång, och en tredje revision utan platsuppgifter begärde mätning även i PDF-manualen. Dessa tre referenspaket kom från FastAPI, CAD-worker, SQLite och testobjektlager. Repots ordinarie Chromium-test för kundmåtten kördes också mot FastAPI med SQLite, Redis, separata Celery-processer för utkorg och generering samt ett versionshanterat S3-lager (VersityGW). Testet öppnade den sparade möbeln igen, hämtade två ZIP-revisioner och kontrollerade ändrade platsmått. PostgreSQL och SeaweedFS i Compose behöver verifieras på GitHub för den nya kodversionen. I varje av de två första ZIP-filerna kontrollerades 57 trädelar, 98 vinklar, 114 DXF, samtliga 196 pilothål, 122 spårbottnar och 94 ändstopp mot återimporterad geometri. Tre andra nedladdade konstruktioner omfattade 6, 13 och 88 trädelar i plywood eller MDF. När hyllrader ändrades från två till sex i en egen körning följde WebGL-vyn servermodellen från 11 till 19 delar. Efter omprov av CAM- och postprocessorimport utan domänpaketet passerade 54 riktade tester för paketgränser, mått, monteringsplats och provpaket. Den fristående granskningsrapporten gav samma innehåll vid två körningar på stabilt källträd. Paketversionen `custombuild.shelving-workshop-review.v12` hindrar nya begäranden från att återanvända äldre exportjobb.

| Levererat paket | SHA-256 |
| --- | --- |
| Kundmått med ursprungliga reservationer | `62e5150f895a957c12611a95d1be40d4e049bdac1f4519b7463dfacbff7597e5` |
| Frivilligt måttalternativ med provdata | `7df079a35fffaee58fc395762bdf82e54bd1365ec323d8338f78515fa2c0ae73` |

**Stödområde för konstruktionsprov.** På startsidan, välj **Bokhylla med verkstadsunderlag** och öppna kundbokhyllan. Rektangulär stomme, fasta hyllor, raka avdelare, valbara fackbredder/hyllhöjder och spårmonterad rygg. Provbeslag GN 970-ALM-30-30-30-B med två DIN 96 A4 5×16 per bärande spårfog. Sockel, underskåp, luckor, flyttbara hyllor och annan rygg ingår inte i denna provkonstruktion. Tidigare familjer finns kvar.

| Indata | Kompilatorns område |
| --- | --- |
| Bredd × höjd × djup | 250–6000 × 300–4000 × 100–1200 mm |
| Indelning | 0–16 avdelare, 0–40 hyllrader, högst 128 trädelar per export |
| Stomme, rygg | 17–19 mm svartvalnöt, MDF eller björkplywood, infälld katalogrygg 5,5–6,5 mm |
| Nyttolast | 0–5000 N jämnt fördelad per hel rad. Angiven last per meter räknas om för revisionens bredd. |

Detta är beräkningsgränser. Varje kombination måste dessutom klara fria öppningar, spår, anliggning, pilotplacering, hyllspann och CAD. Fysiskt tillåten brukslast är öppen. Spara revision och hämta granskningspaketet utan maskinval.

**Referensens plats.** 4340 × 2540 × 280 mm är kundens yttermått. Arbetsfilen antar befintlig 90 × 20 mm list längs båda sidor och bakväggen. Stommen blir 4300 × 2540 × 260 mm, massiv amerikansk svartvalnöt med 6 mm björkplywood i ryggen. Tak- och frontutrymme är separata mätningar. Vid fem avdelare och sju rader kommer gavlarna 6 mm utanför slutläget i steg 12/13. Vid listens höjd behöver träet 26 mm per sidovägg. Arbetsfilen reserverar 20. Träet når 2558 mm över botten och 248 mm framför fronten. Detta förhindrar den angivna monteringen mellan sidolisterna. Det går att hämta provunderlaget med blockeraren i förhandsgranskning, platsrapport, provprotokoll och PDF-manual.

**Vägg utan list.** Listprofilen visar vilka väggar som redan är kända. För övriga sidor kan kunden markera en begränsande vägg vid kundmåttet eller bekräfta fritt utrymme. Oangiven väggplacering ger `requires_measurement`. Mellan bara väggar kräver gavlarna 6 mm införing även utan list. Att bara ange en önskad möbelbredd säger inget om var rummet slutar. Tak- och frontmätning kan också överstiga möbelns 6000 mm indatagräns.

**Ändrad monteringsplats.** När en befintlig list tas bort i formuläret finns den observerade väggen kvar som bar vägg. En delvis uppmätt plats kan ha både kända väggar och okända sidor. Om kunden tar bort alla platsmått för GN 970-profilen visar förhandsgranskningen, platsrapporten, PDF-manualen och provprotokollet att platsen behöver mätas. Manualens platssida visar träets högsta läge, erforderligt frontutrymme och tillgängliga mätvärden. Mått redovisas i millimeter med bibehållen mikrometerupplösning. Provpaketet kan hämtas i alla dessa lägen.

**Separat måttalternativ för prov.** Behåll yttermåtten men välj 30 mm reservation per sida och 40 mm ovanför. Räkna om stommen till 4280 × 2500 × 260 mm. Provdata 2540 mm fri takhöjd och 600 mm framför ger 4 mm nominell sidomån per sida, 22 mm ovanför högsta rörelsen och 352 mm framför införingsbanan. Dessa är demonstrationsmått. Mät plats, fixturer och handutrymme före prov. Ingen ändring görs automatiskt i kundens original.

**Verifiering.** Sju rader, 30 kg/m och förslaget fem avdelare ger 57 trädelar, 98 vinklar, 196 skruvar, 56 trästeg och 114 DXF. Föregående exakta geometriprov och ZIP-hashar finns på PR #27 (c5f0073). För provprofilen jämför regressionen den importerade gavelns start- och slutläge med en separat 20 × 90 mm listvolym. Matrisen provar även saknad mätning, kontakt, 1 µm över/under gränsen, höjd över list, ändrad arbetsfil och återgenerering. Ingen positiv nominell marginal godkänner en fysisk order. Kör `pytest tests/unit/test_bookcase_site.py tests/integration/test_shelving_workshop_trial.py` och kundmåttsfallet i `apps/web/e2e/furniture-live.spec.ts`. På [a950331](https://github.com/pilotens/Custombuild/commit/a9503317b70520434c302566eb297c1e1478e3ae) laddades två v9-revisioner ner i byggd Chromium med FastAPI, riktig CAD-worker, SQLite och testobjektlager. Båda gav 155 STEP-solider, 254 filer och rätt material. Per ZIP kontrollerades 196 pilothål, 122 spårbottnar och 94 ändstopp i återimporterad geometri. Tidigare v10-paket och kontrollsummor redovisas på PR #27. En ombyggnad av webbklienten kunde återanvända föregående sidmanifest och dölja det nya inmatningsfältet. Byggskriptet tar bort föregående kompilerade serverutdata ur identifierad Next-byggnad. Ombyggnad från den felaktiga katalogen och ett nytt Chromium-prov visade nu båda mätfälten. Se PR #27 för aktuell CI.

**Öppna kriterier och nästa arbete.** PostgreSQL-ändringen finns på [e9aee77](https://github.com/pilotens/Custombuild/commit/e9aee779c2de9185cab4310561847263ce514fcf). [Exakt image, skanning och byte av volym](https://github.com/pilotens/Custombuild/actions/runs/42429234326/job/128574198566) passerade. Den 8 november listade Wolfis hämtade x86_64-index fortfarande högst glibc-2.44 2.44-r14 och sqlite-libs 3.54.1-r0. Följ aktuell PR-CI och kontrollera nästa korrigerade runtime för glibc (CVE-2026-84243) i web/API/worker och SQLite (CVE-2026-119586) i API/worker. Skanna faktiskt byggd image och kör Compose med den innan merge. Dispens eller sänkt tröskel ingår inte.

**Externt prov.** Bekräfta verklig listplacering och fritt monteringsutrymme samt tillgång till obrutna topp-/bottenämnen (4276 mm färdig längd i originalet). ZIP innehåller råämnesmått, fiberriktning, bearbetningssida, ritningar, steg och mätlista. Prova skruv, vinkel och spår i verkliga batcher. Mät ±0,10 mm ämnestjocklek/kontur, ±0,05 mm spårdjup och minst 0,05 mm stoppmån enligt `inspection/provprotokoll.txt`. Bekräfta fogstyrka, fuktrörelse, stabilitet och väggförankring med definierade lastfall. Lastbehovet för fogarna är en konservativ konstruktionsgräns, inte uppmätt kapacitet eller instruktion att belasta en osäkrad möbel. Om ämnen måste delas krävs modellerad skarv och nytt prov. Nivå A har kvar drifts- och säkerhetsgrindar. Nivå B saknar kvalificerat material-, förbands- och lastintervall.
