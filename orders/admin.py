from django.contrib import admin

from .models import Customer, Order, OrderItem


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "email", "created_at"]
    search_fields = ["name", "email"]


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ["id", "customer", "status", "created_at"]
    list_filter = ["status"]
    list_select_related = ["customer"]  # no N+1 on the changelist
    inlines = [OrderItemInline]


@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = ["id", "order", "product_name", "quantity", "unit_price_cents"]
    list_select_related = ["order"]
