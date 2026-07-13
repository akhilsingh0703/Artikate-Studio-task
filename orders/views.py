"""Section 1 — order summary endpoint.

Two implementations of the same endpoint are exposed so the N+1 regression and
its fix can be profiled side by side with django-silk:

* ``summary_naive``  -> /api/orders/summary/naive/   (reproduces the bug)
* ``order_summary``  -> /api/orders/summary/         (the fixed version)

Both accept ``?customer_id=<id>`` to scope the summary to a single customer,
mirroring the mobile dashboard call described in the scenario.
"""

from django.db.models import Prefetch
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Order, OrderItem
from .serializers import OrderSummarySerializer


def _base_queryset(request):
    qs = Order.objects.all()
    customer_id = request.query_params.get("customer_id")
    if customer_id:
        qs = qs.filter(customer_id=customer_id)
    return qs


@api_view(["GET"])
def summary_naive(request):
    """Deliberately slow version.

    ``Order.objects.all()`` loads only the orders. Every ``customer.name`` in
    the serializer triggers one extra SELECT on ``orders_customer`` and every
    ``order.items.all()`` triggers one extra SELECT on ``orders_orderitem``.
    For N orders that is 1 + 2N queries, so 200 orders => ~401 queries. That is
    the 30s timeout in the incident.
    """
    orders = _base_queryset(request)
    data = OrderSummarySerializer(orders, many=True).data
    return Response({"count": len(data), "results": data})


@api_view(["GET"])
def order_summary(request):
    """Fixed version — constant query count regardless of order volume.

    * ``select_related("customer")`` turns the per-row customer lookup into a
      single SQL JOIN (resolved in the initial query, no extra round trips).
    * ``prefetch_related`` batches every related item into ONE ``IN (...)``
      query, then joins them in Python.

    Total: 2 queries (orders + items) whether there are 5 orders or 5,000.
    """
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
