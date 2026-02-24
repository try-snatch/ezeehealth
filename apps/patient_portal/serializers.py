from rest_framework import serializers
from apps.authentication.models import User
from .models import (
    UploadedDocument, DocumentInsight, Dependant, DocumentShare,
    ChatMessage, Alert,
)


class PatientRegisterSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)
    mobile = serializers.CharField(max_length=15)
    email = serializers.EmailField(required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, min_length=8)

    def validate_mobile(self, value):
        if User.objects.filter(mobile=value).exists():
            raise serializers.ValidationError("A user with this mobile number already exists.")
        return value

    def validate_password(self, value):
        if value.isdigit() or value.isalpha():
            raise serializers.ValidationError("Password must contain both letters and numbers.")
        return value


class PatientProfileSerializer(serializers.ModelSerializer):
    profile_picture_url = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'mobile', 'email', 'first_name', 'last_name', 'role',
            'zoho_contact_id', 'profile_completed', 'is_corporate',
            'lead_source', 'company_name', 'gender', 'age_in_years',
            'mailing_street', 'mailing_city', 'mailing_zip', 'mailing_state',
            'primary_doctor_id', 'doctor_email', 'doctor_mobile',
            'profile_picture', 'profile_picture_url',
        ]
        read_only_fields = [
            'id', 'mobile', 'role', 'zoho_contact_id', 'profile_completed',
            'profile_picture_url',
        ]

    def get_profile_picture_url(self, obj):
        if obj.profile_picture:
            return obj.get_profile_picture_url()
        return None


class CompleteProfileSerializer(serializers.Serializer):
    lead_source = serializers.ChoiceField(choices=['Corporate', 'Direct'])
    primary_doctor = serializers.CharField(required=False, allow_blank=True)
    doctor_email = serializers.EmailField(required=False, allow_blank=True)
    doctor_mobile = serializers.CharField(max_length=20, required=False, allow_blank=True)
    gender = serializers.CharField(required=False, allow_blank=True)
    age_in_years = serializers.IntegerField(required=False, allow_null=True)
    mailing_street = serializers.CharField(required=False, allow_blank=True)
    mailing_city = serializers.CharField(required=False, allow_blank=True)
    mailing_zip = serializers.CharField(required=False, allow_blank=True)
    mailing_state = serializers.CharField(required=False, allow_blank=True)


class UpdateProfileSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=150, required=False)
    last_name = serializers.CharField(max_length=150, required=False)
    email = serializers.EmailField(required=False)
    gender = serializers.CharField(required=False, allow_blank=True)
    age_in_years = serializers.IntegerField(required=False, allow_null=True)
    mailing_street = serializers.CharField(required=False, allow_blank=True)
    mailing_city = serializers.CharField(required=False, allow_blank=True)
    mailing_zip = serializers.CharField(required=False, allow_blank=True)
    mailing_state = serializers.CharField(required=False, allow_blank=True)


class UploadedDocumentSerializer(serializers.ModelSerializer):
    has_insights = serializers.SerializerMethodField()
    is_shared = serializers.SerializerMethodField()
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = UploadedDocument
        fields = [
            'id', 'title', 'category', 'file_extension', 'file_size',
            'ai_readable', 'ai_processed', 'uploaded_at',
            'has_insights', 'is_shared', 'download_url',
        ]

    def get_has_insights(self, obj):
        return obj.insights.exists()

    def get_is_shared(self, obj):
        return obj.shares.filter(is_active=True).exists()

    def get_download_url(self, obj):
        from apps.patients.s3_utils import generate_presigned_url_for_key
        try:
            return generate_presigned_url_for_key(obj.s3_key, expiration=3600)
        except Exception:
            return None


class DocumentInsightSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentInsight
        fields = ['id', 'title', 'summary', 'key_findings', 'risk_flags', 'tags', 'created_at']


class DependantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Dependant
        fields = [
            'id', 'full_name', 'relationship', 'age', 'gender',
            'zoho_contact_id', 'email', 'mobile', 'created_at',
        ]
        read_only_fields = ['id', 'zoho_contact_id', 'created_at']


class CreateDependantSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)
    relationship = serializers.ChoiceField(
        choices=['Father', 'Mother', 'Sibling', 'Child', 'Spouse', 'Other']
    )
    age = serializers.IntegerField(required=False, allow_null=True)
    gender = serializers.CharField(required=False, allow_blank=True)


class DocumentShareSerializer(serializers.ModelSerializer):
    document_title = serializers.CharField(source='document.title', read_only=True)
    document_category = serializers.CharField(source='document.category', read_only=True)

    class Meta:
        model = DocumentShare
        fields = [
            'id', 'document_id', 'document_title', 'document_category',
            'doctor_email', 'shared_at', 'is_active',
        ]


class ChatMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatMessage
        fields = ['id', 'sender', 'message', 'timestamp']


class AlertSerializer(serializers.ModelSerializer):
    class Meta:
        model = Alert
        fields = ['id', 'message', 'is_read', 'alert_type', 'created_at']
