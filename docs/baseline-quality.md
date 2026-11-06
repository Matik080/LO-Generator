# Východisková kvalita (baseline) – Mlynček B

Východisková hodnota pre metriku **„Kvalita sa nezhorší“** z [návrhu](api-design.md): priemerné skóre faithfulness voči zdroju a podiel LO, ktoré prejdú filtrom kvality. Ďalšie zmeny (nové typy otázok, iné modely, rozdelenie do hodín) sa porovnávajú s týmito číslami na rovnakom dokumente a s rovnakými modelmi.

## Podmienky merania

| | |
| --- | --- |
| Dátum | 6. 11. 2026 |
| Dokument | `Testing_text.pdf` – OZNAL, kapitola 3 *Exploratory Data Analysis*, 26 strán (rovnaký testovací dokument ako v bakalárskej práci) |
| Kód | commit `ea0eed2` + oprava párovania regenerovaných LO s jednotkami *(doplniť hash po commite)* |
| Generovanie | `qwen/qwen3.8-27b` (Groq) |
| Hodnotenie | `qwen/qwen3.8-27b`, `openai/gpt-oss-120b` (Groq) |
| Embeddingy | `all-MiniLM-L6-v2` (lokálne) |
| Filter kvality | faithfulness ≥ 3,0 (priemer modelov), hint quality ≥ 2,0, najviac 2 kolá regenerácie |
| Trvanie | približne 17 minút |

## Výsledky

140 atomických jednotiek → 140 LO (každá jednotka má práve jedno LO, ID sú unikátne). 130 LO má otázku s možnosťami (všetky LO typov vhodných pre `choice`).

**Prvé hodnotenie** = LO z prvého generovania, pred regeneráciou. **Konečný stav** = verzie LO, ktoré pipeline vráti.

| Metrika | Prvé hodnotenie | Konečný stav |
| --- | --- | --- |
| Podiel LO, ktoré prejdú filtrom | 74,3 % (104 / 140) | **93,6 %** (131 / 140) |
| Faithfulness – `gpt-oss-120b` | 4,21 | **4,31** |
| Faithfulness – `qwen3.8-27b` | 4,07 | 4,15 |
| Faithfulness – priemer oboch modelov | 4,14 | 4,23 |
| Question clarity – priemer | 4,32 | 4,35 |
| Hint quality – priemer | 4,27 | 4,28 |
| Distractor plausibility – priemer | 2,52 | 2,52 |

- Regenerovaných LO: 31. Po dvoch kolách stále neprešlo 9 LO; vo výsledku sú v najlepšie hodnotenej verzii.
- Skóre sú na škále 1–5.
- `gpt-oss-120b` vrátil v prvom hodnotení pre 2 LO poškodený JSON. Tieto 2 LO v prvom kole posúdil iba `qwen3.8-27b`.
- Strana zdroja: pri 2 zo 151 jednotiek sa číslo sekcie od LLM opravilo podľa zhody s textom.
- Graf prerekvizít: 128 hrán.

## Porovnanie s bakalárskou prácou

Číslo z bakalárskej práce je z `OZNAL_GENERATION_OUTPUTS/output/evaluations_final_metrics_OZNAL.json` (hodnotenie konečných LO):

| | Bakalárska práca | Baseline |
| --- | --- | --- |
| Rozsah | celý predmet OZNAL, 695 LO | kapitola 3, 140 LO |
| Faithfulness – `gpt-oss-120b` | 4,19 | 4,31 |

Porovnanie je iba orientačné:

- **Iný rozsah:** celý predmet oproti jednej kapitole. LO z bakalárskej práce sa nedajú obmedziť na kapitolu 3, súbor neobsahuje zdroj LO.
- **Spoločný je iba `gpt-oss-120b`.** Bakalárska práca hodnotila aj modelmi `kimi-k2` a `llama-3.3-70b`, ktoré sú dnes dostupné iba cez OpenRouter.
- **Iný model generovania:** bakalárska práca generovala modelom Llama 3.3 70B.
- **Podiel LO, ktoré prejdú filtrom, sa porovnať nedá.** Filter vyradí LO pre „neatomické“ alebo „nie samostatné“ iba vtedy, keď sa zhodnú všetky modely. Výsledok preto závisí od počtu hodnotiacich modelov: tie isté LO z bakalárskej práce prejdú na 87,9 % s tromi modelmi, ale iba na 65,6 % so samotným `gpt-oss-120b`.

## Cieľ pre ďalšie zmeny

Na rovnakom dokumente a s rovnakými modelmi (generovanie aj hodnotenie):

- faithfulness (`gpt-oss-120b`, konečný stav) aspoň **4,31**,
- podiel LO, ktoré prejdú filtrom (konečný stav), aspoň **93,6 %**.

Výstupy LLM sa medzi behmi líšia. Toto je jeden platný beh, preto ešte nevieme, aký rozdiel je iba náhodný. Pred prísnym porovnaním odporúčam druhý beh na odhad rozptylu.

**Neplatný beh:** prvý beh (`baseline_run1`) sa do výsledkov nezapočítava. Odhalil chybu: keď sa v regenerácii nepodarilo spracovať jednu dávku LO, výsledky sa priradili k nesprávnym jednotkám, 5 LO sa stratilo a 5 bolo dvakrát. Chyba je opravená a pokrytá testami.

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
