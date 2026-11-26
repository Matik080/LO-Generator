# LO Pipeline

Automated generation of structured learning objects (LOs) from educational source materials. Given PDF, HTML, or PPTX files, the pipeline extracts atomic knowledge units, generates question/answer/hint learning objects with multiple-choice distractors, evaluates and filters them by quality, and constructs a prerequisite knowledge graph (DAG) over the results.

Built as part of a bachelor's thesis at FIIT STU Bratislava.

---

## Table of Contents

- [Overview](#overview)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running as a service (/v1 API)](#running-as-a-service-v1-api)
- [Development UI with Docker](#development-ui-with-docker)
- [Running without Docker](#running-without-docker)
- [Using the Web UI](#using-the-web-ui)
- [API Reference](#api-reference)
- [Project Structure](#project-structure)

---

## Overview

The pipeline (`lo_pipeline/runner.py`) runs these stages:

1. **extract** - text per page/slide/section from PDF (PDFium; optional OCR of scanned pages), PPTX (optional OCR of pictures) and HTML
2. **language** - the documents' language (Slovak or English, no LLM) and the output language (job option `language`)
3. **units** - an LLM extracts self-contained atomic knowledge units (definitions, rules, examples, algorithm steps, …) from each section; they are deduplicated (MiniLM) and their concepts normalised (spaCy). The units are usually written in English, also for Slovak documents, because spaCy and MiniLM work best in English
4. **learning_objects** - each unit becomes one learning object: question, answer, hint and multiple-choice options (about 30 % of choice questions get two correct options)
5. **lo_evaluation** - two models (`qwen/qwen3.8-27b` and `openai/gpt-oss-120b` on Groq) score each LO; LOs below the faithfulness or hint thresholds are regenerated up to `LOGEN_MAX_ATTEMPTS` times
6. **graph** - a concept co-occurrence graph from the units, candidate prerequisite edges from type hierarchy and embedding similarity, each edge verified by an LLM, cycles removed (a DAG)
7. **questions** - question names and time estimates; `no_answer` tasks from LOs that share a concept; texts longer than the ECEH columns are shortened once by the LLM
8. **split** - lessons: order from the prerequisite graph, topics = communities of the concept graph, modes `single` / `lessons` / `topics` / `syllabus`, lesson titles and descriptions by the LLM
9. **output** - the `units` result and the report (built by the worker from the pipeline result)

When the output language differs from the units' language (e.g. a Slovak document, Slovak output, English units), the prompts of stages 4, 5, 7 and 8 get an instruction to write everything for students in the output language.

---

## Requirements

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/) - required (you're going to save yourself so much trouble, just use the Docker version of this app)
- Or Python 3.11+ for running without Docker

**API keys** - at least one of:

| Provider | Used for | Get key at |
|---|---|---|
| [Groq](https://console.groq.com) | `qwen/qwen3.8-27b` (generation) + `qwen/qwen3.8-27b`, `openai/gpt-oss-120b` (evaluation) | console.groq.com |
| [OpenRouter](https://openrouter.ai) | other models (optional) | openrouter.ai |
| GitHub Models | Fallback | github.com/marketplace/models |
| OpenAI | Fallback | platform.openai.com |

Groq alone is enough to run the full pipeline. Any other OpenAI-compatible provider (also a local vLLM or Ollama server) can be used through `LOGEN_LLM_BASE_URL`. Models listed in `LEGACY_ROUTES` (`lo_pipeline/llm/service.py`, e.g. `llama-3.3-70b-versatile`, `moonshotai/kimi-k2*`) are routed through OpenRouter.

---

## Installation

```bash
git clone https://github.com/Matik080/LO-Generator.git
cd LO-Generator
```

Copy the environment template and fill in your API keys:

```bash
cp fastapi/.env.example fastapi/.env
# then edit fastapi/.env with your keys
```

---

## Configuration

Edit `fastapi/.env`:

```env
# Primary LLM provider — priority: GROQ > OPENROUTER > GITHUB > OPENAI
GROQ_API_KEY=your_groq_api_key_here
OPENROUTER_API_KEY=your_openrouter_api_key_here   # optional, for Kimi K2

# Optional / fallback
GITHUB_TOKEN=your_github_token_here
OPENAI_API_KEY=your_openai_api_key_here

# Seconds to wait between every LLM call (0 = no delay).
# Increase to 0.5 or 1.0 if you hit rate limits.
INTER_CALL_DELAY=0.1
```

You only need to fill in the keys you have. The pipeline selects the first available provider in priority order. `moonshotai/kimi-k2-instruct` always routes through OpenRouter regardless of which provider is primary.

These names still work. The newer `LOGEN_*` variables (any OpenAI-compatible provider via `LOGEN_LLM_BASE_URL`, models, thresholds, batch sizes, embeddings) are all listed with their defaults in [`fastapi/.env.example`](fastapi/.env.example) and in the table under [Running as a service](#running-as-a-service-v1-api).

---

## Running as a service (/v1 API)

The service for the ECEH platform (Mlynček B): Laravel uploads documents to `/v1/jobs`, a worker processes them in the background and the result comes back in the `units` format. Four containers: `api`, `worker` (runs the pipeline), `scheduler` (webhooks and cleanup) and `redis`. Everything runs from the `fastapi/` folder:

```bash
cd fastapi
cp .env.example .env          # fill in GROQ_API_KEY, LOGEN_REDIS_PASSWORD and LOGEN_API_KEYS
docker compose up -d --build
docker compose ps             # api becomes "healthy" within about 20 s
```

The first build takes several minutes (PyTorch, MiniLM, spaCy and NLTK are downloaded into the image, so the service needs no internet when it starts; it still needs it for the LLM provider). Containers run as a non-root user; Redis has a password and keeps its data on disk (`appendonly`), so jobs survive a restart.

**Reaching the API.** By default the API port is **not** published on the host, only inside Docker networks. Two optional files add access:

| Command | API reachable at |
|---|---|
| `docker compose up -d` | only inside the stack's network |
| `docker compose -f docker-compose.yml -f docker-compose.local.yml up -d` | `http://localhost:8000` (this computer only); `/docs` shows all endpoints |
| `docker compose -f docker-compose.yml -f docker-compose.eceh.yml up -d` | `http://lo-generator:8000` from ECEH's Docker network (`LOGEN_ECEH_NETWORK`, default `eceh`) |

**Environment variables** (`fastapi/.env`, all listed in `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `LOGEN_LLM_BASE_URL` / `LOGEN_LLM_API_KEY` | - | any OpenAI-compatible API (Groq, OpenRouter, OpenAI, a vLLM or Ollama server); a local server needs no key |
| `GROQ_API_KEY` (or `OPENROUTER_API_KEY`, `GITHUB_TOKEN`, `OPENAI_API_KEY`) | - | older way to choose the provider, used when the two above are not set |
| `LOGEN_LLM_MODEL` (older: `GEN_MODEL`) | provider's default | generation model |
| `LOGEN_LLM_MODEL_EVAL` | `qwen/qwen3.8-27b,openai/gpt-oss-120b` | evaluation models of the quality filter |
| `LOGEN_LLM_MAX_CONCURRENCY` | `4` | independent LLM batches sent at the same time within one job; `1` = one after another |
| `LOGEN_LLM_MAX_RETRIES` | `6` | attempts per LLM call; a rejected key or unknown model fails at once (`llm_error`) |
| `LOGEN_INTER_CALL_DELAY` / `LOGEN_REASONING_EFFORT` (older: without `LOGEN_`) | `0` / - | pause between LLM calls; GPT-OSS reasoning effort on Groq |
| `LOGEN_EMBED_PROVIDER` / `LOGEN_EMBED_MODEL` | `local` / `all-MiniLM-L6-v2` | embeddings: local MiniLM, or `openai_compatible` with `LOGEN_EMBED_BASE_URL` / `_API_KEY` (Groq has no embeddings API; the similarity thresholds are calibrated for MiniLM) |
| `LOGEN_FAITHFULNESS_THRESHOLD`, `LOGEN_MAX_ATTEMPTS`, `LOGEN_DEDUP_THRESHOLD`, `LOGEN_*_BATCH_SIZE` | see `.env.example` | quality filter, deduplication and batch sizes |
| `LOGEN_REDIS_PASSWORD` | - (required) | Redis password, letters and digits only |
| `LOGEN_API_KEYS` | empty | comma-separated keys accepted in `X-API-Key`; empty = no key needed (local only) |
| `LOGEN_MAX_FILES` / `LOGEN_MAX_UPLOAD_MB` | `10` / `50` | files per job / MB per file |
| `LOGEN_MAX_ACTIVE_JOBS` | `10` | queued + running jobs; more get `429 too_many_jobs` |
| `LOGEN_JOB_TIMEOUT_S` | `21600` | a longer job fails with `internal_error` (`reason: timeout`) |
| `LOGEN_WORKERS` | `1` | worker containers, i.e. jobs processed at the same time (each job still makes up to `LOGEN_LLM_MAX_CONCURRENCY` LLM calls at once); `docker compose up -d --scale worker=N` does the same |
| `LOGEN_HEARTBEAT_TTL_S` | `60` | a worker renews its job's heartbeat every third of this; a job silent this long has lost its worker and is taken over |
| `LOGEN_RECOVERY_INTERVAL_S` | `60` | how often the scheduler looks for jobs whose worker died and queues them again |
| `LOGEN_DOCS_ENABLED` | `true` | `/docs` and `/openapi.json` |
| `LOGEN_ECEH_NETWORK` | `eceh` | ECEH's Docker network (`docker-compose.eceh.yml`) |
| `LOGEN_WEBHOOK_ALLOWED_HOSTS` / `LOGEN_WEBHOOK_SECRET` | empty / - | hosts allowed in `callback_url` (empty = webhooks off) and the HMAC key; the secret is required when hosts are set |
| `LOGEN_WEBHOOK_TIMEOUT_S` | `5` | one webhook delivery attempt |
| `LOGEN_FAILED_INPUT_TTL_HOURS` / `LOGEN_RESULT_TTL_DAYS` | `24` / `7` | how long a failed job keeps its document (retry) / how long finished jobs are kept |
| `LOGEN_CLEANUP_INTERVAL_S` | `600` | how often old documents and jobs are deleted |

`LOGEN_BACKEND`, `LOGEN_REDIS_URL` and `LOGEN_DATA_DIR` are set by `docker-compose.yml`. Without compose, `LOGEN_BACKEND=mock` (the default) runs a mock with the same endpoints and example answers.

**Example with curl** (with `docker-compose.local.yml`; from ECEH's network use `http://lo-generator:8000`):

```bash
# 1. create a job: files + options (all options are optional)
curl -s -H "X-API-Key: $KEY" \
  -F "files=@modul1.pdf" \
  -F 'options={"external_ref": "gen-1", "questions": {"hints": 2}}' \
  http://localhost:8000/v1/jobs
# -> 202 {"job_id": "3f6b…", "status": "queued", ...}

# 2. state, stage and progress (0-1); repeat until "completed" or "failed"
curl -s -H "X-API-Key: $KEY" http://localhost:8000/v1/jobs/3f6b…

# 3. result in the units format, and the report (LOs, sources, quality, warnings)
curl -s -H "X-API-Key: $KEY" http://localhost:8000/v1/jobs/3f6b…/result
curl -s -H "X-API-Key: $KEY" http://localhost:8000/v1/jobs/3f6b…/report
```

Other endpoints: `GET /v1/jobs?external_ref=gen-1` (find a job), `DELETE /v1/jobs/{id}` (cancel a running job, delete a finished one), `POST /v1/jobs/{id}/retry` (run a failed job again within 24 h, it continues where it stopped), `GET /v1/schema/{lesson|report|options}`, `GET /v1/info`, `GET /health` (no key). Every error has the form `{"error": {"code", "message", "details"}}`.

**Job options** (the `options` form field, JSON; all optional; the full rules are in `GET /v1/schema/options`, the same contract as Mlynček A):

| Option | Values | What B does |
|---|---|---|
| `language` | `auto` (default) \| `sk` \| `en` | language of questions, hints and lesson texts; `auto` = the documents' language (detected) |
| `extraction.ocr` | `false` (default) \| `true` | read scanned PDF pages and pictures in PPTX with Tesseract (Slovak + English); only when `/v1/info` reports `ocr_available`, otherwise `400 invalid_options` |
| `questions.types` | `choice`, `open`, `no_answer` | one LO gives one `choice` (or `open`) question; `no_answer` = tasks without automatic grading, one per group of LOs sharing a concept |
| `questions.hints` | `0` \| `1` \| `2` (default) | 1st hint = the source (file and page, written by code: "Zdroj"/"Source"), 2nd = the LLM hint |
| `questions.choice_options` | 2–10 | B generates up to 4 options (`choice_options_unavailable` when fewer than requested) |
| `questions.per_section` | 1–10 (default 3, as Mlynček A) | B makes one question per LO; any value above 1 (also the default) gives the warning `per_section_unsupported` |
| `split` | `{"mode": "single"}` (default) | one lesson |
| | `{"mode": "lessons", "lesson_count": 5}` | that many lessons in prerequisite order, even in length, cut preferably between topics (`lesson_count_reduced`, `lessons_unbalanced`) |
| | `{"mode": "topics"}` | one lesson per topic (concept-graph community); small topics merged, big ones cut (3–15 LOs) |
| | `{"mode": "syllabus", "syllabus": [{"title": "…", "description": "…", "pages": "D1:1-10"}]}` | the teacher's lessons; LOs by page (`D1` = first uploaded file), else by similarity (`syllabus_unmentioned_sections`, `empty_lesson`, `prerequisite_order`); `D3` with two files → `400 invalid_options` |
| | `{"mode": "syllabus", "syllabus_text": "3 hodiny: …"}` | the LLM turns the text into lessons (titles in the teacher's words, "vynechaj …" leaves content out: `syllabus_skipped_sections`); unusable text → `topics` (`syllabus_plan_invalid`) |
| `split.lesson_minutes` | 5–240 | accepted but not used for the number of lessons (as Mlynček A; with a syllabus: `syllabus_lesson_minutes_ignored`) |

Warnings go to the report (`warnings[]` with `code` and `message`); the codes are the same as Mlynček A's. The split never makes a job fail. In the result, the question text and the choices are HTML-escaped (ECEH shows them as HTML, so `#include <stdio.h>` appears exactly like that); names and lesson titles are plain text. A question text or choice still longer than its ECEH column after one LLM shortening leaves the question out (`field_too_long`); a too-long hint is left out (`hint_too_long`). The report's `quality` has `question_faithfulness_avg` and `answerability_avg` (B's faithfulness and question-clarity scores, 0–1) and `topic_coverage` (share of the document's topics that got a question).

**OCR in the image.** The image is built with Tesseract by default. Without it: `docker build --build-arg INSTALL_OCR=0 .` (or `false`); `/v1/info` then reports `"ocr_available": false`.

**Webhooks.** With `callback_url` in the options (its host must be in `LOGEN_WEBHOOK_ALLOWED_HOSTS`), the service POSTs the job state there on `job.stage_changed` (stages `extract`, `language`, `units`, `learning_objects`, `lo_evaluation`, `graph`, `questions`, `split`, `output`), `job.completed`, `job.failed` and `job.cancelled`; the result itself is fetched from `/result`. Headers and body are the same as Mlynček A's:

```http
X-QGen-Event: job.completed
X-QGen-Delivery: 5b0e9c1a-…            (the same in every retry)
X-QGen-Timestamp: 1793701125
X-QGen-Signature: sha256=<HMAC-SHA256(LOGEN_WEBHOOK_SECRET, timestamp + "." + body)>
```

Without a 2xx answer within `LOGEN_WEBHOOK_TIMEOUT_S` the delivery is retried after 10 s, 60 s and 300 s, each attempt signed with its own timestamp. Messages can arrive out of order or twice: compare `occurred_at` and `X-QGen-Delivery`. Polling `GET /v1/jobs/{id}` always works as a fallback.

**What is kept and for how long.** The uploaded documents (and the checkpoint, which holds LLM answers about them) are deleted right after `completed` or `cancelled`, and 24 h after `failed` (until then `POST /retry` works). Finished jobs (result, report, state) are deleted after 7 days; `GET` then answers `404 job_not_found`.

**Logs and stopping:**

```bash
docker compose logs -f worker      # progress of the jobs
docker compose logs -f scheduler   # webhook deliveries and cleanup
docker compose down                # stop (jobs and Redis data are kept in volumes)
docker compose down -v             # stop and delete all jobs
```

---

## Development UI with Docker

The older step-by-step web UI (`main.py`, `pipeline_ui.html`) is not part of the service contract but still works. It runs from the same image:

```bash
cd fastapi
docker compose build
docker compose -f docker-compose.yml -f docker-compose.local.yml --profile dev-ui up -d
```

Open `http://localhost:8001`. Input files and outputs are kept on your host in `fastapi/input/` and `fastapi/output/`.

---

## Running without Docker

```bash
cd fastapi
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
python -m spacy download en_core_web_sm
python -c "import nltk; nltk.download('stopwords')"
```

Copy your environment file:

```bash
cp .env.example .env
# edit .env with your keys
```

Start the server:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Then open `http://localhost:8000` in your browser.

---

## Using the Web UI

Open `http://localhost:8001` after starting the `dev-ui` profile (or `http://localhost:8000` without Docker). The UI walks through the pipeline in order:

1. **Upload Sources** - drag and drop or browse for PDF, HTML, or PPTX files. You can also load an existing `learning_objects.json` here to skip straight to View & Edit.
2. **Extract Units** - the LLM extracts atomic knowledge units from each section of every uploaded file, then deduplicates them semantically
3. **Generate LOs** - units are transformed into learning objects in batches, then multiple-choice variants are generated
4. **Evaluate & Filter** - LOs are evaluated by two Groq models using a faithfulness threshold; failing LOs are regenerated. Kimi K2 can be added as a third evaluator if you have an OpenRouter key.
5. **Build Knowledge Graph** - candidate prerequisite edges are generated and verified by an LLM, enforced as a DAG
6. **View & Edit LOs** - browse, search, filter by type, edit questions/answers/hints/MC options inline, delete unwanted LOs. Every save is written automatically to `fastapi/output/learning_objects.json`. You can also add more source files here to extend the existing run.
7. **Export to ECHub** - enrich LOs and convert to EChub JSON format for upload
8. **Export Files** - download LOs as JSON or CSV, atomic units as JSON, graph data as JSON

---

## API Reference

These are the endpoints of the development UI (`main.py`), not the `/v1` service API described above. They are documented interactively at `/docs` of the development UI.

### Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Serves the web UI |
| `GET` | `/logs` | Server-Sent Events stream of all pipeline log output |
| `POST` | `/extract-units` | Upload a file -> extract and deduplicate atomic units |
| `POST` | `/generate-los` | Atomic units -> learning objects with MC variants. Body: `{units}` |
| `POST` | `/evaluate` | Filter and regenerate LOs by quality. Body: `{learning_objects, units, models, faithfulness_threshold, max_attempts}` |
| `POST` | `/build-graph` | Units + LOs -> concept graph + LLM-verified knowledge graph. Body: `{units, learning_objects}` |
| `POST` | `/statistics` | LOs + evaluations -> aggregate stats. Body: `{learning_objects, evaluations}` |
| `POST` | `/add-sources` | Add new units to an existing LO set and rebuild the graph. Body: `{existing_units, existing_los, new_units}` |
| `POST` | `/export-echub` | Convert LOs to EChub JSON format. Body: `{learning_objects, mode, unit_title, unit_description}` |
| `POST` | `/save-los` | Save current LO list to `/app/output/`. Body: `{learning_objects, filename}` |

### Example: full pipeline via curl

```bash
# Stage 1: extract units from a PDF
curl -X POST http://localhost:8000/extract-units \
  -F "file=@my_textbook.pdf" \
  -o units.json

# Stage 2: generate learning objects
curl -X POST http://localhost:8000/generate-los \
  -H "Content-Type: application/json" \
  -d @units.json \
  -o los.json

# Stage 3: evaluate and filter
curl -X POST http://localhost:8000/evaluate \
  -H "Content-Type: application/json" \
  -d "{\"learning_objects\": $(jq .learning_objects los.json), \"units\": $(jq .units units.json)}" \
  -o los_filtered.json

# Stage 4: build knowledge graph
curl -X POST http://localhost:8000/build-graph \
  -H "Content-Type: application/json" \
  -d "{\"units\": $(jq .units units.json), \"learning_objects\": $(jq .learning_objects los_filtered.json)}" \
  -o graph.json
```

---

## Running the whole pipeline without the UI

`lo_pipeline/runner.py` runs every step on one or more documents and writes one result JSON (LOs, graphs, ECHub export, timings):

```bash
cd fastapi
docker run --rm -v "$(pwd):/app" -w /app lo-generator-fastapi \
  python -m lo_pipeline.runner input/modul1.pdf input/modul2.pptx --out output/result.json
```

From Python: `from lo_pipeline.runner import run_pipeline; result = run_pipeline(["modul1.pdf"], progress_callback=print)`.

**Resuming after a crash.** The CLI saves progress to `<out>.checkpoint/`: the result of each finished step and every LLM answer as it arrives. If a run is interrupted, run the same command again. It skips finished steps and reuses the saved answers, so no LLM call is paid twice. If the input files or options changed, the old checkpoint is discarded and the run starts over. The folder is removed after a successful run. Use `--checkpoint-dir` to choose the folder or `--no-checkpoint` to turn this off.

**A real run with a cost limit.** `tools/capped_run.py` runs the whole pipeline with job options and stops when it reaches a number of LLM calls; it writes the pipeline result, the `units` result and the report to `output/`. `--inspect` only shows the extracted pages and the detected language (no LLM):

```bash
cd fastapi
docker run --rm -v "$(pwd):/app" -w /app lo-generator-fastapi python tools/capped_run.py input/X.pdf --inspect
docker run --rm -u root -v "$(pwd):/app" -w /app lo-generator-fastapi \
  python tools/capped_run.py input/X.pdf --max-calls 120 --language auto --split '{"mode": "topics"}'
```

A 12-slide deck takes about 90–120 calls, the 26-page `Testing_text.pdf` about 400.

---

## Running tests

Tests replay recorded LLM answers, so they need no API key and make no LLM calls. Run them in the Docker image:

```bash
cd fastapi
docker compose build
docker run --rm -v "$(pwd):/app" -e PYTHONHASHSEED=0 -w /app lo-generator-fastapi \
  python -m pytest -q tests
```

`PYTHONHASHSEED=0` keeps the prompt order stable. After a deliberate change to prompts or pipeline logic, re-record the answers with the real LLM (`-e LLM_MODE=record`, needs keys in `fastapi/.env`). This also refreshes the snapshot in `tests/fixtures/snapshots/`; review its diff before committing.

### Failure scenarios in containers (no LLM cost)

`tools/resilience_check.py` starts its own stack (project `logen-resilience`, API on `127.0.0.1:18000`, 2 workers) with a fake LLM that answers from the recorded test answers (`docker-compose.fake-llm.yml`, `tools/fake_llm_server.py`). It then checks 9 scenarios: API restart right after 202, Redis restart during a job, a worker killed once and twice (`worker_lost`), retry, cancelling a running and a waiting job, a webhook retry with a valid signature, and one job queued three times. Finally it deletes the stack. Your own stack is not touched. It runs on the host with the Python standard library only and takes about 5 minutes:

```bash
cd fastapi
docker build -t lo-generator-fastapi:latest .   # the image must contain the current code
python tools/resilience_check.py                # --only 3 4 for some scenarios, --keep to leave the stack running
```

---

## Project Structure

```
.
└── fastapi/
    ├── .env                            <- your API keys and service settings (not committed to the repo)
    ├── .env.example                    <- copy to .env and fill in
    ├── docker-compose.yml              <- the service: api + worker + redis (internal network only)
    ├── docker-compose.local.yml        <- adds http://localhost:8000 and the dev UI profile
    ├── docker-compose.eceh.yml         <- joins ECEH's Docker network (deployment)
    ├── Dockerfile                      <- builds the Python 3.11 image, installs dependencies, downloads models
    ├── requirements.txt                <- all Python dependencies
    ├── pipeline_ui.html                <- single-page web UI served at http://localhost:8000
    ├── main.py                         <- FastAPI app: all endpoints, SSE log streaming, auto-save to output/
    ├── lo_pipeline/                    <- pipeline core (no web/UI code)
    │   ├── extraction/documents.py     <- text extraction from PDF (PDFium, PyPDF2 fallback)/HTML/PPTX, section splitting
    │   ├── extraction/ocr.py           <- optional OCR (Tesseract) of scanned PDF pages and PPTX pictures
    │   ├── language.py                 <- document language (sk/en) and output language
    │   ├── llm/service.py              <- LLM call wrapper with retries and rate limit handling, output-language rule
    │   ├── llm/embeddings.py           <- sentence-transformers wrapper (all-MiniLM-L6-v2) for cosine similarity
    │   ├── llm/json_load.py            <- tolerant JSON parsing of LLM output
    │   ├── units/extractor.py          <- extracts atomic knowledge units from text sections and semantic deduplication
    │   ├── units/normalizer.py         <- normalises concept strings for consistent graph node naming
    │   ├── lo/generator.py             <- batched LO generation and multiple-choice variant generation
    │   ├── lo/evaluator.py             <- batched LLM evaluation of LOs
    │   ├── lo/quality_filter.py        <- quality filter + regeneration of failing LOs
    │   ├── lo/lexical.py               <- tokenizer and lexical overlap
    │   ├── graph/builder.py            <- builds concept co-occurrence graph, generates and LLM-verifies prerequisite edges, enforces DAG
    │   ├── questions/echub.py          <- question names and time estimates (enrich); older ECHub export
    │   ├── questions/no_answer.py      <- no_answer tasks from LOs that share a concept
    │   ├── questions/shorten.py        <- one LLM shortening of texts longer than the ECEH columns
    │   ├── questions/text_safety.py    <- unsafe HTML removed from plain-text fields
    │   ├── questions/units_output.py   <- pipeline result -> `units` result + report (checked against the schemas)
    │   ├── split/                      <- lessons: order, lessons/topics/syllabus modes, syllabus_text plan, titles
    │   ├── contract/                   <- API contract models (units, report, options, jobs) -> schemas/*.json
    │   ├── runner.py                   <- run_pipeline(): every step in one call, progress reporting, CLI
    │   ├── checkpoint.py               <- saved progress so an interrupted run can resume
    │   └── files.py                    <- JSON load/save helpers for standalone scripts
    ├── service/                        <- the /v1 API, job store (Redis + /data), worker, mock backend
    ├── schemas/                        <- JSON Schemas of the contract (units, report, options), generated
    ├── analysis/                       <- thesis analysis scripts (post_statistics, lexical overlap, unit merger)
    ├── tools/                          <- capped_run.py (one real run with an LLM call cap), resilience_check.py, fake_llm_server.py
    ├── tests/                          <- pytest suite; LLM answers are replayed from tests/fixtures/llm_cassette.json
    ├── input/                          <- uploaded source files (persisted on host via Docker volume mount)
    └── output/                         <- generated outputs: atomic_units.json, learning_objects.json, echub_export.json
``` 
---
The *_OUTPUTS folders included are mentioned in the conceptual part of the thesis and are here for the sake of reproducibility and transparency of the evaluation.

## License

MIT