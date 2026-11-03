# Arbetsprov för möbelprodukterna

Varje produktprofil har ett fast arbetsprov på var och en av fem nivåer. Ett godkänt prov betyder att appen gav det utfall som står i beställningen. Ett granskningsunderlag kan innehålla öppna konstruktionsfrågor även när alla dess mått stämmer. Fysisk provning, verkstadsgranskning och frigivning ligger kvar i sina egna flöden.

## Kör proven

Från projektets rot, med Python 3.13 och uv installerade:

```sh
make product-acceptance
make product-acceptance PRODUCT=chair
make product-acceptance PRODUCT=dresser LEVEL=väldigt-svårt
make product-acceptance LEVEL=omöjligt
```

Varje körning skriver ett svenskt arbetsprotokoll i `test-results/product-acceptance.md` och mätunderlaget i `test-results/product-acceptance.xml`. Filerna avser det valda urvalet. GitHub Actions kör hela matrisen i jobbet **Product acceptance (five levels)** och bifogar båda filerna som **furniture-product-acceptance**, även vid ett underkänt arbetsprov. Testerna ingår också i `make test`.

| Nivå | Vad som ska visas | Nivå-ID |
|---|---|---|
| Lätt | En vanlig beställning ger rätt grundgeometri, delar och produktidentitet. | `easy` |
| Medel | En ändrad parameter ger den beställda ändringen. Övriga beroende mått ska fortfarande gå ihop. | `medium` |
| Svårt | Samverkande krav prövas, exempelvis last och spann, lådskenor och mellanväggar, fackproportioner eller råformat. | `hard` |
| Väldigt svårt | Ett sparat kundfall följs hela vägen genom produktens flöde. Verktygets sex möbelprofiler kontrolleras i nedladdade verkstadsfiler. Studions mallar provas med två redigeringar av samma utkast. | `very-hard` |
| Omöjligt | Provet godkänns när systemet stoppar exakt den otillåtna åtgärden med rätt orsak och kontrollerbart slutläge. | `impossible` |

`LEVEL` accepterar även `lätt`, `medel`, `svårt`, `väldigt-svårt` och `omöjligt`. Använd `PRODUCT` från tabellen för att köra en enda produkt. Utan filter körs 65 arbetsprov och en separat kontroll av att matrisen täcker produktkatalogerna.

## Produkter och avgörande kontroller

| Produkt | PRODUCT | Exempel på mätning | Krav för omöjligt prov |
|---|---|---|---|
| Panelbord | `panel-table` | Fri längd mellan gavlarna, båda sargerna, fem delar. | Stoppa en 249,998 mm lång sarg. En sarg på 250,001 mm ryms i det svåra provet. |
| Rektangulärt matbord | `dining-rectangle` | Fyra ben, nio delar och tjugo parade skruvförband. Ett långt belastat spann ska ge en nedböjningsspärr. | Stoppa en ram med mindre än 100 mm fri sarg. |
| Runt matbord | `dining-round` | Mät radien 633,5005 mm ur DXF och kontrollera att alla hörn på benen ligger inom skivan. | Stoppa två diametrar som skiljer 0,001 mm. |
| Matstol | `chair` | Sitsens 414,521 mm mäts separat från hela stolens 562,037 mm. Förband och lutning kontrolleras. | Stoppa en sits som går 0,001 mm in i bakbenen. |
| Byrå | `dresser` | Lådornas slag, spel, fronter och hålbilder för 350 och 450 mm skenor. Borrningar i mellanväggen får inte kollidera. | Stoppa en 450 mm skena när front, rygg och frigång gör stommen för grund. |
| Hyllsystem i möbelverktyget | `shelving` | Fackbredder, hyllcentrum, bakfält och sockel. 128 delar får köas för granskning. | Ett utkast på 129 delar får visas men får inget exportjobb. |
| Valnötsvägg i moduler | `walnut-modules` | Rumsmått, fogar, råämnen, socklar och varje rad i den gemensamma kaplistan. Fyra moduler ger 62 delar i kundfallet. | Stoppa stapling med sockel tills placeringen har valts. Ingen kaplista får skapas. |
| Hyllsystem i studion | `studio-shelving` | Fackens verkliga mått, gammal designidentitet avvisad och rätt revision fryst. | Stoppa 20 hyllplan i 400 mm höjd och bevara det tidigare utkastet. |
| Väggbibliotek | `studio-wall-library` | Underskåpens fronter och de öppna övre facken i samma stomme. | Stoppa produktionsrevisionen med mallens begränsning för gångjärn och infästning. |
| Skänk | `studio-sideboard` | Låg stomme med två fack och olika bredder ovanför skåpen. | Stoppa produktionsrevisionen för mallens obekräftade beslagssystem. |
| Rumsavdelare | `studio-room-divider` | Konceptutkast utan bakstycke och med rätt fackmått. API-provet gäller konceptskissen. Studions mallväljare kräver stöd för alternativ stagning. | Stoppa produktionsrevisionen för den fristående konstruktionen. |
| Hänghylla | `studio-hanging-shelf` | Väggmodell utan sockel. Sparande och återöppning bevarar identitet, tid och mått. | Stoppa produktionsrevisionen för obekräftade väggfästen. |
| Förvaringsvägg | `studio-cupboard` | Höga skåpfronter och hyllor med egen höjdfördelning. | Stoppa produktionsrevisionen för obekräftade höga fronter och gångjärn. |

## Vad som mäts i exportproven

Testerna skapar ett projekt, läser tillbaka det, beställer export för rätt revision och kör den ordinarie arbetarkoden. Testmiljön använder SQLite och en objektlagring i minnet. CAD, ritningar, kaplista, packning, nedladdning och kontroll av åtkomst använder appens verkliga kod. CAD måste vara installerat för dessa prov.

- STEP öppnas på nytt. Antal solider, origo och beställd bredd, djup och höjd kontrolleras med 0,002 mm tolerans för STEP-import.
- Varje kaprad jämförs med delens identitet, material, tjocklek samt råmått och färdigmått i delens U/V-riktning. För moduler kontrolleras även fiberaxeln.
- Ritningsfilerna kontrolleras per del och fysisk sida. Det runda bordets DXF-kontur mäts som en cirkel. Paketet ska innehålla PDF-filer och GLB med giltigt filhuvud, och varje fil ska matcha manifestets SHA-256.
- Äldre exportrevision och åtkomst från en annan organisation ska avslås. Ett godkänt granskningspaket ger ingen frisläppning för fysisk bearbetning.

## Lägg till produkter och följ upp fel

1. Registrera den nya produktprofilen och dess fem beställningar i [testkatalogen](../tests/product_acceptance/catalog.py). Skriv förväntade mått, antal och eventuella stopporsaker innan du kör appen.
2. Lägg mätningen i möbel-, modul- eller studioflödet. Använd kundens mått eller en självständig beräkning som facit. En ny produktfamilj eller studiomall utan arbetsprov ska få katalogkontrollen att falla.
3. Vid avvikelse, behåll den ursprungliga beställningen och skilj ett fel i appen från ett fel i testets facit. Rätta orsaken, kör om samma prov och gör ett nytt arbetsprov med andra mått som prövar samma fel. Behåll ett återprov för den faktiska regressionen.
4. Kör hela matrisen igen. Varje rad i protokollet visar beställning, godkännandekrav, utfall och uppmätta observationer. Ett oväntat fel, ett uteblivet avslag eller ett överhoppat arbetsprov ger en misslyckad körning.

## Delar, ritningar och demontering

På nivån väldigt svårt hämtas delbladet för varje enskild del i de sex möbelprofilerna, sex studiomallarna och valnötsmodulerna. För de sex profiler som genererar ett verkstadspaket jämförs SVG-innehållet på varje bearbetningssida med filen i det nedladdade paketet. Modulprovet följer samtliga 62 kaprader till rätt del i rätt modul.

Delbladet innehåller delidentitet, modellhash, råmått, färdigmått, material, förband, motdelar och faktiska U/V-koordinater för borrhål och borrserier. Yttermått på en planka är skilda från de tillgängliga råformaten. Marginal och fiberriktning ingår i formatkontrollen. Ett hål i en regelbunden serie redovisas på varje fysisk position. Ett skruvbeslag som visas från båda motdelarna räknas en gång i möbelns stycklista.

Särskilda återprov förskjuter ett pilothål, flyttar en bearbetning till fel del, tar ut ett stolsben och mäter fria öppningar bakom underskåp. Ett annat prov testar en hel rygg mot skivformatet och kontrollerar att underskåpsbottnarna slutar vid dess framsida. I ett separat felprov återstår enbart de översta 200 mm av ett bakstycke. Just det facket ska då visas som delvis täckt och grannfacket som helt täckt. En lådsida följs till sina tre skenfästen och bara den låda som berörs när sidan tas ut. En uppmätt rund stolpe prövas vid kontakt, en mikrometers överlapp och en mikrometers mellanrum. En demonteringsvy ändrar ingen sparad revision. Den visar brutna modellerade förband och återstående golvstöd. Lastkapacitet och stabilitet för den ändrade möbeln behöver prövas separat.

En stor konceptmall kan öppnas i Studio även om en obruten del överskrider skivformat eller maskinbord. Ett återprov med en 4 200 mm bred väggstomme mäter att bredden ligger kvar och att båda spärrarna fortfarande visas.
