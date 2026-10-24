"""
Where the DAST queue surface keeps what one process writes and another reads.

Django's default cache is one process's own memory, which is enough for a read cache but not for a
value that crosses processes. A queue command is sent by the Celery worker that holds the Docker
socket, while its receipt -- or the gateway's refusal, naming the field that was wrong -- is
collected by the web process that accepted the click. That store has to be one both of them see.

It is a cache of its own rather than the platform's default because the two want opposite things
from an outage. Everything kept here is recoverable, and the code that reads it treats an
unreachable store as an empty one; the default cache carries request throttling, which has no such
handling and would answer 500 on sign-in while Valkey was down. Promoting this URL to ``default``
would additionally make throttle counters and the bulk-finding marker shared across processes --
worth doing, but as a deliberate platform-wide decision rather than a side effect of this surface.

Where the store lives follows the Celery broker -- the Valkey this deployment already runs -- rather
than a second setting someone has to remember, and falls back to process memory exactly where there
is no Valkey at all (unit tests run on a SQLite broker).
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

#: The cache alias the DAST queue surface reads and writes.
QUEUE_CACHE_ALIAS = "dast_queue"

#: Broker schemes that are a Valkey/Redis server, and therefore usable as a cache.
_SHARED_SCHEMES = frozenset({"redis", "rediss"})


def shared_cache_location(broker_url: str) -> str:
    """
    The cache URL implied by a Celery broker URL, or "" when that broker is not a shared store.

    The cache takes the database after the broker's own, so a flush of either never touches the
    other.
    """
    broker = urlsplit(broker_url)
    if broker.scheme not in _SHARED_SCHEMES:
        return ""
    database = broker.path.strip("/")
    if database and not database.isdigit():
        return ""
    return urlunsplit(broker._replace(path=f"/{int(database or 0) + 1}"))
