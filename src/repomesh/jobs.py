from __future__ import annotations

import time

from repomesh.coordination.repository import TERMINAL, CoordinationRepository
from repomesh.models import Job


class JobManager:
    """Control-plane facade. Submission and waiting never execute indexing."""

    def __init__(self, coordination: CoordinationRepository) -> None:
        self.coordination = coordination

    def submit(
        self, repository_id: str, mode: str, idempotency_key: str | None, priority: int = 0
    ) -> Job:
        return self.coordination.submit_job(repository_id, mode, idempotency_key, priority)

    def wait(self, job_id: str, timeout: float = 300) -> Job:
        deadline = time.monotonic() + timeout
        while True:
            job = self.coordination.get_job(job_id)
            if job is None:
                raise ValueError("job not found")
            if job.status in TERMINAL or time.monotonic() >= deadline:
                return job
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))

    def cancel(self, job_id: str) -> Job:
        return self.coordination.request_cancel(job_id)
