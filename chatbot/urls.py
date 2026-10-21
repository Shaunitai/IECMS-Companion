"""URL routes for the chatbot app."""

from __future__ import annotations

from django.urls import path

from . import views

app_name = "chatbot"

urlpatterns = [
    path("", views.index, name="index"),
    path("sources/<slug:source_id>/", views.source_document, name="source_document"),
    path("api/chat/", views.chat, name="chat"),
    path("api/summarise/", views.summarise_document, name="summarise"),
]
