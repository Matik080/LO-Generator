"""The job worker (sprint task 4a): runs one queued job from start to finish.

RQ calls process_job(job_id) in the worker process; everything else comes from the job folder
and Redis (service/store.py). run_job is the same with the store and pipeline passed in (tests).

    queued -> running -> completed            result.json + report.json written
                      -> failed               error code, see _job_error
                      -> cancelled            DELETE during the run; stops after the current LLM batch

The checkpoint in the job folder is kept when a run stops, so a retry or a restarted worker
continues where it stopped and no LLM call is paid twice.

Start the worker with `python -m service.worker` (LOGEN_REDIS_URL, LOGEN_DATA_DIR); it first
recovers jobs a previous worker left unfinished (recover_jobs), then processes the queue.
"""

import logging
from typing import Callable, Optional

from rq.timeouts import JobTimeoutException

from lo_pipeline.checkpoint import FileCheckpoint
from lo_pipeline.contract import JobError
from lo_pipeline.extraction.documents import ExtractionError
from lo_pipeline.llm.service import LLMError
from lo_pipeline.questions.units_output import InvalidOutputError, build_units_and_report
from service.progress import overall_progress
from service.store import JobStore

logger = logging.getLogger(__name__)

RUNNING_PROGRESS_MAX = 0.99  # 1.0 only when the result is saved
MAX_ATTEMPTS = 2             # runs of one job that a lost worker may interrupt (then worker_lost)


class JobCancelled(Exception):
    """Raised from the progress callback when the job was cancelled; stops run_pipeline."""


class NoLearningObjects(LLMError):
    """The pipeline finished but produced no LOs (the LLM gave no usable answers)."""


def _default_pipeline(*args, **kwargs):
    from lo_pipeline.runner import run_pipeline  # imported here: loads spaCy etc. only in the worker
    return run_pipeline(*args, **kwargs)


def _job_error(exc: Exception, stage: Optional[str], attempt: int) -> JobError:
    """Exception -> job error (codes shared with Mlynček A, contract/jobs.py)."""
    details = {"stage": stage, "attempt": attempt}
    if isinstance(exc, ExtractionError):
        return JobError(code="extraction_failed", message=str(exc), details=details)
    if isinstance(exc, LLMError):
        return JobError(code="llm_error", message=str(exc), details=details)
    if isinstance(exc, JobTimeoutException):  # raised by RQ after LOGEN_JOB_TIMEOUT_S
        return JobError(code="internal_error", message="The job took longer than the time limit.",
                        details={**details, "reason": "timeout"})
    if isinstance(exc, InvalidOutputError):
        return JobError(code="internal_error", message="The result did not pass the output schema.",
                        details={**details, "reason": "invalid_output"})
    # anything else is a bug: no exception text in the API (it may contain document text), only in the log
    return JobError(code="internal_error", message="Unexpected error while processing the job.",
                    details={**details, "reason": type(exc).__name__})


def run_job(store: JobStore, job_id: str, pipeline: Callable = _default_pipeline) -> None:
    """Runs the job if it is queued; afterwards it is completed, failed or cancelled."""
    fields = store.fields(job_id)
    if fields is None:
        logger.info(f"[Worker] Job {job_id} no longer exists; skipped.")
        return
    if fields["status"] != "queued":
        logger.info(f"[Worker] Job {job_id} is {fields['status']}, not queued; skipped.")
        return
    if store.cancel_requested(job_id):
        store.update(job_id, status="cancelled")
        return

    attempt = store.increment_attempts(job_id)
    store.update(job_id, status="running", stage="extract", progress=0.0)
    logger.info(f"[Worker] Job {job_id}: attempt {attempt} started.")
    current = {"stage": "extract", "progress": 0.0}

    def on_progress(stage: str, done: int, total: int) -> None:
        if store.cancel_requested(job_id):
            raise JobCancelled()
        progress = min(overall_progress(stage, done / total if total else 0.0), RUNNING_PROGRESS_MAX)
        progress = max(progress, current["progress"])  # never backwards
        if (stage, progress) != (current["stage"], current["progress"]):
            current.update(stage=stage, progress=progress)
            store.update(job_id, stage=stage, progress=progress)

    job = store.load(job_id)
    sources = [{"path": str(path), "title": name} for path, name in zip(store.input_paths(job), job.files)]
    checkpoint = FileCheckpoint(str(store.checkpoint_dir(job_id)))
    try:
        result = pipeline(sources, progress_callback=on_progress, checkpoint=checkpoint)
        if not result.get("learning_objects"):
            raise NoLearningObjects("The LLM gave no usable learning objects for these documents.")
        units, report = build_units_and_report(result, job.options)
        if store.cancel_requested(job_id):
            raise JobCancelled()  # DELETE came after the last progress report: no result
        store.save_output(job_id, units, report)
        # everything run_pipeline returned (LOs, graphs, full quality summary per model): not part of
        # the API, kept in the job folder for diagnostics and quality comparisons (baseline-quality.md)
        store.save_pipeline_result(job_id, result)
    except JobCancelled:
        logger.info(f"[Worker] Job {job_id} cancelled during '{current['stage']}'.")
        store.update(job_id, status="cancelled")
        return
    except Exception as exc:
        if store.cancel_requested(job_id):  # e.g. the folder was deleted under the running job
            logger.info(f"[Worker] Job {job_id} was cancelled; error after that ignored: {type(exc).__name__}")
            store.update(job_id, status="cancelled")
            return
        error = _job_error(exc, current["stage"], attempt)
        log = logger.exception if error.code == "internal_error" else logger.warning
        log(f"[Worker] Job {job_id} failed in '{current['stage']}': {error.code}")
        store.mark_failed(job_id, error)
        return
    finally:
        checkpoint.close()

    store.update(job_id, status="completed", stage="output", progress=1.0)
    logger.info(f"[Worker] Job {job_id} completed: "
                f"{sum(len(u['questions']) for u in units['units'])} question(s).")


def recover_jobs(store: JobStore, enqueue: Callable[[str], None]) -> dict[str, int]:
    """Called when the worker starts, before it takes jobs from the queue.

    The prototype runs one worker, so a job still marked `running` lost its worker (crash,
    restart, out of memory). It goes back to the queue and continues from its checkpoint,
    unless it already had MAX_ATTEMPTS attempts (-> failed, worker_lost) or was cancelled.
    `queued` jobs are queued again too (e.g. the API stopped between saving a job and queueing
    it); a job that ends up twice in the queue runs once, run_job skips it the second time.
    """
    counts = {"requeued": 0, "worker_lost": 0, "cancelled": 0}
    for job_id in store.all_ids():
        fields = store.fields(job_id)
        if fields is None or fields["status"] not in ("queued", "running"):
            continue
        if fields["status"] == "running":
            attempts = int(fields["attempts"])
            if store.cancel_requested(job_id):
                store.update(job_id, status="cancelled")
                counts["cancelled"] += 1
                continue
            if attempts >= MAX_ATTEMPTS:
                store.mark_failed(job_id, JobError(
                    code="worker_lost",
                    message=f"The worker stopped during the job {attempts} times.",
                    details={"stage": fields["stage"] or None, "attempt": attempts}))
                counts["worker_lost"] += 1
                logger.warning(f"[Worker] Job {job_id}: worker lost {attempts} times; failed.")
                continue
            store.update(job_id, status="queued")
            logger.info(f"[Worker] Job {job_id}: worker was lost during attempt {attempts}; queued again.")
        enqueue(job_id)
        counts["requeued"] += 1
    return counts


def store_from_settings(settings) -> JobStore:
    from redis import Redis
    return JobStore(Redis.from_url(settings.redis_url, decode_responses=True), settings.data_dir)


def process_job(job_id: str) -> None:
    """Entry point for RQ (the queue stores only the job id)."""
    from service.settings import ServiceSettings
    run_job(store_from_settings(ServiceSettings.from_env()), job_id)


def main() -> None:
    """python -m service.worker: recover unfinished jobs, then process the queue until stopped."""
    from rq import Worker

    from service.jobqueue import enqueue_job, make_queue
    from service.settings import ServiceSettings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = ServiceSettings.from_env()
    store, queue = store_from_settings(settings), make_queue(settings.redis_url)
    counts = recover_jobs(store, lambda job_id: enqueue_job(queue, job_id, settings.job_timeout_s))
    logger.info(f"[Worker] Started. Recovered jobs: {counts}")
    Worker([queue], connection=queue.connection).work()


if __name__ == "__main__":
    main()
