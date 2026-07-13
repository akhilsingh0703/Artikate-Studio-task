from django.urls import path

from . import views

app_name = "orders"

urlpatterns = [
    path("summary/", views.order_summary, name="summary"),
    path("summary/naive/", views.summary_naive, name="summary-naive"),
]
