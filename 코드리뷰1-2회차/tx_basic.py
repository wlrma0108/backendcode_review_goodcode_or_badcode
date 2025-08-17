from decimal import Decimal
from django.db import transaction, IntegrityError
from django.db.models import F
from .models import Product, Order, OrderItem, Ledger

# 1) 기본: atomic 블록 하나로 '모두 성공/모두 실패'
@transaction.atomic
def create_order(*, user, items: list[dict]) -> Order:
    """items = [{'sku': 'A', 'qty': 2}, ...]"""
    # 필요한 상품을 한 번에 잠그고(select_for_update) 가져오기 → 동시성/초과판매 방지
    skus = [i['sku'] for i in items]
    products = {p.sku: p for p in Product.objects.select_for_update().filter(sku__in=skus)}

    order = Order.objects.create(user=user, total_amount=Decimal('0.00'), status='pending')
    total = Decimal('0.00')
    bulk_items = []

    for it in items:
        p = products[it['sku']]
        q = int(it['qty'])
        if p.stock < q:
            raise ValueError(f"Out of stock: {p.sku}")
        # F-표현식 → 원자적 차감
        Product.objects.filter(pk=p.pk).update(stock=F('stock') - q)
        bulk_items.append(OrderItem(order=order, product=p, quantity=q, unit_price=p.price))
        total += p.price * q

    OrderItem.objects.bulk_create(bulk_items)
    order.total_amount = total
    order.save(update_fields=['total_amount'])

    # 커밋 이후 후처리(웹훅/브로커 발행 등)는 on_commit에 넣기
    transaction.on_commit(lambda: emit_order_created(order))
    return order

def emit_order_created(order: Order):
    # TODO: Celery 등으로 비동기 발행
    pass


# 2) 중첩 트랜잭션(저장지점)으로 부분 롤백
@transaction.atomic
def charge_and_log(*, order: Order, amount: Decimal) -> bool:
    try:
        # 실패 가능성이 높은 블록을 저장지점으로 감싸기
        with transaction.atomic():
            Ledger.objects.create(order=order, kind='charge', amount=amount)
            # 외부 결제 로그 동기화 등...
        # 저장지점 성공 → 바깥 atomic과 함께 커밋
        order.status = 'paid'
        order.save(update_fields=['status'])
        transaction.on_commit(lambda: emit_paid(order))
        return True
    except Exception:
        # 저장지점 내부만 롤백되고, 외부 atomic은 유지
        return False

def emit_paid(order: Order):
    pass


# 3) 수동 저장지점 API 예시
@transaction.atomic
def process_with_manual_savepoint(order: Order):
    sid = transaction.savepoint()
    try:
        Ledger.objects.create(order=order, kind='charge', amount=order.total_amount)
    except IntegrityError:
        transaction.savepoint_rollback(sid)
    else:
        transaction.savepoint_commit(sid)
    # 이후 추가 처리 ...
