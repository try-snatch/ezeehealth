"""
Rate limiting utilities for authentication flows.
Uses Django cache to track email sends and verification attempts.
"""
from django.core.cache import cache
from django.utils import timezone
from datetime import timedelta


def check_email_rate_limit(email, action='email', limit_seconds=60):
    """
    Check if an email action is rate limited.

    Args:
        email: Email address to check
        action: Action type (e.g., 'email_verification', 'password_reset')
        limit_seconds: Cooldown period in seconds

    Returns:
        tuple: (is_allowed: bool, wait_time: int) where wait_time is seconds remaining
    """
    cache_key = f"email_rate_limit:{action}:{email.lower()}"
    last_sent = cache.get(cache_key)

    if last_sent:
        elapsed = (timezone.now() - last_sent).total_seconds()
        if elapsed < limit_seconds:
            wait_time = int(limit_seconds - elapsed)
            return False, wait_time

    # Set the rate limit
    cache.set(cache_key, timezone.now(), timeout=limit_seconds)
    return True, 0


def check_code_attempt_limit(identifier, action='verification', max_attempts=5, window_minutes=10):
    """
    Check if code verification attempts are within limits.

    Args:
        identifier: Unique identifier (email, mobile, etc.)
        action: Action type (e.g., 'email_verification', 'password_reset', 'otp')
        max_attempts: Maximum allowed attempts
        window_minutes: Time window for attempts

    Returns:
        tuple: (is_allowed: bool, attempts_remaining: int, reset_time: int)
    """
    cache_key = f"code_attempts:{action}:{identifier.lower()}"
    attempts_data = cache.get(cache_key, {'count': 0, 'first_attempt': timezone.now()})

    # Check if window has expired
    elapsed = (timezone.now() - attempts_data['first_attempt']).total_seconds()
    if elapsed > (window_minutes * 60):
        # Reset the counter
        attempts_data = {'count': 0, 'first_attempt': timezone.now()}

    current_attempts = attempts_data['count']

    if current_attempts >= max_attempts:
        reset_time = int((window_minutes * 60) - elapsed)
        return False, 0, reset_time

    attempts_remaining = max_attempts - current_attempts
    return True, attempts_remaining, 0


def increment_failed_attempts(identifier, action='verification', max_attempts=5, window_minutes=10):
    """
    Increment failed verification attempt counter.

    Args:
        identifier: Unique identifier (email, mobile, etc.)
        action: Action type
        max_attempts: Maximum allowed attempts
        window_minutes: Time window for attempts

    Returns:
        int: Attempts remaining (0 if limit reached)
    """
    cache_key = f"code_attempts:{action}:{identifier.lower()}"
    attempts_data = cache.get(cache_key, {'count': 0, 'first_attempt': timezone.now()})

    # Check if window has expired
    elapsed = (timezone.now() - attempts_data['first_attempt']).total_seconds()
    if elapsed > (window_minutes * 60):
        # Reset the counter
        attempts_data = {'count': 1, 'first_attempt': timezone.now()}
    else:
        attempts_data['count'] += 1

    # Store for the full window duration
    cache.set(cache_key, attempts_data, timeout=window_minutes * 60)

    attempts_remaining = max(0, max_attempts - attempts_data['count'])
    return attempts_remaining


def clear_failed_attempts(identifier, action='verification'):
    """
    Clear failed attempt counter after successful verification.

    Args:
        identifier: Unique identifier (email, mobile, etc.)
        action: Action type
    """
    cache_key = f"code_attempts:{action}:{identifier.lower()}"
    cache.delete(cache_key)


def get_rate_limit_info(email, action='email'):
    """
    Get current rate limit status without modifying it.

    Args:
        email: Email address to check
        action: Action type

    Returns:
        dict: {'is_limited': bool, 'wait_time': int}
    """
    cache_key = f"email_rate_limit:{action}:{email.lower()}"
    last_sent = cache.get(cache_key)

    if not last_sent:
        return {'is_limited': False, 'wait_time': 0}

    elapsed = (timezone.now() - last_sent).total_seconds()
    if elapsed < 60:  # Default 60s limit
        return {
            'is_limited': True,
            'wait_time': int(60 - elapsed)
        }

    return {'is_limited': False, 'wait_time': 0}


def get_attempt_info(identifier, action='verification', max_attempts=5, window_minutes=10):
    """
    Get current attempt status without modifying it.

    Args:
        identifier: Unique identifier
        action: Action type
        max_attempts: Maximum allowed attempts
        window_minutes: Time window

    Returns:
        dict: {'is_limited': bool, 'attempts_remaining': int, 'reset_time': int}
    """
    cache_key = f"code_attempts:{action}:{identifier.lower()}"
    attempts_data = cache.get(cache_key)

    if not attempts_data:
        return {
            'is_limited': False,
            'attempts_remaining': max_attempts,
            'reset_time': 0
        }

    elapsed = (timezone.now() - attempts_data['first_attempt']).total_seconds()

    # Window expired
    if elapsed > (window_minutes * 60):
        return {
            'is_limited': False,
            'attempts_remaining': max_attempts,
            'reset_time': 0
        }

    current_attempts = attempts_data['count']
    attempts_remaining = max(0, max_attempts - current_attempts)

    if current_attempts >= max_attempts:
        return {
            'is_limited': True,
            'attempts_remaining': 0,
            'reset_time': int((window_minutes * 60) - elapsed)
        }

    return {
        'is_limited': False,
        'attempts_remaining': attempts_remaining,
        'reset_time': 0
    }
