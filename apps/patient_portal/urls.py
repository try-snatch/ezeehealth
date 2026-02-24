from django.urls import path
from . import views

urlpatterns = [
    # Phase 1: Auth + Profile
    path('register/', views.PatientRegisterView.as_view(), name='patient-register'),
    path('me/', views.PatientMeView.as_view(), name='patient-me'),
    path('complete-profile/', views.CompleteProfileView.as_view(), name='patient-complete-profile'),
    path('update-profile/', views.UpdateProfileView.as_view(), name='patient-update-profile'),
    path('update-primary-doctor/', views.UpdatePrimaryDoctorView.as_view(), name='patient-update-doctor'),
    path('suggested-primary-doctor/', views.SuggestedPrimaryDoctorView.as_view(), name='patient-suggested-doctor'),
    path('corporate-doctors/', views.CorporateDoctorsView.as_view(), name='patient-corporate-doctors'),
    path('ezeehealth-doctors/', views.EzeeHealthDoctorsView.as_view(), name='patient-ezeehealth-doctors'),
    path('check-existing-user/', views.CheckExistingUserView.as_view(), name='patient-check-existing'),
    path('identify-user/', views.IdentifyUserView.as_view(), name='patient-identify-user'),

    # Phase 2: Documents
    path('documents/', views.DocumentListCreateView.as_view(), name='patient-documents'),
    path('documents/<uuid:pk>/', views.DocumentDetailView.as_view(), name='patient-document-detail'),
    path('documents/<uuid:pk>/metadata/', views.DocumentMetadataView.as_view(), name='patient-document-metadata'),
    path('documents/<uuid:pk>/insights/', views.DocumentInsightsView.as_view(), name='patient-document-insights'),
    path('documents/<uuid:pk>/ai-toggle/', views.AIToggleView.as_view(), name='patient-document-ai-toggle'),
    path('critical-insights/', views.CriticalInsightsView.as_view(), name='patient-critical-insights'),
    path('doctor-documents/', views.DoctorDocumentsView.as_view(), name='patient-doctor-documents'),

    # Phase 3: Document Sharing
    path('documents/<uuid:pk>/share/', views.ShareDocumentView.as_view(), name='patient-share-document'),
    path('documents/<uuid:pk>/share/revoke/', views.RevokeShareView.as_view(), name='patient-revoke-share'),
    path('documents/<uuid:pk>/share/status/', views.ShareStatusView.as_view(), name='patient-share-status'),
    path('documents/shared/', views.SharedDocumentsListView.as_view(), name='patient-shared-documents'),

    # Phase 4: Dashboard + Journeys
    path('dashboard/', views.PatientDashboardView.as_view(), name='patient-dashboard'),
    path('journeys/', views.JourneyListView.as_view(), name='patient-journeys'),
    path('journeys/<str:deal_id>/', views.JourneyDetailView.as_view(), name='patient-journey-detail'),
    path('ssh-details/', views.SSHDetailsView.as_view(), name='patient-ssh-details'),

    # Phase 5: Dependants
    path('dependants/', views.DependantListCreateView.as_view(), name='patient-dependants'),

    # Phase 6: AI Chat
    path('ai-chat/', views.AIChatView.as_view(), name='patient-ai-chat'),
    path('ai-chat/history/', views.ChatHistoryView.as_view(), name='patient-chat-history'),
    path('speech-to-text/', views.SpeechToTextView.as_view(), name='patient-speech-to-text'),

    # Phase 7: Alerts + Meetings
    path('alerts/', views.AlertsListView.as_view(), name='patient-alerts'),
    path('alerts/unread-count/', views.UnreadAlertCountView.as_view(), name='patient-unread-alerts'),
    path('alerts/mark-read/', views.MarkAlertsReadView.as_view(), name='patient-mark-alerts-read'),
    path('meetings/', views.MeetingsView.as_view(), name='patient-meetings'),
]
