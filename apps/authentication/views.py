import logging

from rest_framework import views, status, permissions
from rest_framework.response import Response
from django.conf import settings
from django.core.cache import cache
from rest_framework_simplejwt.tokens import RefreshToken
from .models import User, Clinic
from .serializers import (
    RegisterSerializer, LoginRequestSerializer, VerifyOTPSerializer, UserSerializer,
    VerifyEmailSerializer, ResendEmailVerificationSerializer, ForgotPasswordSerializer,
    ResetPasswordSerializer, VerifyInvitationSerializer, StaffSetupSerializer
)
from .utils import generate_otp, send_auth_otp
from .email_utils import (
    generate_verification_code, send_verification_email,
    send_password_reset_email
)
from apps.patient_portal.models import PatientInvite
from .rate_limiting import (
    check_email_rate_limit, check_code_attempt_limit,
    increment_failed_attempts, clear_failed_attempts
)
from apps.integrations.zoho_service import ZohoService
from django.db import transaction
from django.contrib.auth import authenticate
from django.utils import timezone
from datetime import timedelta

logger = logging.getLogger(__name__)


class RegisterView(views.APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        if serializer.is_valid():
            logger.info("Register request for mobile %s, clinic '%s'", data.get('mobile'), data.get('clinic_name'))
            data = serializer.validated_data

            # Create Clinic
            clinic, created = Clinic.objects.get_or_create(
                name=data['clinic_name'],
                defaults={
                    'doctor_name': data['doctor_name'],
                    'phone': data['mobile'],
                    'email': data.get('email'),
                    'registration_number': data.get('registration_number')
                }
            )

            logger.info("Clinic '%s' %s", clinic.name, "created" if created else "already existed")

            # Create Doctor User
            # Split Name
            name_parts = data['doctor_name'].strip().split(' ', 1)
            first_name = name_parts[0]
            last_name = name_parts[1] if len(name_parts) > 1 else ''


            with transaction.atomic():
                user = User.objects.create_user(
                    mobile=data['mobile'],
                    first_name=first_name,
                    last_name=last_name,
                    role='owner',
                    clinic=clinic,
                    email=data.get('email'),
                    registration_number=data.get('registration_number')
                )
                password = data.get('password')
                if password:
                    user.set_password(password)
                    user.save(update_fields=['password'])

                # Set email as unverified for new users
                user.is_email_verified = False
                user.save(update_fields=['is_email_verified'])

            # Sync with Zoho
            zoho_data = {
                "Name": data['doctor_name'],
                "Mobile": data['mobile'],
                "Email": data.get('email', ''),
                "Registration_No": data.get('registration_number', ''),
                "Clinic_Name": clinic.name,
            }
            try:
                ZohoService.create_or_update_doctor(zoho_data)
            except Exception as e:
                logger.error("Failed to sync with Zoho during registration: %s", e)

            otp = generate_otp()
            logger.debug("Registration OTP for %s: %s", user.mobile, otp)
            cache.set(f"otp_2fa_{user.id}", otp, timeout=300)
            send_auth_otp(user.mobile, otp)

            # Generate and send email verification code if email provided
            if user.email:
                email_code = generate_verification_code()
                user.email_verification_code = email_code
                user.email_verification_sent_at = timezone.now()
                user.save(update_fields=['email_verification_code', 'email_verification_sent_at'])

                try:
                    send_verification_email(
                        email=user.email,
                        code=email_code,
                        user_name=f"{user.first_name} {user.last_name}".strip()
                    )
                except Exception as e:
                    logger.error("Failed to send verification email to %s: %s", user.email, e)

            logger.info("Registration successful for %s (user_id=%s)", user.mobile, user.id)
            return Response({"message": "Registration successful. OTP sent.", "identifier": user.mobile}, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class LoginView(views.APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        """
        Expecting:
        {
            "identifier": "<email, mobile or username>",
            "password": "<password>"
        }
        If user has 2FA enabled -> send SMS OTP and reply {"2fa_required": true, "method":"sms"}
        Else -> return tokens.
        """
        identifier = request.data.get('identifier')
        password = request.data.get('password')

        if not identifier or not password:
            return Response({"error": "identifier and password required"}, status=status.HTTP_400_BAD_REQUEST)

        logger.info("Login attempt for identifier '%s'", identifier)
        user = authenticate(request, username=identifier, password=password)
        if not user:
            logger.warning("Login failed — invalid credentials for '%s'", identifier)
            return Response({"error": "Invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED)

        # Check if account is still pending (staff invitation not completed)
        if hasattr(user, 'account_status') and user.account_status == 'pending':
            logger.warning("Login blocked — account pending for '%s' (user_id=%s)", identifier, user.id)
            return Response({"error": "Account not activated. Please check your email for the invitation link."}, status=status.HTTP_403_FORBIDDEN)

        # If user has 2FA enabled => send OTP via MSG91 and return 2fa_required
        if user.is_2fa_enabled:
            # rate-limit: prevent OTP flood (simple)
            last_sent = user.last_otp_sent_at
            if last_sent and (timezone.now() - last_sent).total_seconds() < 30:
                return Response({"error": "OTP sent recently. Try after a short while."}, status=status.HTTP_429_TOO_MANY_REQUESTS)

            otp = generate_otp()
            cache_key = f"otp_2fa_{user.id}"
            cache.set(cache_key, otp, timeout=300)  # 5 minutes expiry
            logger.debug("Login 2FA OTP for %s: %s", user.mobile, otp)

            send_success = send_auth_otp(user.mobile, otp)

            # update last_otp_sent_at so throttling works
            user.last_otp_sent_at = timezone.now()
            user.save(update_fields=['last_otp_sent_at'])

            if send_success:
                logger.info("Login 2FA OTP sent to %s (user_id=%s)", user.mobile, user.id)
            else:
                logger.error("Login 2FA OTP sending FAILED for %s (user_id=%s)", user.mobile, user.id)

            return Response({"2fa_required": True, "method": "sms", "identifier": user.mobile}, status=status.HTTP_200_OK)

        # No 2FA -> issue tokens
        logger.info("Login success (no 2FA) for %s (user_id=%s, role=%s)", user.mobile, user.id, user.role)
        refresh = RefreshToken.for_user(user)
        return Response({
            'refresh': str(refresh),
            'access': str(refresh.access_token),
            'user': UserSerializer(user).data
        }, status=status.HTTP_200_OK)

class VerifyOTPView(views.APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        """
        Expecting:
        {
            "identifier": "<mobile or username>",
            "otp": "123456"
        }
        """
        identifier = request.data.get('identifier')
        otp = request.data.get('otp')

        if not identifier or not otp:
            return Response({"error": "identifier and otp required"}, status=status.HTTP_400_BAD_REQUEST)

        # find user by mobile or username (same lookup as other places)
        user = User.objects.filter(mobile=identifier).first() or User.objects.filter(username=identifier).first()
        if not user:
            logger.warning("OTP verify — user not found for identifier '%s'", identifier)
            return Response({"error": "User not found. Please Register"}, status=status.HTTP_404_NOT_FOUND)

        # Development bypass (existing)
        if otp == "123456" and getattr(settings, 'DEBUG', False):
            cached_otp = "123456"
        else:
            cached_otp = cache.get(f"otp_2fa_{user.id}")

        logger.debug("OTP verify for %s — submitted: %s, cached: %s", identifier, otp, cached_otp)

        if cached_otp and cached_otp == otp:
            # OTP correct -> issue tokens
            update_fields = []

            # mark 2FA enabled if not already
            if not user.is_2fa_enabled:
                user.is_2fa_enabled = True
                user.last_otp_sent_at = timezone.now()
                update_fields += ['is_2fa_enabled', 'last_otp_sent_at']

            # Activate pending accounts (patient registration, staff setup)
            if user.account_status == 'pending':
                user.account_status = 'active'
                update_fields.append('account_status')

            if update_fields:
                user.save(update_fields=update_fields)

            # cleanup both keys (unified + legacy)
            cache.delete(f"otp_2fa_{user.id}")
            cache.delete(f"otp_{user.mobile}")

            refresh = RefreshToken.for_user(user)
            logger.info("OTP verified — login success for %s (user_id=%s, role=%s)", user.mobile, user.id, user.role)
            return Response({
                'refresh': str(refresh),
                'access': str(refresh.access_token),
                'user': UserSerializer(user).data
            }, status=status.HTTP_200_OK)

        # If we get here -> invalid/expired OTP
        logger.warning("OTP verify failed for %s (user_id=%s) — invalid or expired", identifier, user.id)
        return Response({"error": "Invalid or expired OTP"}, status=status.HTTP_400_BAD_REQUEST)



class MeView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        data = UserSerializer(user).data

        # Add profile picture URL if available
        if user.profile_picture:
            data['profile_picture_url'] = user.get_profile_picture_url()

        return Response(data)

    def patch(self, request):
        """
        All users can upload profile pictures.
        Only owners (and admins) can update clinic name.

        Supports both JSON and multipart/form-data for profile picture uploads.
        """
        user = request.user

        # Handle profile picture upload (allowed for all users)
        profile_picture_file = request.FILES.get('profile_picture')
        if profile_picture_file:
            try:
                # Validate file type
                allowed_types = ['image/jpeg', 'image/png', 'image/gif']
                if profile_picture_file.content_type not in allowed_types:
                    return Response({
                        "profile_picture": "Invalid file type. Only JPEG, PNG, and GIF images are allowed."
                    }, status=status.HTTP_400_BAD_REQUEST)

                # Validate file size (max 5MB)
                max_size = 5 * 1024 * 1024  # 5MB in bytes
                if profile_picture_file.size > max_size:
                    return Response({
                        "profile_picture": "File size exceeds 5MB limit."
                    }, status=status.HTTP_400_BAD_REQUEST)

                # Delete old profile picture if exists
                if user.profile_picture:
                    from apps.patients.s3_utils import delete_user_profile_picture
                    try:
                        delete_user_profile_picture(user.profile_picture)
                    except Exception as e:
                        logger.warning("Failed to delete old profile picture for user %s: %s", user.id, e)

                # Upload new profile picture
                from apps.patients.s3_utils import upload_user_profile_picture
                s3_key = upload_user_profile_picture(user.id, profile_picture_file, profile_picture_file.name)

                if not s3_key:
                    return Response({
                        "profile_picture": "Failed to upload profile picture to S3. Please check AWS credentials."
                    }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

                user.profile_picture = s3_key
                user.save()
            except Exception as e:
                logger.error("Profile picture upload error for user %s: %s", user.id, e, exc_info=True)
                return Response({
                    "error": f"Profile picture upload failed: {str(e)}"
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            # If only profile picture was uploaded, return success
            if 'clinic' not in request.data:
                response_data = UserSerializer(user).data
                if user.profile_picture:
                    response_data['profile_picture_url'] = user.get_profile_picture_url()
                return Response(response_data)

        # Check if clinic update is requested
        if 'clinic' not in request.data:
            return Response({"error": "No data provided for update"}, status=status.HTTP_400_BAD_REQUEST)

        # Only owners (and admins) can update clinic name
        if user.role not in ['owner', 'admin']:
            return Response({
                "error": "Only clinic owners can update clinic information."
            }, status=status.HTTP_403_FORBIDDEN)

        # ONLY accept clinic data - no other profile fields can be updated
        data = {}
        if isinstance(request.data['clinic'], dict):
            data['clinic'] = request.data['clinic']

        # Reject if any other fields are provided
        disallowed_user_fields = set(request.data.keys()) - {'clinic', 'profile_picture'}
        if disallowed_user_fields:
            return Response({
                "error": f"Only clinic name can be updated. Cannot update: {', '.join(disallowed_user_fields)}"
            }, status=status.HTTP_400_BAD_REQUEST)

        # Clinic update - RESTRICTED to name only
        clinic_data = data.get('clinic')
        if not user.clinic:
            return Response({"error": "User is not associated with a clinic"}, status=status.HTTP_400_BAD_REQUEST)

        # Validate that only 'name' field is being updated
        allowed_clinic_fields = {'name'}
        provided_clinic_fields = set(clinic_data.keys())
        disallowed_fields = provided_clinic_fields - allowed_clinic_fields

        if disallowed_fields:
            return Response({
                "clinic": f"Only the clinic name can be updated. Cannot update: {', '.join(disallowed_fields)}"
            }, status=status.HTTP_400_BAD_REQUEST)

        from .models import Clinic
        clinic = user.clinic
        new_name = clinic_data.get('name', '').strip()

        if not new_name:
            return Response({"clinic": {"name": "Clinic name cannot be empty"}}, status=status.HTTP_400_BAD_REQUEST)

        if new_name != clinic.name:
            if Clinic.objects.filter(name=new_name).exclude(id=clinic.id).exists():
                return Response({"clinic": {"name": "A clinic with this name already exists."}}, status=status.HTTP_400_BAD_REQUEST)
            clinic.name = new_name
            clinic.doctor_name = f"{user.first_name} {user.last_name}".strip()
            clinic.save()

        # Return response with profile picture URL
        response_data = UserSerializer(user).data
        if user.profile_picture:
            response_data['profile_picture_url'] = user.get_profile_picture_url()

        return Response(response_data)


class VerifyEmailView(views.APIView):
    """Verify email address with 6-digit code."""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = VerifyEmailSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data['email'].lower()
        code = serializer.validated_data['code']

        # Rate limit: Check attempt limit
        is_allowed, attempts_remaining, reset_time = check_code_attempt_limit(
            email, action='email_verification', max_attempts=5, window_minutes=10
        )
        if not is_allowed:
            return Response({
                "error": f"Too many failed attempts. Please try again in {reset_time} seconds."
            }, status=status.HTTP_429_TOO_MANY_REQUESTS)

        # Find user by email
        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            increment_failed_attempts(email, action='email_verification')
            return Response({"error": "Invalid verification code."}, status=status.HTTP_400_BAD_REQUEST)

        # Check if code matches
        if not user.email_verification_code or user.email_verification_code != code:
            increment_failed_attempts(email, action='email_verification')
            return Response({"error": "Invalid verification code."}, status=status.HTTP_400_BAD_REQUEST)

        # Check if code is expired (10 minutes)
        if user.email_verification_sent_at:
            elapsed = (timezone.now() - user.email_verification_sent_at).total_seconds()
            if elapsed > settings.EMAIL_VERIFICATION_EXPIRY:
                return Response({"error": "Verification code has expired. Please request a new one."}, status=status.HTTP_400_BAD_REQUEST)

        # Mark email as verified and clear the code
        user.is_email_verified = True
        user.email_verification_code = None
        user.email_verification_sent_at = None
        user.save(update_fields=['is_email_verified', 'email_verification_code', 'email_verification_sent_at'])

        # Clear failed attempts
        clear_failed_attempts(email, action='email_verification')

        return Response({"message": "Email verified successfully."}, status=status.HTTP_200_OK)


class ResendEmailVerificationView(views.APIView):
    """Resend email verification code."""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = ResendEmailVerificationSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data['email'].lower()

        # Rate limit: 60 second cooldown
        is_allowed, wait_time = check_email_rate_limit(email, action='email_verification', limit_seconds=60)
        if not is_allowed:
            return Response({
                "error": f"Please wait {wait_time} seconds before requesting another code."
            }, status=status.HTTP_429_TOO_MANY_REQUESTS)

        # Find user
        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            # Don't reveal if email exists or not (security)
            return Response({"message": "If the email exists, a verification code has been sent."}, status=status.HTTP_200_OK)

        # Check if already verified
        if user.is_email_verified:
            return Response({"error": "Email is already verified."}, status=status.HTTP_400_BAD_REQUEST)

        # Generate new code
        code = generate_verification_code()
        user.email_verification_code = code
        user.email_verification_sent_at = timezone.now()
        user.last_email_sent_at = timezone.now()
        user.save(update_fields=['email_verification_code', 'email_verification_sent_at', 'last_email_sent_at'])

        # Send email
        try:
            send_verification_email(
                email=user.email,
                code=code,
                user_name=f"{user.first_name} {user.last_name}".strip()
            )
        except Exception as e:
            logger.error("Failed to send verification email to %s: %s", email, e)
            return Response({"error": "Failed to send verification email. Please try again."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({"message": "Verification code sent."}, status=status.HTTP_200_OK)


class ForgotPasswordView(views.APIView):
    """Request password reset code via email."""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data['email'].lower()

        # Rate limit: 60 second cooldown
        is_allowed, wait_time = check_email_rate_limit(email, action='password_reset', limit_seconds=60)
        if not is_allowed:
            return Response({
                "error": f"Please wait {wait_time} seconds before requesting another code."
            }, status=status.HTTP_429_TOO_MANY_REQUESTS)

        # Find user (don't reveal if exists or not for security)
        try:
            user = User.objects.get(email__iexact=email)

            # Generate reset code
            code = generate_verification_code()
            user.password_reset_code = code
            user.password_reset_sent_at = timezone.now()
            user.last_email_sent_at = timezone.now()
            user.save(update_fields=['password_reset_code', 'password_reset_sent_at', 'last_email_sent_at'])

            # Send email
            try:
                send_password_reset_email(
                    email=user.email,
                    code=code,
                    user_name=f"{user.first_name} {user.last_name}".strip()
                )
            except Exception as e:
                logger.error("Failed to send password reset email to %s: %s", email, e)
        except User.DoesNotExist:
            pass  # Don't reveal if email exists

        # Always return success to not reveal if email exists
        return Response({"message": "If the email exists, a password reset code has been sent."}, status=status.HTTP_200_OK)


class ResetPasswordView(views.APIView):
    """Reset password with email code."""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data['email'].lower()
        code = serializer.validated_data['code']
        new_password = serializer.validated_data['new_password']

        # Rate limit: Check attempt limit
        is_allowed, attempts_remaining, reset_time = check_code_attempt_limit(
            email, action='password_reset', max_attempts=5, window_minutes=10
        )
        if not is_allowed:
            return Response({
                "error": f"Too many failed attempts. Please try again in {reset_time} seconds."
            }, status=status.HTTP_429_TOO_MANY_REQUESTS)

        # Find user
        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            increment_failed_attempts(email, action='password_reset')
            return Response({"error": "Invalid reset code."}, status=status.HTTP_400_BAD_REQUEST)

        # Check if code matches
        if not user.password_reset_code or user.password_reset_code != code:
            increment_failed_attempts(email, action='password_reset')
            return Response({"error": "Invalid reset code."}, status=status.HTTP_400_BAD_REQUEST)

        # Check if code is expired (10 minutes)
        if user.password_reset_sent_at:
            elapsed = (timezone.now() - user.password_reset_sent_at).total_seconds()
            if elapsed > settings.PASSWORD_RESET_EXPIRY:
                return Response({"error": "Reset code has expired. Please request a new one."}, status=status.HTTP_400_BAD_REQUEST)

        # Reset password and clear the code
        user.set_password(new_password)
        user.password_reset_code = None
        user.password_reset_sent_at = None
        user.save(update_fields=['password', 'password_reset_code', 'password_reset_sent_at'])

        # Clear failed attempts
        clear_failed_attempts(email, action='password_reset')

        return Response({"message": "Password reset successfully."}, status=status.HTTP_200_OK)


class VerifyInvitationView(views.APIView):
    """Verify staff invitation code and return user info."""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = VerifyInvitationSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        invitation_code = serializer.validated_data['invitation_code']

        # Find user with this invitation code
        try:
            user = User.objects.get(invitation_code=invitation_code, account_status='pending')
        except User.DoesNotExist:
            return Response({"error": "Invalid or expired invitation code."}, status=status.HTTP_404_NOT_FOUND)

        # Check if invitation is expired (7 days)
        if user.invitation_sent_at:
            elapsed = (timezone.now() - user.invitation_sent_at).total_seconds()
            if elapsed > settings.INVITATION_EXPIRY:
                return Response({"error": "Invitation has expired. Please contact your administrator."}, status=status.HTTP_400_BAD_REQUEST)

        # Return user info for display
        return Response({
            "staff_name": f"{user.first_name} {user.last_name}".strip(),
            "email": user.email,
            "mobile": user.mobile,
            "role": user.role,
            "clinic_name": user.clinic.name if user.clinic else "EzeeHealth"
        }, status=status.HTTP_200_OK)


class StaffSetupAccountView(views.APIView):
    """Staff sets password and activates account."""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = StaffSetupSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        invitation_code = serializer.validated_data['invitation_code']
        password = serializer.validated_data['password']

        # Find user with this invitation code
        try:
            user = User.objects.get(invitation_code=invitation_code, account_status='pending')
        except User.DoesNotExist:
            return Response({"error": "Invalid or expired invitation code."}, status=status.HTTP_404_NOT_FOUND)

        # Check if invitation is expired (7 days)
        if user.invitation_sent_at:
            elapsed = (timezone.now() - user.invitation_sent_at).total_seconds()
            if elapsed > settings.INVITATION_EXPIRY:
                return Response({"error": "Invitation has expired. Please contact your administrator."}, status=status.HTTP_400_BAD_REQUEST)

        # Set password and activate account
        user.set_password(password)
        user.account_status = 'active'
        user.is_2fa_enabled = True  # Enable 2FA for staff
        user.invitation_code = None  # Clear invitation code
        user.invitation_sent_at = None
        user.save(update_fields=['password', 'account_status', 'is_2fa_enabled', 'invitation_code', 'invitation_sent_at'])

        # Generate and send OTP for mobile verification
        otp = generate_otp()
        cache.set(f"otp_2fa_{user.id}", otp, timeout=300)
        logger.debug("Staff setup OTP for %s: %s", user.mobile, otp)
        send_auth_otp(user.mobile, otp)
        user.last_otp_sent_at = timezone.now()
        user.save(update_fields=['last_otp_sent_at'])

        return Response({
            "message": "Account setup successful. OTP sent for verification.",
            "otp_required": True,
            "identifier": user.mobile
        }, status=status.HTTP_200_OK)


class PatientVerifyInviteView(views.APIView):
    """Verify patient invitation code and return patient info."""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        invitation_code = request.data.get('invitation_code', '').strip()
        if not invitation_code:
            return Response({"error": "Invitation code is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            invite = PatientInvite.objects.select_related('patient').get(
                invitation_code=invitation_code,
                is_used=False,
            )
        except PatientInvite.DoesNotExist:
            return Response({"error": "Invalid or expired invitation."}, status=status.HTTP_404_NOT_FOUND)

        if timezone.now() > invite.expires_at:
            return Response({"error": "Invitation has expired. Please contact your clinic."}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "patient_name": invite.name,
            "clinic_name": invite.clinic_name,
            "mobile": invite.phone,
        }, status=status.HTTP_200_OK)


class PatientSetupAccountView(views.APIView):
    """Patient sets password and activates their account."""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        invitation_code = request.data.get('invitation_code', '').strip()
        password = request.data.get('password', '')

        if not invitation_code or not password:
            return Response({"error": "invitation_code and password are required."}, status=status.HTTP_400_BAD_REQUEST)

        if len(password) < 8:
            return Response({"error": "Password must be at least 8 characters."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            invite = PatientInvite.objects.select_related('patient').get(
                invitation_code=invitation_code,
                is_used=False,
            )
        except PatientInvite.DoesNotExist:
            return Response({"error": "Invalid or expired invitation."}, status=status.HTTP_404_NOT_FOUND)

        if timezone.now() > invite.expires_at:
            return Response({"error": "Invitation has expired. Please contact your clinic."}, status=status.HTTP_400_BAD_REQUEST)

        patient = invite.patient

        # Check if a User account already exists for this mobile
        if User.objects.filter(mobile=patient.phone).exists():
            return Response({"error": "An account already exists for this mobile number."}, status=status.HTTP_400_BAD_REQUEST)

        # Parse first/last name from full_name
        name_parts = (patient.full_name or '').strip().split(' ', 1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else ''

        # Create the patient User account
        user = User(
            mobile=patient.phone,
            email=patient.email or None,
            first_name=first_name,
            last_name=last_name,
            role='patient',
            account_status='pending',
            is_2fa_enabled=True,
            invitation_sent_at=invite.sent_at,
        )
        user.set_password(password)
        user.save()

        # Mark invite as used
        invite.is_used = True
        invite.save(update_fields=['is_used'])

        # Send OTP for mobile verification (same pattern as staff)
        otp = generate_otp()
        cache.set(f"otp_2fa_{user.id}", otp, timeout=300)
        logger.debug("Patient setup OTP for %s: %s", user.mobile, otp)
        send_auth_otp(user.mobile, otp)
        user.last_otp_sent_at = timezone.now()
        user.save(update_fields=['last_otp_sent_at'])

        return Response({
            "message": "Account created. OTP sent for verification.",
            "otp_required": True,
            "identifier": user.mobile,
        }, status=status.HTTP_200_OK)
