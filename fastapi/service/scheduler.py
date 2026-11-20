"""The scheduler container (sprint task 7): webhook deliveries and cleanup.

    python -m service.scheduler

- Webhooks: an RQ worker for the queue logen-webhooks only (the pipeline worker takes the queue
  logen), so a webhook is sent within a moment even while a long job runs. with_scheduler=True
  runs RQ's scheduler too: it puts the delayed retries (10 s, 60 s, 300 s) back into the queue.
  The deliveries are small HTTP posts, so the worker does not fork a process per delivery.
- Cleanup: a background thread runs service.cleanup.sweep at start and then every
  LOGEN_CLEANUP_INTERVAL_S seconds (old inputs and finished jobs, see cleanup.py).
"""

import logging
import threading
from datetime import timedelta

logger = logging.getLogger(__name__)


def cleanup_loop(store, result_ttl: timedelta, interval_s: float, stop: threading.Event) -> None:
    """sweep() now and then every interval_s seconds until `stop` is set; errors are logged, not fatal."""
    from service.cleanup import sweep

    while True:
        try:
            counts = sweep(store, result_ttl)
            if any(counts.values()):
                logger.info(f"[Cleanup] {counts}")
        except Exception:
            logger.exception("[Cleanup] Sweep failed; trying again next time.")
        if stop.wait(interval_s):
            return


def main() -> None:
    from redis import Redis
    from rq import Queue, SimpleWorker

    from service.settings import ServiceSettings
    from service.webhooks import WEBHOOK_QUEUE
    from service.worker import store_from_settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = ServiceSettings.from_env()
    if not settings.webhooks_enabled:
        logger.info("[Scheduler] Webhooks are off (LOGEN_WEBHOOK_ALLOWED_HOSTS is empty).")

    threading.Thread(target=cleanup_loop, daemon=True, name="cleanup",
                     args=(store_from_settings(settings), timedelta(days=settings.result_ttl_days),
                           settings.cleanup_interval_s, threading.Event())).start()

    connection = Redis.from_url(settings.redis_url)
    SimpleWorker([Queue(WEBHOOK_QUEUE, connection=connection)], connection=connection).work(with_scheduler=True)


if __name__ == "__main__":
    main()
