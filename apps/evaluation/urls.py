from django.urls import path

from .views import (
    AgreementView,
    AuditTrailView,
    BiasReportView,
    ExportReportView,
    InvocationDashboardView,
    RefreshBiasReportView,
    ReplayView,
    ThresholdView,
)

app_name = "evaluation"

urlpatterns = [
    path("biais/", BiasReportView.as_view(), name="bias_report"),
    path("biais/recalculer/", RefreshBiasReportView.as_view(), name="refresh_bias_report"),
    path("seuil/", ThresholdView.as_view(), name="threshold"),
    path("accord/", AgreementView.as_view(), name="agreement"),
    path("rapport.pdf", ExportReportView.as_view(), name="export_pdf"),
    path("appels/", InvocationDashboardView.as_view(), name="invocations"),
    path("rejeu/", ReplayView.as_view(), name="replay"),
    path("journal/", AuditTrailView.as_view(), name="audit_trail"),
]
