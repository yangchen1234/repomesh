from collections import defaultdict
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class OrderAccepted:
    order_id: str
    customer_id: str


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[type, list[Callable[[object], None]]] = defaultdict(list)

    def subscribe(self, event_type: type, handler: Callable[[object], None]) -> None:
        self._handlers[event_type].append(handler)

    def publish(self, event: object) -> None:
        for handler in self._handlers[type(event)]:
            handler(event)
