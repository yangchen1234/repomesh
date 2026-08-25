from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class OrderLine:
    sku: str
    quantity: int
    unit_price: Decimal


@dataclass(frozen=True)
class Order:
    order_id: str
    customer_id: str
    lines: tuple[OrderLine, ...]

    def total(self) -> Decimal:
        return sum((line.unit_price * line.quantity for line in self.lines), Decimal("0"))
