"""
Checkpoints let an interrupted run_pipeline continue instead of starting over.

Two things are saved:
  - the result of every finished stage: a resumed run skips those stages;
  - every LLM answer, as soon as it arrives: inside the stage that was interrupted, calls that were already
    made are answered from the checkpoint (same prompt -> same saved answer), so no LLM call is paid twice.

A checkpoint also stores a fingerprint of the input files and options. If it doesn't match, the old
checkpoint is discarded and the run starts fresh, so results from different inputs are never mixed.

FileCheckpoint keeps everything in one folder (the CLI's --checkpoint-dir, later /data/jobs/{id}/checkpoint).
MemoryCheckpoint is for tests.
"""
import hashlib
import json
import logging
import os
import shutil
from dataclasses import asdict

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1


def fingerprint(sources, options) -> str:
    """Hash of the input files' bytes and titles plus the pipeline options."""
    h = hashlib.sha256(f"v{FORMAT_VERSION}".encode())
    for src in sources:
        h.update(b"\0source\0" + (src.title or "").encode("utf-8") + b"\0")
        with open(src.path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    h.update(json.dumps(asdict(options), sort_keys=True).encode("utf-8"))
    return h.hexdigest()


class Checkpoint:
    """Interface. Stage results are JSON-serializable dicts; LLM answers are strings keyed by request hash."""

    def start(self, fp: str) -> bool:
        """Begin a run. Returns True if a matching checkpoint exists (resume), False if starting fresh."""
        raise NotImplementedError

    def completed_stage(self, stage: str):
        """The saved result of `stage`, or None if the stage didn't finish."""
        raise NotImplementedError

    def save_stage(self, stage: str, result: dict):
        raise NotImplementedError

    # LLM response cache (used through llm.service.use_response_cache)
    def get(self, key: str):
        raise NotImplementedError

    def put(self, key: str, response: str):
        raise NotImplementedError

    def clear(self):
        """Remove everything (called after the run finished successfully)."""
        raise NotImplementedError


class MemoryCheckpoint(Checkpoint):
    def __init__(self):
        self.fp, self.stages, self.responses = None, {}, {}

    def start(self, fp):
        if self.fp == fp:
            return bool(self.stages or self.responses)
        self.fp, self.stages, self.responses = fp, {}, {}
        return False

    def completed_stage(self, stage):
        return self.stages.get(stage)

    def save_stage(self, stage, result):
        self.stages[stage] = json.loads(json.dumps(result))  # same round-trip as the file version

    def get(self, key):
        return self.responses.get(key)

    def put(self, key, response):
        self.responses[key] = response

    def clear(self):
        self.fp, self.stages, self.responses = None, {}, {}


class FileCheckpoint(Checkpoint):
    """
    Folder layout:
      meta.json            {"version", "fingerprint", "stages": [finished stage names]}
      stage_<name>.json    result of one finished stage
      llm_calls.jsonl      one saved LLM answer per line, appended as they arrive
    Files are written atomically (temp file + rename), so a crash never leaves a half-written stage file.
    A crash while appending can cut the last line of llm_calls.jsonl; that line is ignored on load.
    """

    def __init__(self, directory: str):
        self.dir = directory
        self.meta = None
        self.responses = {}
        self._calls_file = None

    def _path(self, name):
        return os.path.join(self.dir, name)

    def start(self, fp):
        os.makedirs(self.dir, exist_ok=True)
        meta = _read_json(self._path("meta.json"))
        if meta and meta.get("version") == FORMAT_VERSION and meta.get("fingerprint") == fp:
            self.meta = meta
            self.responses = _read_jsonl(self._path("llm_calls.jsonl"))
            resumed = bool(meta["stages"] or self.responses)
            if resumed:
                logger.info(f"[Checkpoint] Resuming: {len(meta['stages'])} stage(s) done, "
                            f"{len(self.responses)} saved LLM answer(s).")
            return resumed
        if meta:
            logger.warning("[Checkpoint] Existing checkpoint is for other input files or options; starting fresh.")
        self._wipe()
        os.makedirs(self.dir, exist_ok=True)
        self.meta = {"version": FORMAT_VERSION, "fingerprint": fp, "stages": []}
        _write_json_atomic(self._path("meta.json"), self.meta)
        return False

    def completed_stage(self, stage):
        if stage not in self.meta["stages"]:
            return None
        return _read_json(self._path(f"stage_{stage}.json"))

    def save_stage(self, stage, result):
        _write_json_atomic(self._path(f"stage_{stage}.json"), result)
        if stage not in self.meta["stages"]:
            self.meta["stages"].append(stage)
        _write_json_atomic(self._path("meta.json"), self.meta)  # stage counts as done only after this

    def get(self, key):
        return self.responses.get(key)

    def put(self, key, response):
        self.responses[key] = response
        if self._calls_file is None:
            self._calls_file = open(self._path("llm_calls.jsonl"), "a", encoding="utf-8")
        self._calls_file.write(json.dumps({"key": key, "response": response}, ensure_ascii=False) + "\n")
        self._calls_file.flush()
        os.fsync(self._calls_file.fileno())

    def close(self):
        if self._calls_file is not None:
            self._calls_file.close()
            self._calls_file = None

    def clear(self):
        self.close()
        self._wipe()
        self.meta, self.responses = None, {}

    def _wipe(self):
        self.close()
        if os.path.isdir(self.dir):
            for name in os.listdir(self.dir):
                # only our own files (incl. leftover .tmp), so a folder shared with other files stays intact
                if name.startswith(("meta.json", "stage_", "llm_calls")):
                    os.remove(self._path(name))
            if not os.listdir(self.dir):
                shutil.rmtree(self.dir, ignore_errors=True)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _read_jsonl(path):
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    out[entry["key"]] = entry["response"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue  # a line cut off by a crash
    except FileNotFoundError:
        pass
    return out


def _write_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
