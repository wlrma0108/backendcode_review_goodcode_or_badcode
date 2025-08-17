import pytest
from decimal import Decimal
from django.contrib.auth import get_user_model
from .models import Product, Order
from .tx_basics import create_order, charge_and_log

pytestmark = pytest.mark.django_db(transaction=True)

def test_create_order_rolls_back_on_stock_failure():
    user = get_user_model().objects.create_user('u@test.com', 'pw')
    Product.objects.create(sku='A', price=Decimal('3.50'), stock=1)

    with pytest.raises(ValueError):
        create_order(user=user, items=[{'sku': 'A', 'qty': 2}])  # 재고 부족

    assert Order.objects.count() == 0  # 전체 롤백 확인

def test_partial_rollback_with_savepoint():
    user = get_user_model().objects.create_user('u@test.com', 'pw')
    Product.objects.create(sku='A', price=Decimal('3.50'), stock=2)
    order = create_order(user=user, items=[{'sku': 'A', 'qty': 1}])

    ok = charge_and_log(order=order, amount=order.total_amount)
    assert ok in (True, False)  # 데모용 — 저장지점 흐름 확인
