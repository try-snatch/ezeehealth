import requests
import os

class MSG91Service:
    @staticmethod
    def send_otp(mobile, otp):
        """
        Send OTP using MSG91 API.
        """
        # Ensure mobile number has country code (defaulting to 91 for India)
        if len(mobile) == 10:
            mobile = "91" + mobile

        msg91_api_key = os.getenv("MSG91_API_KEY")
        msg91_template_id = os.getenv("MSG91_TEMPLATE_ID")

        if not all([msg91_api_key, msg91_template_id]):
            print(f"MSG91 credentials missing: key = {msg91_api_key},template = {msg91_template_id}")
            return False

        url = "https://api.msg91.com/api/v5/otp"
        params = {
            "authkey": msg91_api_key,
            "template_id": msg91_template_id,
            "mobile": mobile,
            "otp": otp,
            "otp_length": 6,
        }

        try:
            response = requests.get(url, params=params)
            if response.status_code == 200:
                return True
            print(f"MSG91 Error: {response.text}")
            return False
        except Exception as e:
            print(f"Error sending OTP via MSG91: {e}")
            return False

    @staticmethod
    def send_sms(mobile, message):
        """
        Send a transactional SMS using MSG91 Flow API.
        Requires MSG91_INVITE_TEMPLATE_ID env var with a pre-approved template.
        """
        if not mobile:
            print("MSG91 send_sms: no mobile number provided")
            return False

        if len(mobile) == 10:
            mobile = "91" + mobile

        msg91_api_key = os.getenv("MSG91_API_KEY")
        template_id = os.getenv("MSG91_INVITE_TEMPLATE_ID")

        if not all([msg91_api_key, template_id]):
            # Log the message for manual inspection when template not configured
            print(f"MSG91 invite SMS (template not configured): To={mobile} | {message}")
            return False

        url = "https://api.msg91.com/api/v5/flow/"
        headers = {
            "authkey": msg91_api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "template_id": template_id,
            "short_url": 0,
            "recipients": [
                {
                    "mobiles": mobile,
                    "message": message,
                }
            ]
        }

        try:
            response = requests.post(url, json=payload, headers=headers)
            if response.status_code == 200:
                return True
            print(f"MSG91 send_sms Error: {response.text}")
            return False
        except Exception as e:
            print(f"Error sending SMS via MSG91: {e}")
            return False
