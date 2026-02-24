from rest_framework import serializers
from .models import User, Clinic
from .utils import generate_otp, send_auth_otp
from .email_utils import generate_invitation_code, send_staff_invitation_email
from django.core.cache import cache
from django.utils import timezone


class ClinicSerializer(serializers.ModelSerializer):
    class Meta:
        model = Clinic
        fields = ['id', 'name', 'doctor_name', 'registration_number', 'phone', 'email']
        extra_kwargs = {
            'name': {'required': False, 'validators': []}, # Bypass automatic uniqueness check
            'doctor_name': {'required': False},
            'phone': {'required': False},
        }

class UserSerializer(serializers.ModelSerializer):
    clinic = ClinicSerializer(required=False)
    doctor_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'mobile', 'doctor_name', 'email', 'role', 'clinic', 'can_view_financial', 'registration_number', 'profile_picture']
        read_only_fields = ['id', 'mobile', 'role', 'can_view_financial', 'profile_picture']

    def get_doctor_name(self, obj):
        return f"{obj.first_name} {obj.last_name}".strip()

    def update(self, instance, validated_data):
        clinic_data = validated_data.pop('clinic', None)
        doctor_name = self.initial_data.get('doctor_name') # doctor_name is not in validated_data since it's a SerializerMethodField or write_only manually

        # Update User fields
        if doctor_name:
            name_parts = doctor_name.strip().split(' ', 1)
            instance.first_name = name_parts[0]
            instance.last_name = name_parts[1] if len(name_parts) > 1 else ''

        instance.email = validated_data.get('email', instance.email)
        instance.registration_number = validated_data.get('registration_number', instance.registration_number)
        instance.save()

        # Update Clinic fields if provided
        if clinic_data and instance.clinic:
            clinic = instance.clinic
            new_name = clinic_data.get('name', clinic.name)

            # Manual uniqueness check if name is changing
            if new_name != clinic.name:
                if Clinic.objects.filter(name=new_name).exclude(id=clinic.id).exists():
                    raise serializers.ValidationError({"clinic": {"name": "A clinic with this name already exists."}})
                clinic.name = new_name

            clinic.doctor_name = f"{instance.first_name} {instance.last_name}".strip()
            clinic.email = instance.email
            clinic.registration_number = instance.registration_number
            clinic.save()

        return instance

class RegisterSerializer(serializers.Serializer):
    doctor_name = serializers.CharField(max_length=255)
    clinic_name = serializers.CharField(max_length=255)
    mobile = serializers.CharField(max_length=15)
    email = serializers.EmailField(required=False)
    registration_number = serializers.CharField(max_length=100, required=False)
    password = serializers.CharField(write_only=True, required=True, allow_blank=False)


class LoginRequestSerializer(serializers.Serializer):
    identifier = serializers.CharField() # Can be mobile or registration number

class VerifyOTPSerializer(serializers.Serializer):
    identifier = serializers.CharField()
    otp = serializers.CharField(min_length=6, max_length=6)


class VerifyEmailSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField(min_length=6, max_length=6)


class ResendEmailVerificationSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ResetPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField(min_length=6, max_length=6)
    new_password = serializers.CharField(min_length=6, write_only=True)


class VerifyInvitationSerializer(serializers.Serializer):
    invitation_code = serializers.CharField(max_length=32)


class StaffSetupSerializer(serializers.Serializer):
    invitation_code = serializers.CharField(max_length=32)
    password = serializers.CharField(min_length=6, write_only=True)

class StaffSerializer(serializers.ModelSerializer):
    # Write-only fields the frontend will send
    send_credentials_via_sms = serializers.BooleanField(write_only=True, required=False, default=False)
    profile_picture_url = serializers.SerializerMethodField()

    def get_profile_picture_url(self, obj):
        if obj.profile_picture:
            return obj.get_profile_picture_url()
        return None

    class Meta:
        model = User
        fields = [
            'id', 'mobile', 'first_name', 'last_name',
            'email', 'registration_number', 'role',
            'can_view_financial', 'account_status', 'invitation_sent_at',
            'send_credentials_via_sms', 'profile_picture_url',
        ]
        read_only_fields = ['id', 'account_status', 'invitation_sent_at']
        extra_kwargs = {
            'role': {'required': True},
            'mobile': {'read_only': False},
            'email': {'required': True},  # Email is now required for invitations
        }

    def get_extra_kwargs(self):
        kwargs = super().get_extra_kwargs()
        if self.instance:  # updating existing staff
            # Make most fields read-only on update - only allow role and can_view_financial
            kwargs.update({
                'mobile': {'read_only': True},
                'first_name': {'read_only': True},
                'last_name': {'read_only': True},
                'email': {'read_only': True},
                'registration_number': {'read_only': True},
            })
        return kwargs

    def validate_role(self, value):
        # allow the staff roles your frontend uses
        if value not in ['receptionist', 'nurse', 'assistant']:
            raise serializers.ValidationError("Invalid role for staff.")
        return value

    def validate_mobile(self, value):
        # Ensure uniqueness ignoring self when updating
        qs = User.objects.filter(mobile=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("User with this mobile number already exists.")
        return value

    def validate_email(self, value):
        # Ensure uniqueness ignoring self when updating
        qs = User.objects.filter(email__iexact=value.lower())
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("User with this email already exists.")
        return value.lower()

    def create(self, validated_data):
        send_sms_flag = validated_data.pop('send_credentials_via_sms', False)
        clinic = self.context.get('clinic') or validated_data.pop('clinic', None)
        request_user = self.context.get('request').user if self.context.get('request') else None

        # Build user object
        user = User(**validated_data)
        if clinic:
            user.clinic = clinic

        # Set account as pending (staff will activate via invitation)
        user.account_status = 'pending'

        # Set unusable password (will be set via invitation)
        user.set_unusable_password()

        # 2FA will be enabled after account setup
        user.is_2fa_enabled = False

        # Generate invitation code
        user.invitation_code = generate_invitation_code()
        user.invitation_sent_at = timezone.now()

        # Save to get an ID
        user.save()

        # Send invitation email
        try:
            staff_name = f"{user.first_name} {user.last_name}".strip()
            clinic_name = clinic.name if clinic else "EzeeHealth"
            invited_by = f"{request_user.first_name} {request_user.last_name}".strip() if request_user else "Admin"

            send_staff_invitation_email(
                email=user.email,
                invitation_code=user.invitation_code,
                staff_name=staff_name,
                clinic_name=clinic_name,
                invited_by=invited_by
            )
        except Exception as e:
            # Log but do not fail user creation if email fails
            print(f"Warning: failed to send staff invitation email: {e}")

        return user

    def update(self, instance, validated_data):
        """Only allow updating role and can_view_financial."""
        allowed_fields = {'role', 'can_view_financial'}
        filtered_data = {k: v for k, v in validated_data.items() if k in allowed_fields}

        for attr, val in filtered_data.items():
            setattr(instance, attr, val)

        instance.save()
        return instance
