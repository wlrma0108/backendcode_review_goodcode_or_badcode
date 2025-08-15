# apps/shop/views_bad.py
import json
from django.http import JsonResponse
from .models import Product, Order, OrderItem

def create_order(request):
    data = json.loads(request.body)              
    items = data.get("items", [])               
    order = Order.objects.create(user=request.user, total_amount=0)

    total = 0.0                                  
    for it in items:
        prod = Product.objects.get(sku=it["sku"])  
        if prod.stock < it["quantity"]:            
            return JsonResponse({"error": "out of stock"}, status=400)
        prod.stock -= it["quantity"]
        prod.save()                                

        OrderItem.objects.create(
            order=order, product=prod,
            quantity=it["quantity"], unit_price=prod.price
        )
        total += float(prod.price) * it["quantity"]

    order.total_amount = total
    order.save()
    notify_webhook(order.id)                   
    return JsonResponse({"id": str(order.id), "total": total}, status=200)  


def list_orders(request):
    qs = Order.objects.filter(user=request.user).order_by("-created_at")  
    data = []
    for o in qs:                                       
        items = [{"sku": it.product.sku,               
                  "qty": it.quantity,
                  "price": str(it.unit_price)} for it in o.items.all()]
        data.append({"id": str(o.id),
                     "total": str(o.total_amount),
                     "items": items})
    return JsonResponse({"results": data})             