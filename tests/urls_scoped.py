from django.contrib import admin
from django.contrib.auth.views import LoginView
from django.urls import path


urlpatterns = [
    path("admin/", admin.site.urls),
    path(
        "employee-login/",
        LoginView.as_view(template_name="admin/login.html"),
        name="employee-login",
    ),
    path(
        "customer-login/",
        LoginView.as_view(template_name="admin/login.html"),
        name="customer-login",
    ),
]
