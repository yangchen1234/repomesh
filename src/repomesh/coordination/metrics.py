from __future__ import annotations

from collections.abc import Iterator

from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
    Metric,
)

from repomesh.coordination.repository import CoordinationRepository


class CoordinationCollector:
    """An API scrape observes every process through durable coordination state."""

    def __init__(self, store: CoordinationRepository) -> None:
        self.store = store

    def collect(self) -> Iterator[Metric]:
        snapshot = self.store.snapshot()
        for key in ("claimed", "completed", "failed", "retried", "reclaimed"):
            yield CounterMetricFamily(
                f"repomesh_jobs_{key}", f"Job attempts {key}", value=snapshot[key]
            )
        for key in ("queue_depth", "active_jobs", "active_workers", "active_leases"):
            yield GaugeMetricFamily(f"repomesh_{key}", key.replace("_", " "), value=snapshot[key])
        durations = snapshot["durations"]
        buckets = [(str(bound), durations[f"b{bound}"]) for bound in (1, 5, 10, 30, 60, 300, 900)]
        buckets.append(("+Inf", durations["count"]))
        yield HistogramMetricFamily(
            "repomesh_job_duration_seconds",
            "Finished attempt duration",
            buckets=buckets,
            sum_value=durations["sum"],
        )
