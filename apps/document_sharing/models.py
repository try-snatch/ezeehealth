"""
Document Sharing models - stub for future unified sharing.
For now, the SharedPatientDocument model in the patients app handles this.
This app will later provide a unified document sharing mechanism.
"""
from django.db import models
from django.conf import settings
import uuid


class SharedDocument(models.Model):
    """
    Unified document sharing model (future use).
    Will replace SharedPatientDocument when both patient portal
    and doctor app share documents through a single model.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Who shared it
    shared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='shared_documents'
    )

    # Who it's shared with
    shared_with_email = models.EmailField(db_index=True)
    shared_with_mobile = models.CharField(max_length=20, blank=True)

    # Document reference
    document_title = models.CharField(max_length=255)
    s3_key = models.CharField(max_length=500)
    file_extension = models.CharField(max_length=10)
    file_size = models.BigIntegerField(default=0)

    # Access control
    shared_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True, db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-shared_at']
        indexes = [
            models.Index(fields=['shared_with_email', 'is_active']),
        ]

    def __str__(self):
        return f"{self.document_title} -> {self.shared_with_email}"
