from rest_framework import serializers
from .models import Patient, Referral


class ReferralSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = Referral
        fields = [
            'id', 'status', 'status_display', 'diagnosis',
            'suggested_specialty', 'suggested_sshs', 'revenue',
            'zoho_lead_id', 'zoho_deal_id', 'zoho_contact_id',
            'referred_date', 'converted_date',
        ]


class PatientSerializer(serializers.ModelSerializer):
    latest_referral = serializers.SerializerMethodField()

    class Meta:
        model = Patient
        fields = '__all__'
        read_only_fields = ['clinic', 'created_at', 'status_updated_at']

    def get_latest_referral(self, obj):
        referral = obj.referrals.first()  # ordered by -referred_date
        if referral:
            return ReferralSerializer(referral).data
        return None

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get('request')
        if request and request.user and not request.user.can_view_financial:
            # Hide revenue from latest_referral too
            if data.get('latest_referral'):
                data['latest_referral'].pop('revenue', None)
        return data


class PatientDetailSerializer(serializers.ModelSerializer):
    """Detailed patient serializer with all referrals."""
    referrals = ReferralSerializer(many=True, read_only=True)
    latest_referral = serializers.SerializerMethodField()

    class Meta:
        model = Patient
        fields = '__all__'

    def get_latest_referral(self, obj):
        referral = obj.referrals.first()
        if referral:
            return ReferralSerializer(referral).data
        return None
