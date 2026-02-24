from django.db import models
from apps.authentication.models import Clinic
from django.conf import settings
from django.utils import timezone
import uuid
import secrets

class Patient(models.Model):
    GENDER_CHOICES = (
        ('male', 'Male'),
        ('female', 'Female'),
        ('other', 'Other'),
    )

    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE, related_name='patients')

    # Core identity data
    full_name = models.CharField(max_length=255)
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES)
    age = models.IntegerField(null=True, blank=True)
    phone = models.CharField(max_length=20, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    diagnosis = models.TextField(blank=True, null=True)

    # OPD status (referral status lives on the Referral model)
    status = models.CharField(max_length=20, default='opd')

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    status_updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.full_name} (OPD)"


class Referral(models.Model):
    """Each time a patient is referred to a specialist/hospital — a separate episode."""
    STATUS_CHOICES = (
        ('referred', 'Referred'),
        ('converted', 'Converted'),
        ('admitted', 'Admitted'),
        ('ongoing', 'Ongoing Treatment'),
        ('discharged', 'Discharged'),
        ('billing_received', 'Billing Received'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name='referrals')
    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE)

    # Zoho Sync (per-referral)
    zoho_lead_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    zoho_deal_id = models.CharField(max_length=100, blank=True, null=True)
    zoho_contact_id = models.CharField(max_length=100, blank=True, null=True)

    # Referral details
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='referred')
    diagnosis = models.TextField(blank=True, null=True)
    suggested_specialty = models.CharField(max_length=255, blank=True, null=True)
    suggested_sshs = models.CharField(max_length=255, blank=True, null=True,
                                      help_text="Suggested Super Specialty Hospitals")
    revenue = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)

    # Timestamps
    referred_date = models.DateTimeField(auto_now_add=True)
    converted_date = models.DateTimeField(null=True, blank=True)
    status_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-referred_date']

    def __str__(self):
        return f"Referral for {self.patient.full_name} ({self.status})"


class SharedPatientDocument(models.Model):
    """
    Documents shared from the patient app.
    Patients can share their medical documents with their primary doctor,
    and they appear here for doctors to view.
    """
    id = models.UUIDField(primary_key=True, editable=False)

    # Patient information
    patient_zoho_id = models.CharField(max_length=255, db_index=True)
    patient_name = models.CharField(max_length=255)
    patient_email = models.EmailField()
    patient_phone = models.CharField(max_length=20)

    # Document metadata
    title = models.CharField(max_length=255)
    category = models.CharField(max_length=50)
    uploaded_at = models.DateTimeField()

    # S3 storage info
    s3_key = models.CharField(max_length=500)
    file_extension = models.CharField(max_length=10)
    file_size = models.BigIntegerField(default=0)

    # Access control
    doctor_email = models.EmailField(db_index=True)
    doctor_mobile = models.CharField(max_length=20)
    shared_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True, db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'shared_patient_documents'
        indexes = [
            models.Index(fields=['doctor_email', 'is_active']),
            models.Index(fields=['patient_zoho_id', 'is_active']),
        ]
        verbose_name = 'Shared Patient Document'
        verbose_name_plural = 'Shared Patient Documents'

    def __str__(self):
        return f"{self.patient_name} - {self.title}"

class SharedDocumentInsight(models.Model):
    """
    AI-generated insights for shared patient documents.
    These are generated in the patient app and synchronized here.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shared_document = models.ForeignKey(
        SharedPatientDocument,
        on_delete=models.CASCADE,
        related_name='insights',
        to_field='id'
    )

    # AI insights data
    title = models.CharField(max_length=255)
    summary = models.TextField()
    key_findings = models.JSONField(default=list)
    risk_flags = models.JSONField(default=list)
    tags = models.JSONField(default=list)

    created_at = models.DateTimeField()

    class Meta:
        db_table = 'shared_document_insights'
        indexes = [
            models.Index(fields=['shared_document']),
        ]
        verbose_name = 'Shared Document Insight'
        verbose_name_plural = 'Shared Document Insights'

    def __str__(self):
        return f"Insights: {self.title}"


class PatientDocument(models.Model):
    CATEGORY_CHOICES = (
        ('Doctor Consultation', 'Doctor Consultation'),
        ('Lab Tests', 'Lab Tests'),
        ('Prescription', 'Prescription'),
        ('Imaging/Scan', 'Imaging/Scan'),
        ('Health Record', 'Health Record'),
        ('Others', 'Others'),
    )
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name='documents')
    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    s3_key = models.CharField(max_length=500)
    title = models.CharField(max_length=255, blank=True)
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default='Others')
    file_extension = models.CharField(max_length=10)
    file_size = models.BigIntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    ai_processed = models.BooleanField(default=False)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f"{self.patient.full_name} - {self.title or self.s3_key}"


class PatientDocumentInsight(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.OneToOneField(PatientDocument, on_delete=models.CASCADE, related_name='insight')
    title = models.CharField(max_length=255)
    summary = models.TextField()
    key_findings = models.JSONField(default=list)
    risk_flags = models.JSONField(default=list)
    tags = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Insight: {self.title}"


class DocumentUploadLink(models.Model):
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name='upload_links')
    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    token = models.CharField(max_length=32, unique=True, db_index=True)
    is_used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    @property
    def is_expired(self):
        return timezone.now() > self.expires_at

    @property
    def is_valid(self):
        return not self.is_expired and not self.is_used

    @staticmethod
    def generate_token():
        return secrets.token_urlsafe(24)[:32]

    def __str__(self):
        return f"Upload link for {self.patient.full_name} ({self.token[:8]}...)"
