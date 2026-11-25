"""Webhooks to the ECEH backend (sprint task 7, api-design-appendix.md: "Webhook").

When a job reaches a new stage or a final state, a POST goes to the job's options.callback_url.
Headers and body are the same as Mlynček A's, so Laravel checks both services with one code:

    X-QGen-Event: job.completed
    X-QGen-Delivery: <id of this delivery, the same in every retry>
    X-QGen-Timestamp: <unix seconds of this attempt>
    X-QGen-Signature: sha256=<HMAC-SHA256(LOGEN_WEBHOOK_SECRET, timestamp + "." + body)>
    body: WebhookPayload (state only; the result is fetched from /result and /report)

Deliveries go through their own RQ queue (logen-webhooks), processed by the scheduler container
(service/scheduler.py), so a webhook never waits for a long pipeline job. A delivery that does
not get 2xx is retried after 10 s, 60 s and 300 s (RQ keeps the retries in Redis), then given up;
the job's state does not change and Laravel still has GET /v1/jobs/{id}.
Each attempt is signed with its own timestamp: Laravel rejects messages older than 5 minutes,
and the last retry comes about 6 minutes after the event.
"""

import hashlib
import hmac
import logging
import time
import uuid
from typing import Callable, Optional

import httpx

from lo_pipeline.contract import WebhookPayload

logger = logging.getLogger(__name__)

WEBHOOK_QUEUE = "logen-webhooks"
RETRY_INTERVALS_S = [10, 60, 300]
FAILED_DELIVERY_TTL_S = 7 * 24 * 60 * 60  # how long RQ keeps a given-up delivery (for diagnostics)


class WebhookDeliveryFailed(Exception):
    """No 2xx answer; RQ retries the delivery."""


def signature(secret: str, timestamp: int, body: bytes) -> str:
    """'sha256=' + HMAC-SHA256(secret, timestamp + '.' + body), as Laravel computes it."""
    digest = hmac.new(secret.encode("utf-8"), str(timestamp).encode("ascii") + b"." + body, hashlib.sha256)
    return "sha256=" + digest.hexdigest()


def send(url: str, body: str, event: str, delivery_id: str, secret: str, timeout_s: float,
         now: Callable[[], float] = time.time, post: Optional[Callable] = None) -> int:
    """One delivery attempt. Returns the HTTP status; raises WebhookDeliveryFailed without 2xx."""
    post = post or httpx.post
    raw = body.encode("utf-8")
    timestamp = int(now())
    headers = {
        "Content-Type": "application/json",
        "X-QGen-Event": event,
        "X-QGen-Delivery": delivery_id,
        "X-QGen-Timestamp": str(timestamp),
        "X-QGen-Signature": signature(secret, timestamp, raw),
    }
    try:
        response = post(url, content=raw, headers=headers, timeout=timeout_s)
    except httpx.HTTPError as e:
        raise WebhookDeliveryFailed(f"{event} {delivery_id}: {type(e).__name__}") from e
    if not 200 <= response.status_code < 300:
        raise WebhookDeliveryFailed(f"{event} {delivery_id}: HTTP {response.status_code}")
    return response.status_code


def deliver(url: str, body: str, event: str, delivery_id: str) -> int:
    """RQ job in the scheduler container: one attempt with the configured secret and timeout."""
    from service.settings import ServiceSettings

    settings = ServiceSettings.from_env()
    status = send(url, body, event, delivery_id, settings.secret(), settings.webhook_timeout_s)
    logger.info(f"[Webhook] {event} {delivery_id} delivered (HTTP {status}).")
    return status


class WebhookNotifier:
    """JobStore.on_event: builds the payload at the moment of the event and queues its delivery."""

    def __init__(self, enqueue: Callable[[str, str, str, str], None]):
        """`enqueue(url, body, event, delivery_id)` puts one delivery into the webhook queue."""
        self.enqueue = enqueue

    def __call__(self, store, job_id: str, event: str) -> None:
        url = store.load(job_id).options.callback_url
        if url is None:
            return  # this job wants no webhooks
        status = store.status(job_id)
        payload = WebhookPayload(event=event, job_id=job_id, external_ref=status.external_ref,
                                 status=status.status, stage=status.stage, progress=status.progress,
                                 error=status.error, occurred_at=store.clock())
        delivery_id = str(uuid.uuid4())
        self.enqueue(str(url), payload.model_dump_json(), event, delivery_id)
        logger.info(f"[Webhook] {event} of job {job_id} queued ({delivery_id}).")


def rq_enqueue(queue) -> Callable[[str, str, str, str], None]:
    """enqueue(...) for WebhookNotifier: an RQ job with retries after 10 s, 60 s and 300 s."""
    from rq import Retry

    def enqueue(url: str, body: str, event: str, delivery_id: str) -> None:
        queue.enqueue("service.webhooks.deliver", url, body, event, delivery_id,
                      retry=Retry(max=len(RETRY_INTERVALS_S), interval=RETRY_INTERVALS_S),
                      job_timeout=60, result_ttl=0, failure_ttl=FAILED_DELIVERY_TTL_S)
    return enqueue


def notifier_from_settings(settings) -> Optional[WebhookNotifier]:
    """A notifier for JobStore(on_event=...), or None when webhooks are off (no allowed hosts)."""
    if not settings.webhooks_enabled:
        return None
    from service.redis_client import connect
    from rq import Queue
    return WebhookNotifier(rq_enqueue(Queue(WEBHOOK_QUEUE, connection=connect(settings.redis_url))))
