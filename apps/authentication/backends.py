# authentication/backends.py
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model

User = get_user_model()

class EmailOrMobileBackend(ModelBackend):
    """
    Authenticate with email OR mobile OR username (for admin).
    Does not allow authentication for pending accounts.
    """
    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None or password is None:
            return None

        user = None

        # Try email first (if contains @)
        if '@' in username:
            try:
                user = User.objects.get(email__iexact=username.lower())
            except User.DoesNotExist:
                pass

        # Fall back to mobile
        if user is None:
            try:
                user = User.objects.get(mobile=username)
            except User.DoesNotExist:
                return None

        # Check if account is pending
        if hasattr(user, 'account_status') and user.account_status == 'pending':
            return None

        # Verify password and user can authenticate
        if user.check_password(password) and self.user_can_authenticate(user):
            return user

        return None
