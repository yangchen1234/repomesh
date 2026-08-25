from dataclasses import dataclass


@dataclass
class TrackingUpdate:
    parcel_id: str
    status: str
    location: str


class TrackingTimeline:
    def __init__(self) -> None:
        self._updates: dict[str, list[TrackingUpdate]] = {}

    def append(self, update: TrackingUpdate) -> None:
        self._updates.setdefault(update.parcel_id, []).append(update)

    def latest(self, parcel_id: str) -> TrackingUpdate | None:
        updates = self._updates.get(parcel_id, [])
        return updates[-1] if updates else None
