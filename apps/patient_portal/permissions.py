from rest_framework.permissions import BasePermission


class IsPatientUser(BasePermission):
    """Only allows access to users with role='patient' whose account is active."""
    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        return user.role == 'patient' and user.account_status == 'active'


class IsPatientWithProfile(BasePermission):
    """Patient must have completed their profile."""
    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if user.role != 'patient' or user.account_status != 'active':
            return False
        return user.profile_completed
