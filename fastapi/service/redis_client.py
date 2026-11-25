"""Redis connections of the API, worker and scheduler (sprint 2, task 5).

A Redis restart (deployment, crash; the data stays thanks to appendonly) closes every connection.
redis-py retries a failed command only 3 times (about 7 s) by default; with RETRIES and a backoff
of 0.5, 1, 2, 4, then 5 s, every command waits up to about 30 s for Redis to come back, so a short
restart does not fail a job or an API request.
"""

from redis import Redis
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError, TimeoutError
from redis.retry import Retry

RETRIES = 8
BACKOFF = dict(cap=5.0, base=0.5)


def connect(url: str, decode_responses: bool = False) -> Redis:
    """A client that rides out a short Redis restart. RQ needs decode_responses=False (bytes)."""
    return Redis.from_url(url, decode_responses=decode_responses,
                          retry=Retry(ExponentialBackoff(**BACKOFF), RETRIES,
                                      supported_errors=(ConnectionError, TimeoutError)),
                          retry_on_error=[ConnectionError, TimeoutError])
