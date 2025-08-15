import hashlib, json
from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .serializers import OrderCreateIn
from .services import create_order
from .models_idem import IdempotencyKey
from .models import Order

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_order_view(request):
    ser = OrderCreateIn(data=request.data)
    ser.is_valid(raise_exception=True)

    idem = request.headers.get("Idempotency-Key")
    body_hash = hashlib.sha256(json.dumps(ser.validated_data, sort_keys=True).encode()).hexdigest()

    if idem:
        with transaction.atomic():
            rec, created = IdempotencyKey.objects.select_for_update().get_or_create(
                key=idem, user=request.user,
                defaults={"request_hash": body_hash, "status_code": 0, "response_body": {}},
            )
            if not created and rec.request_hash == body_hash and rec.status_code:
                return Response(rec.response_body, status=rec.status_code)

            order = create_order(user=request.user, items=ser.validated_data["items"])
            payload = {"id": str(order.id), "total": str(order.total_amount)}
            rec.request_hash, rec.response_body, rec.status_code = body_hash, payload, status.HTTP_201_CREATED
            rec.save(update_fields=["request_hash", "response_body", "status_code"])
    else:
        order = create_order(user=request.user, items=ser.validated_data["items"])
        payload = {"id": str(order.id), "total": str(order.total_amount)}

    headers = {"Location": f"/api/orders/{order.id}"}
    return Response(payload, status=status.HTTP_201_CREATED, headers=headers)
