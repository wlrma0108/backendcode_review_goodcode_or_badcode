import uuid
from decimal import Decimal
from django.db import models

class Product(models.Model):
    sku = models.CharField(max_length=50, unique=True)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    stock = models.PositiveIntegerField(default=0)

class Order(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey("users.User", on_delete=models.PROTECT)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    status = models.CharField(max_length=20, default="pending")  # pending/paid/canceled
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)

class Ledger(models.Model):
    """간단한 회계 원장: 주문별 금액 기록(예: 결제/환불 로그)"""
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="ledgers")
    kind = models.CharField(max_length=20)  # charge/refund
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

class OutboxEvent(models.Model):
    """트랜잭셔널 아웃박스: 커밋과 함께 기록 후 워커가 전송"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    aggregate_type = models.CharField(max_length=50)   # 'Order'
    aggregate_id = models.CharField(max_length=64)     # order.id
    event_type = models.CharField(max_length=50)       # 'OrderCreated'
    payload = models.JSONField()
    status = models.CharField(max_length=20, default="pending")  # pending/sending/sent/failed
    attempts = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
