import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceConfig:
    database_url: str
    payment_timeout_seconds: float

    @classmethod
    def from_environment(cls) -> "ServiceConfig":
        database_url = os.environ.get("PARCELFLOW_DATABASE_URL", "sqlite:///parcelflow.db")
        timeout = float(os.environ.get("PARCELFLOW_PAYMENT_TIMEOUT", "4.0"))
        return cls(database_url=database_url, payment_timeout_seconds=timeout)
