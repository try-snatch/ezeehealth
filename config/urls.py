from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import TemplateView
from rest_framework_simplejwt.views import TokenRefreshView

from apps.authentication.views import (
    RegisterView, LoginView, VerifyOTPView, MeView,
    VerifyEmailView, ResendEmailVerificationView, ForgotPasswordView,
    ResetPasswordView, VerifyInvitationView, StaffSetupAccountView,
    PatientVerifyInviteView, PatientSetupAccountView,
)
from apps.patients.views import (
    PatientListCreateView, PatientDetailView, DashboardStatsView,
    OPDPatientRegistrationView, CreateReferralView,
    list_shared_documents, get_document_details, list_patients_with_shared_documents,
    PatientDocumentListUploadView, PatientDocumentDetailView,
    PatientDocumentInsightView, ZohoWebhookView, SendPatientInviteView,
    GenerateDocumentUploadLinkView, VerifyDocumentUploadTokenView, DocumentUploadViaTokenView,
)
from apps.staff.views import StaffListView, StaffDetailView

urlpatterns = [
    path('admin/', admin.site.urls),

    # Staff Setup Page (served as static HTML)
    path('staff/setup/<str:invitation_code>/',
         TemplateView.as_view(template_name='staff_setup/index.html'),
         name='staff-setup-page'),

    # Patient Setup Page (served as static HTML)
    path('patient/setup/<str:invitation_code>/',
         TemplateView.as_view(template_name='patient_setup/index.html'),
         name='patient-setup-page'),

    # Document Upload Page (served as static HTML)
    path('document-upload/<str:token>/',
         TemplateView.as_view(template_name='document_upload/index.html'),
         name='document-upload-page'),

    # Auth
    path('api/auth/register/', RegisterView.as_view(), name='register'),
    path('api/auth/request-otp/', LoginView.as_view(), name='login'),
    path('api/auth/verify-otp/', VerifyOTPView.as_view(), name='verify-otp'),
    path('api/auth/me/', MeView.as_view(), name='me'),
    path('api/auth/verify-email/', VerifyEmailView.as_view(), name='verify-email'),
    path('api/auth/resend-email-verification/', ResendEmailVerificationView.as_view(), name='resend-email-verification'),
    path('api/auth/forgot-password/', ForgotPasswordView.as_view(), name='forgot-password'),
    path('api/auth/reset-password/', ResetPasswordView.as_view(), name='reset-password'),
    path('api/auth/verify-invitation/', VerifyInvitationView.as_view(), name='verify-invitation'),
    path('api/auth/staff/setup-account/', StaffSetupAccountView.as_view(), name='staff-setup-account'),
    path('api/auth/patient/verify-invite/', PatientVerifyInviteView.as_view(), name='patient-verify-invite'),
    path('api/auth/patient/setup-account/', PatientSetupAccountView.as_view(), name='patient-setup-account'),
    path('api/auth/token/refresh/', TokenRefreshView.as_view(), name='token-refresh'),

    # Patients
    path('api/patients/', PatientListCreateView.as_view(), name='patients-list'),
    path('api/patients/register-opd/', OPDPatientRegistrationView.as_view(), name='register-opd'),
    path('api/patients/<int:pk>/', PatientDetailView.as_view(), name='patients-detail'),
    path('api/patients/<int:pk>/create-referral/', CreateReferralView.as_view(), name='create-referral'),
    path('api/patients/<int:pk>/documents/', PatientDocumentListUploadView.as_view(), name='patient-documents'),
    path('api/patients/<int:pk>/documents/<uuid:doc_id>/', PatientDocumentDetailView.as_view(), name='patient-document-detail'),
    path('api/patients/<int:pk>/documents/<uuid:doc_id>/insights/', PatientDocumentInsightView.as_view(), name='patient-document-insights'),
    path('api/patients/<int:pk>/send-invite/', SendPatientInviteView.as_view(), name='send-patient-invite'),
    path('api/patients/<int:pk>/document-upload-link/', GenerateDocumentUploadLinkView.as_view(), name='generate-document-upload-link'),
    path('api/document-upload/verify/<str:token>/', VerifyDocumentUploadTokenView.as_view(), name='verify-document-upload-token'),
    path('api/document-upload/<str:token>/', DocumentUploadViaTokenView.as_view(), name='document-upload-via-token'),

    # Dashboard (support both old and new URL)
    path('api/dashboard/', DashboardStatsView.as_view(), name='dashboard'),
    path('api/doctor/dashboard/', DashboardStatsView.as_view(), name='doctor-dashboard'),

    # Staff
    path('api/staff/', StaffListView.as_view(), name='staff-list'),
    path('api/staff/<int:pk>/', StaffDetailView.as_view(), name='staff-detail'),

    # Zoho Webhook (both URL patterns for compatibility)
    path('api/webhooks/zoho/', ZohoWebhookView.as_view(), name='zoho-webhook'),
    path('api/zoho/webhooks/', ZohoWebhookView.as_view(), name='zoho-webhook-alt'),

    # Patient Portal
    path('api/patient/', include('apps.patient_portal.urls')),

    # Shared Patient Documents
    path('api/patient-documents/', list_shared_documents, name='list-shared-documents'),
    path('api/patient-documents/<uuid:document_id>/', get_document_details, name='document-details'),
    path('api/patient-documents/patients/', list_patients_with_shared_documents, name='patients-with-documents'),
]

# Note: WhiteNoise handles static files automatically in production
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
