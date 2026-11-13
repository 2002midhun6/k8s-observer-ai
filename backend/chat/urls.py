from django.urls import path

from . import views

urlpatterns = [
    path('aws/status', views.aws_status),
    path('aws/connect', views.aws_connect),
    path('chat', views.chat_view),
]
