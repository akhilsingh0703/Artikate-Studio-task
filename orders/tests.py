from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.urls import reverse

from .models import Customer, Order, OrderItem


# Strip middleware so the query counter measures the VIEW's queries only, not
# silk's profiler writes, the tenant lookup, or session/auth queries.
@override_settings(MIDDLEWARE=[])
class OrderSummaryQueryCountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = Customer.objects.create(
            name="Heavy Buyer", email="heavy@artikate.test"
        )
        orders = Order.objects.bulk_create(
            [Order(customer=cls.customer) for _ in range(50)]
        )
        OrderItem.objects.bulk_create(
            [
                OrderItem(order=o, product_name="Poster", quantity=2, unit_price_cents=1000)
                for o in orders
                for _ in range(3)
            ]
        )

    def _count_queries(self, url):
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 50)
        return len(ctx.captured_queries)

    def test_naive_endpoint_has_n_plus_one(self):
        url = reverse("orders:summary-naive") + f"?customer_id={self.customer.id}"
        n = self._count_queries(url)
        # 1 (orders) + 2 per order (customer + items) ~= 100+ queries.
        self.assertGreater(n, 50)

    def test_fixed_endpoint_is_constant(self):
        url = reverse("orders:summary") + f"?customer_id={self.customer.id}"
        n = self._count_queries(url)
        # orders + prefetched items (+ a couple of framework/session queries).
        self.assertLessEqual(n, 6)

    def test_fixed_uses_far_fewer_queries_than_naive(self):
        naive_url = reverse("orders:summary-naive") + f"?customer_id={self.customer.id}"
        fixed_url = reverse("orders:summary") + f"?customer_id={self.customer.id}"
        self.assertLess(self._count_queries(fixed_url), self._count_queries(naive_url))

    def test_summary_totals_are_correct(self):
        url = reverse("orders:summary") + f"?customer_id={self.customer.id}"
        row = self.client.get(url).json()["results"][0]
        # 3 items x (2 * 1000 cents)
        self.assertEqual(row["item_count"], 3)
        self.assertEqual(row["total_cents"], 6000)
