from django.db.models import Prefetch
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Order, OrderItem
from .serializers import OrderSummarySerializer

# Two versions of the same endpoint so the N+1 and its fix can be compared in
# silk:
#   /api/orders/summary/naive/  - reproduces the bug
#   /api/orders/summary/        - the fix
# Both take ?customer_id=<id> to scope to one customer.


def _base_queryset(request):
    qs = Order.objects.all()
    customer_id = request.query_params.get("customer_id")
    if customer_id:
        qs = qs.filter(customer_id=customer_id)
    return qs


@api_view(["GET"])
def summary_naive(request):
    # Loads only orders, so each customer.name and each order.items.all() in the
    # serializer fires its own query -> 1 + 2N. 200 orders ~= 400+ queries,
    # which is the 30s timeout.
    orders = _base_queryset(request)
    data = OrderSummarySerializer(orders, many=True).data
    return Response({"count": len(data), "results": data})


@api_view(["GET"])
def order_summary(request):
    # select_related folds the to-one customer into the initial JOIN; prefetch
    # pulls all items in one IN (...) query. 2 queries regardless of order count.
    orders = (
        _base_queryset(request)
        .select_related("customer")
        .prefetch_related(
            Prefetch("items", queryset=OrderItem.objects.only(
                "id", "order_id", "product_name", "quantity", "unit_price_cents"
            ))
        )
    )
    data = OrderSummarySerializer(orders, many=True).data
    return Response({"count": len(data), "results": data})
