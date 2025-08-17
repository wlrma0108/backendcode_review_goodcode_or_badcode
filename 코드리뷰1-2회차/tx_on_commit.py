from django.db import transaction
from .models import OutboxEvent, Order

def create_outbox_for_order_created(order: Order):
    # 1) 트랜잭션 안에서 Outbox 레코드 생성
    OutboxEvent.objects.create(
        aggregate_type='Order',
        aggregate_id=str(order.id),
        event_type='OrderCreated',
        payload={'order_id': str(order.id)},
        status='pending',
    )
    # 2) 커밋 이후 디스패처 기동(브로커 전송 등)
    transaction.on_commit(lambda: schedule_outbox_dispatch())

def schedule_outbox_dispatch():
    # TODO: Celery/커맨드에서 OutboxEvent를 읽어 전송
    pass
