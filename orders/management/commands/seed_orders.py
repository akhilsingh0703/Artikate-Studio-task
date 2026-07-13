import random

from django.core.management.base import BaseCommand
from django.db import transaction

from orders.models import Customer, Order, OrderItem

PRODUCTS = ["Poster Print", "Canvas", "Sticker Pack", "Art Book", "Frame", "Mug"]


class Command(BaseCommand):
    help = "Create one customer with lots of orders/items to show the N+1 fix."

    def add_arguments(self, parser):
        parser.add_argument("--orders", type=int, default=250)  # above the 200 threshold
        parser.add_argument("--items", type=int, default=3)

    @transaction.atomic
    def handle(self, *args, **options):
        n_orders = options["orders"]
        n_items = options["items"]

        customer = Customer.objects.create(
            name="Heavy Buyer",
            email=f"heavy-{random.randint(1000, 9999)}@artikate.test",
        )

        orders = Order.objects.bulk_create(
            [
                Order(customer=customer, status=random.choice(Order.Status.values))
                for _ in range(n_orders)
            ]
        )

        items = []
        for order in orders:
            for _ in range(n_items):
                items.append(
                    OrderItem(
                        order=order,
                        product_name=random.choice(PRODUCTS),
                        quantity=random.randint(1, 5),
                        unit_price_cents=random.randint(500, 5000),
                    )
                )
        OrderItem.objects.bulk_create(items)

        self.stdout.write(
            self.style.SUCCESS(
                f"Created customer id={customer.id} with {n_orders} orders "
                f"and {len(items)} items.\n"
                f"Compare /api/orders/summary/naive/?customer_id={customer.id} "
                f"and /api/orders/summary/?customer_id={customer.id} at /silk/"
            )
        )
