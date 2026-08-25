from collections import defaultdict

from .models import Order


class InsufficientInventory(RuntimeError):
    pass


class InventoryLedger:
    """Maintains available units and atomic order reservations."""

    def __init__(self) -> None:
        self._available: dict[str, int] = defaultdict(int)
        self._reservations: dict[str, dict[str, int]] = {}

    def receive(self, sku: str, quantity: int) -> None:
        if quantity <= 0:
            raise ValueError("received quantity must be positive")
        self._available[sku] += quantity

    def reserve(self, order: Order) -> None:
        requested = {line.sku: line.quantity for line in order.lines}
        missing = {sku: amount for sku, amount in requested.items() if self._available[sku] < amount}
        if missing:
            raise InsufficientInventory(f"insufficient inventory: {missing}")
        for sku, amount in requested.items():
            self._available[sku] -= amount
        self._reservations[order.order_id] = requested

    def release(self, order_id: str) -> None:
        for sku, amount in self._reservations.pop(order_id, {}).items():
            self._available[sku] += amount
