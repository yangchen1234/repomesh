from dataclasses import dataclass
from typing import Protocol

from .events import EventBus, OrderAccepted
from .inventory import InventoryLedger
from .models import Order


class PaymentPort(Protocol):
    def authorize(self, order_id: str, amount_cents: int) -> str: ...


@dataclass
class CheckoutReceipt:
    order_id: str
    payment_authorization: str


class CheckoutService:
    """Coordinates stock reservation, payment authorization, and domain events."""

    def __init__(self, inventory: InventoryLedger, payments: PaymentPort, events: EventBus) -> None:
        self.inventory = inventory
        self.payments = payments
        self.events = events

    def place_order(self, order: Order) -> CheckoutReceipt:
        self.inventory.reserve(order)
        try:
            authorization = self.payments.authorize(order.order_id, int(order.total() * 100))
        except Exception:
            self.inventory.release(order.order_id)
            raise
        self.events.publish(OrderAccepted(order.order_id, order.customer_id))
        return CheckoutReceipt(order.order_id, authorization)
