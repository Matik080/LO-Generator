# Jira úlohy – Mlynček B (šprint 2: rovnaký rozsah ako Mlynček A nad vlastným jadrom)

**Cieľ šprintu:** Mlynček B ponúka cez rovnaké rozhranie rovnaký rozsah ako Mlynček A, ale nad vlastným jadrom (atomické jednotky → LO s hodnotením a regeneráciou → graf prerekvizít a pojmov): všetky režimy rozdelenia do hodín, všetky typy otázok, jazyk výstupu, voliteľné OCR, odolnosť vo viacerých kontajneroch a Laravel prepnutý z mocku na skutočnú službu.

**Zásada:** S Mlynčekom A je spoločné iba rozhranie (endpointy, `options`, stavy, kódy chýb, tvar `units` a reportu a kódy varovaní, ktoré Laravel ukazuje učiteľovi). Spracovanie je vlastné: nové funkcie stoja na jadre B a jadro sa nemení. Každá úloha uvádza, z ktorej časti jadra vychádza.

Predchádzajúci šprint (úlohy 1–7, prototyp integrácie) je v [jira-sprint-mlyncek-b.md](jira-sprint-mlyncek-b.md). Návrh je v [api-design.md](api-design.md) (kap. 6–8) a ciele kvality v [baseline-quality.md](baseline-quality.md).

| # | Úloha | Priorita | Odhad (MD) | Závisí od |
| --- | --- | --- | --- | --- |
| 1 | Nové typy otázok nad learning objektmi B | High | 1,5–2 | – |
| 2 | Rozdelenie do hodín podľa grafu prerekvizít B | High | 3–3,5 | 1 (poradie typov v hodine) |
| 3 | Jazyk výstupu a voliteľné OCR v extrakcii B | Medium | 2 | – |
| 4 | Výstup units a report podľa DB ECEH (doladenie) | Medium | 1–1,5 | 1, 2 |
| 5 | Odolnosť a viac workerov | Medium | 1,5–2 | – |
| 6 | Nasadenie s ECEH, prepnutie Laravelu a aktualizácia špecifikácie | High | 1,5–2 | 1–5 |

---

## 1. Nové typy otázok nad learning objektmi B

Jadro B ostáva bez zmeny: 1 atomická jednotka → 1 LO → 1 otázka. LO už dnes obsahuje otázku, odpoveď a nápovedu a B už dnes vytvára otázky `choice` (LO s MC možnosťami) a `open` (ostatné LO). Úloha iba dopĺňa, čo v kontrakte chýba, a robí to okolo jadra, nie v ňom:

- `choice` s viacerými správnymi možnosťami: rozšírenie dnešného kroku MC variantov, ak jednotka viac správnych odpovedí pripúšťa; inak ostáva jedna správna ako dnes.
- `no_answer` (úloha bez vyhodnotenia): nový malý krok po grafe, jedna úloha zo skupiny LO s rovnakým pojmom `anchor` (graf pojmov B). Generovanie a filter LO sa nemenia.
- `per_section`: B dáva jednu otázku na LO (LO je atomické, viac otázok k jednému faktu by sa opakovalo). Pri `per_section > 1` je v reporte varovanie, ako dnes.

Nápovedy ostávajú dve: prvú (zdroj, teda súbor a strana) vytvorí kód, druhú napíše LLM. Druhá nápoveda je vlastnosť B. Časy sa orežú na rozsahy podľa typu (kap. 7). Nové volania LLM bežia súbežne (`map_batches`).

Akceptačné kritériá:

- extrakcia jednotiek, generovanie, hodnotenie a filter LO a graf fungujú rovnako ako doteraz (snapshot týchto častí sa nemení),
- `choice` má aspoň 2 možnosti a aspoň 1 správnu, `open` má práve 1 položku a `no_answer` žiadnu,
- `no_answer` vzniknú pri skupinách LO s rovnakým `anchor`, ak ich `questions.types` obsahuje; varovanie `question_type_unsupported` sa už neobjavuje,
- testy s falošným LLM pre viac správnych možností a pre `no_answer`; nová nahrávka iba pre nové volania,
- na `Testing_text.pdf` 2 behy splnia ciele baseline B (faithfulness `gpt-oss-120b` ≥ 4,31, podiel LO, ktoré prejdú filtrom, ≥ 93,6 %).

## 2. Rozdelenie do hodín podľa grafu prerekvizít B

Rozdelenie stojí na grafe prerekvizít a grafe pojmov, ktoré B už vytvára (kap. 8):

- **Poradie LO** je topologické poradie grafu prerekvizít. Pri zhode rozhoduje súbor a strana atomickej jednotky.
- **Témy** sú komunity v grafe pojmov (Louvain). LO sa k téme priradí podľa svojho pojmu `anchor`.

Režimy (kontrakt rovnaký ako pri Mlynčeku A):

- `single`: jedna hodina,
- `lessons`: `lesson_count` súvislých úsekov s čo najrovnomernejšou dĺžkou; rez je radšej na hranici témy,
- `topics`: hodina = komunita; veľké komunity sa rozdelia, malé susedné sa spoja,
- `syllabus`: zoznam hodín so stranami (`D1:1-10`), ktoré sa porovnajú so stranami atomických jednotiek, inak sa LO priradí podľa podobnosti (MiniLM). Text `syllabus_text` LLM iba prevedie na zoznam hodín.

`lesson_minutes` sa prijme, ale počet hodín neurčuje (rovnako ako pri Mlynčeku A). Názov a popis hodiny vytvorí LLM podľa LO v hodine, aj v režime `single`. V hodine idú otázky v poradí LO a pri jednom LO najprv `choice`, potom `open` a nakoniec `no_answer`. Varovania, ktoré Laravel ukazuje učiteľovi, majú rovnaké kódy ako pri Mlynčeku A. Rozdelenie nikdy nespôsobí zlyhanie úlohy: pri chybe sa použije záloha `topics` alebo náhradné názvy. Pribudne krok `split`.

Akceptačné kritériá:

- žiadna hodina neporuší prerekvizity z grafu B; pri sylabe učiteľa je porušenie iba varovanie,
- žiadna hodina nie je prázdna,
- `lessons` vráti `lesson_count` hodín, alebo menej s varovaním,
- `topic` (komunita z grafu pojmov) je vyplnené v reporte v `sections` aj v `question_meta`,
- unit testy pokrývajú všetky režimy vrátane cyklov, prázdneho vstupu, neplatného sylabu a strany `D3` pri dvoch súboroch (400 `invalid_options`),
- varovanie `split_mode_unsupported` sa už neobjavuje.

## 3. Jazyk výstupu a voliteľné OCR v extrakcii B

Krok `language` (`auto` / `sk` / `en`; pri `auto` sa jazyk rozpozná z textu). Jazyk dostanú prompty B (extrakcia jednotiek, LO, hodnotenie, otázky) aj texty, ktoré píše kód („Zdroj“ / „Source“, „strana“ / „page“). Na slovenskom texte treba overiť normalizáciu pojmov (spaCy) a prah deduplikácie jednotiek, lebo na nich stojí graf.

OCR (`extraction.ocr`) spracuje strany PDF a obrázky v PPTX bez textu. Výsledok ide do rovnakých sekcií ako dnes. Image sa dá zostaviť s OCR aj bez neho (build argument) a `/v1/info` hlási `ocr_available`.

Akceptačné kritériá:

- slovenský aj anglický dokument dajú jednotky, LO aj otázky v správnom jazyku,
- naskenovaný PDF s OCR dá jednotky so správnymi stranami, bez OCR skončí `extraction_failed`,
- image bez OCR naštartuje bez OCR knižníc a `extraction.ocr = true` v ňom vráti 400 `invalid_options`,
- testy bežia bez skutočného LLM.

## 4. Výstup units a report podľa DB ECEH (doladenie)

Príliš dlhé `name`, `description` alebo `choices` sa najprv skrátia jedným volaním LLM (kap. 6, bod 2). Ak je text stále dlhší, `name` sa skráti na koniec slova a pri ostatných poliach sa otázka vyradí s varovaním `field_too_long`. Názov nápovedy má najviac 50 znakov.

Report ukazuje to, čo má iba B:

- `sections` = atomické LO s typom, tvrdením, prerekvizitami z grafu a témou,
- `quality` zo skóre hodnotenia B,
- väzbu otázok na LO (`question_meta`, v ECEH `lo_ref`).

Akceptačné kritériá:

- každý výstup prejde JSON schémou a limitmi stĺpcov ECEH,
- nebezpečné HTML sa do výstupu nedostane,
- report obsahuje každé LO aj každú otázku práve raz,
- testy pokrývajú dlhé texty a nebezpečné HTML z falošného LLM.

## 5. Odolnosť a viac workerov

Worker počas behu obnovuje heartbeat úlohy v Redise s expiráciou. Obnova po reštarte preberá iba úlohy s vypršaným heartbeatom, takže môže bežať viac workerov naraz (`docker compose up --scale worker=N`). Pribudne skript, ktorý scenáre výpadkov overí v kontajneroch s nahratými alebo falošnými odpoveďami LLM, teda bez nákladov.

Akceptačné kritériá: v kontajneroch prejde týchto 8 scenárov:

- reštart API hneď po odpovedi 202,
- reštart Redisu počas behu,
- pád workera (úloha pokračuje z checkpointu),
- druhý pád workera (`worker_lost`),
- zrušenie bežiacej úlohy,
- zrušenie čakajúcej úlohy,
- retry po `failed`,
- opakovanie webhooku.

Dva workery navyše nespracujú tú istú úlohu dvakrát.

## 6. Nasadenie s ECEH, prepnutie Laravelu a aktualizácia špecifikácie

Nasadenie s Adelou podľa [integracny-test-eceh.md](integracny-test-eceh.md). Laco prepne Laravel z mocku na Mlynček B, s rovnakým kódom ako pre Mlynček A. Patrí sem aj README, zapracovanie zmien do špecifikácie (Confluence a `docs/api-design*.md`) podľa zoznamu zmien a záverečné meranie kvality.

Akceptačné kritériá:

- `docker compose up` na čistom stroji spustí službu a `/health` vráti 200 do 60 s,
- Laravel vytvorí úlohu, dostane webhooky a uloží výsledok,
- špecifikácia zodpovedá implementácii a pomenúva, čím sa B líši od A: atomické jednotky, LO s hodnotením a regeneráciou, graf prerekvizít a pojmov, tematický celok = atomické LO s jednou otázkou, dve nápovedy, vstupy PPTX a HTML, kroky `units` a `graph`,
- 2 behy na `Testing_text.pdf` splnia ciele z baseline.

---

## Poznámky

- **Dve nápovedy** sú vedomý rozdiel oproti Mlynčeku A, ktorý má po požiadavke tímu iba nápovedu so zdrojom. Ak tím bude chcieť rovnaké správanie, druhá nápoveda sa dá vypnúť (`questions.hints = 1`).
- **Skutočné volania LLM (Groq)** pribudnú pri novej nahrávke testov (úlohy 1 a 2) a pri meraní kvality (úlohy 1 a 6). Pred každým takým behom treba odhadnúť počet volaní.
