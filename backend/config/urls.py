from django.urls import path

from backend.api import api

urlpatterns = [path("api/", api.urls)]
