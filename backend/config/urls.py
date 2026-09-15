from django.urls import path

from backend.api.api import api

urlpatterns = [path("api/", api.urls)]
