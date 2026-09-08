"""PostgreSQL coordination, separate from SQLite index/retrieval storage."""


class LeaseLost(RuntimeError):
    """The worker no longer has authority to mutate this job or repository."""


class WorkerIdentityInUse(RuntimeError):
    """A live process already registered this worker identity."""
