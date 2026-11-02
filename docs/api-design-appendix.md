# Prílohy

Prílohy obsahujú konkrétne technické podklady pre implementáciu [Mlynčeka B](api-design.md). Ide o náčrt, pri implementácii sa môžu zmeniť bez zmeny samotného návrhu.

**API kontrakt je rovnaký ako pri Mlynčeku A** (stránka *Prílohy*): rovnaké endpointy, parametre `options`, stavy, webhooky, chybové kódy aj výstupný formát. Položky, ktoré má iba Mlynček B, sú označené **(B)**. Položky, na ktorých sa obe stránky zatiaľ nedohodli, sú označené **(návrh)**.

---

## Príloha A - Detaily API

Všetky endpointy majú prefix `/v1`. Ak je nastavený `LOGEN_API_KEYS`, stránka posiela header `X-API-Key`. Pre nasadenie s ECEH je `LOGEN_API_KEYS` povinný a kľúč má iba Laravel. `/health` je bez kľúča a vracia iba stav, nie konfiguráciu.

| Metóda | Endpoint | Popis |
| --- | --- | --- |
| GET | `/health` | Stav služby (API, Redis, worker). |
| GET | `/v1/info` | Verzia služby, nastavené modely, limity. |
| POST | `/v1/jobs` | Vytvorí úlohu: multipart `files[]` (PDF, PPTX a HTML **(B)**) + `options` (JSON). Vráti 202 a `job_id`. Spustí všetky kroky. 202 až po zápise PDF na disk a zaradení do fronty. Ak `external_ref` už existuje, vráti 200 a pôvodné `job_id`. |
| GET | `/v1/jobs?external_ref=…` | Zoznam úloh s daným identifikátorom stránky (najnovšie prvé): `job_id`, stav, čas vytvorenia. |
| GET | `/v1/jobs/{id}` | Stav úlohy: `queued` / `running` / `completed` / `failed` / `cancelled`, krok, priebeh, chyba. |
| GET | `/v1/jobs/{id}/result` | Výsledok presne vo formáte units (`{"units": [...]}`). Každý unit je jedna hodina. |
| GET | `/v1/jobs/{id}/report` | Údaje mimo formátu units: LO, citované strany, skóre kvality, časy spracovania, varovania rozdelenia. Aj väzba otázok na LO (`question_map`). Pri B v `extensions` aj graf prerekvizít. |
| DELETE | `/v1/jobs/{id}` | Zruší bežiacu úlohu alebo zmaže výsledky. |
| POST | `/v1/jobs/{id}/retry` | Znova spustí úlohu v stave `failed` s rovnakým `job_id`. Funguje 24 h po chybe, kým služba nezmaže PDF. |
| GET | `/v1/schema/lesson` | JSON schéma formátu units. |
| GET | `/v1/schema/report` | JSON schéma reportu. |

FastAPI navyše poskytuje `/docs` a `/openapi.json` (R14). V produkcii sa dajú vypnúť (`LOGEN_DOCS_ENABLED=false`) **(B)**.

**Príklad: vytvorenie úlohy**

```bash
curl -X POST http://logen-api:8000/v1/jobs \
  -H "X-API-Key: <kluc>" \
  -F "files=@modul1.pdf" \
  -F 'options={"external_ref": "gen-01JB7Q3K",
              "callback_url": "http://eceh-backend/api/internal/qgen/webhook",
              "split": {"mode": "topics", "lesson_minutes": 45}}'
```

`HTTP/1.1 202 Accepted`
```json
{ "job_id": "3f6b1c2e-8d4a-4f7b-9c1e-5a2d7e9b0c41", "status": "queued", "links": { "self": "/v1/jobs/3f6b1c2e-8d4a-4f7b-9c1e-5a2d7e9b0c41" } }
```

Rovnaký request znova (napríklad po timeoute): `HTTP/1.1 200 OK`
```json
{ "job_id": "3f6b1c2e-8d4a-4f7b-9c1e-5a2d7e9b0c41", "status": "running", "replayed": true, "links": { "self": "/v1/jobs/3f6b1c2e-8d4a-4f7b-9c1e-5a2d7e9b0c41" } }
```

`job_id` je pri B UUID4. Pre Laravel je to nepriehľadný reťazec, rovnako ako pri A.

**Príklad: stav úlohy**

`GET /v1/jobs/3f6b1c2e-8d4a-4f7b-9c1e-5a2d7e9b0c41`
```json
{
  "job_id": "3f6b1c2e-8d4a-4f7b-9c1e-5a2d7e9b0c41",
  "external_ref": "gen-01JB7Q3K",
  "status": "running",
  "stage": "questions",
  "progress": 0.62,
  "created_at": "2026-11-03T10:12:01Z",
  "updated_at": "2026-11-03T10:16:40Z",
  "error": null,
  "retryable_until": null
}
```

Neúspešná úloha má rovnaký tvar, ale `"status": "failed"`, `error` v jednotnom formáte chyby a `retryable_until` (do kedy funguje retry):
```json
{
  "job_id": "3f6b1c2e-8d4a-4f7b-9c1e-5a2d7e9b0c41",
  "external_ref": "gen-01JB7Q3K",
  "status": "failed",
  "stage": "extract",
  "progress": 0.02,
  "created_at": "2026-11-03T10:12:01Z",
  "updated_at": "2026-11-03T10:12:09Z",
  "error": {
    "code": "no_text_extracted",
    "message": "V súbore 'sken.pdf' sa nenašiel žiadny text. Dokument môže byť naskenovaný; skúste extraction.ocr = true.",
    "details": { "file": "sken.pdf" }
  },
  "retryable_until": "2026-11-04T10:12:09Z"
}
```

Názvy krokov v poli `stage` v poradí: `extract`, `language`, `units`, `learning_objects`, `lo_evaluation`, `graph`, `questions`, `q_evaluation`, `split`, `output` zodpovedajú krokom Mlynčeka A, `units` a `graph` má iba B. `progress` je číslo od 0 do 1, vážené obvyklým počtom volaní LLM v jednotlivých krokoch.

### Vstupné parametre (`options`)

Parametre sú rovnaké ako pri Mlynčeku A. Všetky sú voliteľné, chýbajúce hodnoty sa doplnia z konfigurácie. Bez parametra `split` sa všetko dá do jednej hodiny.

```json
{
  "external_ref": "gen-01JB7Q3K",
  "callback_url": "http://eceh-backend/api/internal/qgen/webhook",
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
| `external_ref` | vlastný identifikátor stránky. Unikátny, pre ECEH povinný (Laravel posiela `generations.external_ref`) | - |
| `callback_url` | adresa pre webhooky. Musí byť v `LOGEN_WEBHOOK_ALLOWED_HOSTS`, inak `400 invalid_options` | - |
| `language` | `auto` \| `sk` \| `en` | prompty B sú currently v angličtine, generovanie v slovenčine je future work (hlavná stránka, kap. 4, 5) |
| `extraction.ocr` | OCR obrázkov, voliteľné (predvolene `false`) | B dnes OCR nemá. V obraze bez OCR (`INSTALL_OCR=false`) vráti `true` chybu `400 invalid_options` |
| `questions.types` | ktoré typy generovať | - |
| `questions.per_learning_object` | počet otázok na jedno LO | B dnes vytvára jedno LO = jednu otázku, ďalšie pribudnú v novom kroku 7 |
| `questions.choice_options` | počet možností pri `choice` | B dnes vždy generuje 4 (3 distraktory + správna) |
| `questions.hints` | počet nápovied (napr. ľahká a ťažká) | B má dnes 1 nápovedu |
| `split.mode` | `single` \| `lessons` \| `topics` \| `syllabus` | - |
| `split.lesson_count` | počet hodín (`lessons`) | - |
| `split.lesson_minutes` | dĺžka hodiny v minútach | predvolene `LOGEN_DEFAULT_LESSON_MINUTES` |
| `split.syllabus` | zoznam hodín (`syllabus`) | - |

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
`pages` = `D<číslo dokumentu>:<od>-<do>`. Pri PPTX **(B)** sú to čísla snímok, pri HTML **(B)** poradie sekcií pod nadpismi.

**(B) voliteľný objekt `advanced`.** Slúži iba na testovanie a porovnávanie modelov. Laravel ho nepotrebuje a Mlynček A ho ignoruje.
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

### Webhook (služba → backend ECEH)

Worker pošle HTTP POST na `callback_url` pri udalostiach `job.stage_changed`, `job.completed`, `job.failed` a `job.cancelled`. Telo nesie iba stav, výsledok si Laravel stiahne cez `/result` a `/report`. Hlavičky aj telo sú rovnaké ako pri Mlynčeku A (aj predpona `X-QGen-`), aby Laravel overoval oba mlynčeky rovnakým kódom.

```http
POST /api/internal/qgen/webhook HTTP/1.1
Content-Type: application/json
X-QGen-Event: job.completed
X-QGen-Delivery: 5b0e9c1a-8f2d-4c55-9a4e-0d7c2f61b3aa
X-QGen-Timestamp: 1793701125
X-QGen-Signature: sha256=8a1f...c94e

{ "event": "job.completed", "job_id": "3f6b1c2e-8d4a-4f7b-9c1e-5a2d7e9b0c41", "external_ref": "gen-01JB7Q3K",
  "status": "completed", "stage": "output", "progress": 1.0, "error": null,
  "occurred_at": "2026-11-03T10:21:45Z" }
```

`X-QGen-Delivery` je ID doručenia, rovnaké pri opakovaní. `X-QGen-Signature` = HMAC-SHA256(`LOGEN_WEBHOOK_SECRET`, `timestamp + "." + telo`).

| Pravidlo | Hodnota |
| --- | --- |
| Úspech | Laravel odpovie 2xx do `LOGEN_WEBHOOK_TIMEOUT_S` (predvolene 5 s). |
| Opakovanie | Pri inej odpovedi alebo timeoute: po 10 s, 60 s a 300 s. Potom služba doručenie vzdá. Stav úlohy sa nemení. |
| Poradie | Správy môžu prísť mimo poradia alebo dvakrát. Laravel porovná `occurred_at` a `X-QGen-Delivery` a staršiu alebo opakovanú správu ignoruje. |
| Podpis | Laravel vypočíta HMAC-SHA256 zo spojenia `timestamp + "." + telo` s kľúčom webhooku. Správu staršiu ako 5 minút odmietne. |
| Povolené adresy | Iba hostitelia z `LOGEN_WEBHOOK_ALLOWED_HOSTS`. Inú adresu API odmietne pri vytvorení úlohy (`400 invalid_options`). |
| Záloha | Ak webhook nepríde, Laravel číta `GET /v1/jobs/{id}`. |

Kontrola podpisu v Laraveli (náčrt):
```php
$ts   = $request->header('X-QGen-Timestamp');
$sig  = $request->header('X-QGen-Signature');
$calc = 'sha256=' . hash_hmac('sha256', $ts . '.' . $request->getContent(), config('logen.webhook_secret'));
abort_unless(hash_equals($calc, $sig) && abs(time() - (int) $ts) < 300, 401);
```

Ak budú A aj B používať rovnaký tajný kľúč, Laravel vystačí s jedným nastavením.

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
          "description": "Prečo by každé volanie malloc malo mať zodpovedajúce volanie free?",
          "type": "open",
          "time_to_handle": 180,
          "time_to_explain": 60,
          "hints": [ { "name": "Nápoveda", "url": "<p>Čo sa stane s pamäťou, ktorá sa nikdy neuvoľní?</p>" } ],
          "choices": [
            { "text": "Inak program stráca pamäť (memory leak).", "is_correct": true }
          ]
        },
        {
          "name": "Esej: manuálna vs. automatická správa pamäte",
          "description": "Napíšte krátku úvahu (5-10 viet), v ktorej porovnáte manuálnu správu pamäte v C s garbage collection.",
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

HTML v `hints[].url` sa pred uložením prečistí: povolené značky sú `p`, `br`, `strong`, `em`, `ul`, `ol`, `li`, `code`, `pre`, `sub`, `sup`, ostatné značky a všetky atribúty sa odstránia. Texty dodržia limity DB ECEH (hlavná stránka, kap. 6). Výsledok sa pred uložením validuje voči schéme z `GET /v1/schema/lesson` (`additionalProperties: false` a `maxLength` na každej úrovni). Schéma by mala byť jeden spoločný súbor pre Mlynček A aj B.

### Report (`GET /v1/jobs/{id}/report`)

Spoločné polia (`learning_objects`, `question_map`, `quality`, `warnings`, `timings`) majú rovnaký tvar ako pri Mlynčeku A a popisuje ich `GET /v1/schema/report`. Údaje, ktoré má iba B, sú v objekte `extensions`, aby spoločná schéma ostala rovnaká.

```json
{
  "learning_objects": [
    { "id": "3f9a1c2b", "title": "Návratová hodnota malloc",
      "statement": "Funkcia malloc vráti smerník na alokovaný blok pamäte.",
      "lo_type": "definition", "source": { "file": "modul1.pdf", "pages": [12] },
      "keywords": ["malloc", "smerník", "pamäť"], "bloom_level": null, "topic": "Dynamická pamäť" }
  ],
  "question_map": [ { "unit": 0, "question": 0, "lo_id": "3f9a1c2b" }, { "unit": 0, "question": 1, "lo_id": "3f9a1c2b" } ],
  "quality": { "faithfulness_avg": 0.9, "answerability_avg": null, "topic_coverage": null },
  "warnings": [
    { "code": "lesson_over_capacity", "message": "Hodina 3 prekračuje 45 minút o 24 %." },
    { "code": "field_too_long", "message": "Otázka vyradená: zadanie má viac ako 4000 znakov." }
  ],
  "timings": { "extract": 12.4, "learning_objects": 220.7, "questions": 180.6, "split": 8.1 },
  "extensions": {
    "learning_objects": [
      { "id": "3f9a1c2b", "anchor": "malloc", "prerequisite_ids": ["a81c09de"],
        "scores": { "faithfulness": 4.5, "hint_quality": 4.0, "atomic": "yes", "self_contained": "yes", "regenerated": false } }
    ],
    "knowledge_graph": {
      "nodes": [ { "id": "3f9a1c2b" }, { "id": "a81c09de" } ],
      "edges": [ { "source": "a81c09de", "target": "3f9a1c2b" } ]
    },
    "timings": { "units": 141.0, "lo_evaluation": 310.2, "graph": 95.3 },
    "models": { "llm": "openai/gpt-oss-120b", "eval": ["qwen/qwen3.8-27b", "openai/gpt-oss-120b"], "embeddings": "all-MiniLM-L6-v2" }
  }
}
```

Poznámky k B **(návrh)**:
- `id` LO je dnešný hash z `generate_id` (A používa tvar `LO-12`). Pre Laravel je to nepriehľadný reťazec.
- `bloom_level` B dnes nemá, posiela `null`.
- `topic` je názov komunity v grafe pojmov (hlavná stránka, kap. 8).
- `quality.faithfulness_avg` je priemer skóre faithfulness (1-5) prepočítaný na 0-1. `answerability_avg` a `topic_coverage` B dnes nepočíta. Doplnia sa, ak sa ich výpočet dohodne s A, inak `null`.

### Informácie o službe (`GET /v1/info`)

```json
{
  "service": "lo-generator",
  "version": "1.0.0",
  "models": { "llm": "openai/gpt-oss-120b", "eval": ["qwen/qwen3.8-27b", "openai/gpt-oss-120b"], "embeddings": "all-MiniLM-L6-v2" },
  "limits": { "max_files": 10, "max_upload_mb": 50, "max_pages": 300, "max_active_jobs": 10, "file_types": [".pdf", ".pptx", ".html", ".htm"] },
  "ocr_available": false
}
```

### Chybové odpovede

Všetky chyby majú rovnaký tvar: `{"error": {"code": "...", "message": "...", "details": {...}}}`. Kódy sú rovnaké ako pri Mlynčeku A.

| HTTP | code | Kedy |
| --- | --- | --- |
| 400 | `invalid_options` | Neplatná kombinácia parametrov (napr. `mode=syllabus` bez zoznamu hodín). Aj `callback_url` mimo povolených hostiteľov, `extraction.ocr = true` v obraze bez OCR, model mimo povoleného zoznamu **(B)**. |
| 401 | `unauthorized` | Chýba alebo je zlý API kľúč. |
| 404 | `job_not_found` | Úloha neexistuje alebo už bola zmazaná (TTL). |
| 409 | `result_not_ready` | Výsledok ešte nie je hotový. |
| 409 | `job_not_retryable` | Retry pri úlohe, ktorá nie je v stave `failed`, alebo PDF už služba zmazala. |
| 413 | `file_too_large` | Súbor je väčší ako limit, má priveľa strán (pri PPTX snímok, pri HTML sekcií) alebo je priveľa súborov. Pri PPTX aj priveľká rozbalená veľkosť **(B)**. |
| 415 | `unsupported_file` | Súbor nie je PDF, PPTX ani HTML **(B: A prijíma iba PDF)**, alebo sa nedá otvoriť (PDF nezačína `%PDF` alebo je zaheslované, PPTX nie je platný balík prezentácie, HTML nie je text v UTF-8). |
| 422 | `validation_error` | Telo requestu nezodpovedá schéme (generuje FastAPI). |
| 429 | `too_many_jobs` | Aktívnych úloh je viac ako `LOGEN_MAX_ACTIVE_JOBS`. Odpoveď má header `Retry-After`. |
| 502 | `llm_error` | Poskytovateľ LLM zlyhal aj po opakovaniach alebo vrátil neplatný JSON. |

**Kódy chýb neúspešnej úlohy** (pole `error.code` v stave úlohy `failed` a vo webhooku `job.failed`) **(návrh pre A aj B)**:

| code | Kedy |
| --- | --- |
| `no_text_extracted` | V žiadnom dokumente sa nenašiel text (napr. sken bez OCR). |
| `no_learning_objects` | Z textu nevzniklo žiadne použiteľné LO. |
| `llm_error` | Poskytovateľ LLM alebo embeddingov zlyhal aj po opakovaniach (ako HTTP 502). |
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
def get_embedder() -> EmbeddingClient: ...   # vlastný poskytovateľ, nezávislý od LLM: local (MiniLM) | openai_compatible
# všetky volania LLM sú obmedzené semaforom podľa LOGEN_LLM_MAX_CONCURRENCY
```

Dnešná funkcia `call_llm(messages, model, temperature, ...)` v `llm_service.py` zostane ako tenký obal nad `get_llm(role).generate(...)`. Doménové moduly ďalej volajú `call_llm` a ich prompty sa nemenia. Opakovanie pokusov, čakanie pri limite požiadaviek a `LEGACY_ROUTES` sa presunú do `OpenAICompatibleAdapter`.

| Adaptér | Pokrýva | Generovanie / embeddingy |
| --- | --- | --- |
| `OpenAICompatibleAdapter` | Groq, OpenRouter, OpenAI, Mistral, GitHub Models, Gemini (OpenAI endpoint), lokálny server (Ollama, vLLM, LM Studio) | áno / áno (`/v1/chat/completions`, `/v1/embeddings`) |
| `LocalEmbedder` **(B)** | `all-MiniLM-L6-v2` vo workeri (dnešný `embedding_service.py`) | - / áno |
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
# lokálny vLLM server (návrh Adely: Qwen, približne 27B), embeddingy ostávajú lokálne (MiniLM)
LOGEN_LLM_PROVIDER=openai_compatible
LOGEN_LLM_BASE_URL=http://gpu-server:8001/v1
LOGEN_LLM_MODEL=<nazov-modelu-qwen-na-serveri>
LOGEN_LLM_MAX_CONCURRENCY=4
LOGEN_EMBED_PROVIDER=local
LOGEN_EMBED_MODEL=all-MiniLM-L6-v2
```

---

## Príloha C - Štruktúra projektu po zmenách

Štruktúra zodpovedá Mlynčeku A: jadro (`lo_pipeline/`, v A `qgen/`) je oddelené od služby (`service/`).

```text
fastapi/
├── lo_pipeline/                 # jadro - dnešné moduly bez HTML UI a globálneho stavu
│   ├── extraction/              # utilities.py (PDF, PPTX, HTML), ocr.py (NOVÉ, voliteľné), language.py (NOVÉ)
│   ├── units/                   # atomic_unit_extractor.py, concept_normalizer.py
│   ├── lo/                      # learning_object_generator.py, hodnotenie (learning_object_evaluator.py, filter z ems.py)
│   ├── graph/                   # graph_builder.py (prerekvizity, graf pojmov)
│   ├── questions/               # NOVÉ - choice/open/no_answer, nápovedy, časy (z echub_format_converter.py)
│   ├── split/lessons.py         # NOVÉ - rozdelenie do hodín (unit = hodina)
│   ├── output/units.py          # NOVÉ - mapovanie do formátu units + JSON schéma
│   ├── output/limits.py         # NOVÉ - dĺžkové limity podľa DB ECEH, skrátenie a vyradenie
│   ├── output/sanitize.py       # NOVÉ - čistenie HTML v hints[].url (zoznam povolených značiek)
│   ├── output/report.py         # NOVÉ - report: learning_objects, question_map, quality, warnings, extensions
│   ├── llm/                     # base.py, factory.py, adapters/, embeddings.py, json_load.py (parse_json)
│   ├── runner.py                # run_pipeline(options, progress_callback)
│   └── config.py                # nastavenia (pydantic-settings)
├── service/
│   ├── main.py                  # FastAPI aplikácia
│   ├── routes/                  # health.py, info.py, jobs.py, schema.py
│   ├── jobs/                    # queue.py, tasks.py, store.py
│   ├── jobs/cleanup.py          # NOVÉ - mazanie PDF po koncovom stave a výsledkov po TTL
│   ├── webhooks.py              # NOVÉ - odoslanie, podpis HMAC, opakovanie
│   └── errors.py                # jednotný formát chýb
├── analysis/                    # skripty k bakalárskej práci: post_statistics.py, lexical_overlap_computation.py, atomic_unit_merger.py
├── tests/                       # unit, kontraktové a integračné testy
├── requirements.txt, requirements-ocr.txt, requirements-dev.txt
└── Dockerfile, docker-compose.yml, .env.example
```

Odstráni sa: súčasný `main.py` s endpointmi pre jednotlivé kroky a globálnym prúdom logov (`_LogCapture`, `/logs`), `ems.process_book_with_checkpoints` (nahradí ho `runner.py`) a `pipeline_ui.html` (alebo zostane ako nástroj pre vývojárov mimo kontraktu, len na testing). `env_template` sa premenuje na `.env.example`, rovnako ako v A.

---

## Príloha D - Docker a konfigurácia

### Dockerfile (náčrt)

```dockerfile
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# false = image bez Tesseractu
ARG INSTALL_OCR=true
WORKDIR /app

# OCR závislosti sú v samostatnom súbore (requirements-ocr.txt: pytesseract, pdf2image)
COPY requirements.txt requirements-ocr.txt ./
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements.txt \
 && if [ "$INSTALL_OCR" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-slk poppler-utils \
      && rm -rf /var/lib/apt/lists/* \
      && pip install --no-cache-dir -r requirements-ocr.txt; \
    fi

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

OCR ostáva (rozhodnutie 25. 10.). Menší image bez OCR sa zostaví príkazom `docker compose build --build-arg INSTALL_OCR=false`. V takom image služba odmietne `extraction.ocr = true` chybou `400 invalid_options`.

- **torch** tool B potrebuje kvôli lokálnym embeddingom (sentence-transformers), nie kvôli OCR ako A. Bez OCR preto image B ostane väčší ako image A. Pri `LOGEN_EMBED_PROVIDER=openai_compatible` môžu torch a sentence-transformers z obrazu odpadnúť a image bude výrazne menší.
- **OCR**: `tesseract-ocr` (`eng`, `slk`), `poppler-utils`, `pytesseract`, `pdf2image`, približne +100-150 MB. Použije sa pre strany, kde PyPDF2 nenájde text.
- **GPU** netreba (MiniLM beží na CPU). Treba ju iba pre voliteľný lokálny kontajner `llm`.
- **Veľkosť:** odhad 1,5-2,5 GB s torch, bez neho niekoľko stoviek MB. *Po builde doplniť nameranú hodnotu (`docker images`).*
- **Python 3.11** používa B dnes (lock súbor je pre túto verziu). A používa 3.12. Prechod na 3.12 je možný, ale nie je nutný.

### docker-compose.yml (náčrt)

```yaml
services:
  api:
    build: .
    expose: ["8000"]                       # iba interná sieť, port nie je zverejnený
    # ports: ["127.0.0.1:8000:8000"]       # iba na lokálny vývoj
    env_file: .env
    volumes: [data:/data]
    depends_on: [redis]
    networks: [logen, eceh]                # sieť zdieľaná s Laravelom
    restart: unless-stopped
  worker:
    build: .
    command: rq worker --url redis://:${LOGEN_REDIS_PASSWORD}@redis:6379 logen
    deploy:
      replicas: ${LOGEN_WORKERS:-1}        # počet workerov podľa záťaže
    env_file: .env
    volumes: [data:/data]
    depends_on: [redis]
    networks: [logen, eceh]                # worker posiela webhooky na Laravel
    restart: unless-stopped
  scheduler:                               # mazanie PDF a výsledkov po TTL, opakovanie webhookov
    build: .
    command: rqscheduler --url redis://:${LOGEN_REDIS_PASSWORD}@redis:6379
    env_file: .env
    volumes: [data:/data]
    depends_on: [redis]
    networks: [logen]
    restart: unless-stopped
  redis:
    image: redis:7-alpine
    command: redis-server --appendonly yes --requirepass ${LOGEN_REDIS_PASSWORD}   # queue a stav prežijú reštart
    volumes: [redis:/data]
    networks: [logen]
    restart: unless-stopped
  llm:                                     # voliteľný lokálny model
    image: vllm/vllm-openai                # vLLM namiesto Ollama (návrh Adely)
    command: ["--model", "${LOGEN_LOCAL_MODEL}", "--port", "8001"]
    profiles: [local-llm]
    volumes: [models:/root/.cache/huggingface]
    networks: [logen]
    deploy:
      resources:
        reservations:
          devices: [{ driver: nvidia, count: 1, capabilities: [gpu] }]
networks:
  logen:
  eceh:
    external: true                         # sieť ECEH (názov určí Adela)
volumes:
  data:
  redis:
  models:
```

Ak lokálny model pobeží na samostatnom GPU serveri, profil `local-llm` sa nespustí. Stačí nastaviť `LOGEN_LLM_BASE_URL` na adresu servera.

Queue sa volá `logen` (A používa `qgen`), takže oba mlynčeky môžu zdieľať jeden Redis, ak by bežali spolu.

### Konfigurácia (premenné prostredia)

Prefix `LOGEN_` (A používa `QGEN_`) umožňuje prevádzkovať obe služby vedľa seba s jedným `.env`. Názvy premenných za prefixom zodpovedajú A.

| Premenná | Predvolená hodnota | Význam |
| --- | --- | --- |
| `LOGEN_LLM_PROVIDER` | `openai_compatible` | `openai_compatible` \| `anthropic` (Gemini ide cez svoj OpenAI endpoint) |
| `LOGEN_LLM_BASE_URL` / `LOGEN_LLM_API_KEY` | Groq / - | Adresa a kľúč poskytovateľa (cloud alebo lokálny server) |
| `LOGEN_LLM_MODEL` (+ `_UNITS`, `_LO`, `_QUESTIONS`, `_EVAL`, `_PREREQ`) | `openai/gpt-oss-120b`; `_EVAL` = `qwen/qwen3.8-27b,openai/gpt-oss-120b` | Predvolený model a voliteľne model pre každú rolu (dnes `GEN_MODEL` a konštanty v `main.py`, `ems.py`) |
| `LOGEN_EMBED_PROVIDER` / `_BASE_URL` / `_MODEL` | `local` / - / `all-MiniLM-L6-v2` | Poskytovateľ, adresa a model embeddingov. Embeddingy sú povinné, poskytovateľ je nezávislý od LLM |
| `LOGEN_DEDUP_THRESHOLD` / `LOGEN_TOPIC_MATCH_THRESHOLD` | `0.82` / nakalibrovať | Prahy podobnosti; kalibrujú sa pre embedding model |
| `LOGEN_FAITHFULNESS_THRESHOLD` / `LOGEN_HINT_QUALITY_THRESHOLD` / `LOGEN_MAX_ATTEMPTS` | `3.0` / `2.0` / `2` | Filter kvality **(B)** |
| `LOGEN_DEFAULT_LESSON_MINUTES` | `45` | Dĺžka hodiny, ak ju stránka nepošle |
| `LOGEN_OCR_ENABLED` | `false` | Predvolené zapnutie OCR |
| `LOGEN_API_KEYS` | prázdne | Povolené kľúče pre `X-API-Key`. Prázdne = bez overenia (lokálne). Pri nasadení s ECEH povinné |
| `LOGEN_MAX_UPLOAD_MB` / `LOGEN_MAX_PAGES` | `50` / `300` | Limity vstupu (pri PPTX sa počítajú snímky, pri HTML sekcie) |
| `LOGEN_MAX_UNZIPPED_MB` | `200` | Max. rozbalená veľkosť PPTX (ochrana pred ZIP bombou) **(B)** |
| `LOGEN_RESULT_TTL_DAYS` | `7` | Ako dlho sa uchovávajú výsledky v `/data` |
| `LOGEN_MAX_FILES` | `10` | Najviac súborov v jednej úlohe |
| `LOGEN_FAILED_INPUT_TTL_HOURS` | `24` | Ako dlho ostane PDF po stave `failed` (pre retry). Po `completed` a `cancelled` sa PDF zmaže hneď |
| `LOGEN_MAX_ACTIVE_JOBS` | `10` | Najviac úloh v stave `queued` alebo `running`. Ďalšie dostanú 429 |
| `LOGEN_WORKERS` | `1` | Počet kontajnerov worker |
| `LOGEN_LLM_MAX_CONCURRENCY` | `4` | Najviac súbežných volaní LLM z jedného workera |
| `LOGEN_WEBHOOK_SECRET` | - | Tajný kľúč pre podpis webhookov. Rovnaký kľúč má Laravel |
| `LOGEN_WEBHOOK_ALLOWED_HOSTS` | prázdne | Hostitelia povolení v `callback_url`. Prázdne = webhooky vypnuté |
| `LOGEN_WEBHOOK_TIMEOUT_S` | `5` | Časový limit jedného doručenia webhooku |
| `LOGEN_LOCAL_MODEL` | - | Model pre kontajner `llm` (profil `local-llm`), napríklad Qwen podľa výberu Adely |
| `LOGEN_INTER_CALL_DELAY` / `LOGEN_REASONING_EFFORT` | `0.1` / - | Pauza medzi volaniami LLM, úroveň uvažovania pre GPT-OSS **(B, dnes bez prefixu)** |
| `LOGEN_REDIS_PASSWORD` | - (povinné) | Heslo pre Redis (`requirepass`) **(B)** |
| `LOGEN_DOCS_ENABLED` | `true` | `/docs` a `/openapi.json`, v produkcii `false` **(B)** |
| `LOGEN_DATA_DIR` / `LOGEN_JOB_TIMEOUT_MINUTES` | `/data` / `120` | Infraštruktúra **(B, návrh)** |

Kvôli spätnej kompatibilite fungujú aj súčasné premenné (`GROQ_API_KEY`, `OPENROUTER_API_KEY`, `GEN_MODEL`, `INTER_CALL_DELAY`, `SPACY_MODEL`, …) ako náhradné hodnoty, ak variant s prefixom `LOGEN_` nie je nastavený.
