from django.urls import path

from .views import CVDocumentListView, CVUploadView

app_name = "parsing"

urlpatterns = [
    path("deposer/", CVUploadView.as_view(), name="upload"),
    path("documents/", CVDocumentListView.as_view(), name="documents"),
]
