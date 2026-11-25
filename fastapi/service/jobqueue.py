"""The RQ queue between the API and the worker (sprint task 4a).

The queue carries only the job id; the job itself is in the job folder and Redis (store.py).
RQ needs its own Redis connection without decode_responses (it stores bytes).
"""

from rq import Queue

from service.redis_client import connect

QUEUE_NAME = "logen"
DEFAULT_JOB_TIMEOUT_S = 6 * 60 * 60  # RQ's default (180 s) would stop every real job
FAILURE_TTL_S = 24 * 60 * 60         # RQ's own record of a crashed job; our state is in the store


def make_queue(redis_url: str) -> Queue:
    return Queue(QUEUE_NAME, connection=connect(redis_url))


def waiting_job_ids(queue: Queue) -> set[str]:
    """Our job ids that wait in the queue (not yet taken by a worker)."""
    return {job.args[0] for job in queue.get_jobs() if job is not None and job.args}


def enqueue_job(queue: Queue, job_id: str, timeout_s: int = DEFAULT_JOB_TIMEOUT_S) -> None:
    queue.enqueue("service.worker.process_job", job_id,
                  job_timeout=timeout_s, result_ttl=0, failure_ttl=FAILURE_TTL_S)
