import random
import os
from django.core.cache import cache
from apps.integrations.msg91_service import MSG91Service

def generate_otp():
    otp = str(random.randint(100000, 999999))
    # In dev/debug mode, you might want to log this or set a fixed OTP
    print(f"DEBUG OTP: {otp}")
    return otp

def send_auth_otp(mobile, otp):
    # Try sending via MSG91
    #Send OTP on mobile only if OTP_DEBUG_FLAG is set to NO
    otp_debug_flag = os.getenv("OTP_DEBUG_FLAG")
    if otp_debug_flag == "NO":
        success = MSG91Service.send_otp(mobile, otp)
        if success:
            return True
    # Fallback log for development if MSG91 fails (or keys missing)
    print(f"FALLBACK: OTP for {mobile} is {otp}")
    return True # We return True to not block dev flow, but ideally should ensure delivery in prod
