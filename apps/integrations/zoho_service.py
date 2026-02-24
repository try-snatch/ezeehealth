import requests
import os
import logging
from django.utils import timezone
from datetime import timedelta
from pathlib import Path
import json
from django.conf import settings
from .models import ZohoToken

logger = logging.getLogger(__name__)

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
            logger.error("Token endpoint unreachable: %s", e)
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
            logger.info("Refreshed Zoho access token via refresh_token.")
            return zoho_token, None
        logger.error("Refresh token flow failed. Status: %s. Body: %s", status, body)
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
            logger.debug("is_lead check for %s: %s", record_id, response.status_code)
            return response.status_code == 200
        except Exception as e:
            logger.error("Error checking if record is lead %s: %s", record_id, e)
            return False

    @staticmethod
    def is_deal(record_id):
        """Check if a record ID belongs to the Deals module"""
        try:
            url = f"{API_DOMAIN}/crm/v8/Deals/{record_id}"
            response = requests.get(url, headers=ZohoService.get_headers(), timeout=10)
            logger.debug("is_deal check for %s: %s", record_id, response.status_code)
            return response.status_code == 200
        except Exception as e:
            logger.error("Error checking if record is deal %s: %s", record_id, e)
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
                logger.warning("search_doctor(%s): no doctor found in Zoho", mobile)
            else:
                logger.error("search_doctor(%s): Zoho returned %s — %s", mobile, response.status_code, response.text[:500])
            return None
        except Exception as e:
            logger.error("search_doctor(%s): exception — %s", mobile, e)
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
            logger.error("Error in create_or_update_doctor: %s", e)
            return None

    # ==================== LEAD METHODS ====================

    @staticmethod
    def get_leads(doctor_mobile):
        """Fetch all Leads (referrals) for a doctor - Used for Dashboard"""
        doctor = ZohoService.search_doctor(doctor_mobile)
        if not doctor:
            logger.warning("get_leads: no doctor found for mobile %s — returning empty leads", doctor_mobile)
            return []

        doctor_id = doctor['id']

        try:
            url = f"{API_DOMAIN}/crm/v8/Leads/search"
            params = {"criteria": f"(Doctor_Name.id:equals:{doctor_id})"}

            headers = ZohoService.get_headers()
            response = requests.get(url, headers=headers, params=params)

            if response.status_code == 200:
                data = response.json().get("data", [])
                logger.info("get_leads: fetched %d leads from Zoho for doctor %s", len(data), doctor_mobile)
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

            logger.error("get_leads: Zoho returned %s for doctor %s — %s", response.status_code, doctor_mobile, response.text[:500])
            return []
        except Exception as e:
            logger.error("get_leads: exception for doctor %s — %s", doctor_mobile, e)
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
            logger.error("Error creating lead in Zoho: %s", e)
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
            logger.error("Error fetching lead %s: %s", lead_id, e)
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
                logger.warning("Lead conversion partial result for %s: %s", lead_id, data)
                return data if data else None

            logger.error("Lead conversion failed for %s: %s %s", lead_id, response.status_code, response.text[:500])
            return None
        except Exception as e:
            logger.error("Error converting lead %s: %s", lead_id, e)
            return None

    # ==================== DEAL/PATIENT METHODS ====================

    @staticmethod
    def get_patients(doctor_mobile):
        """Fetch all Deals (converted patients) for a doctor - Used for Patients Page"""
        doctor = ZohoService.search_doctor(doctor_mobile)
        if not doctor:
            logger.warning("get_patients: no doctor found for mobile %s — returning empty deals", doctor_mobile)
            return []

        doctor_id = doctor['id']

        try:
            url = f"{API_DOMAIN}/crm/v8/Deals/search"
            params = {"criteria": f"(Primary_Doctor.id:equals:{doctor_id})"}

            headers = ZohoService.get_headers()
            response = requests.get(url, headers=headers, params=params)

            if response.status_code == 200:
                data = response.json().get("data", [])
                logger.info("get_patients: fetched %d deals from Zoho for doctor %s", len(data), doctor_mobile)

                # Load stages mapping
                stages_map = {}
                try:
                    stages_path = Path(settings.BASE_DIR) / 'apps' / 'patients' / 'stages.json'
                    with open(stages_path, 'r') as f:
                        stages_data = json.load(f)
                        for item in stages_data.get('stages', []):
                            stages_map[item['stage']] = item['heading']
                except Exception as e:
                    logger.error("Error loading stages.json: %s", e)

                patients = []

                for deal in data:
                    contact_info = deal.get("Contact_Name")
                    contact_name = contact_info.get("name") if contact_info else deal.get("Deal_Name")

                    stage_name = deal.get("Stage")
                    mapped_status = stages_map.get(stage_name, stage_name)

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

            logger.error("get_patients: Zoho returned %s for doctor %s — %s", response.status_code, doctor_mobile, response.text[:500])
            return []
        except Exception as e:
            logger.error("get_patients: exception for doctor %s — %s", doctor_mobile, e)
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
            logger.error("Error fetching contact %s: %s", contact_id, e)
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
            logger.debug("Zoho update response for %s/%s: %s", module, record_id, resp.json())
            if resp.status_code in [200, 201]:
                return True
            logger.error("Zoho update_record failed for %s/%s: %s %s", module, record_id, resp.status_code, resp.text[:500])
            return False
        except Exception as e:
            logger.error("Error in update_record %s/%s: %s", module, record_id, e)
            return False

    @staticmethod
    def update_lead(lead_id, lead_data):
        """Update a Lead record"""
        return ZohoService.update_record("Leads", lead_id, lead_data)

    @staticmethod
    def update_deal(deal_id, deal_data):
        """Update a Deal record"""
        return ZohoService.update_record("Deals", deal_id, deal_data)

    # ==================== CONTACT METHODS (Patient Portal) ====================

    @staticmethod
    def search_contact_by_email(email):
        """Search Zoho Contacts by email."""
        try:
            url = f"{API_DOMAIN}/crm/v8/Contacts/search"
            params = {"criteria": f"(Email:equals:{email})"}
            resp = requests.get(url, headers=ZohoService.get_headers(), params=params, timeout=10)
            if resp.status_code == 200:
                return resp.json().get("data", [])
            return []
        except Exception as e:
            logger.error("Error searching contact by email: %s", e)
            return []

    @staticmethod
    def search_contact_by_phone(phone):
        """Search Zoho Contacts by mobile."""
        try:
            url = f"{API_DOMAIN}/crm/v8/Contacts/search"
            params = {"criteria": f"(Mobile:equals:{phone})"}
            resp = requests.get(url, headers=ZohoService.get_headers(), params=params, timeout=10)
            if resp.status_code == 200:
                return resp.json().get("data", [])
            return []
        except Exception as e:
            logger.error("Error searching contact by phone: %s", e)
            return []

    @staticmethod
    def create_contact(contact_data):
        """Create a new Zoho Contact. Returns the new contact's ID or None."""
        try:
            url = f"{API_DOMAIN}/crm/v8/Contacts"
            payload = {"data": [contact_data]}
            resp = requests.post(url, headers=ZohoService.get_headers(), json=payload, timeout=15)
            if resp.status_code in (200, 201):
                result = resp.json().get('data', [{}])[0]
                if result.get('status') == 'success':
                    return result.get('details', {}).get('id')
            logger.error("Zoho create_contact failed: %s %s", resp.status_code, resp.text[:500])
            return None
        except Exception as e:
            logger.error("Error creating contact: %s", e)
            return None

    @staticmethod
    def update_contact(contact_id, contact_data):
        """Update an existing Zoho Contact."""
        return ZohoService.update_record("Contacts", contact_id, contact_data)

    # ==================== DEAL METHODS (Patient Journey) ====================

    @staticmethod
    def get_deals_by_contact(contact_id):
        """Get all Deals associated with a Contact (patient journeys)."""
        try:
            url = f"{API_DOMAIN}/crm/v8/Contacts/{contact_id}/Deals"
            fields = (
                'Deal_Name,Contact_Name,Stage,Next_Step,Lead_Source,Description,Pipeline,'
                'id,Discharge_Doc_PDF,Record_Status__s,Discharge_Dt,Mobile,Email,'
                'Mapped_Registrations,Registration_Amount,Payment_Date,Appointment_Date_Time,'
                'Payment_Details,Treatment,Treatment_Category,Approx_Treatment_Duration_in_Days,'
                'Approx_Treatment_Cost,Marketing_Rep_Name,Ezeehealth_Staff,'
                'Create_Patient_Registration,Discharge_Status,Registered_SSH,'
                'Attendant_Mobile,Attendant_Name_3,Provisional_Diagnosis_3,'
                'Discharge_Bill_Amount_Details,Patient_Service_Exec_Mobile,'
                'Marketing_Exec_Mobile,Primary_Doctor'
            )
            params = {"fields": fields, "sort_by": "Created_Time", "sort_order": "desc"}
            resp = requests.get(url, headers=ZohoService.get_headers(), params=params, timeout=15)
            if resp.status_code == 200:
                return resp.json().get("data", [])
            return []
        except Exception as e:
            logger.error("Error fetching deals by contact %s: %s", contact_id, e)
            return []

    @staticmethod
    def get_deal(deal_id):
        """Fetch a single Deal by ID with full field set."""
        try:
            url = f"{API_DOMAIN}/crm/v8/Deals/{deal_id}"
            fields = (
                'Deal_Name,Contact_Name,Stage,Stage_History,Next_Step,Lead_Source,Description,'
                'Pipeline,id,Discharge_Doc_PDF,Record_Status__s,Discharge_Dt,Mobile,Email,'
                'Mapped_Registrations,Registration_Amount,Payment_Date,Appointment_Date_Time,'
                'Payment_Details,Treatment,Treatment_Category,Approx_Treatment_Duration_in_Days,'
                'Approx_Treatment_Cost,Marketing_Rep_Name,Ezeehealth_Staff,'
                'Create_Patient_Registration,Discharge_Status,Registered_SSH,'
                'Attendant_Mobile,Attendant_Name_3,Provisional_Diagnosis_3,'
                'Discharge_Bill_Amount_Details,Patient_Service_Exec_Mobile,'
                'Marketing_Exec_Mobile,Primary_Doctor'
            )
            params = {"fields": fields}
            resp = requests.get(url, headers=ZohoService.get_headers(), params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                return data[0] if data else None
            return None
        except Exception as e:
            logger.error("Error fetching deal %s: %s", deal_id, e)
            return None

    @staticmethod
    def get_deal_stage_history(deal_id):
        """Get stage history for a Deal."""
        try:
            url = f"{API_DOMAIN}/crm/v8/Deals/{deal_id}/Stage_History"
            params = {
                "fields": "Stage,Stage_Name,Modified_Time,Modified_By,From_Stage,To_Stage,Duration_in_Stage",
                "per_page": 200,
            }
            resp = requests.get(url, headers=ZohoService.get_headers(), params=params, timeout=10)
            if resp.status_code == 200:
                return resp.json().get("data", [])
            return []
        except Exception as e:
            logger.error("Error fetching deal stage history for %s: %s", deal_id, e)
            return []

    # ==================== EVENT METHODS (Meetings) ====================

    @staticmethod
    def get_events_for_contact(contact_zoho_id):
        """Fetch Zoho Events and filter for a specific contact."""
        try:
            url = f"{API_DOMAIN}/crm/v8/Events"
            params = {
                "fields": (
                    "Event_Title,Start_DateTime,End_DateTime,Venue,Participants,"
                    "What_Id,Who_Id,Owner,Description,Remind_At,All_day,"
                    "Created_Time,Modified_Time"
                ),
                "per_page": 200,
            }
            resp = requests.get(url, headers=ZohoService.get_headers(), params=params, timeout=15)
            if resp.status_code != 200:
                return []
            meetings = resp.json().get("data", [])
            user_meetings = []
            for m in meetings:
                who_id = m.get("Who_Id")
                if who_id and str(who_id.get("id")) == str(contact_zoho_id):
                    user_meetings.append(m)
                    continue
                participants = m.get("Participants") or []
                for p in participants:
                    if str(p.get("participant", "")) == str(contact_zoho_id):
                        user_meetings.append(m)
                        break
            return user_meetings
        except Exception as e:
            logger.error("Error fetching events for contact %s: %s", contact_zoho_id, e)
            return []

    # ==================== SSH / CORPORATE / DOCTORS (Patient Portal) ====================

    @staticmethod
    def get_ssh_details(ssh_name):
        """Get Super Specialty Hospital details by name."""
        try:
            url = f"{API_DOMAIN}/crm/v8/SSH/search"
            params = {
                "criteria": f"(Name:equals:'{ssh_name}')",
                "fields": (
                    "Name,Email,Secondary_Email,Phone_1,Phone_2,Treatments,"
                    "SPOC_1,SPOPC_1_Email,SPOC_2,SPOC_1_Mobile,SPOC_2_Email,"
                    "SPOC_2_Mobile,Decision_Maker_Name,SSH_Address,Type_of_Hospital"
                ),
            }
            resp = requests.get(url, headers=ZohoService.get_headers(), params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                return data[0] if data else None
            return None
        except Exception as e:
            logger.error("Error fetching SSH details for %s: %s", ssh_name, e)
            return None

    @staticmethod
    def get_corporate_by_email_domain(domain):
        """Find corporate record by email domain."""
        try:
            url = f"{API_DOMAIN}/crm/v8/Corporate/search"
            params = {
                "criteria": f"(Email_Domain:equals:{domain})",
                "fields": "Name,Email_Domain,Marketing_Rep,Industry_Type,Primary_Doctor_Name,id",
            }
            resp = requests.get(url, headers=ZohoService.get_headers(), params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                return data[0] if data else None
            return None
        except Exception as e:
            logger.error("Error fetching corporate for domain %s: %s", domain, e)
            return None

    @staticmethod
    def get_doctors_by_corporate(corporate_id):
        """Get all doctors associated with a corporate."""
        try:
            url = f"{API_DOMAIN}/crm/v8/Doctors/search"
            params = {
                "criteria": f"(Corporate:equals:{corporate_id})",
                "fields": "Name,Email,Phone,Mobile,Specialization,Experience_in_Years,Associated_Hospital,Consultation_Fees",
                "per_page": 200,
            }
            resp = requests.get(url, headers=ZohoService.get_headers(), params=params, timeout=10)
            if resp.status_code == 200:
                return resp.json().get("data", [])
            return []
        except Exception as e:
            logger.error("Error fetching corporate doctors for %s: %s", corporate_id, e)
            return []

    @staticmethod
    def get_ezeehealth_doctors():
        """Get all doctors under the Ezeehealth corporate entity."""
        try:
            corporate = ZohoService.get_corporate_by_email_domain("ezeehealth.ai")
            if not corporate:
                # Fallback: search by name
                url = f"{API_DOMAIN}/crm/v8/Corporate/search"
                params = {"criteria": "(Name:equals:Ezeehealth)", "fields": "Name,id"}
                resp = requests.get(url, headers=ZohoService.get_headers(), params=params, timeout=10)
                if resp.status_code == 200:
                    data = resp.json().get("data", [])
                    corporate = data[0] if data else None
            if not corporate:
                return []
            return ZohoService.get_doctors_by_corporate(corporate.get("id"))
        except Exception as e:
            logger.error("Error fetching Ezeehealth doctors: %s", e)
            return []
