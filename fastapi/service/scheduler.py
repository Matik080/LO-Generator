"""The scheduler container (sprint task 7): webhook deliveries, cleanup and job recovery.

    python -m service.scheduler

- Webhooks: an RQ worker for the queue logen-webhooks only (the pipeline worker takes the queue
  logen), so a webhook is sent within a moment even while a long job runs. with_scheduler=True
  runs RQ's scheduler too: it puts the delayed retries (10 s, 60 s, 300 s) back into the queue.
  The deliveries are small HTTP posts, so the worker does not fork a process per delivery.
- Cleanup: a background thread runs service.cleanup.sweep at start and then every
  LOGEN_CLEANUP_INTERVAL_S seconds (old inputs and finished jobs, see cleanup.py).
- Recovery: another thread runs service.worker.recover_jobs every LOGEN_RECOVERY_INTERVAL_S
  seconds. A job whose worker died (heartbeat expired) goes back to the queue even when no
  worker restarts, e.g. with several workers or when a worker restarted before the heartbeat expired.
"""

import logging
import threading
from datetime import timedelta

logger = logging.getLogger(__name__)


def repeat(name: str, task, interval_s: float, stop: threading.Event) -> None:
    """task() now and then every interval_s seconds until `stop` is set; errors are logged, not fatal.
    task() returns counts; they are logged when one of them is not 0."""
    while True:
        try:
            counts = task()
            if any(counts.values()):
                logger.info(f"[{name}] {counts}")
        except Exception:
            logger.exception(f"[{name}] Failed; trying again next time.")
        if stop.wait(interval_s):
            return


def cleanup_loop(store, result_ttl: timedelta, interval_s: float, stop: threading.Event) -> None:
    """cleanup.sweep() now and then every interval_s seconds until `stop` is set."""
    from service.cleanup import sweep
    repeat("Cleanup", lambda: sweep(store, result_ttl), interval_s, stop)


RECOVERY_CHANGES = ("requeued", "worker_lost", "cancelled")  # logged; jobs left alone are not


def recovery_loop(recover, interval_s: float, stop: threading.Event) -> None:
    """recover() (worker.recover_jobs with the real store and queue) every interval_s seconds."""
    repeat("Recovery", lambda: {k: v for k, v in recover().items() if k in RECOVERY_CHANGES},
           interval_s, stop)


def main() -> None:
    from redis import Redis
    from rq import Queue, SimpleWorker

    from service.settings import ServiceSettings
    from service.webhooks import WEBHOOK_QUEUE
    from service.worker import recover_from_settings, store_from_settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = ServiceSettings.from_env()
    if not settings.webhooks_enabled:
        logger.info("[Scheduler] Webhooks are off (LOGEN_WEBHOOK_ALLOWED_HOSTS is empty).")

    threading.Thread(target=cleanup_loop, daemon=True, name="cleanup",
                     args=(store_from_settings(settings), timedelta(days=settings.result_ttl_days),
                           settings.cleanup_interval_s, threading.Event())).start()
    threading.Thread(target=recovery_loop, daemon=True, name="recovery",
                     args=(lambda: recover_from_settings(settings), settings.recovery_interval_s,
                           threading.Event())).start()

    connection = Redis.from_url(settings.redis_url)
    SimpleWorker([Queue(WEBHOOK_QUEUE, connection=connection)], connection=connection).work(with_scheduler=True)


if __name__ == "__main__":
    main()
