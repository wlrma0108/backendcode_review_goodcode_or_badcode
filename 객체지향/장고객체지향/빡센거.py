from __future__ import annotations
from dataclasses import dataclass, field
from typing import Protocol, List, Dict, Optional, Iterable, Callable, Any, Tuple
from enum import Enum, auto
from datetime import datetime, timedelta
import functools
import threading
import uuid
import random
import math
import logging

# ---------------------------
# 공용 설정/로깅
# ---------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("shop")


# ---------------------------
# 도메인: Value Objects
# ---------------------------
@dataclass(frozen=True)
class Money:
    amount: int  # KRW, 원 단위 정수로 가정
    currency: str = "KRW"

    def __post_init__(self):
        if self.amount < 0:
            raise ValueError("Money must be non-negative")

    def __add__(self, other: "Money") -> "Money":
        self._assert_same_currency(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._assert_same_currency(other)
        if other.amount > self.amount:
            raise ValueError("Money subtraction would be negative")
        return Money(self.amount - other.amount, self.currency)

    def __mul__(self, k: float) -> "Money":
        return Money(int(round(self.amount * k)), self.currency)

    def _assert_same_currency(self, other: "Money"):
        if self.currency != other.currency:
            raise ValueError("Currency mismatch")

    @classmethod
    def zero(cls) -> "Money":
        return cls(0, "KRW")


@dataclass(frozen=True)
class SKU:
    value: str

    def __post_init__(self):
        if not self.value or len(self.value) > 64:
            raise ValueError("Invalid SKU")


# ---------------------------
# 도메인: 엔티티
# ---------------------------
class OrderStatus(Enum):
    DRAFT = auto()
    SUBMITTED = auto()
    PAID = auto()
    SHIPPED = auto()
    CANCELED = auto()


@dataclass
class Customer:
    id: str
    email: str
    joined_at: datetime
    first_purchase_done: bool = False


@dataclass
class Product:
    sku: SKU
    name: str
    price: Money
    category: str


@dataclass
class InventoryItem:
    sku: SKU
    quantity: int

    def reserve(self, qty: int):
        if qty <= 0:
            raise ValueError("qty must be positive")
        if self.quantity < qty:
            raise ValueError("insufficient inventory")
        self.quantity -= qty

    def restock(self, qty: int):
        if qty <= 0:
            raise ValueError("qty must be positive")
        self.quantity += qty


@dataclass
class OrderLine:
    sku: SKU
    name: str
    unit_price: Money
    qty: int

    @property
    def line_total(self) -> Money:
        return self.unit_price * self.qty


@dataclass
class Order:
    id: str
    customer_id: str
    lines: List[OrderLine] = field(default_factory=list)
    status: OrderStatus = OrderStatus.DRAFT
    subtotal: Money = field(default_factory=Money.zero)
    discount_total: Money = field(default_factory=Money.zero)
    grand_total: Money = field(default_factory=Money.zero)
    created_at: datetime = field(default_factory=datetime.utcnow)
    events: List["DomainEvent"] = field(default_factory=list)

    def add_line(self, product: Product, qty: int):
        if self.status is not OrderStatus.DRAFT:
            raise ValueError("Can only add lines in DRAFT")
        if qty <= 0:
            raise ValueError("qty must be positive")

        self.lines.append(
            OrderLine(sku=product.sku, name=product.name, unit_price=product.price, qty=qty)
        )
        self._recalc_totals()

    def apply_discount(self, discount: Money):
        if discount.amount < 0:
            raise ValueError("negative discount")
        new_discount = self.discount_total + discount
        if new_discount.amount > self.subtotal.amount:
            raise ValueError("discount exceeds subtotal")
        self.discount_total = new_discount
        self._recalc_totals()

    def submit(self):
        if not self.lines:
            raise ValueError("empty order")
        if self.status is not OrderStatus.DRAFT:
            raise ValueError("already submitted")
        self.status = OrderStatus.SUBMITTED
        self.events.append(OrderSubmitted(order_id=self.id))

    def mark_paid(self, payment_id: str):
        if self.status is not OrderStatus.SUBMITTED:
            raise ValueError("order not submitted")
        self.status = OrderStatus.PAID
        self.events.append(PaymentReceived(order_id=self.id, payment_id=payment_id))

    def ship(self, tracking_no: str):
        if self.status is not OrderStatus.PAID:
            raise ValueError("not paid")
        self.status = OrderStatus.SHIPPED
        self.events.append(OrderShipped(order_id=self.id, tracking_no=tracking_no))

    def cancel(self, reason: str):
        if self.status in (OrderStatus.PAID, OrderStatus.SHIPPED):
            raise ValueError("cannot cancel after payment/ship")
        self.status = OrderStatus.CANCELED
        self.events.append(OrderCanceled(order_id=self.id, reason=reason))

    def _recalc_totals(self):
        self.subtotal = Money.zero()
        for l in self.lines:
            self.subtotal = self.subtotal + l.line_total
        self.grand_total = self.subtotal - self.discount_total


# ---------------------------
# 도메인: 이벤트
# ---------------------------
class DomainEvent: ...
@dataclass
class OrderSubmitted(DomainEvent):
    order_id: str

@dataclass
class PaymentReceived(DomainEvent):
    order_id: str
    payment_id: str

@dataclass
class OrderShipped(DomainEvent):
    order_id: str
    tracking_no: str

@dataclass
class OrderCanceled(DomainEvent):
    order_id: str
    reason: str


# ---------------------------
# 가격 전략(Strategy) & 프로모션 명세(Specification)
# ---------------------------
class PricingStrategy(Protocol):
    def price_for(self, product: Product, qty: int, now: datetime) -> Money: ...

class SimplePricing:
    def price_for(self, product: Product, qty: int, now: datetime) -> Money:
        return product.price * qty

class TieredPricing:
    """
    1~4개: 정가
    5~9개: 5% 할인
    10개 이상: 10% 할인
    """
    def price_for(self, product: Product, qty: int, now: datetime) -> Money:
        if qty >= 10:
            return product.price * qty * 0.90
        if qty >= 5:
            return product.price * qty * 0.95
        return product.price * qty

class PromotionSpec(Protocol):
    def is_satisfied(self, order: Order, customer: Customer) -> bool: ...
    def discount(self, order: Order, customer: Customer) -> Money: ...

@dataclass
class MinAmountSpec:
    threshold: Money
    rate: float  # 0.05 = 5%

    def is_satisfied(self, order: Order, customer: Customer) -> bool:
        return order.subtotal.amount >= self.threshold.amount

    def discount(self, order: Order, customer: Customer) -> Money:
        if not self.is_satisfied(order, customer):
            return Money.zero()
        return Money(int(round(order.subtotal.amount * self.rate)))

@dataclass
class FirstPurchaseSpec:
    fixed_amount: Money

    def is_satisfied(self, order: Order, customer: Customer) -> bool:
        return not customer.first_purchase_done

    def discount(self, order: Order, customer: Customer) -> Money:
        return self.fixed_amount if self.is_satisfied(order, customer) else Money.zero()

@dataclass
class CategoryBundleSpec:
    category: str
    free_qty: int  # e.g., "해당 카테고리 X개 이상이면 1개 무료" 같은 처리

    def is_satisfied(self, order: Order, customer: Customer) -> bool:
        total_qty = sum(l.qty for l in order.lines if l.name and l.sku and True)
        # 간단화: 카테고리는 line name으로 판단하지 않고, bundle 적용 시점에 주입된 정보 사용
        return total_qty >= self.free_qty

    def discount(self, order: Order, customer: Customer) -> Money:
        # 가장 저렴한 라인을 무료로
        if not self.is_satisfied(order, customer):
            return Money.zero()
        cheapest = min(order.lines, key=lambda l: l.unit_price.amount, default=None)
        return cheapest.unit_price if cheapest else Money.zero()

class CompositePromotion:
    def __init__(self, specs: Iterable[PromotionSpec]):
        self.specs = list(specs)

    def discount_for(self, order: Order, customer: Customer) -> Money:
        total = Money.zero()
        for s in self.specs:
            d = s.discount(order, customer)
            total = total + d
        # 할인 한도(예: 최대 30%) 같은 정책이 있으면 여기에서 캡
        cap = int(round(order.subtotal.amount * 0.30))
        return Money(min(total.amount, cap))


# ---------------------------
# 인프라: 저장소(Repository) 인터페이스/구현
# ---------------------------
class OrderRepository(Protocol):
    def get(self, order_id: str) -> Optional[Order]: ...
    def add(self, order: Order) -> None: ...
    def update(self, order: Order) -> None: ...
    def list_by_customer(self, customer_id: str) -> List[Order]: ...

class ProductRepository(Protocol):
    def get(self, sku: SKU) -> Optional[Product]: ...
    def add(self, product: Product) -> None: ...

class InventoryRepository(Protocol):
    def get(self, sku: SKU) -> Optional[InventoryItem]: ...
    def add(self, item: InventoryItem) -> None: ...
    def update(self, item: InventoryItem) -> None: ...

class CustomerRepository(Protocol):
    def get(self, customer_id: str) -> Optional[Customer]: ...
    def add(self, customer: Customer) -> None: ...
    def update(self, customer: Customer) -> None: ...

# 간단 LRU 캐시 데코레이터 (제품 조회 캐시)
def lru_cache_simple(maxsize=128):
    def deco(fn):
        cache: Dict[Any, Any] = {}
        order: List[Any] = []
        lock = threading.Lock()

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            with lock:
                if key in cache:
                    return cache[key]
            result = fn(*args, **kwargs)
            with lock:
                cache[key] = result
                order.append(key)
                if len(order) > maxsize:
                    oldest = order.pop(0)
                    cache.pop(oldest, None)
            return result
        return wrapper
    return deco

class InMemoryProductRepository(ProductRepository):
    def __init__(self):
        self._store: Dict[str, Product] = {}
        self._lock = threading.Lock()

    @lru_cache_simple(256)
    def get(self, sku: SKU) -> Optional[Product]:
        with self._lock:
            return self._store.get(sku.value)

    def add(self, product: Product) -> None:
        with self._lock:
            self._store[product.sku.value] = product

class InMemoryOrderRepository(OrderRepository):
    def __init__(self):
        self._store: Dict[str, Order] = {}
        self._lock = threading.Lock()

    def get(self, order_id: str) -> Optional[Order]:
        with self._lock:
            return self._store.get(order_id)

    def add(self, order: Order) -> None:
        with self._lock:
            if order.id in self._store:
                raise ValueError("Order already exists")
            self._store[order.id] = order

    def update(self, order: Order) -> None:
        with self._lock:
            if order.id not in self._store:
                raise ValueError("Order not found")
            self._store[order.id] = order

    def list_by_customer(self, customer_id: str) -> List[Order]:
        with self._lock:
            return [o for o in self._store.values() if o.customer_id == customer_id]

class InMemoryInventoryRepository(InventoryRepository):
    def __init__(self):
        self._store: Dict[str, InventoryItem] = {}
        self._lock = threading.Lock()

    def get(self, sku: SKU) -> Optional[InventoryItem]:
        with self._lock:
            item = self._store.get(sku.value)
            # 참조 공유 방지(단순 예시)
            return None if item is None else InventoryItem(item.sku, item.quantity)

    def add(self, item: InventoryItem) -> None:
        with self._lock:
            self._store[item.sku.value] = InventoryItem(item.sku, item.quantity)

    def update(self, item: InventoryItem) -> None:
        with self._lock:
            if item.sku.value not in self._store:
                raise ValueError("Inventory not found")
            self._store[item.sku.value] = InventoryItem(item.sku, item.quantity)

class InMemoryCustomerRepository(CustomerRepository):
    def __init__(self):
        self._store: Dict[str, Customer] = {}
        self._lock = threading.Lock()

    def get(self, customer_id: str) -> Optional[Customer]:
        with self._lock:
            return self._store.get(customer_id)

    def add(self, customer: Customer) -> None:
        with self._lock:
            self._store[customer.id] = customer

    def update(self, customer: Customer) -> None:
        with self._lock:
            if customer.id not in self._store:
                raise ValueError("Customer not found")
            self._store[customer.id] = customer


# ---------------------------
# 인프라: 결제 게이트웨이 어댑터
# ---------------------------
class PaymentGateway(Protocol):
    def charge(self, customer: Customer, amount: Money, order_id: str) -> str: ...

class DummyPaymentGateway:
    def charge(self, customer: Customer, amount: Money, order_id: str) -> str:
        # 90% 성공, 10% 실패 시뮬레이션
        if random.random() < 0.1:
            raise RuntimeError("Payment gateway temporary error")
        return f"PGPAY-{uuid.uuid4().hex[:12]}"

class FailingPaymentGateway:
    def charge(self, customer: Customer, amount: Money, order_id: str) -> str:
        raise RuntimeError("Always failing (for tests)")

# 재시도 데코레이터 (멱등키는 서비스 계층에서 관리)
def retry(times=3, backoff=0.05, exceptions=(RuntimeError,)):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last = None
            for i in range(times):
                try:
                    return fn(*args, **kwargs)
                except exceptions as e:
                    last = e
                    logger.warning(f"[retry] {fn.__name__} failed ({i+1}/{times}): {e}")
                    if i < times - 1:
                        # 간단 백오프
                        import time
                        time.sleep(backoff * (2 ** i))
            assert last is not None
            raise last
        return wrapper
    return deco


# ---------------------------
# 인프라: 이벤트 버스 (동기)
# ---------------------------
class EventBus:
    def __init__(self):
        self._handlers: Dict[type, List[Callable[[DomainEvent], None]]] = {}

    def subscribe(self, event_type: type, handler: Callable[[DomainEvent], None]):
        self._handlers.setdefault(event_type, []).append(handler)

    def publish(self, events: Iterable[DomainEvent]):
        for e in events:
            for h in self._handlers.get(type(e), []):
                try:
                    h(e)
                except Exception as ex:
                    logger.exception(f"event handler error: {ex}")

# ---------------------------
# 애플리케이션: 유닛 오브 워크
# ---------------------------
class UnitOfWork(Protocol):
    orders: OrderRepository
    products: ProductRepository
    inventory: InventoryRepository
    customers: CustomerRepository
    events: List[DomainEvent]

    def __enter__(self) -> "UnitOfWork": ...
    def __exit__(self, exc_type, exc, tb) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...

class InMemoryUnitOfWork(UnitOfWork):
    def __init__(
        self,
        orders: OrderRepository,
        products: ProductRepository,
        inventory: InventoryRepository,
        customers: CustomerRepository,
        event_bus: EventBus,
    ):
        self.orders = orders
        self.products = products
        self.inventory = inventory
        self.customers = customers
        self._bus = event_bus
        self.events: List[DomainEvent] = []
        self._active = False

    def __enter__(self) -> "InMemoryUnitOfWork":
        self._active = True
        self.events.clear()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self._active = False

    def commit(self) -> None:
        # 이벤트 플러시
        self._bus.publish(self.events)
        self.events.clear()

    def rollback(self) -> None:
        logger.info("rollback called (in-memory: no-op)")

# ---------------------------
# 도메인 정책(Policy) / 서비스
# ---------------------------
class InventoryPolicy(Protocol):
    def reserve(self, uow: UnitOfWork, sku: SKU, qty: int) -> None: ...

class StrictInventoryPolicy:
    def reserve(self, uow: UnitOfWork, sku: SKU, qty: int) -> None:
        item = uow.inventory.get(sku)
        if not item:
            raise ValueError("inventory missing")
        item.reserve(qty)
        uow.inventory.update(item)

class LenientInventoryPolicy:
    """재고가 부족하면 가능한 만큼만 잡고 백오더 허용(예시)"""
    def reserve(self, uow: UnitOfWork, sku: SKU, qty: int) -> None:
        item = uow.inventory.get(sku)
        if not item:
            raise ValueError("inventory missing")
        to_reserve = min(item.quantity, qty)
        if to_reserve > 0:
            item.reserve(to_reserve)
            uow.inventory.update(item)
        # 부족분은 백오더 큐에 넣는다고 가정(생략)

# 서비스: 주문/결제
class OrderService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        pricing: PricingStrategy,
        promotions: CompositePromotion,
        inventory_policy: InventoryPolicy,
        payment_gateway: PaymentGateway,
    ):
        self._uow_factory = uow_factory
        self._pricing = pricing
        self._promos = promotions
        self._inventory = inventory_policy
        self._pg = payment_gateway
        self._idempotency_store: Dict[str, str] = {}  # payment 멱등키 저장

    def create_order(self, customer_id: str) -> str:
        with self._uow_factory() as uow:
            order_id = f"ORD-{uuid.uuid4().hex[:10]}"
            order = Order(id=order_id, customer_id=customer_id)
            uow.orders.add(order)
            uow.events += order.events
            logger.info(f"order created: {order_id}")
            return order_id

    def add_item(self, order_id: str, sku: str, qty: int):
        with self._uow_factory() as uow:
            order = uow.orders.get(order_id)
            if not order:
                raise ValueError("order not found")
            product = uow.products.get(SKU(sku))
            if not product:
                raise ValueError("product not found")

            # 재고 선점
            self._inventory.reserve(uow, product.sku, qty)

            # 가격 전략에 따라 금액 결정 (여기서는 표시용, 최종 합계는 order.recalc)
            price = self._pricing.price_for(product, qty, datetime.utcnow())
            order.add_line(product, qty)
            # 일관성 검사용 로그
            logger.info(f"add item: {sku} x{qty}, priced={price.amount} subtotal={order.subtotal.amount}")
            uow.orders.update(order)
            uow.events += order.events

    def apply_promotions(self, order_id: str):
        with self._uow_factory() as uow:
            order = uow.orders.get(order_id)
            if not order:
                raise ValueError("order not found")
            customer = uow.customers.get(order.customer_id)
            if not customer:
                raise ValueError("customer not found")
            d = self._promos.discount_for(order, customer)
            if d.amount > 0:
                order.apply_discount(d)
                uow.orders.update(order)
                logger.info(f"promotion applied: -{d.amount} grand={order.grand_total.amount}")

    def submit(self, order_id: str):
        with self._uow_factory() as uow:
            order = uow.orders.get(order_id)
            if not order:
                raise ValueError("order not found")
            order.submit()
            uow.orders.update(order)
            uow.events += order.events
            logger.info(f"order submitted: {order.id}")

    @retry(times=3, backoff=0.1)
    def _charge(self, customer: Customer, amount: Money, order_id: str, idem_key: str) -> str:
        # 멱등성: 같은 idem_key로 반복 호출이면 동일 payment_id 반환
        if idem_key in self._idempotency_store:
            return self._idempotency_store[idem_key]
        payment_id = self._pg.charge(customer, amount, order_id)
        self._idempotency_store[idem_key] = payment_id
        return payment_id

    def checkout(self, order_id: str, idem_key: Optional[str] = None) -> str:
        idem_key = idem_key or f"idem:{order_id}"
        with self._uow_factory() as uow:
            order = uow.orders.get(order_id)
            if not order:
                raise ValueError("order not found")
            customer = uow.customers.get(order.customer_id)
            if not customer:
                raise ValueError("customer not found")
            if order.status is not OrderStatus.SUBMITTED:
                raise ValueError("order not submitted")

            payment_id = self._charge(customer, order.grand_total, order.id, idem_key)
            order.mark_paid(payment_id)
            uow.orders.update(order)
            # 첫 구매 처리
            if not customer.first_purchase_done:
                customer.first_purchase_done = True
                uow.customers.update(customer)
            uow.events += order.events
            logger.info(f"payment ok: {payment_id}, order grand={order.grand_total.amount}")
            return payment_id

    def ship(self, order_id: str, tracking_no: str):
        with self._uow_factory() as uow:
            order = uow.orders.get(order_id)
            if not order:
                raise ValueError("order not found")
            order.ship(tracking_no)
            uow.orders.update(order)
            uow.events += order.events
            logger.info(f"order shipped: {order.id} track={tracking_no}")


# ---------------------------
# 이벤트 핸들러(알림/감사 로그 등)
# ---------------------------
def on_order_submitted(evt: OrderSubmitted):
    logger.info(f"[EH] Order submitted -> audit log write (order={evt.order_id})")

def on_payment_received(evt: PaymentReceived):
    logger.info(f"[EH] Payment received -> send receipt email (order={evt.order_id}, pay={evt.payment_id})")

def on_order_shipped(evt: OrderShipped):
    logger.info(f"[EH] Order shipped -> notify customer (order={evt.order_id}, tracking={evt.tracking_no})")

def on_order_canceled(evt: OrderCanceled):
    logger.info(f"[EH] Order canceled -> restock and notify (order={evt.order_id}, reason={evt.reason})")


# ---------------------------
# 부트스트랩 & 간단 시나리오 테스트
# ---------------------------
def bootstrap_services(
    pg: Optional[PaymentGateway] = None,
    inventory_policy: Optional[InventoryPolicy] = None,
) -> Tuple[OrderService, InMemoryUnitOfWork]:
    bus = EventBus()
    bus.subscribe(OrderSubmitted, on_order_submitted)
    bus.subscribe(PaymentReceived, on_payment_received)
    bus.subscribe(OrderShipped, on_order_shipped)
    bus.subscribe(OrderCanceled, on_order_canceled)

    repo_orders = InMemoryOrderRepository()
    repo_products = InMemoryProductRepository()
    repo_inventory = InMemoryInventoryRepository()
    repo_customers = InMemoryCustomerRepository()

    # 기본 데이터
    cust = Customer(id="CUST-001", email="user@example.com", joined_at=datetime.utcnow())
    repo_customers.add(cust)

    p1 = Product(SKU("SKU-APPLE"), "사과 1kg", Money(5500), "fruit")
    p2 = Product(SKU("SKU-BEEF"), "한우 등심 500g", Money(28900), "meat")
    p3 = Product(SKU("SKU-MILK"), "우유 1L", Money(2200), "dairy")

    for p in (p1, p2, p3):
        repo_products.add(p)

    repo_inventory.add(InventoryItem(p1.sku, 50))
    repo_inventory.add(InventoryItem(p2.sku, 10))
    repo_inventory.add(InventoryItem(p3.sku, 100))

    uow_factory = lambda: InMemoryUnitOfWork(
        orders=repo_orders,
        products=repo_products,
        inventory=repo_inventory,
        customers=repo_customers,
        event_bus=bus,
    )

    pricing = TieredPricing()  # 전략 교체 가능
    promos = CompositePromotion([
        MinAmountSpec(Money(30000), rate=0.05),
        FirstPurchaseSpec(Money(3000)),
        # 카테고리 번들 예시는 단순화되어 실제 카테고리 검증은 생략
    ])
    inv_policy = inventory_policy or StrictInventoryPolicy()
    gateway = pg or DummyPaymentGateway()

    svc = OrderService(uow_factory, pricing, promos, inv_policy, gateway)
    # UoW 인스턴스를 굳이 밖에서 보고 싶다면(테스트용)
    return svc, uow_factory()


# ---------------------------
# 데모 실행
# ---------------------------
if __name__ == "__main__":
    service, uow = bootstrap_services()

    # 1) 주문 생성
    order_id = service.create_order("CUST-001")

    # 2) 품목 추가(재고 선점 + 가격전략)
    service.add_item(order_id, "SKU-APPLE", qty=6)  # 5% 할인 구간 (전략 내부), Order는 정가로 합산 후 프로모션에서 총액할인
    service.add_item(order_id, "SKU-BEEF", qty=1)
    service.add_item(order_id, "SKU-MILK", qty=3)

    # 3) 프로모션 적용(최소금액 5% + 첫구매 3000원)
    service.apply_promotions(order_id)

    # 4) 주문 제출 → 이벤트 발생(OrderSubmitted)
    service.submit(order_id)

    # 5) 결제(멱등키로 재시도/중복방지)
    payment_id = service.checkout(order_id, idem_key="idem-123")
    # 같은 키로 또 호출해도 동일 payment_id 보장
    assert payment_id == service.checkout(order_id, idem_key="idem-123")

    # 6) 배송 처리 → 이벤트 발생(OrderShipped)
    service.ship(order_id, tracking_no="TRACK-9876543210")

    # 조회 헬퍼(데모)
    with uow:
        o = uow.orders.get(order_id)
        logger.info(f"== ORDER STATE ==")
        logger.info(f"status={o.status}, subtotal={o.subtotal.amount}, discount={o.discount_total.amount}, grand={o.grand_total.amount}")
        for ln in o.lines:
            logger.info(f" - {ln.sku.value} {ln.name} x{ln.qty} line_total={ln.line_total.amount}")
