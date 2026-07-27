from django.urls import path

from .views import BiasReportView, RefreshBiasReportView

app_name = "evaluation"

urlpatterns = [
    path("biais/", BiasReportView.as_view(), name="bias_report"),
    path("biais/recalculer/", RefreshBiasReportView.as_view(), name="refresh_bias_report"),
]
