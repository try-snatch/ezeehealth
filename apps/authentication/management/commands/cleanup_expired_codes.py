"""
Django management command to clean up expired verification codes and invitations.
Run this command periodically via cron (e.g., every hour).

Usage:
    python manage.py cleanup_expired_codes
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.conf import settings
from apps.authentication.models import User


class Command(BaseCommand):
    help = 'Clean up expired verification codes and invitations'

    def handle(self, *args, **options):
        now = timezone.now()

        # Clean up expired email verification codes (10 minutes)
        email_verification_cutoff = now - timezone.timedelta(seconds=settings.EMAIL_VERIFICATION_EXPIRY)
        expired_email_verifications = User.objects.filter(
            email_verification_code__isnull=False,
            email_verification_sent_at__lt=email_verification_cutoff
        )
        email_count = expired_email_verifications.count()
        expired_email_verifications.update(
            email_verification_code=None,
            email_verification_sent_at=None
        )
        self.stdout.write(self.style.SUCCESS(f'Cleared {email_count} expired email verification codes'))

        # Clean up expired password reset codes (10 minutes)
        password_reset_cutoff = now - timezone.timedelta(seconds=settings.PASSWORD_RESET_EXPIRY)
        expired_password_resets = User.objects.filter(
            password_reset_code__isnull=False,
            password_reset_sent_at__lt=password_reset_cutoff
        )
        reset_count = expired_password_resets.count()
        expired_password_resets.update(
            password_reset_code=None,
            password_reset_sent_at=None
        )
        self.stdout.write(self.style.SUCCESS(f'Cleared {reset_count} expired password reset codes'))

        # Deactivate expired staff invitations (7 days)
        invitation_cutoff = now - timezone.timedelta(seconds=settings.INVITATION_EXPIRY)
        expired_invitations = User.objects.filter(
            account_status='pending',
            invitation_code__isnull=False,
            invitation_sent_at__lt=invitation_cutoff
        )
        invitation_count = expired_invitations.count()
        expired_invitations.update(
            account_status='inactive',
            invitation_code=None,
            invitation_sent_at=None
        )
        self.stdout.write(self.style.SUCCESS(f'Deactivated {invitation_count} expired staff invitations'))

        # Summary
        total_cleaned = email_count + reset_count + invitation_count
        if total_cleaned == 0:
            self.stdout.write(self.style.SUCCESS('No expired codes found'))
        else:
            self.stdout.write(self.style.SUCCESS(f'Cleanup complete: {total_cleaned} items processed'))
