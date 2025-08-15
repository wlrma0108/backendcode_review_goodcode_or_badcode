# apps/shop/views_bad.py
import json
from django.http import JsonResponse
from .models import Product, Order, OrderItem

def create_order(request):
    data = json.loads(request.body)              # ❌ 스키마/타입 검증 없음
    items = data.get("items", [])                # [{"sku":"A","quantity":2}, ...]
    order = Order.objects.create(user=request.user, total_amount=0)

    total = 0.0                                  # ❌ float 사용 → 정밀도 손실
    for it in items:
        prod = Product.objects.get(sku=it["sku"])   # ❌ 루프 안 개별 조회 → N+1
        if prod.stock < it["quantity"]:             # 경쟁조건 未방어
            return JsonResponse({"error": "out of stock"}, status=400)
        prod.stock -= it["quantity"]
        prod.save()                                 # ❌ 줄마다 저장, 트랜잭션 경계 없음

        OrderItem.objects.create(
            order=order, product=prod,
            quantity=it["quantity"], unit_price=prod.price
        )
        total += float(prod.price) * it["quantity"]

    order.total_amount = total
    order.save()
    notify_webhook(order.id)                    # ❌ 커밋 보장 전 외부 호출
    return JsonResponse({"id": str(order.id), "total": total}, status=200)  # ❌ 201 아님
