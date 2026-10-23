# Prílohy

Prílohy obsahujú konkrétne technické podklady pre implementáciu [Mlynčeka B](api-design.md). Ide o náčrt, pri implementácii sa môžu zmeniť bez zmeny samotného návrhu.

**API kontrakt je rovnaký ako pri Mlynčeku A** (stránka *Prílohy*): rovnaké endpointy, parametre `options`, stavy, chybové kódy aj výstupný formát. Položky, ktoré má iba Mlynček B, sú označené **(B)**. Položky, na ktorých sa obe stránky zatiaľ nedohodli, sú označené **(návrh)**.

---

## Príloha A – Detaily API

Všetky endpointy majú prefix `/v1`. Ak je nastavený `LOGEN_API_KEYS`, stránka posiela header `X-API-Key`.

| Metóda | Endpoint | Popis |
| --- | --- | --- |
| GET | `/health` | Stav služby (API, Redis, worker). |
| GET | `/v1/info` | Verzia služby, nastavené modely, limity. |
| POST | `/v1/jobs` | Vytvorí úlohu: multipart `files[]` (PDF, HTML, PPTX **(B)**) + `options` (JSON). Vráti 202 a `job_id`. Spustí všetky kroky. |
| GET | `/v1/jobs?external_ref=…` | Zoznam úloh s daným identifikátorom stránky (najnovšie prvé): `job_id`, stav, čas vytvorenia. |
| GET | `/v1/jobs/{id}` | Stav úlohy: `queued` / `running` / `completed` / `failed` / `cancelled`, krok, priebeh, chyba. |
| GET | `/v1/jobs/{id}/result` | Výsledok presne vo formáte units (`{"units": [...]}`). Každý unit je jedna hodina. |
| GET | `/v1/jobs/{id}/report` | Údaje mimo formátu units: LO, citované strany, skóre kvality, časy spracovania, varovania rozdelenia. Pri B aj graf prerekvizít. |
| DELETE | `/v1/jobs/{id}` | Zruší bežiacu úlohu alebo zmaže výsledky. |
| GET | `/v1/schema/lesson` | JSON schéma formátu units. |

FastAPI navyše poskytuje `/docs` a `/openapi.json` (R14).

**Príklad: vytvorenie úlohy**

```bash
curl -X POST http://localhost:8000/v1/jobs \
  -H "X-API-Key: <kluc>" \
  -F "files=@modul1.pdf" \
  -F 'options={"split": {"mode": "topics", "lesson_minutes": 45}}'
```

`HTTP/1.1 202 Accepted`
```json
{ "job_id": "7f3c2a1e", "status": "queued", "links": { "self": "/v1/jobs/7f3c2a1e" } }
```

**Príklad: stav úlohy**

`GET /v1/jobs/7f3c2a1e`
```json
{
  "job_id": "7f3c2a1e",
  "external_ref": "user-152",
  "status": "running",
  "stage": "questions",
  "progress": 0.62,
  "created_at": "2026-11-03T10:12:01Z",
  "updated_at": "2026-11-03T10:16:40Z",
  "error": null
}
```

Neúspešná úloha má rovnaký tvar, ale `"status": "failed"` a `error` v jednotnom formáte chyby:
```json
{
  "job_id": "7f3c2a1e",
  "external_ref": "user-152",
  "status": "failed",
  "stage": "extract",
  "progress": 0.02,
  "created_at": "2026-11-03T10:12:01Z",
  "updated_at": "2026-11-03T10:12:09Z",
  "error": {
    "code": "no_text_extracted",
    "message": "V súbore 'sken.pdf' sa nenašiel žiadny text. Dokument môže byť naskenovaný; skúste extraction.ocr = true.",
    "details": { "file": "sken.pdf" }
  }
}
```

Názvy krokov v poli `stage` v poradí: `extract`, `language`, `units`, `learning_objects`, `lo_evaluation`, `graph`, `questions`, `q_evaluation`, `split`, `output` zodpovedajú krokom Mlynčeka A, `units` a `graph` má iba B. `progress` je číslo od 0 do 1, vážené obvyklým počtom volaní LLM v jednotlivých krokoch.

### Vstupné parametre (`options`)

Parametre sú rovnaké ako pri Mlynčeku A. Všetky sú voliteľné, chýbajúce hodnoty sa doplnia z konfigurácie. Bez parametra `split` sa všetko dá do jednej hodiny.

```json
{
  "external_ref": "user-152",
  "language": "auto",
  "extraction": { "ocr": false },
  "questions": {
    "types": ["choice", "open", "no_answer"],
    "per_learning_object": 3,
    "choice_options": 4,
    "hints": 2
  },
  "split": {
    "mode": "topics",
    "lesson_count": null,
    "lesson_minutes": 45,
    "syllabus": null
  }
}
```

| Parameter | Význam | Poznámka k B |
| --- | --- | --- |
| `external_ref` | vlastný identifikátor stránky (voliteľný) | – |
| `language` | `auto` \| `sk` \| `en` | prompty B sú currently v angličtine, generovanie v slovenčine je future work (hlavná stránka, kap. 4, 5) |
| `extraction.ocr` | OCR obrázkov (voliteľné) | B dnes OCR nemá, samostatný variant obrazu |
| `questions.types` | ktoré typy generovať | – |
| `questions.per_learning_object` | počet otázok na jedno LO | B dnes vytvára jedno LO = jednu otázku, ďalšie pribudnú v novom kroku 7 |
| `questions.choice_options` | počet možností pri `choice` | B dnes vždy generuje 4 (3 distraktory + správna) |
| `questions.hints` | počet nápovied (napr. ľahká a ťažká) | B má dnes 1 nápovedu |
| `split.mode` | `single` \| `lessons` \| `topics` \| `syllabus` | – |
| `split.lesson_count` | počet hodín (`lessons`) | – |
| `split.lesson_minutes` | dĺžka hodiny v minútach | predvolene `LOGEN_DEFAULT_LESSON_MINUTES` |
| `split.syllabus` | zoznam hodín (`syllabus`) | – |

Príklady rozdelenia (rovnaké ako pri Mlynčeku A):
```text
"split": { "mode": "single" }
"split": { "mode": "lessons", "lesson_count": 6, "lesson_minutes": 45 }
"split": { "mode": "topics", "lesson_minutes": 45 }
"split": { "mode": "syllabus", "syllabus": [
    { "title": "Úvod do C", "pages": "D1:1-10" },
    { "title": "Smerníky a pamäť", "description": "malloc, free, smerníková aritmetika" },
    { "title": "Štruktúry" } ] }
```
`pages` = `D<číslo dokumentu>:<od>-<do>`. Pri B je "strana" HTML dokumentu sekcia pod nadpisom a "strana" PPTX je snímka (rovnako ako dnešné `source_page`).

**(B) voliteľný objekt `advanced`.** Slúži iba na testovanie a porovnávanie modelov. Stránka ho nepotrebuje a Mlynček A ho ignoruje.
```json
{
  "advanced": {
    "llm_model": "openai/gpt-oss-120b",
    "eval_models": ["qwen/qwen3.8-27b", "openai/gpt-oss-120b"],
    "faithfulness_threshold": 3.0,
    "max_attempts": 2
  }
}
```
Modely iba z povoleného zoznamu v konfigurácii; iné → `invalid_options`.

### Výsledok (`GET /v1/jobs/{id}/result`)

```json
{
  "units": [
    {
      "title": "Smerníky a dynamická pamäť",
      "description": "Smerníky, alokácia pamäte pomocou malloc a jej uvoľnenie pomocou free.",
      "questions": [
        {
          "name": "Návratová hodnota malloc",
          "description": "Ktoré tvrdenia o návratovej hodnote funkcie malloc sú pravdivé?",
          "type": "choice",
          "time_to_handle": 90,
          "time_to_explain": 30,
          "hints": [
            { "name": "Ľahká nápoveda", "url": "<p>Ako potom pristupujete k alokovanej pamäti?</p>" },
            { "name": "Ťažká nápoveda", "url": "<p>Zamyslite sa, čo sa stane, keď nie je dosť pamäte.</p>" }
          ],
          "choices": [
            { "text": "Vráti smerník na začiatok alokovaného bloku", "is_correct": true },
            { "text": "Ak alokácia zlyhá, vráti NULL", "is_correct": true },
            { "text": "Vráti počet alokovaných bajtov", "is_correct": false },
            { "text": "Vráti kópiu dát", "is_correct": false }
          ]
        },
        {
          "name": "Uvoľňovanie dynamickej pamäte",
          "description": "Vysvetlite, prečo by každé volanie malloc malo mať zodpovedajúce volanie free.",
          "type": "open",
          "time_to_handle": 300,
          "time_to_explain": 60,
          "hints": [ { "name": "Nápoveda", "url": "<p>Čo sa stane s pamäťou, ktorá sa nikdy neuvoľní?</p>" } ],
          "choices": [
            { "text": "Pamäť z malloc zostáva rezervovaná, kým sa nezavolá free; bez free program stráca pamäť (memory leak).", "is_correct": true }
          ]
        },
        {
          "name": "Esej: manuálna vs. automatická správa pamäte",
          "description": "Napíšte krátku úvahu (5–10 viet), v ktorej porovnáte manuálnu správu pamäte v C s garbage collection.",
          "type": "no_answer",
          "time_to_handle": 600,
          "time_to_explain": 0,
          "hints": [],
          "choices": []
        }
      ]
    }
  ]
}
```

Výsledok sa pred uložením validuje voči schéme z `GET /v1/schema/lesson` (`additionalProperties: false` na každej úrovni). Schéma by mala byť jeden spoločný súbor pre Mlynček A aj B.

### Report (`GET /v1/jobs/{id}/report`)

Formát reportu nie je spoločný, každý mlynček vracia svoje interné údaje. Pri B:

```json
{
  "job_id": "7f3c2a1e",
  "options": { "split": { "mode": "topics", "lesson_minutes": 45 } },
  "sources": [
    { "document": "D1", "file": "modul1.pdf", "sections": 34, "units": 61, "ocr_pages": 0 }
  ],
  "lessons": [
    { "index": 1, "title": "Smerníky a dynamická pamäť", "learning_objects": ["3f9a1c2b", "a81c09de"], "seconds": 2610, "deviation": -0.033 }
  ],
  "learning_objects": [
    {
      "id": "3f9a1c2b",
      "lo_type": "definition",
      "source_statement": "Funkcia malloc vráti smerník na alokovaný blok pamäte.",
      "cited_pages": ["D1:12"],
      "anchor": "malloc",
      "prerequisite_ids": ["a81c09de"],
      "lesson": 1,
      "questions": [0, 1],
      "scores": { "faithfulness": 4.5, "hint_quality": 4.0, "atomic": "yes", "self_contained": "yes", "regenerated": false }
    }
  ],
  "knowledge_graph": {
    "nodes": [ { "id": "3f9a1c2b" }, { "id": "a81c09de" } ],
    "edges": [ { "source": "a81c09de", "target": "3f9a1c2b" } ]
  },
  "warnings": [
    { "code": "lesson_over_capacity", "message": "Hodina 3 prekračuje 45 minút o 24 %.", "lesson": 3 }
  ],
  "timings": { "extract": 12.4, "units": 141.0, "learning_objects": 220.7, "lo_evaluation": 310.2, "graph": 95.3, "questions": 180.6, "split": 8.1, "total": 968.3 },
  "models": { "llm": "openai/gpt-oss-120b", "eval": ["qwen/qwen3.8-27b", "openai/gpt-oss-120b"], "embeddings": "all-MiniLM-L6-v2" }
}
```

### Informácie o službe (`GET /v1/info`)

```json
{
  "service": "lo-generator",
  "version": "1.0.0",
  "models": { "llm": "openai/gpt-oss-120b", "eval": ["qwen/qwen3.8-27b", "openai/gpt-oss-120b"], "embeddings": "all-MiniLM-L6-v2" },
  "limits": { "max_upload_mb": 50, "max_pages": 300, "file_types": [".pdf", ".html", ".htm", ".pptx"] },
  "ocr_available": false
}
```

### Chybové odpovede

Všetky chyby majú rovnaký tvar: `{"error": {"code": "...", "message": "...", "details": {...}}}`. Kódy sú rovnaké ako pri Mlynčeku A.

| HTTP | code | Kedy |
| --- | --- | --- |
| 400 | `invalid_options` | Neplatná kombinácia parametrov (napr. `mode=syllabus` bez zoznamu hodín, model mimo povoleného zoznamu). |
| 401 | `unauthorized` | Chýba alebo je zlý API kľúč. |
| 404 | `job_not_found` | Úloha neexistuje alebo už bola zmazaná (TTL). |
| 409 | `result_not_ready` | Výsledok ešte nie je hotový. |
| 413 | `file_too_large` | Súbor je väčší ako limit alebo má priveľa strán. |
| 415 | `unsupported_file` | Súbor nie je PDF, HTML ani PPTX **(B: A prijíma iba PDF)**. |
| 422 | `validation_error` | Telo requestu nezodpovedá schéme (generuje FastAPI). |
| 502 | `llm_error` | Poskytovateľ LLM zlyhal aj po opakovaniach alebo vrátil neplatný JSON. |

**Kódy chýb neúspešnej úlohy** (pole `error.code` v stave úlohy `failed`) **(návrh pre A aj B)**:

| code | Kedy |
| --- | --- |
| `no_text_extracted` | V žiadnom dokumente sa nenašiel text (napr. sken bez OCR). |
| `no_learning_objects` | Z textu nevzniklo žiadne použiteľné LO. |
| `llm_error` | Poskytovateľ zlyhal aj po opakovaniach (ako HTTP 502). |
| `llm_auth` | Poskytovateľ odmietol API kľúč. |
| `invalid_output` | Výsledok neprešiel schémou units. |
| `interrupted` | Worker sa počas úlohy dvakrát reštartoval (limit 2 pokusov). |
| `internal_error` | Akákoľvek iná chyba; podrobnosti sú v logu úlohy. |

---

## Príloha B - LLM layer: rozhranie a konfigurácia

### Návrh rozhrania

Rozhranie je rovnaké ako pri Mlynčeku A. B posiela aj systémový prompt, preto má `generate` voliteľný argument `system`. Rozhranie pre A sa tým nemení.

```python
# lo_pipeline/llm/base.py
class LLMResponse:
    text: str            # rovnaké pole .text ako dnes -> ostatné moduly sa nemenia
    model: str
    usage: dict          # tokeny vstup / výstup (ak ich poskytovateľ vracia)

class LLMClient(Protocol):
    def generate(self, prompt: str, *, model: str, json_mode: bool = True,
                 temperature: float = 0.2, max_tokens: int | None = None,
                 system: str | None = None) -> LLMResponse: ...

class EmbeddingClient(Protocol):
    def embed(self, texts: list[str], *, model: str) -> list[list[float]]: ...

# lo_pipeline/llm/factory.py
def get_llm(role: str) -> LLMClient: ...     # role: units | lo | eval | prereq | questions | enrich | split
def get_embedder() -> EmbeddingClient: ...   # local (MiniLM) | openai_compatible
```

Dnešná funkcia `call_llm(messages, model, temperature, ...)` v `llm_service.py` zostane ako tenký obal nad `get_llm(role).generate(...)`. Doménové moduly ďalej volajú `call_llm` a ich prompty sa nemenia. Opakovanie pokusov, čakanie pri limite požiadaviek a `LEGACY_ROUTES` sa presunú do `OpenAICompatibleAdapter`.

| Adaptér | Pokrýva | Generovanie / embeddingy |
| --- | --- | --- |
| `OpenAICompatibleAdapter` | Groq, OpenRouter, OpenAI, Mistral, GitHub Models, Gemini (OpenAI endpoint), lokálny server (Ollama, vLLM, LM Studio) | áno / áno (`/v1/chat/completions`, `/v1/embeddings`) |
| `LocalEmbedder` **(B)** | `all-MiniLM-L6-v2` vo workeri (dnešný `embedding_service.py`) | – / áno |
| `AnthropicAdapter` | Claude modely (iba v prípade potreby) | áno / nie |

### Príklady konfigurácie

```bash
# Groq (dnešné predvolené nastavenie B)
LOGEN_LLM_PROVIDER=openai_compatible
LOGEN_LLM_BASE_URL=https://api.groq.com/openai/v1
LOGEN_LLM_API_KEY=...
# OpenRouter alebo iný cloud s OpenAI API: mení sa iba adresa a kľúč
LOGEN_LLM_BASE_URL=https://openrouter.ai/api/v1
# lokálny model u nás (Ollama / vLLM) - generovanie aj embeddingy
LOGEN_LLM_PROVIDER=openai_compatible
LOGEN_LLM_BASE_URL=http://llm:11434/v1
LOGEN_EMBED_PROVIDER=openai_compatible
LOGEN_EMBED_BASE_URL=http://llm:11434/v1
```

---

## Príloha C – Štruktúra projektu po zmenách

Štruktúra zodpovedá Mlynčeku A: jadro (`lo_pipeline/`, v A `qgen/`) je oddelené od služby (`service/`).

```text
fastapi/
├── lo_pipeline/                 # jadro - dnešné moduly bez HTML UI a globálneho stavu
│   ├── extraction/              # utilities.py (PDF/HTML/PPTX), ocr.py (NOVÉ, voliteľné), language.py (NOVÉ)
│   ├── units/                   # atomic_unit_extractor.py, concept_normalizer.py
│   ├── lo/                      # learning_object_generator.py, hodnotenie (learning_object_evaluator.py, filter z ems.py)
│   ├── graph/                   # graph_builder.py (prerekvizity, graf pojmov)
│   ├── questions/               # NOVÉ - choice/open/no_answer, nápovedy, časy (z echub_format_converter.py)
│   ├── split/lessons.py         # NOVÉ - rozdelenie do hodín (unit = hodina)
│   ├── output/units.py          # NOVÉ - mapovanie do formátu units + JSON schéma
│   ├── llm/                     # base.py, factory.py, adapters/, embeddings.py, json_load.py (parse_json)
│   ├── runner.py                # run_pipeline(options, progress_callback)
│   └── config.py                # nastavenia (pydantic-settings)
├── service/
│   ├── main.py                  # FastAPI aplikácia
│   ├── routes/                  # health.py, info.py, jobs.py, schema.py
│   ├── jobs/                    # queue.py, tasks.py, store.py
│   └── errors.py                # jednotný formát chýb
├── analysis/                    # skripty k bakalárskej práci: post_statistics.py, lexical_overlap_computation.py, atomic_unit_merger.py
├── tests/                       # unit, kontraktové a integračné testy
├── requirements.txt, requirements-dev.txt
└── Dockerfile, docker-compose.yml, .env.example
```

Odstráni sa: súčasný `main.py` s endpointmi pre jednotlivé kroky a globálnym prúdom logov (`_LogCapture`, `/logs`), `ems.process_book_with_checkpoints` (nahradí ho `runner.py`) a `pipeline_ui.html` (alebo zostane ako nástroj pre vývojárov mimo kontraktu, len na testing). `env_template` sa premenuje na `.env.example`, rovnako ako v A.

---

## Príloha D – Docker a konfigurácia

### Dockerfile (náčrt)

```dockerfile
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements.txt

# modely sa stiahnu pri builde - služba pri štarte nepotrebuje internet
RUN python -m spacy download en_core_web_sm \
 && python -c "import nltk; nltk.download('stopwords', download_dir='/usr/share/nltk_data')" \
 && python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2', cache_folder='/models')"

COPY lo_pipeline/ lo_pipeline/
COPY service/ service/
RUN useradd -m app && mkdir -p /data && chown -R app /app /models /data
USER app

EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
CMD ["uvicorn", "service.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- **torch** tool B potrebuje kvôli lokálnym embeddingom (sentence-transformers). Pri `LOGEN_EMBED_PROVIDER=openai_compatible` môžu torch a sentence-transformers z obrazu odpadnúť a obraz bude výrazne menší (otvorená otázka, spolu s OCR).
- **OCR** (voliteľné): samostatný build target s `tesseract-ocr` (`eng`, `slk`), `poppler-utils`, `pytesseract`, `pdf2image`, približne +100–150 MB.
- **GPU** netreba (MiniLM beží na CPU). Treba ju iba pre voliteľný lokálny kontajner `llm`.
- **Veľkosť:** odhad 1,5-2,5 GB s torch, bez neho niekoľko stoviek MB. *Po builde doplniť nameranú hodnotu (`docker images`).*
- **Python 3.11** používa B dnes (lock súbor je pre túto verziu). A používa 3.12. Prechod na 3.12 je možný, ale nie je nutný.

### docker-compose.yml (náčrt)

```yaml
services:
  api:
    build: .
    ports: ["8000:8000"]
    env_file: .env
    volumes: [data:/data]
    depends_on: [redis]
    restart: unless-stopped
  worker:
    build: .
    command: rq worker --url redis://redis:6379 logen
    env_file: .env
    volumes: [data:/data]
    depends_on: [redis]
    restart: unless-stopped
  redis:
    image: redis:7-alpine
    command: redis-server --appendonly yes   # queue a stav prežijú reštart
    volumes: [redis:/data]
    restart: unless-stopped
  llm:                                       # voliteľný lokálny model (generovanie aj embeddingy)
    image: ollama/ollama
    profiles: [local-llm]
    volumes: [models:/root/.ollama]
    deploy:
      resources:
        reservations:
          devices: [{ driver: nvidia, count: 1, capabilities: [gpu] }]
volumes:
  data:
  redis:
  models:
```

Queue sa volá `logen` (A používa `qgen`), takže oba mlynčeky môžu zdieľať jeden Redis, ak by sa testovali concurrently.

### Konfigurácia (premenné prostredia)

Prefix `LOGEN_` (A používa `QGEN_`) umožňuje prevádzkovať obe služby vedľa seba s jedným `.env`. Názvy premenných za prefixom zodpovedajú A.

| Premenná | Predvolená hodnota | Význam |
| --- | --- | --- |
| `LOGEN_LLM_PROVIDER` | `openai_compatible` | `openai_compatible` \| `anthropic` (Gemini ide cez svoj OpenAI endpoint) |
| `LOGEN_LLM_BASE_URL` / `LOGEN_LLM_API_KEY` | Groq / – | Adresa a kľúč poskytovateľa (cloud alebo lokálny server) |
| `LOGEN_LLM_MODEL` (+ `_UNITS`, `_LO`, `_QUESTIONS`, `_EVAL`, `_PREREQ`) | `openai/gpt-oss-120b`; `_EVAL` = `qwen/qwen3.8-27b,openai/gpt-oss-120b` | Predvolený model a voliteľne model pre každú rolu (dnes `GEN_MODEL` a konštanty v `main.py`, `ems.py`) |
| `LOGEN_EMBED_PROVIDER` / `_BASE_URL` / `_MODEL` | `local` / – / `all-MiniLM-L6-v2` | Poskytovateľ, adresa a model embeddingov |
| `LOGEN_DEDUP_THRESHOLD` / `LOGEN_TOPIC_MATCH_THRESHOLD` | `0.82` / nakalibrovať | Prahy podobnosti; kalibrujú sa pre embedding model |
| `LOGEN_FAITHFULNESS_THRESHOLD` / `LOGEN_HINT_QUALITY_THRESHOLD` / `LOGEN_MAX_ATTEMPTS` | `3.0` / `2.0` / `2` | Filter kvality **(B)** |
| `LOGEN_DEFAULT_LESSON_MINUTES` | `45` | Dĺžka hodiny, ak ju stránka nepošle |
| `LOGEN_OCR_ENABLED` | `false` | Predvolené zapnutie OCR |
| `LOGEN_API_KEYS` | prázdne | Povolené kľúče pre `X-API-Key`; prázdne = bez overenia (lokálne) |
| `LOGEN_MAX_UPLOAD_MB` / `LOGEN_MAX_PAGES` | `50` / `300` | Limity vstupu |
| `LOGEN_RESULT_TTL_DAYS` | `7` | Ako dlho sa uchovávajú výsledky v `/data` |
| `LOGEN_INTER_CALL_DELAY` / `LOGEN_REASONING_EFFORT` | `0.1` / – | Pauza medzi volaniami LLM, úroveň uvažovania pre GPT-OSS **(B, dnes bez prefixu)** |
| `LOGEN_REDIS_URL` / `LOGEN_DATA_DIR` / `LOGEN_JOB_TIMEOUT_MINUTES` | `redis://redis:6379` / `/data` / `120` | Infraštruktúra **(B, návrh)** |

Kvôli spätnej kompatibilite fungujú aj dnešné premenné (`GROQ_API_KEY`, `OPENROUTER_API_KEY`, `GEN_MODEL`, `INTER_CALL_DELAY`, `SPACY_MODEL`, …) ako náhradné hodnoty, ak variant s prefixom `LOGEN_` nie je nastavený.
