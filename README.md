# Řekni výrok

Hlasový fact-check v reálném čase. Řekneš větu, aplikace ji průběžně přepisuje a ještě během mluvení ověřuje proti české Wikipedii. Výsledek je jedna ze čtyř karet: **PRAVDA**, **NEPRAVDA**, **NESMYSL** (věta nedává smysl jako tvrzení) nebo **NELZE OVĚŘIT** (s důvodem, proč). Pod mikrofonem běží „detektor lži": seismograf, který při pravdě vyjede nahoru, při nepravdě dolů a při neutrálu drží střed. Vpravo je historie výroků s větou z důkazu a odkazem na zdroj.

Verdikt nedělá žádný velký jazykový model. Rozhoduje lokální NLI model (`MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7`, ~560 MB, běží na CPU) plus několik pravidel pro čísla, vztahy typu „hlavní město" a nesmysly. Nikdy neukážeme PRAVDA/NEPRAVDA bez věty z důkazu a odkazu.

## Co potřebuješ

- macOS nebo Linux, **Python 3.9+** (na Macu stačí `python3` z Xcode Command Line Tools) a **Node 20+**.
- **Google Chrome** – frontend používá Web Speech API (`cs-CZ`), které Safari ani Firefox pro češtinu spolehlivě neumí.
- Připojení k internetu při prvním startu (stažení modelu) a za běhu (MediaWiki API), pokud si nepostavíš lokální index.

## Backend (FastAPI + NLI)

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # torch + transformers, chvíli to trvá
uvicorn main:app --port 8000 --reload    # první start stáhne model (~560 MB) a zahřeje ho
```

Backend běží na `http://localhost:8000`. Rychlý test z terminálu:

```bash
curl -s localhost:8000/check -H 'Content-Type: application/json' -d '{"claim":"Tunel Blanka měří 100 km."}'
curl -s localhost:8000/check_many -H 'Content-Type: application/json' -d '{"text":"Tunel Blanka je tunelový komplex. Měří 1000 km."}'
```

Odpověď obsahuje `card`, `hint` (důvod u NELZE OVĚŘIT / NESMYSL), `evidence` (věta, titulek článku, URL, skóre souhlasu a rozporu, `by_rule`) a `timings` v milisekundách po fázích.

Volitelné proměnné prostředí: `WIKI_UA` (User-Agent pro MediaWiki API, slušnost vůči Wikipedii), `WIKI_INDEX` (cesta k lokálnímu indexu), `TYPE_MODE=nli` (klasifikace typu výroku modelem místo pravidel; pomalejší, v demu vypnuto).

### Lokální index Wikipedie (volitelné, ale rychlejší)

Bez indexu se při každém výroku volá MediaWiki API (~300 ms). Index úvodů nejčtenějších článků plus ručně vybraných titulků (`backend/data/seed_titles.txt`) srazí hledání důkazů na jednotky milisekund:

```bash
cd backend && source .venv/bin/activate
python3 scripts/build_index.py --days 30     # ~10–15 min, vytvoří backend/data/wiki.sqlite (FTS5)
```

Soubor je v `.gitignore`, každý si ho staví sám. Pokud index existuje, backend ho použije automaticky a na API sáhne jen při neúspěchu.

### Vyhodnocení

```bash
cd backend && python3 eval.py        # projede backend/tests/claims.json a vypíše přesnost po kartách a latence
```

## Frontend (Next.js)

```bash
cd frontend
npm install
npm run dev                            # http://localhost:3000, otevřít v Chrome
```

Adresa backendu se dá přepsat přes `NEXT_PUBLIC_API` (výchozí `http://localhost:8000`). Pro vzdálený backend si uvědom, že Chrome povolí mikrofon jen na `localhost` nebo přes HTTPS.

### Ovládání

| Klávesa / prvek | Co dělá |
|---|---|
| Tlačítko mikrofonu nebo **mezerník** | Start / stop poslechu |
| **p** nebo tlačítko ⏸ vedle mikrofonu | Pauza seismografu; při pauze se dá scrollovat zpět kolečkem, trackpadem nebo tažením (drží posledních 30 minut) |
| **d** | Demo bez mikrofonu: simulované „namlouvání" připravených vět (`DEMO` v `app/page.tsx`); totéž spustí URL `?demo` |
| **/** | Skryté textové pole pro ručně napsaný výrok |
| **i** | Debug overlay s odpověďmi backendu a časy |
| **Esc** | Zavře textové pole a zastaví demo |

## Jak to funguje

**Přepis.** Web Speech API posílá průběžné (interim) i finální výsledky. Každá změna průběžného přepisu se po 200 ms debounce pošle na `POST /check` jako spekulativní dotaz; běžící dotaz se zruší, výsledky se cachují podle normalizovaného textu. Finální přepis se nikdy neruší, a pokud obsahuje víc vět, jde na `POST /check_many`, které vrátí verdikt pro každou větu zvlášť (předchozí věta slouží jako kontext, takže „Je dlouhý 1000 km." po „Tunel Blanka je tunelový komplex." ví, o čem mluví).

**Typ výroku.** Pravidla v `backend/engine.py`: méně než tři slova je neúplná věta, otazník nebo tázací zájmeno je otázka, názorová slova bez čísla jsou názor. Nic z toho se neověřuje a končí kartou NESMYSL s nápovědou. Kauzální tvrzení o politice („snížil chudobu", „zajistil růst") končí jako NELZE OVĚŘIT, protože Wikipedie taková tvrzení neumí potvrdit.

**Důkazy** (`backend/evidence.py`). Z výroku se vezmou obsahová slova, napřed se hledá podle vedoucí entity (velká písmena na začátku: „Tunel Blanka", „Brno"), výsledky se přerovnají podle shody titulku s výrokem. K článku o podmětu se přidá i článek o předmětu („Brno je hlavní město **Česka**" přitáhne článek Česko). Když se k podmětu žádný článek nenajde, je to NELZE OVĚŘIT s důvodem. Když se článek najde, ale slova výroku se v něm vůbec nevyskytují, je to NELZE OVĚŘIT, nebo NESMYSL, pokud výrok obsahuje osobní slova („tvoje bába").

**Verdikt** (`backend/engine.py`). Nejdřív pravidla bez modelu: čísla se porovnají s čísly ve větách zdroje (±5 %, roky přesně); vztahy „X je hlavní město / nejvyšší hora / nejdelší řeka / největší město Y" se rozhodnou z věty, která říká, kdo ten vztah pro Y drží („Hlavním městem je Praha."). Teprve potom NLI: věty úvodu se seřadí podle překryvu s výrokem, horní čtyři jdou do modelu jako premisy (s názvem článku jako kotvou), výrok je hypotéza. Souhlas ≥ 0,70 dává PRAVDA, rozpor ≥ 0,70 NEPRAVDA, jinak NELZE OVĚŘIT se skóre v nápovědě. Nejsilnější věta se zobrazí jako důkaz.

**Seismograf** (`frontend/app/LieStrip.tsx`). Canvas, 60 fps. Každá věta je segment s časem začátku a konce; výška jehly je verdikt segmentu (předběžný verdikt z průběžného přepisu je poloviční výchylka), rozkmit jehly je hlasitost z `AnalyserNode`. Po dokončení věty jehla verdikt 8 s drží a pak se vrací do středu.

## Struktura repa

```
backend/
  main.py          FastAPI: /health, /check, /check_many, cache výsledků, zámek nad modelem
  engine.py        typ výroku, číselné a vztahové pravidlo, grounding, NLI verdikt, dělení na věty
  evidence.py      hledání důkazů: lokální FTS5 index, jinak MediaWiki API, výběr článků podle titulku
  eval.py          vyhodnocení na tests/claims.json
  scripts/build_index.py   stavba lokálního indexu z nejčtenějších článků + seed_titles.txt
  data/seed_titles.txt     ručně vybrané titulky článků pro index
frontend/
  app/page.tsx     UI, Web Speech API, spekulativní ověřování, demo, klávesy
  app/LieStrip.tsx seismograf
  app/globals.css  vzhled (krémové pozadí, 70/30 layout)
```

## Známé limity

Zdrojem je zatím jen úvod článku na české Wikipedii, takže výroky o lidech, aktuálním dění nebo politických příčinách končí jako NELZE OVĚŘIT, a to je správná odpověď. Rozpoznávání řeči běží v Chrome přes Google, offline nefunguje. NLI model na CPU dává verdikt za 150–400 ms podle délky úvodu; první dotaz po startu je pomalejší.
