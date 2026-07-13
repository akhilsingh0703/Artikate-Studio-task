from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("silk/", include("silk.urls", namespace="silk")),
    path("api/orders/", include("orders.urls")),
    path("api/tenants/", include("tenants.urls")),
]
