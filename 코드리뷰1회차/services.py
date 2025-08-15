from decimal import Decimal
from django.db import transaction
from django.db.models import F
from .models import Product, Order, OrderItem

@transaction.atomic
def create_order(*, user, items: list[dict]) -> Order:
    # 일괄 조회 + 행 잠금으로 동시성/경쟁조건 방어
    skus = [i["sku"] for i in items]
    by_sku = {p.sku: p for p in Product.objects.select_for_update().filter(sku__in=skus)}

    total = Decimal("0.00")
    order = Order.objects.create(user=user, total_amount=Decimal("0.00"))

    bulk_items = []
    for it in items:
        p = by_sku.get(it["sku"])
        if not p:
            raise ValueError(f"Unknown SKU: {it['sku']}")
        q = int(it["quantity"])
        if p.stock < q:
            raise ValueError(f"Out of stock: {p.sku}")
        Product.objects.filter(pk=p.pk).update(stock=F("stock") - q)
        bulk_items.append(OrderItem(order=order, product=p, quantity=q, unit_price=p.price))
        total += p.price * q

    OrderItem.objects.bulk_create(bulk_items)
    order.total_amount = total
    order.save(update_fields=["total_amount"])

    transaction.on_commit(lambda: publish_order_created(order.id))
    return order

def publish_order_created(order_id: str):
    # 실제로는 Celery 등으로 발행
    pass
