from django.urls import path

from .views import (
    BiasReportView,
    ExportReportView,
    InvocationDashboardView,
    RefreshBiasReportView,
    ThresholdView,
)

app_name = "evaluation"

urlpatterns = [
    path("biais/", BiasReportView.as_view(), name="bias_report"),
    path("biais/recalculer/", RefreshBiasReportView.as_view(), name="refresh_bias_report"),
    path("seuil/", ThresholdView.as_view(), name="threshold"),
    path("rapport.pdf", ExportReportView.as_view(), name="export_pdf"),
    path("appels/", InvocationDashboardView.as_view(), name="invocations"),
]
