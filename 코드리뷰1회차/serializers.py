from rest_framework import serializers

class OrderItemIn(serializers.Serializer):
    sku = serializers.CharField()
    quantity = serializers.IntegerField(min_value=1)

class OrderCreateIn(serializers.Serializer):
    items = OrderItemIn(many=True)

    def validate_items(self, items):
        if not items:
            raise serializers.ValidationError("At least one item is required.")
        return items
