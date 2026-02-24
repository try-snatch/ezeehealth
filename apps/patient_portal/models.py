"""
Patient Portal models.
"""
from django.db import models
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
import uuid


class UploadedDocument(models.Model):
    """Patient-uploaded medical document."""
    CATEGORY_CHOICES = (
        ('Doctor Consultation', 'Doctor Consultation'),
        ('Health Record', 'Health Record'),
        ('Lab Tests', 'Lab Tests'),
        ('Prescription', 'Prescription'),
        ('Imaging/Scan', 'Imaging/Scan'),
        ('Insurance', 'Insurance'),
        ('Others', 'Others'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='uploaded_documents'
    )
    dependant = models.ForeignKey(
        'Dependant', on_delete=models.CASCADE, null=True, blank=True,
        related_name='uploaded_documents'
    )

    title = models.CharField(max_length=255)
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default='Others')
    s3_key = models.CharField(max_length=500)
    file_extension = models.CharField(max_length=10)
    file_size = models.BigIntegerField(default=0)

    ai_readable = models.BooleanField(default=True)
    ai_processed = models.BooleanField(default=False)

    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f"{self.title} ({self.patient})"


class DocumentInsight(models.Model):
    """AI-generated insights for an uploaded document."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        UploadedDocument,
        on_delete=models.CASCADE,
        related_name='insights'
    )

    title = models.CharField(max_length=255)
    summary = models.TextField()
    key_findings = models.JSONField(default=list)
    risk_flags = models.JSONField(default=list)
    tags = models.JSONField(default=list)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Insight: {self.title}"


class Dependant(models.Model):
    """Patient's dependant (family member they manage documents for)."""
    RELATIONSHIP_CHOICES = (
        ('Father', 'Father'),
        ('Mother', 'Mother'),
        ('Sibling', 'Sibling'),
        ('Child', 'Child'),
        ('Spouse', 'Spouse'),
        ('Other', 'Other'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='dependants'
    )

    full_name = models.CharField(max_length=255)
    relationship = models.CharField(max_length=50, choices=RELATIONSHIP_CHOICES)
    age = models.IntegerField(null=True, blank=True)
    gender = models.CharField(max_length=20, blank=True)

    zoho_contact_id = models.CharField(max_length=100, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    mobile = models.CharField(max_length=20, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.full_name} ({self.relationship})"


class DocumentShare(models.Model):
    """Tracks which documents a patient has shared with their doctor."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(UploadedDocument, on_delete=models.CASCADE, related_name='shares')
    patient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    doctor_email = models.EmailField()
    doctor_mobile = models.CharField(max_length=20, blank=True)
    shared_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ['document', 'doctor_email']

    def __str__(self):
        return f"Share: {self.document.title} → {self.doctor_email}"


class ChatSession(models.Model):
    """AI chat session for a patient."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='patient_chat_sessions'
    )
    dependant = models.ForeignKey(
        Dependant, on_delete=models.CASCADE, null=True, blank=True
    )
    session_start = models.DateTimeField(auto_now_add=True)
    session_validity = models.DurationField(default=timedelta(days=1))

    def is_active(self):
        return timezone.now() < self.session_start + self.session_validity


class ChatMessage(models.Model):
    """Individual message in an AI chat session."""
    SENDER_CHOICES = (('user', 'User'), ('ai', 'AI'))

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name='messages')
    sender = models.CharField(max_length=10, choices=SENDER_CHOICES)
    message = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['timestamp']


class Alert(models.Model):
    """Patient notification/alert."""
    ALERT_TYPE_CHOICES = (
        ('Meeting', 'Meeting'),
        ('Report Generated', 'Report Generated'),
        ('General', 'General'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='patient_alerts'
    )
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    alert_type = models.CharField(max_length=50, choices=ALERT_TYPE_CHOICES, default='General')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Alert: {self.message[:50]}"


class PatientInvite(models.Model):
    """Invitation sent to a patient via email when they become a converted deal."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey('patients.Patient', on_delete=models.CASCADE, related_name='invites')
    invitation_code = models.CharField(max_length=32, unique=True, null=True, blank=True, db_index=True)
    phone = models.CharField(max_length=20)
    email = models.CharField(max_length=255, blank=True)
    name = models.CharField(max_length=255)
    clinic_name = models.CharField(max_length=255)
    is_used = models.BooleanField(default=False)
    sent_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    def __str__(self):
        return f"Invite for {self.name} ({self.phone})"
