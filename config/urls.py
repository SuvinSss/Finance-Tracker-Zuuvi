from django.conf import settings
from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.urls import include, path, re_path
from django.views.static import serve

from finance.views import OwnerLoginView, OwnerLogoutView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", OwnerLoginView.as_view(), name="login"),
    path("logout/", OwnerLogoutView.as_view(), name="logout"),
    # Receipts are private: only signed-in users can open uploaded files.
    re_path(
        rf"^{settings.MEDIA_URL.lstrip('/')}(?P<path>.*)$",
        login_required(serve),
        {"document_root": settings.MEDIA_ROOT},
    ),
    path("", include("finance.urls")),
]
