# Clean-machine test in WSL (sprint 2, task 6)

For the user and for a Claude Code session running in WSL (Ubuntu). Read `CLAUDE.md` in the repo root
first: it has the whole project context (what Mlynček B is, code layout, testing, status). This file
adds only what is specific to running in WSL.

## Goal

Check two acceptance criteria of task 6 (`docs/jira-sprint-2-mlyncek-b.md`) the way a fresh server
would see them, without any LLM cost:

1. `docker compose up` on a clean machine starts the service and `/health` returns 200 within 60 s.
2. The image built without OCR (`INSTALL_OCR=0`) starts, `/v1/info` reports `"ocr_available": false`
   and `extraction.ocr = true` gives `400 invalid_options`.

Also useful: the test suite in an image built without cache (the newest package versions, as on a
new server).

## Rules for this session (same as in CLAUDE.md)

- **Run nothing until the user says so.** Show the command, say what it costs, wait for "run it".
  Reading and editing files is fine.
- **Never commit or push.** Suggest a commit message instead.
- **Real LLM calls cost the user money (Groq).** Nothing below needs them. A small real job
  (`c_memory.html`) is about 30 calls; ask first and give the cost in dollars.
- Explain simply and in small steps; the user is under time pressure.

## Things to know about WSL here

- **Docker is shared with Windows** (Docker Desktop WSL integration): the same images, build cache and
  volumes as on the Windows side. A "clean machine" therefore needs `--no-cache`.
- **The image name is fixed** (`lo-generator-fastapi:latest` in `fastapi/docker-compose.yml`). A build in
  WSL overwrites the image the user works with on Windows. Back it up first (step 1) and restore it at
  the end (step 6). Older backups also exist: `:before-pdfium`, `:before-ocr`.
- **`requirements.txt` is not pinned.** A `--no-cache` build installs the newest versions of every
  package. That is what a new server gets, so it is worth testing, but results may differ from the
  Windows image. `fastapi/requirements.lock.txt` lists the versions of the working image.
- The Windows repo is at `/mnt/c/Users/matti/Desktop/Bakalarska_praca/LO-Generator`. Clone from there
  (only committed changes come along). The ECEH repo (Laravel backend, frontend) is in WSL at
  `/home/dell/VS/WS_26/TP/ecehub`.
- Copyrighted course material in `fastapi/input/` (e.g. `Testing_text.pdf`) is gitignored and is not in
  the clone. Nothing below needs it.

## Steps

### 1. Back up the image and make a clean copy

```bash
docker tag lo-generator-fastapi:latest lo-generator-fastapi:backup-before-wsl
git clone /mnt/c/Users/matti/Desktop/Bakalarska_praca/LO-Generator ~/logen-clean
cd ~/logen-clean/fastapi
cp .env.example .env
```

In `fastapi/.env` set at least (test values, not the production ones):

```env
LOGEN_REDIS_PASSWORD=test1234
LOGEN_API_KEYS=testkey
```

The Groq key is needed only for a real job (not part of this test).

### 2. Build like a clean machine (about 10–20 min: PyTorch, models, Tesseract)

```bash
docker compose -p logen-clean build --no-cache
```

`-p logen-clean` keeps the containers and volumes separate from the user's own stack (project
`lo-generator`).

### 3. Start and time `/health` (criterion: 200 within 60 s)

```bash
docker compose -p logen-clean -f docker-compose.yml -f docker-compose.local.yml up -d
start=$(date +%s); until curl -sf http://localhost:8000/health >/dev/null; do sleep 1; done; echo "health after $(( $(date +%s) - start )) s"
docker compose -p logen-clean ps                                   # api healthy, worker, scheduler, redis
curl -s -H "X-API-Key: testkey" http://localhost:8000/v1/info      # "backend": "redis", "ocr_available": true
```

`docker-compose.local.yml` publishes the API on `127.0.0.1:8000`. If the port is taken (e.g. by the
user's own stack), stop that first.

### 4. Tests in the fresh image (offline, about 2 min, no LLM)

```bash
docker run --rm --network none -v "$PWD:/app" -e PYTHONHASHSEED=0 -w /app \
  lo-generator-fastapi:latest python -m pytest -q -p no:warnings tests
```

On 2026-11-26 the Windows image passed 431 tests. If some fail here, compare the package versions
with `requirements.lock.txt` (`docker run --rm lo-generator-fastapi:latest pip freeze`); a newer
package is the likely cause.

### 5. Image without OCR

```bash
docker build --build-arg INSTALL_OCR=0 -t lo-generator-fastapi:noocr .
docker run --rm -d --name logen-noocr -p 127.0.0.1:8010:8000 lo-generator-fastapi:noocr
sleep 15; curl -s http://localhost:8010/v1/info                    # "ocr_available": false
curl -s -F "files=@tests/fixtures/c_memory.html" -F 'options={"extraction": {"ocr": true}}' \
  http://localhost:8010/v1/jobs                                    # 400 invalid_options
docker rm -f logen-noocr
```

This container runs the mock backend (default `LOGEN_BACKEND=mock`), which is enough for these checks.

### 6. Clean up and restore the user's image

```bash
cd ~
docker compose -p logen-clean -f ~/logen-clean/fastapi/docker-compose.yml down -v
docker tag lo-generator-fastapi:backup-before-wsl lo-generator-fastapi:latest
docker rmi lo-generator-fastapi:noocr
rm -rf ~/logen-clean
```

## What to write down

| Check | Result |
| --- | --- |
| Build without cache finished (time) | |
| `/health` 200 after … s (criterion ≤ 60 s) | |
| `docker compose ps`: 4 containers, api and redis healthy | |
| `/v1/info`: `backend redis`, `ocr_available true` | |
| Tests in the fresh image (passed / failed) | |
| Image without OCR: `ocr_available false`, `ocr: true` → 400 | |

Put the results into the Jira task 6 comment (Slovak) and, if they change the project state, a short
note into `CLAUDE.md` (status section). If anything failed, keep the full output for the Windows session.

## When something goes wrong

| Symptom | Cause / fix |
| --- | --- |
| `required variable LOGEN_REDIS_PASSWORD is missing` | `fastapi/.env` has no `LOGEN_REDIS_PASSWORD`. |
| `port is already allocated` (8000 or 8010) | Another container uses the port: `docker ps`, stop it, or change the port in the command. |
| `api` not healthy, log shows a settings error | `docker compose -p logen-clean logs api`; an invalid value in `.env`. |
| Build fails while downloading | Network/proxy in WSL; retry. PyTorch is about 200 MB. |
| Tests fail only in the fresh image | A newer package version (unpinned `requirements.txt`); compare with `requirements.lock.txt`. Do not "fix" the code without telling the user. |
| Permission denied writing into the clone | Files created by a root container; use `docker run -u root …` for that command. |
