# Integračný test Mlynček B ↔ ECEH

Postup na spoločné stretnutie: nasadiť Mlynček B vedľa ECEH a overiť, že Laravel vytvorí úlohu, dostane webhooky a stiahne výsledok vo formáte `units`. Tým sa splní posledné kritérium úlohy 6 („Laravel vytvorí úlohu a stiahne výsledok vo formáte `units`“).

Všetky príkazy sú z priečinka `fastapi/`. Popis API je v [README](../README.md) (časť *Running as a service*) a v [prílohe A](api-design-appendix.md).

---

## 1. Pred stretnutím (sám)

- [ ] Všetko je commitnuté a pushnuté, na serveri bude rovnaký kód.
- [ ] Testy prechádzajú (`python -m pytest -q tests` v Docker image, cca 2 min).
- [ ] Image sa dá zostaviť: `docker build -t lo-generator-fastapi:latest .` (predvolene s OCR, Tesseract; bez neho `--build-arg INSTALL_OCR=0`).
- [ ] Pripravené tajné hodnoty (napr. `openssl rand -hex 24`):
  - heslo pre Redis (`LOGEN_REDIS_PASSWORD`), iba písmená a číslice,
  - API kľúč pre Laravel (`LOGEN_API_KEYS`),
  - tajný kľúč webhookov (`LOGEN_WEBHOOK_SECRET`). Ten istý musí mať Laravel.
- [ ] Na Groq je kredit. Jedna úloha s malým dokumentom (`c_memory.html`) stojí cca 30 volaní, prezentácia s 12 snímkami cca 90 – 120, `Testing_text.pdf` cca 400 volaní (5 – 6 min).

### Na čo sa opýtať vopred

**Adela (nasadenie):**
- Ako sa volá Docker sieť ECEH (`docker network ls`)? To bude `LOGEN_ECEH_NETWORK`.
- Pod akým menom je kontajner Laravelu v tejto sieti? To bude `LOGEN_WEBHOOK_ALLOWED_HOSTS`.
- Je v sieti ECEH už služba s menom `lo-generator`? (Mlynček B sa v nej ohlási pod týmto menom.)

**Ladislav (Laravel):**
- Na akú adresu má prísť webhook (napr. `http://<laravel>/api/internal/qgen/webhook`)?
- Má Laravel už kontrolu podpisu webhooku (náčrt v prílohe A, časť *Webhook*)? S akým kľúčom?
- Posiela súbory vo formulárovom poli presne `files`? (Pole `files[]` služba neprijme, vráti `422 validation_error`.)
- Číta Laravel aj `/report`, alebo iba `/result`?
- Kontroluje výsledok JSON schémou (`GET /v1/schema/lesson`)?

**Ladislavovi oznámiť (zmeny od posledného šprintu, rozhranie ostáva rovnaké ako pri Mlynčeku A):**
- Webhooky `job.stage_changed` majú aj kroky `language` (po `extract`) a `split` (pred `output`). Obe sú v zozname krokov kontraktu.
- Zadanie otázky (`description`) a texty možností (`choices[].text`) sú escapované HTML (napr. `#include &lt;stdio.h&gt;`), lebo frontend ECEH ich zobrazuje ako HTML cez DOMPurify. `name` a názov a popis hodiny sú obyčajný text.
- Pri `split` inom ako `single` príde viac hodín v `units`; varovania rozdelenia (`lesson_count_reduced`, `empty_lesson`, `syllabus_*` …) majú rovnaké kódy ako pri A a sú v `/report`.
- Texty, ktoré píše kód, sú v jazyku výstupu: pri anglickom dokumente nápoveda „Source“ a „page“, pri slovenskom „Zdroj“ a „strana“.
- `/v1/info` má `ocr_available`; `extraction.ocr = true` bez OCR v image vráti `400 invalid_options`.

---

## 2. Nasadenie

1. Kód a konfigurácia:
   ```bash
   git pull
   cd fastapi
   cp .env.example .env      # ak .env ešte nie je
   ```
2. V `fastapi/.env` vyplniť:
   ```env
   GROQ_API_KEY=...
   LOGEN_LLM_MODEL=qwen/qwen3.8-27b
   LOGEN_REDIS_PASSWORD=...
   LOGEN_API_KEYS=...                       # kľúč, ktorý pošle Laravel v X-API-Key
   LOGEN_ECEH_NETWORK=...                   # názov siete ECEH
   LOGEN_WEBHOOK_ALLOWED_HOSTS=...          # meno kontajnera Laravelu v tejto sieti
   LOGEN_WEBHOOK_SECRET=...                 # rovnaký ako v Laraveli
   ```
3. Zostaviť a spustiť:
   ```bash
   docker build -t lo-generator-fastapi:latest .
   docker compose -f docker-compose.yml -f docker-compose.eceh.yml up -d
   docker compose -f docker-compose.yml -f docker-compose.eceh.yml ps
   ```
   - [ ] Bežia 4 kontajnery: `api` (healthy do cca 20 s), `worker`, `scheduler`, `redis` (healthy).
4. Overiť, že sa Laravel dostane k službe (z kontajnera Laravelu):
   ```bash
   docker exec <laravel-kontajner> curl -s http://lo-generator:8000/health
   ```
   - [ ] Odpoveď `{"status":"ok", ...}`.

   Ak v kontajneri Laravelu nie je `curl`, rovnako sa to overí pomocným kontajnerom v sieti ECEH:
   ```bash
   docker run --rm --network <siet-eceh> lo-generator-fastapi:latest curl -s http://lo-generator:8000/health
   ```

---

## 3. Test z Laravelu

Poradie je od lacného po drahé. Body a–c nestoja nič, body d a ďalej volajú LLM.

### a) Kľúč a základné endpointy
- [ ] `GET /v1/info` bez `X-API-Key` → `401 unauthorized`.
- [ ] `GET /v1/info` so správnym kľúčom → `200`, v odpovedi `"backend": "redis"` a `"ocr_available": true`.
- [ ] `GET /v1/schema/lesson` → JSON schéma výsledku (Laravel si ju môže uložiť).

### b) Chybné požiadavky (bez LLM)
- [ ] Súbor `.docx` → `415 unsupported_file`.
- [ ] `callback_url` s iným hostiteľom → `400 invalid_options`.
- [ ] Súbory v poli `files[]` namiesto `files` → `422 validation_error` (ak áno, upraviť v Laraveli na `files`).

### c) Úloha, ktorá zlyhá (bez LLM)
HTML bez textu (napr. `<html><body></body></html>`) s `callback_url`:
- [ ] `POST /v1/jobs` → `202`, `status: queued`.
- [ ] Prídu webhooky `job.stage_changed` (stage `extract`) a `job.failed` s `error.code = extraction_failed`.
- [ ] Laravel overí podpis (`X-QGen-Signature`) a odpovie `2xx`.
- [ ] `GET /v1/jobs/{id}` vráti rovnaký stav ako posledný webhook.

### d) Malá skutočná úloha (cca 30 volaní LLM, asi minúta)
Dokument `fastapi/tests/fixtures/c_memory.html`, `options` napr. `{"external_ref": "test-1", "callback_url": "<adresa webhooku>"}`:
- [ ] Webhooky `job.stage_changed` pre kroky `extract`, `language`, `units` … `split`, `output` a nakoniec `job.completed`.
- [ ] `GET /v1/jobs/{id}/result` → `200`, jeden unit s názvom a popisom od LLM, otázky `choice` / `open` a na konci skupín `no_answer`, prejde schémou. Dokument je anglický, preto nápovedy „Source“ a „page“.
- [ ] `GET /v1/jobs/{id}/report` → `200` (ak ho Laravel používa).
- [ ] Laravel uloží výsledok do databázy ECEH (tabuľky units, events, helps, options) a otázky sa zobrazia v ECEH.
- [ ] Druhý `POST` s rovnakým `external_ref` → `200`, `replayed: true`, rovnaké `job_id` (žiadna nová úloha).

### e) Zrušenie a retry
- [ ] Spustiť úlohu a počas behu `DELETE /v1/jobs/{id}` → `200`, `status: cancelled`, príde jeden webhook `job.cancelled`.
- [ ] `DELETE` dokončenej úlohy → `204`, potom `GET` → `404 job_not_found`.
- [ ] `POST /v1/jobs/{id}/retry` pre úlohu z bodu c → `200`, `status: queued` (zlyhá znova, ale overí sa, že retry funguje).

### f) Voliteľne: rozdelenie do hodín a jazyk (cca 30 volaní každý)
Ten istý malý dokument s iným `external_ref`:
- [ ] `"split": {"mode": "lessons", "lesson_count": 2}` → v `result` dve hodiny, každá s vlastným názvom; v `report.question_meta` `unit` 0 aj 1.
- [ ] `"language": "sk"` → otázky, možnosti a nápovedy po slovensky, hoci dokument je anglický.
- [ ] `"split": {"mode": "syllabus", "syllabus": [{"title": "A", "pages": "D3:1"}]}` s jedným súborom → `400 invalid_options` (bez LLM).

### g) Voliteľne: veľký dokument (cca 400 volaní, 5 – 6 min)
- [ ] `Testing_text.pdf`: Laravel čaká na webhook `job.completed`, priebeh (`progress`) v ECEH rastie.

---

## 4. Čo zapísať

| Bod | Výsledok | Poznámka |
| --- | --- | --- |
| Nasadenie (4 kontajnery, health) | | |
| a) kľúč a endpointy | | |
| b) chybné požiadavky | | |
| c) zlyhaná úloha + webhooky | | |
| d) malá úloha, výsledok v ECEH | | |
| e) zrušenie a retry | | |
| f) rozdelenie a jazyk | | |
| g) veľký dokument (čas) | | |

Ak prejde aspoň d), úloha 6 je hotová (posledné kritérium). Odchýlky zapísať do Jiry k úlohe 6 alebo 7.

---

## 5. Keď niečo nefunguje

| Prejav | Príčina a riešenie |
| --- | --- |
| `docker compose` hneď skončí s „required variable LOGEN_REDIS_PASSWORD is missing“ | V `fastapi/.env` chýba `LOGEN_REDIS_PASSWORD`. |
| „network … declared as external, but could not be found“ | Zlý `LOGEN_ECEH_NETWORK`. Overiť `docker network ls`. |
| Kontajner `api` sa nespustí, v logu chyba nastavení | Neplatná hodnota v `.env` (napr. `LOGEN_WEBHOOK_ALLOWED_HOSTS` bez `LOGEN_WEBHOOK_SECRET`). Log: `docker compose ... logs api`. |
| Laravel dostane `401 unauthorized` | Kľúč v `X-API-Key` sa nezhoduje s `LOGEN_API_KEYS`. |
| `400 invalid_options` pri `callback_url` | Hostiteľ z `callback_url` nie je v `LOGEN_WEBHOOK_ALLOWED_HOSTS` (musí sedieť presne meno, bez portu). |
| `422 validation_error` „At least one file is required“ | Súbory nie sú v poli `files` (napr. `files[]`). |
| Webhook nepríde | Log `docker compose ... logs scheduler`. Ak tam je „HTTP 401“, Laravel odmieta podpis (rôzny `LOGEN_WEBHOOK_SECRET`). Ak „ConnectError“, scheduler nevidí Laravel (sieť, meno hostiteľa). Služba skúsi doručiť ešte po 10 s, 60 s a 300 s. Záloha: `GET /v1/jobs/{id}`. |
| Úloha `failed` s `llm_error` a správou „rejected the API key“ | Neplatný `GROQ_API_KEY`. Správa „Rate limit“: znížiť `LOGEN_LLM_MAX_CONCURRENCY` (napr. na 2). |
| Úloha visí v `queued` | Nebeží `worker`. `docker compose ... ps`, `docker compose ... logs worker`. |
| `400 invalid_options` pri `extraction.ocr` | Image je zostavený bez OCR (`INSTALL_OCR=0`); `/v1/info` ukáže `"ocr_available": false`. |
| Otázky majú v ECEH viditeľné `&lt;` alebo `&amp;` | Laravel alebo frontend zobrazuje `description` / `choices` ako obyčajný text; patria do HTML zobrazenia (ako dnes v `QuestionView.tsx`). |

Užitočné príkazy (vždy s oboma súbormi `-f docker-compose.yml -f docker-compose.eceh.yml`):
```bash
docker compose ... logs -f worker       # priebeh úloh
docker compose ... logs -f scheduler    # webhooky a mazanie
docker compose ... restart worker       # nedokončená úloha pokračuje z checkpointu
docker compose ... down                 # zastaviť (úlohy ostanú vo volume)
docker compose ... down -v              # zastaviť a zmazať všetky úlohy
```

## Po teste

- Dokumenty sa zmažú samy hneď po dokončení, výsledky po 7 dňoch.
- `LOGEN_API_KEYS` a `LOGEN_WEBHOOK_SECRET` z testu ponechať, ak služba ostane nasadená; inak ich vymeniť.
