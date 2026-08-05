from django.urls import path

from . import views

app_name = "netbox_export"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
]

