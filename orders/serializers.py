from rest_framework import serializers

from .models import Customer, Order, OrderItem


class OrderItemSerializer(serializers.ModelSerializer):
    line_total_cents = serializers.IntegerField(read_only=True)

    class Meta:
        model = OrderItem
        fields = ["id", "product_name", "quantity", "unit_price_cents", "line_total_cents"]


class OrderSummarySerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    item_count = serializers.SerializerMethodField()
    total_cents = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ["id", "status", "created_at", "customer_name", "item_count", "total_cents"]

    def get_item_count(self, order):
        # Relies on ``order.items`` already being prefetched by the view. Calling
        # .count() here would re-issue a COUNT query per row (another N+1).
        return len(order.items.all())

    def get_total_cents(self, order):
        return sum(item.line_total_cents for item in order.items.all())


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = ["id", "name", "email"]
