from django.urls import path

from . import views

app_name = "tenants"

urlpatterns = [
    path("projects/", views.project_list, name="project-list"),
]
