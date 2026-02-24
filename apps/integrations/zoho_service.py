import requests
import os
from django.utils import timezone
from datetime import timedelta
from pathlib import Path
import json
from django.conf import settings
from .models import ZohoToken

DEFAULT_TOKEN_LIFETIME = 3600
ACCOUNTS_URL = os.getenv("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.in")
API_DOMAIN = os.getenv("ZOHO_API_DOMAIN", "https://www.zohoapis.in")


class ZohoService:

    # ==================== TOKEN MANAGEMENT ====================

    @staticmethod
    def _post_token(payload):
        url = f"{ACCOUNTS_URL}/oauth/v2/token"
        try:
            resp = requests.post(url, data=payload, timeout=10)
            try:
                body = resp.json()
            except Exception:
                body = {"error": "non-json-response", "text": resp.text}
            return resp.status_code, body
        except Exception as e:
            print(f"ERROR: Token endpoint unreachable: {e}")
            return None, {"error": "network_error", "exception": str(e)}

    @staticmethod
    def _save_token_from_response(zoho_token, body):
        now = timezone.now()
        if "access_token" in body:
            zoho_token.access_token = body["access_token"]
        if "refresh_token" in body and body["refresh_token"]:
            zoho_token.refresh_token = body["refresh_token"]
        zoho_token.token_issued_time = now
        try:
            expires_in = int(body.get("expires_in", DEFAULT_TOKEN_LIFETIME))
        except Exception:
            expires_in = DEFAULT_TOKEN_LIFETIME
        if hasattr(zoho_token, "expires_in"):
            zoho_token.expires_in = expires_in
        zoho_token.save()
        return zoho_token

    @staticmethod
    def _refresh_with_refresh_token(zoho_token):
        if not zoho_token or not zoho_token.refresh_token:
            return None, "no_refresh_token"

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": zoho_token.refresh_token,
            "client_id": os.getenv("ZOHO_CLIENT_ID", "").strip().strip("'").strip('"'),
            "client_secret": os.getenv("ZOHO_CLIENT_SECRET", "").strip().strip("'").strip('"'),
        }
        status, body = ZohoService._post_token(payload)
        if status == 200 and "access_token" in body:
            ZohoService._save_token_from_response(zoho_token, body)
            print("DEBUG: Refreshed access token via refresh_token.")
            return zoho_token, None
        print(f"ERROR: Refresh token flow failed. Status: {status}. Body: {body}")
        return None, body

    @staticmethod
    def _exchange_auth_code_flow():
        zoho_code = os.getenv("ZOHO_AUTH_CODE", "").strip().strip("'").strip('"')
        client_id = os.getenv("ZOHO_CLIENT_ID", "").strip().strip("'").strip('"')
        client_secret = os.getenv("ZOHO_CLIENT_SECRET", "").strip().strip("'").strip('"')
        redirect_uri = os.getenv("ZOHO_REDIRECT_URI", "").strip().strip("'").strip('"')

        if not all([zoho_code, client_id, client_secret]):
            return None, "missing_credentials"

        payload = {
            "grant_type": "authorization_code",
            "code": zoho_code,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if redirect_uri:
            payload["redirect_uri"] = redirect_uri

        status, body = ZohoService._post_token(payload)
        if status == 200 and "access_token" in body:
            zoho_token = ZohoToken.objects.create(
                access_token=body["access_token"],
                refresh_token=body.get("refresh_token", ""),
                token_issued_time=timezone.now()
            )
            try:
                expires_in = int(body.get("expires_in", DEFAULT_TOKEN_LIFETIME))
                if hasattr(zoho_token, "expires_in"):
                    zoho_token.expires_in = expires_in
                    zoho_token.save()
            except Exception:
                pass
            return zoho_token, None

        return None, body

    @staticmethod
    def _generate_token():
        zoho_token = ZohoToken.objects.first()
        if zoho_token and zoho_token.refresh_token:
            refreshed, err = ZohoService._refresh_with_refresh_token(zoho_token)
            if refreshed:
                return refreshed

        env_refresh = os.getenv("ZOHO_REFRESH_TOKEN", "").strip().strip("'").strip('"')
        if env_refresh and (not zoho_token or not zoho_token.refresh_token):
            if not zoho_token:
                zoho_token = ZohoToken.objects.create(
                    access_token="",
                    refresh_token=env_refresh,
                    token_issued_time=timezone.now()
                )
            else:
                zoho_token.refresh_token = env_refresh
                zoho_token.save()

            refreshed, err = ZohoService._refresh_with_refresh_token(zoho_token)
            if refreshed:
                return refreshed

        exchanged_token, err = ZohoService._exchange_auth_code_flow()
        if exchanged_token:
            return exchanged_token

        return None

    @staticmethod
    def get_access_token():
        zoho_token = ZohoToken.objects.first()
        if not zoho_token:
            zoho_token = ZohoService._generate_token()
            if not zoho_token:
                return None

        now = timezone.now()
        expires_in = DEFAULT_TOKEN_LIFETIME
        if hasattr(zoho_token, "expires_in") and zoho_token.expires_in:
            expires_in = int(zoho_token.expires_in)

        expiry = zoho_token.token_issued_time + timedelta(seconds=expires_in)

        if now >= expiry:
            refreshed, err = ZohoService._refresh_with_refresh_token(zoho_token)
            if not refreshed:
                return None
            zoho_token = refreshed

        return zoho_token.access_token

    @staticmethod
    def get_headers():
        token = ZohoService.get_access_token()
        if not token:
            raise Exception("Could not retrieve Zoho Access Token")
        return {"Authorization": f"Zoho-oauthtoken {token}"}

    # ==================== RECORD TYPE CHECK ====================

    @staticmethod
    def is_lead(record_id):
        """Check if a record ID belongs to the Leads module"""
        try:
            url = f"{API_DOMAIN}/crm/v8/Leads/{record_id}"
            response = requests.get(url, headers=ZohoService.get_headers(), timeout=10)
            print(f"DEBUG: is_lead check for {record_id}: {response.status_code}")
            return response.status_code == 200
        except Exception as e:
            print(f"Error checking if record is lead: {e}")
            return False

    @staticmethod
    def is_deal(record_id):
        """Check if a record ID belongs to the Deals module"""
        try:
            url = f"{API_DOMAIN}/crm/v8/Deals/{record_id}"
            response = requests.get(url, headers=ZohoService.get_headers(), timeout=10)
            print(f"DEBUG: is_deal check for {record_id}: {response.status_code}")
            return response.status_code == 200
        except Exception as e:
            print(f"Error checking if record is deal: {e}")
            return False

    @staticmethod
    def get_record_type(record_id):
        """
        Determine if a record is a Lead or Deal.
        Returns: 'lead', 'deal', or None if not found
        """
        if ZohoService.is_lead(record_id):
            return 'lead'
        if ZohoService.is_deal(record_id):
            return 'deal'
        return None

    # ==================== DOCTOR METHODS ====================

    @staticmethod
    def search_doctor(mobile):
        try:
            url = f"{API_DOMAIN}/crm/v8/Doctors/search"
            params = {"criteria": f"(Mobile:equals:'{mobile}')"}
            response = requests.get(url, headers=ZohoService.get_headers(), params=params)

            if response.status_code == 200:
                data = response.json().get("data", [])
                if data:
                    doctor = data[0]
                    return {
                        "id": doctor.get("id"),
                        "full_name": doctor.get("Name"),
                        "email": doctor.get("Email"),
                        "phone": doctor.get("Mobile"),
                        "registration_no": doctor.get("Registration_No"),
                        "clinic_name": doctor.get("Clinic_Name")
                    }
            return None
        except Exception as e:
            print(f"Error searching doctor: {e}")
            return None

    @staticmethod
    def create_or_update_doctor(doctor_data):
        mobile = doctor_data.get('Mobile')
        if not mobile:
            return None

        try:
            url_search = f"{API_DOMAIN}/crm/v8/Doctors/search"
            params = {"criteria": f"(Mobile:equals:'{mobile}')"}
            headers = ZohoService.get_headers()

            response = requests.get(url_search, headers=headers, params=params)

            existing_id = None
            if response.status_code == 200:
                data = response.json().get("data", [])
                if data:
                    existing_id = data[0].get("id")

            payload = {"data": [doctor_data]}

            if existing_id:
                url_update = f"{API_DOMAIN}/crm/v8/Doctors/{existing_id}"
                resp = requests.put(url_update, headers=headers, json=payload)
                if resp.status_code in [200, 201]:
                    return resp.json().get('data', [{}])[0].get('details', {}).get('id', existing_id)
                return existing_id
            else:
                url_create = f"{API_DOMAIN}/crm/v8/Doctors"
                resp = requests.post(url_create, headers=headers, json=payload)
                if resp.status_code in [200, 201]:
                    return resp.json().get('data', [{}])[0].get('details', {}).get('id')
                return None

        except Exception as e:
            print(f"Error in create_or_update_doctor: {e}")
            return None

    # ==================== LEAD METHODS ====================

    @staticmethod
    def get_leads(doctor_mobile):
        """Fetch all Leads (referrals) for a doctor - Used for Dashboard"""
        doctor = ZohoService.search_doctor(doctor_mobile)
        if not doctor:
            return []

        doctor_id = doctor['id']

        try:
            url = f"{API_DOMAIN}/crm/v8/Leads/search"
            params = {"criteria": f"(Doctor_Name.id:equals:{doctor_id})"}

            headers = ZohoService.get_headers()
            response = requests.get(url, headers=headers, params=params)

            if response.status_code == 200:
                data = response.json().get("data", [])
                leads = []

                for lead in data:
                    full_name = lead.get('Full_Name') or lead.get('Last_Name') or ''
                    if not full_name:
                        first = lead.get('First_Name', '') or ''
                        last = lead.get('Last_Name', '') or ''
                        full_name = f"{first} {last}".strip() or 'Unknown'

                    leads.append({
                        "id": lead.get("id"),
                        "full_name": full_name,
                        "email": lead.get("Email") or "",
                        "phone": lead.get("Mobile") or "",
                        "diagnosis": lead.get("Provisional_Diagnosis") or lead.get("Description") or "",
                        "status": "Referred",
                        "age": lead.get("Age") or "",
                        "gender": lead.get("Gender") or "",
                        "date": lead.get("Modified_Time") or lead.get("Created_Time") or "",
                        "specialty": lead.get("Suggested_SSHs") or "",
                        "hospital": lead.get("Suggested_SSHs") or "",
                        "source": "lead",
                    })
                return leads

            return []
        except Exception as e:
            print(f"Error fetching leads from Zoho: {e}")
            return []

    @staticmethod
    def create_lead(lead_data):
        """Creates a new Lead in Zoho CRM"""
        try:
            url = f"{API_DOMAIN}/crm/v8/Leads"

            name = lead_data.get('name') or lead_data.get('full_name', 'Unknown')

            payload = {
                "data": [
                    {
                        "Last_Name": name,
                        "Full_Name": name,
                        "Mobile": lead_data.get('phone'),
                        "Doctor_Name": lead_data.get('doctor_id'),
                        "Description": lead_data.get('diagnosis'),
                        "Provisional_Diagnosis": lead_data.get('diagnosis'),
                        "Suggested_SSHs": lead_data.get('suggested_specialty'),
                        "Email": lead_data.get('email'),
                        "Age": lead_data.get('age'),
                        "Gender": lead_data.get('gender'),
                        "Lead_Source": "Referral App",
                    }
                ]
            }

            response = requests.post(url, headers=ZohoService.get_headers(), json=payload)

            if response.status_code in [200, 201]:
                data = response.json()
                result = data.get('data', [{}])[0]
                if result.get('status') == 'success':
                    return result.get('details', {}).get('id')
            return None

        except Exception as e:
            print(f"Error creating lead in Zoho: {e}")
            return None

    @staticmethod
    def get_lead(lead_id):
        """Fetch a single Lead by ID"""
        try:
            url = f"{API_DOMAIN}/crm/v8/Leads/{lead_id}"
            response = requests.get(url, headers=ZohoService.get_headers(), timeout=10)
            if response.status_code == 200:
                data = response.json().get("data", [])
                if data:
                    return data[0]
            return None
        except Exception as e:
            print(f"Error fetching lead {lead_id}: {e}")
            return None

    @staticmethod
    def convert_lead_to_contact_and_deal(lead_id):
        """
        Convert a Zoho Lead into a Contact + Deal.
        Uses Zoho's built-in lead conversion API.
        Returns: dict with 'contact_id' and 'deal_id', or None on failure.
        """
        try:
            url = f"{API_DOMAIN}/crm/v8/Leads/{lead_id}/actions/convert"
            payload = {
                "data": [
                    {
                        "overwrite": True,
                        "notify_lead_owner": True,
                        "notify_new_entity_owner": True,
                        "Deals": {
                            "Deal_Name": f"Converted from Lead {lead_id}",
                            "Stage": "Positive Enquiry",
                            "Pipeline": "Standard",
                        }
                    }
                ]
            }
            headers = ZohoService.get_headers()
            response = requests.post(url, headers=headers, json=payload, timeout=15)

            if response.status_code == 200:
                data = response.json().get("data", [{}])[0]
                contact_id = data.get("Contacts")
                deal_id = data.get("Deals")
                if contact_id and deal_id:
                    return {"contact_id": contact_id, "deal_id": deal_id}
                print(f"DEBUG: Lead conversion partial result: {data}")
                return data if data else None

            print(f"ERROR: Lead conversion failed: {response.status_code} {response.text}")
            return None
        except Exception as e:
            print(f"Error converting lead {lead_id}: {e}")
            return None

    # ==================== DEAL/PATIENT METHODS ====================

    @staticmethod
    def get_patients(doctor_mobile):
        """Fetch all Deals (converted patients) for a doctor - Used for Patients Page"""
        doctor = ZohoService.search_doctor(doctor_mobile)
        if not doctor:
            return []

        doctor_id = doctor['id']

        try:
            url = f"{API_DOMAIN}/crm/v8/Deals/search"
            params = {"criteria": f"(Primary_Doctor.id:equals:{doctor_id})"}

            headers = ZohoService.get_headers()
            response = requests.get(url, headers=headers, params=params)

            if response.status_code == 200:
                data = response.json().get("data", [])

                # Load stages mapping
                stages_map = {}
                try:
                    stages_path = Path(settings.BASE_DIR) / 'apps' / 'patients' / 'stages.json'
                    with open(stages_path, 'r') as f:
                        stages_data = json.load(f)
                        for item in stages_data.get('stages', []):
                            stages_map[item['stage']] = item['heading']
                except Exception as e:
                    print(f"Error loading stages.json: {e}")

                patients = []

                for deal in data:
                    contact_info = deal.get("Contact_Name")
                    contact_name = contact_info.get("name") if contact_info else deal.get("Deal_Name")

                    stage_name = deal.get("Stage")
                    mapped_status = stages_map.get(stage_name, stage_name)

                    print(f'Deal: {deal}')

                    patients.append({
                        "id": deal.get("id"),
                        "contact_id": contact_info.get("id") if contact_info else None,
                        "full_name": contact_name or deal.get("Deal_Name"),
                        "phone": deal.get("Mobile") or "",
                        "age": deal.get("Age"),
                        "gender": deal.get("Gender"),
                        "status": mapped_status,
                        "diagnosis": deal.get("Provisional_Diagnosis_3") or deal.get("Description") or "",
                        "revenue": deal.get("Bill_Value", 0),
                        "date": deal.get("Last_Stage_Change_Time") or deal.get("Created_Time") or "",
                        "hospital": deal.get("Suggested_SSHs") or "",
                        "source": "deal",
                    })

                patients.sort(key=lambda x: x.get('date', ''), reverse=True)
                return patients

            return []
        except Exception as e:
            print(f"Error fetching patients from Zoho Deals: {e}")
            return []

    @staticmethod
    def get_contact(contact_id):
        """Fetch a single Contact by ID"""
        try:
            url = f"{API_DOMAIN}/crm/v8/Contacts/{contact_id}"
            response = requests.get(url, headers=ZohoService.get_headers(), timeout=10)
            if response.status_code == 200:
                data = response.json().get("data", [])
                if data:
                    return data[0]
            return None
        except Exception as e:
            print(f"Error fetching contact {contact_id}: {e}")
            return None

    @staticmethod
    def update_deal_stage(deal_id, stage):
        """Update a Deal's stage"""
        return ZohoService.update_record("Deals", deal_id, {"Stage": stage})

    # ==================== UPDATE METHODS ====================

    @staticmethod
    def update_record(module, record_id, record_data):
        """Generic update helper for a Zoho module record"""
        if not record_id:
            return False
        try:
            url = f"{API_DOMAIN}/crm/v8/{module}/{record_id}"
            payload = {"data": [record_data]}
            headers = ZohoService.get_headers()
            resp = requests.put(url, headers=headers, json=payload, timeout=10)
            print(f'DEBUG: Zoho update response for {module}/{record_id}: {resp.json()}')
            if resp.status_code in [200, 201]:
                return True
            print(f"Zoho update_record failed: {resp.status_code} {resp.text}")
            return False
        except Exception as e:
            print(f"Error in update_record: {e}")
            return False

    @staticmethod
    def update_lead(lead_id, lead_data):
        """Update a Lead record"""
        return ZohoService.update_record("Leads", lead_id, lead_data)

    @staticmethod
    def update_deal(deal_id, deal_data):
        """Update a Deal record"""
        return ZohoService.update_record("Deals", deal_id, deal_data)
