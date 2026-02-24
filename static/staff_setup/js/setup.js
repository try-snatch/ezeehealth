// Staff Setup Page JavaScript
(function() {
    'use strict';

    // State
    let invitationCode = '';
    let staffInfo = null;
    let otpIdentifier = '';

    // Get invitation code from URL
    function getInvitationCode() {
        const pathParts = window.location.pathname.split('/');
        const codeIndex = pathParts.indexOf('setup') + 1;
        return pathParts[codeIndex] || '';
    }

    // Show/hide steps
    function showStep(stepId) {
        const steps = ['step-loading', 'step-password', 'step-otp', 'step-success', 'step-error'];
        steps.forEach(id => {
            const el = document.getElementById(id);
            if (el) {
                if (id === stepId) {
                    el.classList.remove('hidden');
                    el.classList.add('fade-in');
                } else {
                    el.classList.add('hidden');
                }
            }
        });
    }

    // Show error
    function showError(message) {
        document.getElementById('error-message').textContent = message;
        showStep('step-error');
    }

    // API call helper
    async function apiCall(endpoint, data = null) {
        const options = {
            method: data ? 'POST' : 'GET',
            headers: {
                'Content-Type': 'application/json',
            },
        };

        if (data) {
            options.body = JSON.stringify(data);
        }

        const response = await fetch(endpoint, options);
        const result = await response.json();

        if (!response.ok) {
            throw new Error(result.error || result.message || 'Request failed');
        }

        return result;
    }

    // Step 1: Verify invitation
    async function verifyInvitation() {
        try {
            showStep('step-loading');

            const data = await apiCall('/api/auth/verify-invitation/', {
                invitation_code: invitationCode
            });

            staffInfo = data;

            // Update UI with staff info
            document.getElementById('welcome-message').textContent = `Welcome, ${data.staff_name}!`;
            document.getElementById('invitation-details').textContent =
                `You've been invited to join ${data.clinic_name} as a ${data.role}.`;

            showStep('step-password');
        } catch (error) {
            showError(error.message || 'Invalid or expired invitation code.');
        }
    }

    // Step 2: Setup password
    async function setupPassword(password) {
        try {
            const submitBtn = document.getElementById('submit-password');
            submitBtn.disabled = true;
            submitBtn.innerHTML = '<div class="spinner mx-auto"></div>';

            const data = await apiCall('/api/auth/staff/setup-account/', {
                invitation_code: invitationCode,
                password: password
            });

            otpIdentifier = data.identifier;
            document.getElementById('otp-mobile').textContent = data.identifier;

            showStep('step-otp');
        } catch (error) {
            document.getElementById('password-error').textContent = error.message;
            document.getElementById('password-error').classList.remove('hidden');
        } finally {
            const submitBtn = document.getElementById('submit-password');
            submitBtn.disabled = false;
            submitBtn.textContent = 'Continue to Mobile Verification';
        }
    }

    // Step 3: Verify OTP
    async function verifyOtp(otp) {
        try {
            const submitBtn = document.getElementById('submit-otp');
            submitBtn.disabled = true;
            submitBtn.innerHTML = '<div class="spinner mx-auto"></div>';

            await apiCall('/api/auth/verify-otp/', {
                identifier: otpIdentifier,
                otp: otp
            });

            // Show success screen
            document.getElementById('success-mobile').textContent = staffInfo.mobile;
            showStep('step-success');
        } catch (error) {
            document.getElementById('otp-error').textContent = error.message;
            document.getElementById('otp-error').classList.remove('hidden');

            const submitBtn = document.getElementById('submit-otp');
            submitBtn.disabled = false;
            submitBtn.textContent = 'Activate Account';
        }
    }

    // Initialize
    document.addEventListener('DOMContentLoaded', function() {
        invitationCode = getInvitationCode();

        if (!invitationCode) {
            showError('No invitation code provided.');
            return;
        }

        // Verify invitation on load
        verifyInvitation();

        // Password form
        const passwordForm = document.getElementById('password-form');
        const passwordInput = document.getElementById('password');
        const confirmPasswordInput = document.getElementById('confirm-password');
        const passwordError = document.getElementById('password-error');
        const confirmPasswordError = document.getElementById('confirm-password-error');

        // Toggle password visibility
        document.getElementById('toggle-password').addEventListener('click', function() {
            const input = document.getElementById('password');
            input.type = input.type === 'password' ? 'text' : 'password';
        });

        document.getElementById('toggle-confirm-password').addEventListener('click', function() {
            const input = document.getElementById('confirm-password');
            input.type = input.type === 'password' ? 'text' : 'password';
        });

        // Password validation
        passwordInput.addEventListener('input', function() {
            passwordError.classList.add('hidden');
            if (passwordInput.value.length > 0 && passwordInput.value.length < 8) {
                passwordError.textContent = 'Password must be at least 8 characters';
                passwordError.classList.remove('hidden');
            }
        });

        confirmPasswordInput.addEventListener('input', function() {
            confirmPasswordError.classList.add('hidden');
            if (confirmPasswordInput.value && confirmPasswordInput.value !== passwordInput.value) {
                confirmPasswordError.textContent = 'Passwords do not match';
                confirmPasswordError.classList.remove('hidden');
            }
        });

        passwordForm.addEventListener('submit', function(e) {
            e.preventDefault();

            const password = passwordInput.value;
            const confirmPassword = confirmPasswordInput.value;

            // Clear errors
            passwordError.classList.add('hidden');
            confirmPasswordError.classList.add('hidden');

            // Validate
            if (password.length < 8) {
                passwordError.textContent = 'Password must be at least 8 characters';
                passwordError.classList.remove('hidden');
                return;
            }

            if (password !== confirmPassword) {
                confirmPasswordError.textContent = 'Passwords do not match';
                confirmPasswordError.classList.remove('hidden');
                return;
            }

            setupPassword(password);
        });

        // OTP form
        const otpForm = document.getElementById('otp-form');
        const otpInput = document.getElementById('otp');
        const otpError = document.getElementById('otp-error');

        // Only allow digits in OTP
        otpInput.addEventListener('input', function(e) {
            this.value = this.value.replace(/\D/g, '').slice(0, 6);
            otpError.classList.add('hidden');
        });

        otpForm.addEventListener('submit', function(e) {
            e.preventDefault();

            const otp = otpInput.value;

            // Clear error
            otpError.classList.add('hidden');

            // Validate
            if (otp.length !== 6) {
                otpError.textContent = 'OTP must be 6 digits';
                otpError.classList.remove('hidden');
                return;
            }

            verifyOtp(otp);
        });
    });
})();
