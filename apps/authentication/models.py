from django.db import models
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.utils.translation import gettext_lazy as _

class Clinic(models.Model):
    name = models.CharField(max_length=255, unique=True)
    doctor_name = models.CharField(max_length=255)
    registration_number = models.CharField(max_length=100, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    phone = models.CharField(max_length=20)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

class UserManager(BaseUserManager):
    def create_user(self, mobile, password=None, **extra_fields):
        if not mobile:
            raise ValueError('The Mobile number must be set')
        user = self.model(mobile=mobile, **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, mobile, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('role', 'admin')

        return self.create_user(mobile, password, **extra_fields)

class User(AbstractUser):
    ROLE_CHOICES = (
        ('owner', 'Owner (Doctor)'),
        ('doctor', 'Doctor'),
        ('receptionist', 'Receptionist'),
        ('nurse', 'Nurse'),
        ('other', 'Other'),
        ('admin', 'Admin'),
        ('patient', 'Patient'),
    )

    ACCOUNT_STATUS_CHOICES = (
        ('active', 'Active'),
        ('pending', 'Pending'),
        ('inactive', 'Inactive'),
    )

    username = None # Disable username field
    mobile = models.CharField(_('mobile number'), max_length=15, unique=True)
    email = models.EmailField(_('email address'), unique=True, null=True, blank=True)

    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE, null=True, blank=True, related_name='users')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='doctor')
    custom_role = models.CharField(max_length=50, blank=True, default='', help_text="Custom role title when role is 'other'")
    registration_number = models.CharField(max_length=100, blank=True, null=True, help_text="For doctors only")

    # Access control
    can_view_financial = models.BooleanField(default=False)

    # Zoho Sync
    zoho_full_name = models.CharField(max_length=255, blank=True, null=True)

    # Profile Picture
    profile_picture = models.CharField(max_length=500, blank=True, null=True, help_text="S3 key for profile picture")

    # 2FA and OTP
    is_2fa_enabled = models.BooleanField(default=False)
    last_otp_sent_at = models.DateTimeField(null=True, blank=True)

    # Email verification
    is_email_verified = models.BooleanField(default=False)
    email_verification_code = models.CharField(max_length=6, null=True, blank=True, db_index=True)
    email_verification_sent_at = models.DateTimeField(null=True, blank=True)

    # Password reset
    password_reset_code = models.CharField(max_length=6, null=True, blank=True, db_index=True)
    password_reset_sent_at = models.DateTimeField(null=True, blank=True)

    # Staff invitations
    account_status = models.CharField(max_length=20, choices=ACCOUNT_STATUS_CHOICES, default='active', db_index=True)
    invitation_code = models.CharField(max_length=32, null=True, blank=True, unique=True, db_index=True)
    invitation_sent_at = models.DateTimeField(null=True, blank=True)

    # Rate limiting
    last_email_sent_at = models.DateTimeField(null=True, blank=True)

    # Patient-specific fields (nullable, only used when role='patient')
    zoho_contact_id = models.CharField(max_length=100, blank=True, null=True, db_index=True,
        help_text="Zoho Contact ID for patient users")
    profile_completed = models.BooleanField(default=False,
        help_text="Whether patient has completed initial profile setup")
    is_corporate = models.BooleanField(default=False)
    lead_source = models.CharField(max_length=50, blank=True, null=True)
    company_name = models.CharField(max_length=255, blank=True, null=True)
    gender = models.CharField(max_length=20, blank=True, null=True)
    age_in_years = models.PositiveIntegerField(blank=True, null=True)
    mailing_street = models.CharField(max_length=255, blank=True, null=True)
    mailing_city = models.CharField(max_length=100, blank=True, null=True)
    mailing_zip = models.CharField(max_length=20, blank=True, null=True)
    mailing_state = models.CharField(max_length=100, blank=True, null=True)
    primary_doctor_id = models.CharField(max_length=100, blank=True, null=True,
        help_text="Zoho Doctor ID for patient's primary doctor")
    doctor_email = models.EmailField(blank=True, null=True)
    doctor_mobile = models.CharField(max_length=20, blank=True, null=True)

    USERNAME_FIELD = 'mobile'
    REQUIRED_FIELDS = []

    objects = UserManager()

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.role})"

    def save(self, *args, **kwargs):
        # Owners and Doctors always have financial access
        if self.role in ['owner', 'doctor']:
            self.can_view_financial = True
        super().save(*args, **kwargs)

    def get_zoho_data(self):
        return {
            "Name": f"{self.first_name} {self.last_name}".strip(),
            "Mobile": self.mobile,
            "Email": self.email or '',
            "Registration_No": self.registration_number or '',
            "Clinic_Name": self.clinic.name if self.clinic else '',
        }

    def get_profile_picture_url(self, expiration=86400):
        """Generate presigned URL for profile picture (24 hour expiry by default)."""
        if not self.profile_picture:
            return None
        from apps.patients.s3_utils import generate_profile_picture_url
        return generate_profile_picture_url(self.profile_picture, expiration)
