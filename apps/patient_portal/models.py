"""
Patient Portal models - stub for future implementation.
These models define the schema for the patient-facing portal.
No views/serializers needed yet (Phase 2).
"""
from django.db import models
from django.conf import settings
import uuid


class UploadedDocument(models.Model):
    """Patient-uploaded medical document."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='uploaded_documents'
    )

    title = models.CharField(max_length=255)
    category = models.CharField(max_length=50, blank=True)
    s3_key = models.CharField(max_length=500)
    file_extension = models.CharField(max_length=10)
    file_size = models.BigIntegerField(default=0)

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
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='dependants'
    )

    full_name = models.CharField(max_length=255)
    relationship = models.CharField(max_length=50)
    age = models.IntegerField(null=True, blank=True)
    gender = models.CharField(max_length=10, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.full_name} ({self.relationship})"


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
