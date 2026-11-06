# Jira úlohy – Mlynček B (šprint: prototyp integrácie)

**Cieľ šprintu:** Laravel (backend ECEH) zavolá Mlynček B cez `/v1` rovnako ako Mlynček A, služba spracuje dokument na pozadí a vráti výsledok vo formáte `units`. Beží v `docker compose` v internej sieti spolu s ECEH.

Úlohy zodpovedajú úlohám Mlynčeka A (SCRUM-40 až 43), aby sa dali porovnať. Odhady sú z kap. 11 návrhu ([api-design.md](api-design.md)).

| # | Úloha | Priorita | Odhad (MD) | Závisí od |
| --- | --- | --- | --- | --- |
| 1 | Baseline kvality, kontrakt a mock API | High | 1,5 | – |
| 2 | Refaktor jadra do balíka lo_pipeline | Medium | hotové | – |
| 3 | LLM vrstva s vymeniteľným poskytovateľom a konfigurácia | Medium | 1–1,5 | 1 (baseline) |
| 4a | Fronta úloh a worker: spracovanie dokumentu na pozadí (Redis + RQ, /data, checkpoint) | High | 2–2,5 | – |
| 4b | REST API /v1: vytvorenie úlohy, stav, výsledok, chyby a API kľúč | High | 1,5–2 | 4a, 5 (pre `/result`) |
| 5 | Výstup vo formáte units a report | High | 1,5–2 | – |
| 6 | Docker compose a nasadenie prototypu | High | 1 | 4b |
| 7 | Webhooky, retry a mazanie vstupov (ak zostane čas) | Medium | 2–2,5 | 4a, 4b |

---

## 1. Baseline kvality, kontrakt a mock API

Zmerať východiskovú kvalitu súčasného spracovania na referenčnom dokumente (priemerné skóre faithfulness a podiel LO, ktoré prejdú filtrom kvality) a zapísať ju ako cieľ pre ďalšie zmeny. Spolu s Mlynčekom A zafixovať kontrakt (endpointy, `options`, stavy, chybové kódy, JSON schéma `units` a reportu). Spustiť mock API s pripravenými odpoveďami, aby Backend mohol začať integráciu ešte pred dokončením spracovania.

Akceptačné kritériá:

- `docs/baseline-quality.md` obsahuje dokument, commit, modely, priemernú faithfulness a podiel LO, ktoré prejdú filtrom (na prvý pokus aj po regenerácii), a porovnanie s výsledkami z bakalárskej práce,
- `run_pipeline` vracia súhrn kvality (`quality`), ktorý neskôr použije `/report`,
- JSON schémy `units` a reportu sú v repozitári a zhodné s Mlynčekom A (jeden spoločný súbor),
- mock `/v1` (`POST /v1/jobs`, `GET /v1/jobs/{id}`, `/result`, `/report`, `/health`) vracia príklady z Prílohy A a tieto príklady prejdú schémou.

## 2. Refaktor jadra do balíka lo_pipeline *(hotové)*

Spracovanie je oddelené od HTML stránky a od globálneho stavu. Jedna funkcia `run_pipeline(sources, options, progress_callback, checkpoint)` spustí všetky kroky a vráti výsledok v pamäti. Pri refaktore sa opravili chyby, ktoré ovplyvňovali kvalitu výsledkov.

Akceptačné kritériá:

- jedno volanie `run_pipeline` spustí všetky kroky bez zápisu súborov a bez globálnych premenných,
- priebeh sa hlási po krokoch a po batchoch cez `progress_callback` (iba rastie),
- prerušený beh pokračuje z checkpointu s rovnakým výsledkom a bez opakovania volaní LLM,
- testy bežia offline s nahratými odpoveďami LLM a výstup sa porovnáva so snapshotom,
- opravené: odstraňovanie cyklov v grafe prerekvizít, filter kvality (vyhodnotenie poslednej regenerácie, miešanie starých skóre, chýbajúce MC možnosti po regenerácii), zdroj a strana pri každej atomickej jednotke.

## 3. LLM vrstva s vymeniteľným poskytovateľom a konfigurácia

Rozhranie pre generovanie a embeddingy (Príloha B): adaptér pre OpenAI-kompatibilné API (Groq, OpenRouter, lokálny vLLM alebo Ollama) s nastaviteľnou base URL, lokálny MiniLM ako predvolený Embedder, voliteľne embeddingy cez API. Konfigurácia cez pydantic-settings s prefixom `LOGEN_`, limit súbežných volaní LLM a zrozumiteľné chyby namiesto pádu služby.

Akceptačné kritériá:

- zmena poskytovateľa alebo modelu je iba zmena `.env` (`LOGEN_LLM_BASE_URL`, `LOGEN_LLM_MODEL`, …), mimo `lo_pipeline/llm` sa neimportuje knižnica výrobcu,
- chýbajúci alebo neplatný kľúč a zlyhanie LLM po opakovaniach ukončí úlohu ako `failed` s kódom `llm_auth` / `llm_error`, nikdy nie prázdny výsledok,
- modely pre hodnotenie, prahy filtra a veľkosti batchov sú v konfigurácii, nie napevno v kóde; staré premenné (`GROQ_API_KEY`, `GEN_MODEL`, …) fungujú ako náhradné,
- `env_template` je premenovaný na `.env.example` a obsahuje všetky premenné,
- kvalita na referenčnom dokumente je aspoň na úrovni baseline (úloha 1).

## 4a. Fronta úloh a worker: spracovanie dokumentu na pozadí (Redis + RQ, /data, checkpoint)

Worker si berie úlohy z fronty v Redise a pre každú spustí `run_pipeline`. Každá úloha má vlastný priečinok `/data/jobs/{id}/` so vstupným súborom, `job.json`, checkpointom, `result.json` a `report.json`. Stav, krok a priebeh úlohy sú v Redise. Spracovanie nezávisí od HTTP spojenia a prežije reštart workera.

Akceptačné kritériá:

- úlohu zaradenú do fronty spracuje worker a výsledok aj report uloží do `/data/jobs/{id}/`,
- stav (`queued` / `running` / `completed` / `failed` / `cancelled`), krok a priebeh sa počas behu zapisujú do Redisu,
- po reštarte workera úloha pokračuje z checkpointu, najviac 2 pokusy, potom `failed` s kódom `interrupted`,
- chyba počas spracovania ukončí úlohu ako `failed` s kódom chyby (`no_text_extracted`, `llm_error`, `internal_error`, …), nikdy nie prázdny výsledok,
- zrušenie úlohy zastaví spracovanie a úloha dostane stav `cancelled`,
- Redis ukladá dáta na disk (appendonly), takže fronta aj stav prežijú reštart,
- testy s falošným LLM pokrývajú dokončenie úlohy, chybu a reštart workera počas behu.

## 4b. REST API /v1: vytvorenie úlohy, stav, výsledok, chyby a API kľúč

FastAPI endpointy podľa Prílohy A, cez ktoré Laravel pošle dokument, sleduje stav úlohy a stiahne výsledok. Jednotný formát chýb, overenie API kľúčom a ochrana pred duplicitnou úlohou cez `external_ref`.

Akceptačné kritériá:

- `POST /v1/jobs` (PDF, PPTX, HTML + `options`) vráti 202 a `job_id` až po zápise súboru a `job.json` na disk a zaradení do fronty; zlý typ alebo veľkosť súboru vráti 415 / 413,
- `GET /v1/jobs/{id}` vráti stav, krok, priebeh a chybu; `/result` a `/report` vrátia výsledok alebo `409 result_not_ready`; `DELETE` úlohu zruší alebo zmaže,
- `GET /v1/jobs?external_ref=…` nájde úlohu; druhý `POST` s rovnakým `external_ref` vráti pôvodné `job_id` (200) a nevytvorí novú úlohu,
- bez platného `X-API-Key` vráti 401 (ak je nastavené `LOGEN_API_KEYS`); nad `LOGEN_MAX_ACTIVE_JOBS` vráti `429 too_many_jobs` s hlavičkou `Retry-After`,
- všetky chyby majú tvar `{"error": {"code", "message", "details"}}`,
- `/health`, `/v1/info` a `/docs` fungujú (`/docs` sa dá vypnúť cez `LOGEN_DOCS_ENABLED`); integračné testy API bežia s falošným LLM.

## 5. Výstup vo formáte units a report

Mapovanie výsledku spracovania do spoločného formátu `units` (Príloha A) a reportu s väzbou otázok na LO. Pre prototyp iba režim rozdelenia `single` (jedna hodina) a súčasné typy otázok `choice` (jedna správna odpoveď) a `open`. Nové typy otázok a ďalšie režimy rozdelenia sú v ďalšom šprinte.

Akceptačné kritériá:

- výsledok prejde JSON schémou `units` (`additionalProperties: false`, `maxLength` podľa DB ECEH); výsledok, ktorý schémou neprejde, ukončí úlohu ako `failed` s kódom `invalid_output`,
- HTML v `hints[].url` je prečistené (iba povolené značky); zdroj `[Source: …]` nie je v nápovede ani v názve otázky, je v reporte,
- report obsahuje `learning_objects`, `question_map`, `quality`, `warnings`, `timings` a `extensions` (graf prerekvizít ako JSON, skóre hodnotenia),
- `split.mode = single` vráti práve jeden unit s `title` a `description`,
- unit testy mapovania a test schémy, aj s dlhými textami a nebezpečným HTML z falošného LLM.

## 6. Docker compose a nasadenie prototypu

Docker image a compose pre `api`, `worker` a `redis` (s heslom a appendonly), modely zabalené v image, volume `/data` a interná sieť s ECEH. Spolu s Adelou nasadiť službu vedľa ECEH a overiť jedno volanie z Laravelu od začiatku do konca.

Akceptačné kritériá:

- `docker compose up` na čistom stroji (iba Docker + `.env`) spustí službu a `/health` vráti 200 do 60 s,
- služba pri štarte nepotrebuje internet (MiniLM, spaCy a NLTK sú v image),
- port API nie je zverejnený mimo internej siete, Redis má heslo, kontajner nebeží ako root,
- README obsahuje spustenie, premenné prostredia a príklad volania cez `curl`,
- Laravel (alebo `curl` z jeho siete) vytvorí úlohu a stiahne výsledok vo formáte `units`.

## 7. Webhooky, retry a mazanie vstupov *(ak zostane čas)*

Webhook pre Backend pri zmene stavu úlohy, opakovanie neúspešnej úlohy a mazanie vstupov a výsledkov po uplynutí doby uchovania. Pre prototyp stačí polling cez `GET /v1/jobs/{id}`, preto je táto úloha voliteľná.

Akceptačné kritériá:

- pri `job.stage_changed`, `job.completed`, `job.failed` a `job.cancelled` príde POST na `callback_url` s podpisom HMAC (`X-QGen-Signature`); pri chybe 3 opakovania (10 s, 60 s, 300 s),
- `callback_url` mimo `LOGEN_WEBHOOK_ALLOWED_HOSTS` vráti `400 invalid_options`,
- `POST /v1/jobs/{id}/retry` znova spustí úlohu v stave `failed` s rovnakým `job_id`, kým služba nezmaže vstup (24 h), potom vráti `409 job_not_retryable`,
- vstupný súbor sa zmaže po `completed` alebo `cancelled`, výsledky po 7 dňoch.

---

## Na ďalší šprint (mimo prototypu)

- Nové typy otázok: `choice` s viacerými správnymi odpoveďami, `no_answer`, 1–2 nápovedy, `per_learning_object` (kap. 7).
- Rozdelenie do hodín: režimy `lessons`, `topics`, `syllabus` (kap. 8).
- Voliteľné OCR a jazyk výstupu (`options.language`, slovenské zdroje).
