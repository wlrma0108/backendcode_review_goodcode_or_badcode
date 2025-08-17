from django.db import transaction, DatabaseError
from django.db.models import F
from .models import Product, Order

# 1) nowait 잠금: 이미 잠겨 있으면 즉시 실패
@transaction.atomic
def reserve_stock_nowait(*, sku: str, qty: int) -> bool:
    try:
        p = Product.objects.select_for_update(nowait=True).get(sku=sku)
    except DatabaseError:
        return False
    if p.stock < qty:
        return False
    Product.objects.filter(pk=p.pk).update(stock=F('stock') - qty)
    return True

# 2) 데드락 예방: 항상 같은 순서로 자원 잠그기
@transaction.atomic
def swap_two_products(p1_id: int, p2_id: int):
    a, b = sorted([p1_id, p2_id])  # 잠금 순서 고정
    pa = Product.objects.select_for_update().get(pk=a)
    pb = Product.objects.select_for_update().get(pk=b)
    # ... 상태 변경 로직

# 3) (PostgreSQL) skip_locked 스타일 배치 선점
def pick_next_batch(limit=100):
    with transaction.atomic():
        rows = (Order.objects
                .select_for_update(skip_locked=True)  # PG에서만 동작
                .filter(status='pending')
                .order_by('created_at')[:limit])
        for o in rows:
            o.status = 'processing'
            o.save(update_fields=['status'])
        return list(rows)
