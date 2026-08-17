"""Tasks that exist to be interrupted.

The queue is configured to survive a worker dying mid-job — `task_acks_late` and
`task_reject_on_worker_lost` — and that configuration is worth exactly as much as
the last time somebody proved it. The review asks for the drill by name: kill the
worker while a job is running and watch the job come back.

Doing that with a real order is not an option: the drill would have to be run
against production money, or against a staging copy that nobody keeps in sync. So
there is a job here that does nothing except take its time and leave a trail.
Kill the worker while it runs, and the log tells you whether the queue kept its
promise.
"""

from __future__ import annotations

import time

from app.core.logging import get_logger
from app.workers.celery_app import celery_app

logger = get_logger("diagnostics")


@celery_app.task(name="diagnostics.slow_noop", bind=True)
def slow_noop(self, seconds: int = 20, label: str = "drill") -> dict[str, object]:
    """Occupy a worker for a while, touching nothing.

    Returns the attempt number, which is the whole point: a second attempt with
    the same id means the interrupted job was redelivered rather than lost.
    """
    seconds = max(1, min(int(seconds), 120))
    attempt = self.request.retries
    logger.warning(
        "diagnostics.started",
        label=label,
        task_id=self.request.id,
        attempt=attempt,
        seconds=seconds,
    )
    # Slept in one-second steps so the log shows how far it got before it was
    # killed, which is the difference between "redelivered" and "never ran".
    for elapsed in range(seconds):
        time.sleep(1)
        if elapsed and elapsed % 5 == 0:
            logger.warning(
                "diagnostics.alive", label=label, task_id=self.request.id, elapsed=elapsed
            )
    logger.warning("diagnostics.finished", label=label, task_id=self.request.id, attempt=attempt)
    return {"label": label, "task_id": self.request.id, "attempt": attempt, "seconds": seconds}
