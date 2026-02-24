"""
Email utilities for authentication flows.
Handles email verification, password reset, and staff invitations.
"""
import random
import secrets
from django.core.mail import send_mail
from django.conf import settings
from django.template.loader import render_to_string
from django.utils.html import strip_tags


def generate_verification_code():
    """Generate a 6-digit verification code."""
    return ''.join([str(random.randint(0, 9)) for _ in range(6)])


def generate_invitation_code():
    """Generate a 32-character secure invitation token."""
    return secrets.token_urlsafe(24)[:32]


def send_verification_email(email, code, user_name):
    """
    Send email verification code to user.

    Args:
        email: User's email address
        code: 6-digit verification code
        user_name: User's full name

    Returns:
        bool: True if email sent successfully, False otherwise
    """
    subject = 'Verify Your Email - EzeeHealth'

    html_message = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{
                font-family: Arial, sans-serif;
                line-height: 1.6;
                color: #333;
            }}
            .container {{
                max-width: 600px;
                margin: 0 auto;
                padding: 20px;
            }}
            .header {{
                background-color: #4F46E5;
                color: white;
                padding: 20px;
                text-align: center;
                border-radius: 5px 5px 0 0;
            }}
            .content {{
                background-color: #f9fafb;
                padding: 30px;
                border: 1px solid #e5e7eb;
            }}
            .code {{
                font-size: 32px;
                font-weight: bold;
                color: #4F46E5;
                letter-spacing: 8px;
                text-align: center;
                padding: 20px;
                background-color: white;
                border: 2px dashed #4F46E5;
                border-radius: 5px;
                margin: 20px 0;
            }}
            .footer {{
                text-align: center;
                margin-top: 20px;
                font-size: 12px;
                color: #6b7280;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Email Verification</h1>
            </div>
            <div class="content">
                <p>Hello {user_name},</p>
                <p>Thank you for registering with EzeeHealth. To complete your registration, please verify your email address using the code below:</p>
                <div class="code">{code}</div>
                <p>This code will expire in <strong>10 minutes</strong>.</p>
                <p>If you didn't request this verification, please ignore this email.</p>
            </div>
            <div class="footer">
                <p>&copy; 2026 EzeeHealth. All rights reserved.</p>
            </div>
        </div>
    </body>
    </html>
    """

    plain_message = f"""
    Hello {user_name},

    Thank you for registering with EzeeHealth. To complete your registration, please verify your email address using the code below:

    {code}

    This code will expire in 10 minutes.

    If you didn't request this verification, please ignore this email.

    (c) 2026 EzeeHealth. All rights reserved.
    """

    try:
        send_mail(
            subject=subject,
            message=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            html_message=html_message,
            fail_silently=False,
        )
        return True
    except Exception as e:
        print(f"Error sending verification email: {e}")
        return False


def send_password_reset_email(email, code, user_name):
    """
    Send password reset code to user.

    Args:
        email: User's email address
        code: 6-digit reset code
        user_name: User's full name

    Returns:
        bool: True if email sent successfully, False otherwise
    """
    subject = 'Reset Your Password - EzeeHealth'

    html_message = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{
                font-family: Arial, sans-serif;
                line-height: 1.6;
                color: #333;
            }}
            .container {{
                max-width: 600px;
                margin: 0 auto;
                padding: 20px;
            }}
            .header {{
                background-color: #DC2626;
                color: white;
                padding: 20px;
                text-align: center;
                border-radius: 5px 5px 0 0;
            }}
            .content {{
                background-color: #f9fafb;
                padding: 30px;
                border: 1px solid #e5e7eb;
            }}
            .code {{
                font-size: 32px;
                font-weight: bold;
                color: #DC2626;
                letter-spacing: 8px;
                text-align: center;
                padding: 20px;
                background-color: white;
                border: 2px dashed #DC2626;
                border-radius: 5px;
                margin: 20px 0;
            }}
            .warning {{
                background-color: #FEF2F2;
                border-left: 4px solid #DC2626;
                padding: 10px;
                margin: 20px 0;
            }}
            .footer {{
                text-align: center;
                margin-top: 20px;
                font-size: 12px;
                color: #6b7280;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Password Reset Request</h1>
            </div>
            <div class="content">
                <p>Hello {user_name},</p>
                <p>We received a request to reset your password. Use the code below to reset your password:</p>
                <div class="code">{code}</div>
                <p>This code will expire in <strong>10 minutes</strong>.</p>
                <div class="warning">
                    <strong>Security Note:</strong> If you didn't request a password reset, please ignore this email and ensure your account is secure.
                </div>
            </div>
            <div class="footer">
                <p>&copy; 2026 EzeeHealth. All rights reserved.</p>
            </div>
        </div>
    </body>
    </html>
    """

    plain_message = f"""
    Hello {user_name},

    We received a request to reset your password. Use the code below to reset your password:

    {code}

    This code will expire in 10 minutes.

    Security Note: If you didn't request a password reset, please ignore this email and ensure your account is secure.

    (c) 2026 EzeeHealth. All rights reserved.
    """

    try:
        send_mail(
            subject=subject,
            message=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            html_message=html_message,
            fail_silently=False,
        )
        return True
    except Exception as e:
        print(f"Error sending password reset email: {e}")
        return False


def send_patient_invitation_email(email, invitation_code, patient_name, clinic_name, referred_by):
    """
    Send patient invitation email with account setup link (mirrors staff invitation).

    Args:
        email: Patient's email address
        invitation_code: Unique 32-char invitation token
        patient_name: Patient's full name
        clinic_name: Name of the referring clinic
        referred_by: Name of the doctor who referred the patient

    Returns:
        bool: True if email sent successfully, False otherwise
    """
    subject = f'Your Health Journey with {clinic_name} — EzeeHealth'

    setup_link = f"{settings.FRONTEND_URL}/patient/setup/{invitation_code}"

    html_message = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{
                font-family: Arial, sans-serif;
                line-height: 1.6;
                color: #333;
            }}
            .container {{
                max-width: 600px;
                margin: 0 auto;
                padding: 20px;
            }}
            .header {{
                background-color: #4F46E5;
                color: white;
                padding: 20px;
                text-align: center;
                border-radius: 5px 5px 0 0;
            }}
            .content {{
                background-color: #f9fafb;
                padding: 30px;
                border: 1px solid #e5e7eb;
            }}
            .button {{
                display: inline-block;
                padding: 12px 30px;
                background-color: #4F46E5;
                color: white;
                text-decoration: none;
                border-radius: 5px;
                margin: 20px 0;
                font-weight: bold;
            }}
            .info-box {{
                background-color: #EFF6FF;
                border-left: 4px solid #3B82F6;
                padding: 15px;
                margin: 20px 0;
            }}
            .footer {{
                text-align: center;
                margin-top: 20px;
                font-size: 12px;
                color: #6b7280;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>You've Been Referred!</h1>
            </div>
            <div class="content">
                <p>Hello {patient_name},</p>
                <p><strong>Dr. {referred_by}</strong> from <strong>{clinic_name}</strong> has referred you for specialised care and created your EzeeHealth patient account.</p>
                <div class="info-box">
                    <strong>What's Next?</strong>
                    <ol>
                        <li>Click the button below to activate your account</li>
                        <li>Create a secure password</li>
                        <li>Verify your mobile number with OTP</li>
                        <li>Track your health journey on EzeeHealth</li>
                    </ol>
                </div>
                <div style="text-align: center;">
                    <a href="{setup_link}" class="button">Activate My Account</a>
                </div>
                <p style="font-size: 12px; color: #6b7280;">
                    Or copy this link into your browser:<br>
                    <a href="{setup_link}">{setup_link}</a>
                </p>
                <p style="margin-top: 30px; font-size: 14px;">
                    <strong>Note:</strong> This invitation link will expire in <strong>30 days</strong>.
                </p>
            </div>
            <div class="footer">
                <p>&copy; 2026 EzeeHealth. All rights reserved.</p>
            </div>
        </div>
    </body>
    </html>
    """

    plain_message = f"""
    Hello {patient_name},

    Dr. {referred_by} from {clinic_name} has referred you for specialised care and created your EzeeHealth patient account.

    What's Next?
    1. Click the link below to activate your account
    2. Create a secure password
    3. Verify your mobile number with OTP
    4. Track your health journey on EzeeHealth

    Activate your account here:
    {setup_link}

    Note: This invitation link will expire in 30 days.

    (c) 2026 EzeeHealth. All rights reserved.
    """

    try:
        send_mail(
            subject=subject,
            message=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            html_message=html_message,
            fail_silently=False,
        )
        return True
    except Exception as e:
        print(f"Error sending patient invitation email: {e}")
        return False


def send_document_upload_link_email(email, token, patient_name, clinic_name, doctor_name):
    """
    Send document upload link email to patient.

    Args:
        email: Patient's email address
        token: Unique 32-char upload link token
        patient_name: Patient's full name
        clinic_name: Name of the clinic
        doctor_name: Name of the requesting doctor

    Returns:
        bool: True if email sent successfully, False otherwise
    """
    subject = f'{clinic_name} — Please Upload Your Medical Documents'

    upload_link = f"{settings.FRONTEND_URL}/document-upload/{token}"

    html_message = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{
                font-family: Arial, sans-serif;
                line-height: 1.6;
                color: #333;
            }}
            .container {{
                max-width: 600px;
                margin: 0 auto;
                padding: 20px;
            }}
            .header {{
                background-color: #4F46E5;
                color: white;
                padding: 20px;
                text-align: center;
                border-radius: 5px 5px 0 0;
            }}
            .content {{
                background-color: #f9fafb;
                padding: 30px;
                border: 1px solid #e5e7eb;
            }}
            .button {{
                display: inline-block;
                padding: 12px 30px;
                background-color: #4F46E5;
                color: white;
                text-decoration: none;
                border-radius: 5px;
                margin: 20px 0;
                font-weight: bold;
            }}
            .info-box {{
                background-color: #EFF6FF;
                border-left: 4px solid #3B82F6;
                padding: 15px;
                margin: 20px 0;
            }}
            .footer {{
                text-align: center;
                margin-top: 20px;
                font-size: 12px;
                color: #6b7280;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Upload Your Documents</h1>
            </div>
            <div class="content">
                <p>Hello {patient_name},</p>
                <p><strong>Dr. {doctor_name}</strong> from <strong>{clinic_name}</strong> has requested you to upload your medical documents.</p>
                <div class="info-box">
                    <strong>How it works:</strong>
                    <ol>
                        <li>Click the button below to open the upload page</li>
                        <li>Select your medical documents (PDF, JPG, PNG)</li>
                        <li>Choose a category for each document</li>
                        <li>Your doctor will be able to view them immediately</li>
                    </ol>
                </div>
                <div style="text-align: center;">
                    <a href="{upload_link}" class="button">Upload Documents</a>
                </div>
                <p style="font-size: 12px; color: #6b7280;">
                    Or copy this link into your browser:<br>
                    <a href="{upload_link}">{upload_link}</a>
                </p>
                <p style="margin-top: 30px; font-size: 14px;">
                    <strong>Note:</strong> This link will expire in <strong>7 days</strong>.
                </p>
            </div>
            <div class="footer">
                <p>&copy; 2026 EzeeHealth. All rights reserved.</p>
            </div>
        </div>
    </body>
    </html>
    """

    plain_message = f"""
    Hello {patient_name},

    Dr. {doctor_name} from {clinic_name} has requested you to upload your medical documents.

    How it works:
    1. Click the link below to open the upload page
    2. Select your medical documents (PDF, JPG, PNG)
    3. Choose a category for each document
    4. Your doctor will be able to view them immediately

    Upload your documents here:
    {upload_link}

    Note: This link will expire in 7 days.

    (c) 2026 EzeeHealth. All rights reserved.
    """

    try:
        send_mail(
            subject=subject,
            message=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            html_message=html_message,
            fail_silently=False,
        )
        return True
    except Exception as e:
        print(f"Error sending document upload link email: {e}")
        return False


def send_staff_invitation_email(email, invitation_code, staff_name, clinic_name, invited_by):
    """
    Send staff invitation email with account setup link.

    Args:
        email: Staff member's email address
        invitation_code: Unique invitation token
        staff_name: Staff member's full name
        clinic_name: Name of the clinic
        invited_by: Name of the person who sent the invitation

    Returns:
        bool: True if email sent successfully, False otherwise
    """
    subject = f'Invitation to Join {clinic_name} - EzeeHealth'

    setup_link = f"{settings.FRONTEND_URL}/staff/setup/{invitation_code}"

    html_message = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{
                font-family: Arial, sans-serif;
                line-height: 1.6;
                color: #333;
            }}
            .container {{
                max-width: 600px;
                margin: 0 auto;
                padding: 20px;
            }}
            .header {{
                background-color: #10B981;
                color: white;
                padding: 20px;
                text-align: center;
                border-radius: 5px 5px 0 0;
            }}
            .content {{
                background-color: #f9fafb;
                padding: 30px;
                border: 1px solid #e5e7eb;
            }}
            .button {{
                display: inline-block;
                padding: 12px 30px;
                background-color: #10B981;
                color: white;
                text-decoration: none;
                border-radius: 5px;
                margin: 20px 0;
                font-weight: bold;
            }}
            .info-box {{
                background-color: #EFF6FF;
                border-left: 4px solid #3B82F6;
                padding: 15px;
                margin: 20px 0;
            }}
            .footer {{
                text-align: center;
                margin-top: 20px;
                font-size: 12px;
                color: #6b7280;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>You're Invited!</h1>
            </div>
            <div class="content">
                <p>Hello {staff_name},</p>
                <p><strong>{invited_by}</strong> has invited you to join the <strong>{clinic_name}</strong> team on EzeeHealth.</p>
                <div class="info-box">
                    <strong>What's Next?</strong>
                    <ol>
                        <li>Click the button below to set up your account</li>
                        <li>Create a secure password</li>
                        <li>Verify your mobile number with OTP</li>
                        <li>Start managing patient referrals</li>
                    </ol>
                </div>
                <div style="text-align: center;">
                    <a href="{setup_link}" class="button">Set Up My Account</a>
                </div>
                <p style="font-size: 12px; color: #6b7280;">
                    Or copy this link into your browser:<br>
                    <a href="{setup_link}">{setup_link}</a>
                </p>
                <p style="margin-top: 30px; font-size: 14px;">
                    <strong>Note:</strong> This invitation link will expire in <strong>7 days</strong>.
                </p>
            </div>
            <div class="footer">
                <p>&copy; 2026 EzeeHealth. All rights reserved.</p>
            </div>
        </div>
    </body>
    </html>
    """

    plain_message = f"""
    Hello {staff_name},

    {invited_by} has invited you to join the {clinic_name} team on EzeeHealth.

    What's Next?
    1. Click the link below to set up your account
    2. Create a secure password
    3. Verify your mobile number with OTP
    4. Start managing patient referrals

    Set up your account here:
    {setup_link}

    Note: This invitation link will expire in 7 days.

    (c) 2026 EzeeHealth. All rights reserved.
    """

    try:
        send_mail(
            subject=subject,
            message=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            html_message=html_message,
            fail_silently=False,
        )
        return True
    except Exception as e:
        print(f"Error sending staff invitation email: {e}")
        return False
