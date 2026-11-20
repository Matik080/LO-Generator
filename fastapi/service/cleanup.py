"""Deleting old inputs and jobs (sprint task 7, api-design-appendix.md: Príloha D).

Uploaded documents are not kept longer than needed:
    completed, cancelled  inputs + checkpoint deleted at once (worker / API; this sweep catches the rest)
    failed                inputs + checkpoint deleted after LOGEN_FAILED_INPUT_TTL_HOURS (24 h);
                          until then POST /retry works, afterwards it answers 409 job_not_retryable
    any final state       the whole job (folder, state, external_ref) deleted LOGEN_RESULT_TTL_DAYS (7)
                          after its last change; GET then answers 404 job_not_found
Queued and running jobs are never touched. A job folder without a state in Redis (e.g. Redis data
lost) is deleted when it is older than the result TTL.

The scheduler container runs sweep() every LOGEN_CLEANUP_INTERVAL_S seconds (service/scheduler.py).
"""

import logging
import shutil
from datetime import datetime, timedelta, timezone

from service.store import ACTIVE, JobStore

logger = logging.getLogger(__name__)


def sweep(store: JobStore, result_ttl: timedelta) -> dict[str, int]:
    """One cleanup pass. Returns how many inputs, jobs and orphan folders were deleted."""
    now = store.clock()
    counts = {"inputs_deleted": 0, "jobs_deleted": 0, "orphan_folders_deleted": 0}
    known = set()
    for job_id in store.all_ids():
        known.add(job_id)
        fields = store.fields(job_id)
        if fields is None:  # only in the index: drop it
            store.redis.zrem("logen:jobs", job_id)
            continue
        status = fields["status"]
        if status in ACTIVE:
            continue
        if now - datetime.fromisoformat(fields["updated_at"]) >= result_ttl:
            store.delete(job_id)
            counts["jobs_deleted"] += 1
            logger.info(f"[Cleanup] Job {job_id} ({status}) deleted after {result_ttl}.")
            continue
        retryable_until = fields.get("retryable_until")
        keep_for_retry = status == "failed" and retryable_until and now < datetime.fromisoformat(retryable_until)
        if not keep_for_retry and store.delete_inputs(job_id):
            counts["inputs_deleted"] += 1
            logger.info(f"[Cleanup] Inputs of job {job_id} ({status}) deleted.")

    if store.jobs_dir.exists():
        for folder in store.jobs_dir.iterdir():
            if folder.name in known or not folder.is_dir():
                continue
            changed = datetime.fromtimestamp(folder.stat().st_mtime, tz=timezone.utc)
            if now - changed >= result_ttl:
                shutil.rmtree(folder, ignore_errors=True)
                counts["orphan_folders_deleted"] += 1
                logger.info(f"[Cleanup] Folder {folder.name} without a job state deleted.")
    return counts
