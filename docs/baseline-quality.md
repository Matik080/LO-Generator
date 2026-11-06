# Východisková kvalita (baseline) – Mlynček B

Východisková hodnota pre metriku **„Kvalita sa nezhorší“** z [návrhu](api-design.md): priemerné skóre faithfulness voči zdroju a podiel LO, ktoré prejdú filtrom kvality. Ďalšie zmeny (nové typy otázok, iné modely, rozdelenie do hodín) sa porovnávajú s týmito číslami na rovnakom dokumente a s rovnakými modelmi.

## Podmienky merania

| | |
| --- | --- |
| Dátum | 6. 11. 2026 |
| Dokument | `Testing_text.pdf` – OZNAL, kapitola 3 *Exploratory Data Analysis*, 26 strán (rovnaký testovací dokument ako v bakalárskej práci) |
| Kód | beh 2: commit `4b3b425`; beh 3: `8b4c63c` + zotavenie extrakcie pri neplatnej odpovedi LLM |
| Generovanie | `qwen/qwen3.8-27b` (Groq) |
| Hodnotenie | `qwen/qwen3.8-27b`, `openai/gpt-oss-120b` (Groq) |
| Embeddingy | `all-MiniLM-L6-v2` (lokálne) |
| Filter kvality | faithfulness ≥ 3,0 (priemer modelov), hint quality ≥ 2,0, najviac 2 kolá regenerácie |
| Trvanie | približne 15–17 minút na beh |

Platné sú dva behy (2 a 3) s rovnakými podmienkami. Prečo nie beh 1, je opísané nižšie.

## Výsledky

**Prvé hodnotenie** = LO z prvého generovania, pred regeneráciou. **Konečný stav** = verzie LO, ktoré pipeline vráti. Skóre sú na škále 1–5.

| Metrika | Beh 2 | Beh 3 | Priemer |
| --- | --- | --- | --- |
| **Konečný stav** | | | |
| Podiel LO, ktoré prejdú filtrom | 93,6 % (131 / 140) | 95,9 % (142 / 148) | **94,8 %** |
| Faithfulness – `gpt-oss-120b` | 4,31 | 4,33 | **4,32** |
| Faithfulness – `qwen3.8-27b` | 4,15 | 4,35 | 4,25 |
| Faithfulness – priemer oboch modelov | 4,23 | 4,34 | 4,29 |
| Question clarity – priemer | 4,35 | 4,55 | 4,45 |
| Hint quality – priemer | 4,28 | 4,28 | 4,28 |
| Distractor plausibility – priemer | 2,52 | 2,63 | 2,58 |
| **Prvé hodnotenie** | | | |
| Podiel LO, ktoré prejdú filtrom | 74,3 % | 87,2 % | 80,8 % |
| Faithfulness – `gpt-oss-120b` | 4,21 | 4,27 | 4,24 |
| Faithfulness – `qwen3.8-27b` | 4,07 | 4,32 | 4,20 |
| **Rozsah** | | | |
| Atomické jednotky = LO | 140 | 148 | |
| LO s otázkou s možnosťami | 130 | 139 | |
| Regenerované LO | 31 | 15 | |
| Po 2 kolách stále neprešlo | 9 | 6 | |
| Hrany grafu prerekvizít | 128 | 123 | |

Čo z porovnania behov vyplýva:

- **Faithfulness podľa `gpt-oss-120b` v konečnom stave je stabilná** (4,31 a 4,33). Je to najspoľahlivejšie číslo na porovnávanie.
- **Podiel LO, ktoré prejdú v konečnom stave, sa líši o 2,3 percentuálneho bodu.** Regenerácia vyrovnáva rozdiely v prvom generovaní.
- **Podiel LO, ktoré prejdú na prvý pokus, kolíše výrazne** (74 % a 87 %). Preto sa nehodí ako cieľ.
- **`qwen3.8-27b` ako hodnotiaci model kolíše viac** ako `gpt-oss-120b`.

Poznámky k behom:

- Každá jednotka má práve jedno LO, ID sú unikátne a všetkých 26 strán má jednotky.
- Beh 2: `gpt-oss-120b` vrátil v prvom hodnotení pre 2 LO poškodený JSON. Tieto 2 LO v prvom kole posúdil iba `qwen3.8-27b`.
- Beh 3: LLM dvakrát vrátil pre strany 13–15 odrezanú odpoveď. Tieto strany sa preto extrahovali po jednej, čo dalo po odstránení duplicít 25 jednotiek (beh 2: 21). Beh 3 bol po páde obnovený z checkpointu.
- Strana zdroja: v oboch behoch sa pri 2 jednotkách opravilo číslo sekcie od LLM podľa zhody s textom.

## Porovnanie s bakalárskou prácou

Číslo z bakalárskej práce je z `OZNAL_GENERATION_OUTPUTS/output/evaluations_final_metrics_OZNAL.json` (hodnotenie konečných LO):

| | Bakalárska práca | Baseline (priemer behov 2 a 3) |
| --- | --- | --- |
| Rozsah | celý predmet OZNAL, 695 LO | kapitola 3, 140 a 148 LO |
| Faithfulness – `gpt-oss-120b` | 4,19 | 4,32 |

Porovnanie je iba orientačné:

- **Iný rozsah:** celý predmet oproti jednej kapitole. LO z bakalárskej práce sa nedajú obmedziť na kapitolu 3, súbor neobsahuje zdroj LO.
- **Spoločný je iba `gpt-oss-120b`.** Bakalárska práca hodnotila aj modelmi `kimi-k2` a `llama-3.3-70b`, ktoré sú dnes dostupné iba cez OpenRouter.
- **Iný model generovania:** bakalárska práca generovala modelom Llama 3.3 70B.
- **Podiel LO, ktoré prejdú filtrom, sa porovnať nedá.** Filter vyradí LO pre „neatomické“ alebo „nie samostatné“ iba vtedy, keď sa zhodnú všetky modely. Výsledok preto závisí od počtu hodnotiacich modelov: tie isté LO z bakalárskej práce prejdú na 87,9 % s tromi modelmi, ale iba na 65,6 % so samotným `gpt-oss-120b`.

## Cieľ pre ďalšie zmeny

Na rovnakom dokumente a s rovnakými modelmi (generovanie aj hodnotenie), konečný stav:

- faithfulness (`gpt-oss-120b`) aspoň **4,31**,
- podiel LO, ktoré prejdú filtrom, aspoň **93,6 %**.

Ciele sú nižšia hodnota z dvoch behov, teda „aspoň taký ako dnes“. Dva behy sú stále malá vzorka. Výsledok tesne pod cieľom (napr. faithfulness 4,29) ešte neznamená zhoršenie, kým ho nepotvrdí ďalší beh.

## Neplatný a prerušený beh

- **Beh 1 sa nezapočítava.** Odhalil chybu: keď sa v regenerácii nepodarilo spracovať jednu dávku LO, výsledky sa priradili k nesprávnym jednotkám, 5 LO sa stratilo a 5 bolo dvakrát. Chyba je opravená a pokrytá testami (commit `4b3b425`).
- **Beh 3 najprv spadol.** LLM vrátil pri extrakcii jednotiek odrezanú odpoveď a extrakcia na to nebola pripravená. Teraz sa neplatná odpoveď skúsi znova, potom sa strany extrahujú po jednej a stranu bez platnej odpovede služba preskočí s varovaním `sections_skipped`. Beh 3 potom pokračoval z checkpointu a pri stranách 13–15 túto opravu použil.

## Ako meranie zopakovať

```bash
cd fastapi
# dokument patrí do input/ (je v .gitignore)
docker run --rm -v "$(pwd):/app" -w /app lo-generator-fastapi \
  python -m lo_pipeline.runner input/Testing_text.pdf --out output/baseline.json
# čísla sú vo výsledku v časti "quality"

# čísla z bakalárskej práce (voliteľne iba vybrané modely)
docker run --rm -v "$(pwd)/..:/repo" -w /repo/fastapi lo-generator-fastapi \
  python -m analysis.thesis_quality_numbers ../OZNAL_GENERATION_OUTPUTS/output/evaluations_final_metrics_OZNAL.json \
  --models openai/gpt-oss-120b
```
