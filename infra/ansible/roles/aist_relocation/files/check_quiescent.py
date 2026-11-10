"""Abort an operator relocation if a worker or broker still owns work."""
# ruff: noqa: INP001 -- Standalone payload executed by Ansible inside the existing worker.
import os

import redis
from celery import current_app

inspector = current_app.control.inspect(timeout=10)
for operation in (inspector.active, inspector.reserved, inspector.scheduled):
    replies = operation()
    if not replies or any(replies.values()):
        msg = "Workers must respond with no active, reserved or scheduled tasks before relocation"
        raise RuntimeError(msg)

broker = redis.Redis.from_url(os.environ["DD_CELERY_BROKER_URL"])
queued = sum(broker.llen(key) for key in broker.scan_iter() if broker.type(key) == b"list")
if queued or broker.hlen("unacked"):
    msg = "The broker still contains queued or unacknowledged messages"
    raise RuntimeError(msg)
