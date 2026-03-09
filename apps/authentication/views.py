import logging

from rest_framework import views, status, permissions
from rest_framework.response import Response
from django.conf import settings
from django.core.cache import cache
from rest_framework_simplejwt.tokens import RefreshToken
from .models import User, Clinic, MOUAgreement
from .serializers import (
    RegisterSerializer, LoginRequestSerializer, VerifyOTPSerializer, UserSerializer,
    VerifyEmailSerializer, ResendEmailVerificationSerializer, ForgotPasswordSerializer,
    ResetPasswordSerializer, VerifyInvitationSerializer, StaffSetupSerializer,
    MOUAgreementSerializer,
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
            data = serializer.validated_data
            logger.info("Register request for mobile %s, clinic '%s'", data.get('mobile'), data.get('clinic_name'))

            # Check for existing mobile/email before creating
            if User.objects.filter(mobile=data['mobile']).exists():
                return Response({"error": "A user with this mobile number is already registered."}, status=status.HTTP_400_BAD_REQUEST)
            if data.get('email') and User.objects.filter(email__iexact=data['email']).exists():
                return Response({"error": "A user with this email is already registered."}, status=status.HTTP_400_BAD_REQUEST)

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
        logger.warning("Registration validation failed: %s", serializer.errors)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class ResendRegistrationOTPView(views.APIView):
    """Resend OTP for a user who just registered but didn't receive/verify OTP."""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        identifier = request.data.get('identifier', '').strip()
        if not identifier:
            return Response({"error": "identifier is required"}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(mobile=identifier).first()
        if not user:
            return Response({"error": "User not found. Please register first."}, status=status.HTTP_404_NOT_FOUND)

        # Rate-limit: prevent OTP flood
        last_sent = user.last_otp_sent_at
        if last_sent and (timezone.now() - last_sent).total_seconds() < 30:
            return Response({"error": "OTP sent recently. Try after a short while."}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        otp = generate_otp()
        cache.set(f"otp_2fa_{user.id}", otp, timeout=300)
        logger.debug("Resend registration OTP for %s: %s", user.mobile, otp)
        send_auth_otp(user.mobile, otp)

        user.last_otp_sent_at = timezone.now()
        user.save(update_fields=['last_otp_sent_at'])

        logger.info("Registration OTP resent for %s (user_id=%s)", user.mobile, user.id)
        return Response({"message": "OTP resent successfully.", "identifier": user.mobile}, status=status.HTTP_200_OK)


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

        # Always require OTP — 2FA is mandatory for all users
        # rate-limit: prevent OTP flood (simple)
        last_sent = user.last_otp_sent_at
        if last_sent and (timezone.now() - last_sent).total_seconds() < 30:
            return Response({"error": "OTP sent recently. Try after a short while."}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        otp = generate_otp()
        cache_key = f"otp_2fa_{user.id}"
        cache.set(cache_key, otp, timeout=300)  # 5 minutes expiry
        logger.debug("Login OTP for %s: %s", user.mobile, otp)

        send_success = send_auth_otp(user.mobile, otp)

        # update last_otp_sent_at so throttling works
        user.last_otp_sent_at = timezone.now()
        user.save(update_fields=['last_otp_sent_at'])

        if send_success:
            logger.info("Login OTP sent to %s (user_id=%s)", user.mobile, user.id)
        else:
            logger.error("Login OTP sending FAILED for %s (user_id=%s)", user.mobile, user.id)

        return Response({"2fa_required": True, "method": "sms", "identifier": user.mobile}, status=status.HTTP_200_OK)

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

        # Google review test account bypass
        REVIEW_ACCOUNT_EMAIL = 'z92lqst553@wnbaldwy.com'
        if otp == "123456" and (getattr(settings, 'DEBUG', False) or user.email == REVIEW_ACCOUNT_EMAIL):
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
            logger.warning("Staff setup validation failed: %s", serializer.errors)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        invitation_code = serializer.validated_data['invitation_code']
        password = serializer.validated_data['password']

        # Find user with this invitation code
        try:
            user = User.objects.get(invitation_code=invitation_code, account_status='pending')
        except User.DoesNotExist:
            logger.warning("Staff setup failed — invalid invitation code: %s", invitation_code[:8])
            return Response({"error": "Invalid or expired invitation code."}, status=status.HTTP_404_NOT_FOUND)

        logger.info("Staff setup started for %s (user_id=%s, role=%s)", user.mobile, user.id, user.role)

        # Check if invitation is expired (7 days)
        if user.invitation_sent_at:
            elapsed = (timezone.now() - user.invitation_sent_at).total_seconds()
            if elapsed > settings.INVITATION_EXPIRY:
                logger.warning("Staff setup failed — invitation expired for user_id=%s (elapsed=%ds)", user.id, int(elapsed))
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

        logger.info("Staff setup successful for %s (user_id=%s) — OTP sent", user.mobile, user.id)
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
            logger.warning("Patient invite verify — invalid code: %s", invitation_code[:8])
            return Response({"error": "Invalid or expired invitation."}, status=status.HTTP_404_NOT_FOUND)

        if timezone.now() > invite.expires_at:
            logger.warning("Patient invite verify — expired for patient '%s'", invite.name)
            return Response({"error": "Invitation has expired. Please contact your clinic."}, status=status.HTTP_400_BAD_REQUEST)

        logger.info("Patient invite verified for '%s' (phone=%s)", invite.name, invite.phone)
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
            logger.warning("Patient setup failed — invalid invitation code: %s", invitation_code[:8])
            return Response({"error": "Invalid or expired invitation."}, status=status.HTTP_404_NOT_FOUND)

        if timezone.now() > invite.expires_at:
            logger.warning("Patient setup failed — invitation expired for '%s'", invite.name)
            return Response({"error": "Invitation has expired. Please contact your clinic."}, status=status.HTTP_400_BAD_REQUEST)

        patient = invite.patient

        # Check if a User account already exists for this mobile
        if User.objects.filter(mobile=patient.phone).exists():
            logger.warning("Patient setup failed — account already exists for mobile %s", patient.phone)
            return Response({"error": "An account already exists for this mobile number."}, status=status.HTTP_400_BAD_REQUEST)

        logger.info("Patient setup started for '%s' (phone=%s)", patient.full_name, patient.phone)

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

        logger.info("Patient setup successful for %s (user_id=%s) — OTP sent", user.mobile, user.id)
        return Response({
            "message": "Account created. OTP sent for verification.",
            "otp_required": True,
            "identifier": user.mobile,
        }, status=status.HTTP_200_OK)


def _generate_mou_pdf(data, user, signature_bytes, signed_at, ip_address):
    """
    Generate a properly formatted MOU PDF using PyMuPDF Story (HTML renderer).
    The output matches the EzeeHealth MOU template with all doctor/hospital fields filled in.
    """
    import fitz
    import io
    import base64
    import html as html_module

    def _ordinal(n):
        if 11 <= (n % 100) <= 13:
            return 'th'
        return {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')

    def _fmt_date(dt):
        return f"{dt.day}{_ordinal(dt.day)} {dt.strftime('%B %Y')}"

    def _e(s):
        """HTML-escape a dynamic value."""
        return html_module.escape(str(s or ''))

    date_str = _fmt_date(signed_at)
    try:
        end_date = signed_at.replace(year=signed_at.year + 1)
    except ValueError:
        end_date = signed_at.replace(year=signed_at.year + 1, day=28)
    end_date_str = _fmt_date(end_date)

    hosp       = _e(data.get('hospital_name', ''))
    signatory  = _e(data.get('authorized_signatory_name', ''))
    address    = _e(data.get('hospital_address', '')).replace('\n', '<br/>')
    bank_acct  = _e(data.get('bank_account_number', '')) or 'To be filled later'
    bank_name  = _e(data.get('bank_name', ''))           or 'To be filled later'
    bank_branch= _e(data.get('bank_branch', ''))         or 'To be filled later'
    bank_ifsc  = _e(data.get('bank_ifsc', ''))           or 'To be filled later'
    bank_addr  = _e(data.get('bank_address', ''))        or 'To be filled later'
    prof_fee   = _e(data.get('professional_fee', ''))    or 'As per agreement'
    sig_b64    = base64.b64encode(signature_bytes).decode('ascii')

    html = f"""<!DOCTYPE html><html><head><style>
body  {{ font-family: serif; font-size: 10pt; line-height: 1.6; color: #000; }}
h1   {{ font-size: 13pt; font-weight: bold; text-align: center; text-decoration: underline; margin: 0 0 10pt 0; }}
.hd  {{ font-weight: bold; margin-top: 10pt; margin-bottom: 2pt; }}
p    {{ margin: 4pt 0; text-align: justify; }}
.ctr {{ text-align: center; }}
ul   {{ margin: 3pt 0 3pt 0; padding-left: 18pt; }}
li   {{ margin: 2pt 0; }}
table{{ width: 100%; border-collapse: collapse; margin: 6pt 0; }}
td, th {{ border: 1px solid #000; padding: 3pt 6pt; font-size: 9.5pt; }}
th   {{ font-weight: bold; text-align: center; background-color: #f0f0f0; }}
</style></head><body>

<h1>Memorandum of Understanding</h1>

<p>This Memorandum of Understanding (the &#34;<b>Agreement</b>&#34;) is made on <b>{date_str}</b>.</p>

<p class="ctr"><b>BETWEEN</b></p>

<p><b>Ezeehealth (Dual Mirror Healthcare Pvt Ltd)</b>, a Company registered under the Companies Act,
2013 having its registered office at 127, 4th Cross, 1st Main, ST BED area, Koramangala,
Karnataka &#8211; 560034 being represented by its authorized signatory Mr. Jyoti Swarup,
hereinafter referred to as <b>&#34;EH&#34;</b> (which expression shall, unless repugnant to the context,
include its successors and permitted assigns)</p>

<p class="ctr"><b>AND</b></p>

<p><b>&#34;{hosp}&#34;</b> represented by its authorized signatory <b>Mr. {signatory}</b> and having its
registered office at {address} (Hereinafter referred as <b>&#34;{hosp}&#34;</b>)</p>

<p>(Hereinafter each shall be referred to as a &#34;<b>Party</b>&#34; and collectively referred to as
the &#34;<b>Parties</b>&#34;)</p>

<p class="hd">WHEREAS:</p>
<p>Ezeehealth (Dual Mirror Healthcare Pvt Ltd) is a digitally enabled platform for Doctors and
Patients. It has a network of Specialty Hospitals. It has a complete ecosystem to handhold
Patient&#8217;s journey; and in addition digitally enable the Primary doctors/Hospitals to remain a
part of their patient&#8217;s journey of treatment through their specialty treatment.</p>
<p>In order to enable it&#8217;s patients to avail specialty treatment <b>Ezeehealth (EH)</b> and
<b>&#34;{hosp}&#34;</b> have agreed to enter into this Agreement wherein <b>Ezeehealth (EH)</b> platform
and Patient concierge services shall be made available to {hosp}.</p>

<p><b>NOW THESE PRESENTS WITNESSETH AND IT IS HEREBY AGREED, DECLARED AND CONFIRMED BY AND
BETWEEN THE PARTIES HERETO AS UNDER</b></p>

<p class="hd">PURPOSE OF THIS AGREEMENT</p>
<p>The Ezeehealth shall provide its platform and concierge services to the patients of
<b>&#34;{hosp}&#34;</b> to avail specialty medical treatment. EH shall not be responsible for clinical
decisions, medical outcomes, or any treatment administered by the Hospital.</p>

<p class="hd">TERM</p>
<p>This Agreement shall be effective from <b>{date_str}</b> and shall remain in force till
<b>{end_date_str}</b>. It is hereby agreed that on the expiry of this period, the Parties shall
have the option to renew this Agreement for further period and on the terms and conditions as
mutually agreed to between the Parties at the time of renewal. Renewal terms, including fee
structure, shall be mutually agreed in writing.</p>

<p class="hd">PAYMENT TERMS</p>
<p>For the Patients of <b>{hosp}</b>:</p>
<p>Ezeehealth will collect the Fee on behalf of <b>{hosp}</b> for providing distinct clinical
services as stated in Appendix A of this MOU. This fee to be paid to {hosp} will be as per the
Professional fee Disclosure form in the <b>Appendix C</b> of this MOU.</p>
<p>The Tertiary healthcare treatment would be provided by any of the partner Specialty hospital
(&#8216;Tertiary Hospital&#8217;) of <b>Ezeehealth (EH)</b> to this patient. The decision regarding the
choice of Tertiary Hospital will be strictly under the purview of {hosp}; wherein Ezeehealth as
a platform will have no say in this matter.</p>
<p>The Parties expressly agree that the Fee payable to <b>{hosp}</b> is a primary obligation of the
Tertiary Hospital and is not a consideration payable by Ezeehealth. Ezeehealth shall solely act
as a &#8216;pure agent&#8217; of Tertiary Hospital, as defined under Rule 33 of the Central Goods and
Services Tax Rules, 2017, for the limited purpose of collection and remittance of the
&#8216;Fee&#8217; from the Tertiary Hospital to {hosp}. Ezeehealth shall not have any right, title,
interest or beneficial ownership in &#8216;distinct clinical services&#8217; and shall neither be treated
as the recipient of such services. Further, Ezeehealth shall not markup, retain or modify the
&#8216;Fee&#8217; for &#8216;distinct clinical services&#8217; in any manner and shall remit the same to {hosp}.</p>
<p>The Fee to <b>{hosp}</b> is for &#8216;distinct clinical services&#8217; only and does not constitute a
referral commission, in line with the Medical Council of India (Professional Conduct, Etiquette
and Ethics) Regulations, 2002</p>
<p><b>Applicable taxes (including GST)</b> shall be levied and paid as per Indian law</p>
<p><b>Bank details of the {hosp} is as follows:</b><br/>
<b>Bank Account number:</b> {bank_acct}<br/>
<b>Bank Name:</b> {bank_name}<br/>
<b>Branch:</b> {bank_branch}<br/>
<b>IFS code:</b> {bank_ifsc}<br/>
<b>Address:</b> {bank_addr}</p>

<p class="hd">CONFIDENTIALITY</p>
<p>Both Parties shall keep confidential all proprietary information, including patient records,
treatment details and business data. Exceptions apply only where disclosure is required by law
or regulatory authorities. Both parties shall comply with the Digital Personal Data Protection
Act, 2023.</p>

<p class="hd">BRANDING</p>
<p>Either party will be allowed to use the other party&#8217;s logo on social media or offices during
the period of this agreement.</p>

<p class="hd">TERMINATION</p>
<p>Either Party may terminate this Agreement with 90 (ninety) days written notice. Upon
termination: Pending dues shall be settled. Each Party shall return confidential materials.
Patient services already in process shall be completed in good faith. All patient cases under
active treatment shall be completed without EH incurring additional liability.</p>

<p class="hd">FORCE MAJEURE</p>
<p>Neither Party shall be liable for failure to perform obligations due to events beyond their
reasonable control (including pandemics, natural disasters or government restrictions).</p>

<p class="hd">INDEMNITY</p>
<p>EH shall indemnify the Hospital only for direct losses arising solely from EH&#8217;s proven breach
of this Agreement, and not for actions or omissions of the Hospital. No indemnity shall apply
to medical negligence or clinical errors by the Hospital.</p>

<p class="hd">LIMITATION OF LIABILITY</p>
<p>Neither Party shall be liable for indirect or consequential damages. Ezeehealth&#8217;s liability
shall not exceed the total service fees received under this Agreement in the preceding
12 months.</p>

<p class="hd">DISPUTE RESOLUTION</p>
<p>Any dispute shall be resolved by arbitration under the Arbitration and Conciliation Act, 1996.
The seat of arbitration shall be Bengaluru, Karnataka, and the language shall be English.
Courts at Bengaluru shall have exclusive jurisdiction.</p>

<p class="hd">AUTHORIZATION</p>
<p>All regulatory authorizations, approvals, registrations, etc. required by the
<b>&#34;{hosp}&#34;</b> and the <b>Ezeehealth (EH)</b> to enable it to carry on its business as it is being
carried on from time to time and to lawfully enter into this Agreement and comply with its
obligations under this Agreement have been obtained or effected and are in full force and
effect.</p>

<p class="hd">NOTICES</p>
<p>All notices to any Party shall be in writing properly addressed to the registered office of the
Party, or to such other addresses as may be provided from time to time by the Party, by
registered mail or courier or through digital medium like email to a registered email id.</p>

<p class="hd">SEVERABILITY</p>
<p>The illegality, invalidity or unenforceability or any provision of this Agreement shall not be
deemed to prejudice the enforceability of the remainder of this Agreement, which shall be
severable there from unless such illegality or invalidity of such part is material to this
Agreement.</p>

<p><b>THE PARTIES HAVE EXECUTED THIS AGREEMENT AS OF THE DATE FIRST SET FORTH ABOVE.</b></p>

<p><b>SIGNED &nbsp;&nbsp; and &nbsp;&nbsp; DELIVERED</b></p>
<p><b>For Dual Mirror Healthcare Pvt. Ltd.<br/>(Ezeehealth)</b><br/>
through its authorized representative<br/>
<b>Name: MR. JYOTI SWARUP</b></p>
<p>Signature: ____________________</p>

<p><b>SIGNED &nbsp;&nbsp; and &nbsp;&nbsp; DELIVERED</b></p>
<p><b>For {hosp}</b><br/>
through its authorized representative<br/>
<b>Name: MR. {signatory.upper()}</b></p>
<p>Signature:</p>
<img src="data:image/png;base64,{sig_b64}" width="180" height="65"/>

<p class="hd">Appendix A</p>
<p class="hd">Distinct Clinical Services</p>
<ul>
<li><b>Pre-Admission Clinical Optimization:</b> Formulating a treatment plan, stabilizing the
patient for transport, or conducting pre-operative assessments required by the receiving
hospital.</li>
<li><b>Consultation &amp; Case Management:</b> Visit the patient in the hospital to monitor
progress, adjust medications, or coordinate with the Tertiary Hospital&#8217;s internal team.</li>
<li><b>Post-Discharge Planning &amp; Counseling:</b> Detailed clinical briefing of the patient on
post-op care, wound management, or long-term medication titration</li>
<li><b>Emergency Stabilization:</b> Providing immediate life-saving care before or during the
transfer to a tertiary center.</li>
</ul>

<p class="hd">Appendix B</p>
<p class="hd">EZEEHEALTH RESPONSIBILITIES AND UNDERTAKINGS</p>
<p>Ezeehealth shall provide <b>&#34;{hosp}&#34;</b> a digital platform, patient concierge services and
it&#8217;s ecosystem of Co-managing Hospitals, processes and back-end support for</p>
<ul>
<li>Receiving a patient needing specialized treatment</li>
<li>Sending a patient needing specialized treatment</li>
<li>Handholding of such Patients during their treatment</li>
<li>Updates on the status of a patient under treatment</li>
</ul>
<p>The Ezeehealth&#8217;s team with digitally enabled processes will follow-up with the patients and
all concerned to ensure that</p>
<ul>
<li>Patient&#8217;s journey is hassle free</li>
<li>There is transparency in the treatment process along with continuous updates</li>
<li>All concerned with the co-management of the treatment are kept in the loop so that patients
experience seamless care</li>
</ul>
<p>Ezeehealth shall provide complete assistance through its Backend team to the
<b>&#34;{hosp}&#34;</b> in their billing process.</p>
<p>The <b>&#34;{hosp}&#34;</b> shall permit the officers and representatives of the
<b>Ezeehealth (EH)</b> during business hours, to enter upon the <b>&#34;{hosp}&#34;</b> office or hospital
and work in hospital premise (area provided by <b>&#34;{hosp}&#34;</b>).</p>
<p>If a case (episode) is brought through Ezeehealth platform or its representative, the patient
will be considered as Ezeehealth&#8217;s patient irrespective of the patients having a prior
hospital&#8217;s UHID from previous visits.</p>
<p>Ezeehealth shall not be liable for delays, failures, or actions of the Hospital or its staff
and does not provide medical advice, diagnosis, or treatment.</p>

<p class="hd">Appendix C</p>
<p class="hd">PROFESSIONAL FEE DISCLOSURE FORM</p>
<table>
<tr><th>Sl. No.</th><th>Particulars</th><th>Amount</th></tr>
<tr>
  <td>1</td>
  <td>Distinct Clinical Services Fee payable to {hosp}</td>
  <td>{prof_fee}</td>
</tr>
</table>

</body></html>"""

    buf = io.BytesIO()
    story = fitz.Story(html=html)
    writer = fitz.DocumentWriter(buf)
    mediabox = fitz.paper_rect('A4')
    where = mediabox + (60, 60, -60, -60)
    more = True
    while more:
        device = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(device)
        writer.end_page()
    writer.close()
    return buf.getvalue()


class SignMOUView(views.APIView):
    """Submit a signed MOU agreement."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = MOUAgreementSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        user = request.user
        data = serializer.validated_data

        # Check if user already signed
        if user.mou_signed:
            return Response({"error": "MOU already signed."}, status=status.HTTP_400_BAD_REQUEST)

        if not user.clinic:
            return Response({"error": "User is not associated with a clinic."}, status=status.HTTP_400_BAD_REQUEST)

        # Extract and upload signature image from base64 data URL
        signature_data = data.pop('signature')
        try:
            import base64
            import io
            from apps.patients.s3_utils import get_s3_client
            from django.conf import settings as django_settings

            # Parse data URL: "data:image/png;base64,iVBOR..."
            if ',' in signature_data:
                header, encoded = signature_data.split(',', 1)
            else:
                encoded = signature_data

            image_bytes = base64.b64decode(encoded)

            s3 = get_s3_client()
            bucket_name = django_settings.AWS_STORAGE_BUCKET_NAME
            timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')
            s3_key = f"mou_signatures/{user.id}/signature_{timestamp}.png"

            s3.upload_fileobj(
                io.BytesIO(image_bytes),
                bucket_name,
                s3_key,
                ExtraArgs={'ContentType': 'image/png'}
            )
        except Exception as e:
            logger.error("Failed to upload MOU signature for user %s: %s", user.id, e, exc_info=True)
            return Response({"error": "Failed to upload signature. Please try again."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Get client IP
        ip_address = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR')

        # Generate MOU PDF and upload to S3
        pdf_s3_key = ''
        try:
            pdf_bytes = _generate_mou_pdf(
                data=data,
                user=user,
                signature_bytes=image_bytes,
                signed_at=timezone.now(),
                ip_address=ip_address,
            )
            pdf_timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')
            pdf_s3_key = f"mou_documents/{user.id}/mou_{pdf_timestamp}.pdf"
            s3.upload_fileobj(
                io.BytesIO(pdf_bytes),
                bucket_name,
                pdf_s3_key,
                ExtraArgs={'ContentType': 'application/pdf'},
            )
        except Exception as e:
            logger.error("Failed to generate/upload MOU PDF for user %s: %s", user.id, e, exc_info=True)

        # Create MOU record
        mou = MOUAgreement.objects.create(
            user=user,
            clinic=user.clinic,
            hospital_name=data['hospital_name'],
            authorized_signatory_name=data['authorized_signatory_name'],
            hospital_address=data['hospital_address'],
            bank_account_number=data.get('bank_account_number', ''),
            bank_name=data.get('bank_name', ''),
            bank_branch=data.get('bank_branch', ''),
            bank_ifsc=data.get('bank_ifsc', ''),
            bank_address=data.get('bank_address', ''),
            professional_fee=data.get('professional_fee', ''),
            signature_s3_key=s3_key,
            mou_pdf_s3_key=pdf_s3_key,
            ip_address=ip_address,
        )

        # Mark user as MOU signed
        user.mou_signed = True
        user.save(update_fields=['mou_signed'])

        logger.info("MOU signed by user %s (user_id=%s, mou_id=%s)", user.mobile, user.id, mou.id)

        # Sync permanent MOU document URL to Zoho (non-blocking)
        if pdf_s3_key:
            try:
                from django.conf import settings as django_settings
                base_url = django_settings.BACKEND_BASE_URL.rstrip('/')
                permanent_url = f"{base_url}/api/auth/mou/{mou.view_token}/"
                ZohoService.update_doctor_mou(user.mobile, permanent_url)
            except Exception as e:
                logger.error("Failed to sync MOU to Zoho for user %s: %s", user.mobile, e)

        return Response({
            "message": "MOU signed successfully.",
            "mou_signed": True,
            "signed_at": mou.signed_at.isoformat(),
        }, status=status.HTTP_201_CREATED)


class MOUStatusView(views.APIView):
    """Check MOU signing status for authenticated user."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        latest_mou = user.mou_agreements.first()
        return Response({
            "mou_signed": user.mou_signed,
            "signed_at": latest_mou.signed_at.isoformat() if latest_mou else None,
        })


class MOUDocumentView(views.APIView):
    """
    Public redirect endpoint for MOU PDF access.
    No login required — the view_token UUID is the credential.
    Generates a fresh short-lived S3 presigned URL and redirects to it.
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request, token):
        from django.http import HttpResponseRedirect, Http404
        from apps.patients.s3_utils import get_s3_client
        from django.conf import settings as django_settings

        try:
            mou = MOUAgreement.objects.get(view_token=token)
        except MOUAgreement.DoesNotExist:
            raise Http404

        if not mou.mou_pdf_s3_key:
            return Response({"error": "MOU document not available."}, status=status.HTTP_404_NOT_FOUND)

        try:
            s3 = get_s3_client()
            url = s3.generate_presigned_url(
                'get_object',
                Params={
                    'Bucket': django_settings.AWS_STORAGE_BUCKET_NAME,
                    'Key': mou.mou_pdf_s3_key,
                    'ResponseContentDisposition': 'inline',
                    'ResponseContentType': 'application/pdf',
                },
                ExpiresIn=3600,  # 1-hour presigned URL, regenerated on each visit
            )
            return HttpResponseRedirect(url)
        except Exception as e:
            logger.error("MOUDocumentView: failed to generate presigned URL for token %s: %s", token, e)
            return Response({"error": "Could not retrieve document."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
