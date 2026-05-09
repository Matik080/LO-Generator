# LO Pipeline

Automated generation of structured learning objects (LOs) from educational source materials. Given PDF, HTML, or PPTX files, the pipeline extracts atomic knowledge units, generates question/answer/hint learning objects with multiple-choice distractors, evaluates and filters them by quality, and constructs a prerequisite knowledge graph (DAG) over the results.

Built as part of a bachelor's thesis at FIIT STU Bratislava.

---

## Table of Contents

- [Overview](#overview)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running with Docker](#running-with-docker)
- [Running without Docker](#running-without-docker)
- [Using the Web UI](#using-the-web-ui)
- [API Reference](#api-reference)
- [Project Structure](#project-structure)

---

## Overview

The pipeline runs in four stages:

1. **Atomic Unit Extraction** — source documents are split into sections; each section is processed by an LLM to extract self-contained atomic knowledge units (definitions, rules, examples, algorithm steps, etc.) and semantically deduplicated
2. **LO Generation** — each atomic unit is transformed into a structured learning object: a question, a detailed answer, an optional hint, and multiple-choice distractors
3. **Quality Filtering** — generated LOs are evaluated by two models (Llama 3.3 70B and GPT-OSS 120B on Groq); LOs that fail faithfulness, atomicity, or self-containment thresholds are regenerated up to a configurable number of times
4. **Knowledge Graph** — a concept co-occurrence graph is built from the units, candidate prerequisite edges are generated from type hierarchy and embedding similarity, each edge is verified by an LLM, and the result is enforced as a DAG

---

## Requirements

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/) — recommended
- Or Python 3.11+ for running without Docker

**API keys** — at least one of:

| Provider | Used for | Get key at |
|---|---|---|
| [Groq](https://console.groq.com) | Llama 3.3 70B + GPT-OSS 120B (generation + evaluation) | console.groq.com |
| [OpenRouter](https://openrouter.ai) | Kimi K2 Instruct (optional third evaluator) | openrouter.ai |
| GitHub Models | Fallback | github.com/marketplace/models |
| OpenAI | Fallback | platform.openai.com |

Groq's developer tier provides both Llama 3.3 70B and GPT-OSS 120B for free and is sufficient to run the full pipeline. OpenRouter is only needed if you want to add Kimi K2 as an additional evaluator model.

---

## Installation

```bash
git clone https://github.com/your-username/lo-pipeline.git
cd lo-pipeline
```

Copy the environment template and fill in your API keys:

```bash
cp env_template fastapi/.env
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

---

## Running with Docker

**Build and start:**

```bash
docker compose up --build
```

Open `http://localhost:8000` in your browser. The web UI loads directly.

Input files and outputs are persisted on your host machine via volume mounts — they survive container restarts:
- `fastapi/input/` — uploaded source files
- `fastapi/output/` — generated outputs (`atomic_units.json`, `learning_objects.json`, `echub_export.json`)

**Stop:**

```bash
docker compose down
```

**Rebuild after code changes:**

```bash
docker compose up --build --force-recreate
```

> **Note:** The first build takes several minutes — it installs PyTorch (CPU) and downloads the `all-MiniLM-L6-v2` sentence embedding model and spaCy's `en_core_web_sm`. Subsequent builds use the Docker layer cache and are much faster.

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
cp ../env_template .env
# edit .env with your keys
```

Start the server:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Then open `http://localhost:8000` in your browser.

---

## Using the Web UI

Open `http://localhost:8000` after starting the container. The UI walks through the pipeline in order:

1. **Upload Sources** — drag and drop or browse for PDF, HTML, or PPTX files. You can also load an existing `learning_objects.json` here to skip straight to View & Edit.
2. **Extract Units** — the LLM extracts atomic knowledge units from each section of every uploaded file, then deduplicates them semantically
3. **Generate LOs** — units are transformed into learning objects in batches, then multiple-choice variants are generated
4. **Evaluate & Filter** — LOs are evaluated by two Groq models using a faithfulness threshold; failing LOs are regenerated. Kimi K2 can be added as a third evaluator if you have an OpenRouter key.
5. **Build Knowledge Graph** — candidate prerequisite edges are generated and verified by an LLM, enforced as a DAG
6. **View & Edit LOs** — browse, search, filter by type, edit questions/answers/hints/MC options inline, delete unwanted LOs. Every save is written automatically to `fastapi/output/learning_objects.json`. You can also add more source files here to extend the existing run.
7. **Export to ECHub** — enrich LOs and convert to EChub JSON format for upload
8. **Export Files** — download LOs as JSON or CSV, atomic units as JSON, graph data as JSON

---

## API Reference

The API is documented interactively at `http://localhost:8000/docs` (Swagger UI) once the server is running.

### Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Serves the web UI |
| `GET` | `/logs` | Server-Sent Events stream of all pipeline log output |
| `POST` | `/extract-units` | Upload a file → extract and deduplicate atomic units |
| `POST` | `/generate-los` | Atomic units → learning objects with MC variants. Body: `{units}` |
| `POST` | `/evaluate` | Filter and regenerate LOs by quality. Body: `{learning_objects, units, models, faithfulness_threshold, max_attempts}` |
| `POST` | `/build-graph` | Units + LOs → concept graph + LLM-verified knowledge graph. Body: `{units, learning_objects}` |
| `POST` | `/statistics` | LOs + evaluations → aggregate stats. Body: `{learning_objects, evaluations}` |
| `POST` | `/add-sources` | Add new units to an existing LO set and rebuild the graph. Body: `{existing_units, existing_los, new_units}` |
| `POST` | `/export-echub` | Convert LOs to EChub JSON format. Body: `{learning_objects, mode, unit_title, unit_description}` |
| `POST` | `/save-los` | Save current LO list to `/app/output/`. Body: `{learning_objects, filename}` |

### Example: full pipeline via curl

```bash
# Stage 1 — extract units from a PDF
curl -X POST http://localhost:8000/extract-units \
  -F "file=@my_textbook.pdf" \
  -o units.json

# Stage 2 — generate learning objects
curl -X POST http://localhost:8000/generate-los \
  -H "Content-Type: application/json" \
  -d @units.json \
  -o los.json

# Stage 3 — evaluate and filter
curl -X POST http://localhost:8000/evaluate \
  -H "Content-Type: application/json" \
  -d "{\"learning_objects\": $(jq .learning_objects los.json), \"units\": $(jq .units units.json)}" \
  -o los_filtered.json

# Stage 4 — build knowledge graph
curl -X POST http://localhost:8000/build-graph \
  -H "Content-Type: application/json" \
  -d "{\"units\": $(jq .units units.json), \"learning_objects\": $(jq .learning_objects los_filtered.json)}" \
  -o graph.json
```

---

## Project Structure

```
.
├── docker-compose.yml
├── env_template                  ← copy to fastapi/.env and fill in keys
└── fastapi/
    ├── .env                      ← your API keys (not committed)
    ├── Dockerfile
    ├── requirements.txt
    ├── pipeline_ui.html          ← served at http://localhost:8000
    ├── main.py                   ← FastAPI app + all endpoints
    ├── llm_service.py            ← LLM call wrapper (Groq / OpenRouter / GitHub / OpenAI)
    ├── atomic_unit_extractor.py
    ├── concept_normalizer.py
    ├── echub_format_converter.py ← EChub JSON export
    ├── embedding_service.py      ← sentence-transformers (all-MiniLM-L6-v2)
    ├── ems.py                    ← offline pipeline runner (used by main.py)
    ├── graph_builder.py
    ├── learning_object_evaluator.py
    ├── learning_object_generator.py
    ├── lexical_overlap_computation.py
    ├── post_statistics.py
    ├── utilities.py
    ├── input/                    ← uploaded source files (persisted via volume)
    └── output/                   ← pipeline outputs (persisted via volume)
```

---

## License

MIT