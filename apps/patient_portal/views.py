"""
Patient Portal API Views.
All endpoints under /api/patient/
"""
import re
import json
import threading
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from rest_framework import views, status, permissions
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from apps.authentication.models import User
from apps.authentication.utils import generate_otp, send_auth_otp
from apps.integrations.zoho_service import ZohoService

from .models import (
    UploadedDocument, DocumentInsight, Dependant, DocumentShare,
    ChatSession, ChatMessage, Alert, PatientInvite,
)
from .permissions import IsPatientUser, IsPatientWithProfile
from .serializers import (
    PatientRegisterSerializer, PatientProfileSerializer,
    CompleteProfileSerializer, UpdateProfileSerializer,
    UploadedDocumentSerializer, DocumentInsightSerializer,
    DependantSerializer, CreateDependantSerializer,
    DocumentShareSerializer, ChatMessageSerializer, AlertSerializer,
)


# ==================== HELPERS ====================

class DependantMixin:
    """Mixin for views that optionally operate on a dependant."""

    def get_dependant(self):
        dependant_id = self.request.query_params.get('dependant_id')
        if not dependant_id:
            return None
        try:
            return Dependant.objects.get(id=dependant_id, patient=self.request.user)
        except Dependant.DoesNotExist:
            return None

    def get_zoho_contact_id(self):
        dep = self.get_dependant()
        if dep and dep.zoho_contact_id:
            return dep.zoho_contact_id
        return self.request.user.zoho_contact_id

    def get_document_owner_filter(self):
        """Return queryset filter kwargs for documents."""
        dep = self.get_dependant()
        if dep:
            return {'patient': self.request.user, 'dependant': dep}
        return {'patient': self.request.user, 'dependant__isnull': True}


def fill_placeholders(text, variables):
    """Replaces {{placeholders}} in text with values from variables dict."""
    def repl(match):
        key = match.group(1).strip()
        return str(variables.get(key, f"{{{{{key}}}}}"))
    return re.sub(r"{{(.*?)}}", repl, text)


# ==================== PHASE 1: AUTH ====================

class PatientRegisterView(views.APIView):
    """POST /api/patient/register/ — Patient signup."""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = PatientRegisterSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        name_first = data['first_name'].strip()
        name_last = data['last_name'].strip()

        user = User.objects.create_user(
            mobile=data['mobile'],
            first_name=name_first,
            last_name=name_last,
            email=data.get('email') or None,
            role='patient',
            account_status='pending',
            is_2fa_enabled=True,
        )
        user.set_password(data['password'])
        user.save(update_fields=['password'])

        otp = generate_otp()
        cache.set(f"otp_2fa_{user.id}", otp, timeout=300)
        if settings.DEBUG:
            print(f'[DEV OTP] Patient Register - mobile: {user.mobile}, otp: {otp}')
        send_auth_otp(user.mobile, otp)
        user.last_otp_sent_at = timezone.now()
        user.save(update_fields=['last_otp_sent_at'])

        return Response({
            "message": "Registration successful. OTP sent.",
            "identifier": user.mobile,
        }, status=status.HTTP_201_CREATED)


# ==================== PHASE 1: PROFILE ====================

class PatientMeView(views.APIView):
    """GET /api/patient/me/ — Patient profile."""
    permission_classes = [IsPatientUser]

    def get(self, request):
        data = PatientProfileSerializer(request.user).data

        # If linked to Zoho, fetch fresh contact data
        if request.user.zoho_contact_id:
            try:
                contact = ZohoService.get_contact(request.user.zoho_contact_id)
                if contact:
                    data['zoho_contact'] = contact
            except Exception as e:
                print(f"Error fetching Zoho contact: {e}")

        return Response(data)


class CompleteProfileView(views.APIView):
    """POST /api/patient/complete-profile/ — Complete onboarding + create Zoho Contact."""
    permission_classes = [IsPatientUser]

    def post(self, request):
        user = request.user
        if user.profile_completed:
            return Response({"error": "Profile already completed."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = CompleteProfileSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data

        # Corporate validation
        if data['lead_source'] == 'Corporate' and not user.is_corporate:
            return Response({"error": "Non-corporate users cannot set lead source to Corporate."},
                            status=status.HTTP_400_BAD_REQUEST)

        # If Corporate, auto-fill doctor from corporate record
        if data['lead_source'] == 'Corporate' and user.email:
            email_domain = user.email.split('@')[-1]
            corporate = ZohoService.get_corporate_by_email_domain(email_domain)
            if corporate:
                user.company_name = corporate.get('Name', '')
                user.primary_doctor_id = data.get('primary_doctor') or ''
                user.doctor_email = data.get('doctor_email') or ''
                user.doctor_mobile = data.get('doctor_mobile') or ''
            else:
                return Response({"error": "No corporate found for your email domain."},
                                status=status.HTTP_400_BAD_REQUEST)
        else:
            # Direct patient — must provide doctor info
            user.primary_doctor_id = data.get('primary_doctor', '')
            user.doctor_email = data.get('doctor_email', '')
            user.doctor_mobile = data.get('doctor_mobile', '')

        user.lead_source = data['lead_source']
        user.gender = data.get('gender', '')
        user.age_in_years = data.get('age_in_years')
        user.mailing_street = data.get('mailing_street', '')
        user.mailing_city = data.get('mailing_city', '')
        user.mailing_zip = data.get('mailing_zip', '')
        user.mailing_state = data.get('mailing_state', '')
        user.save()

        # Push to Zoho CRM as Contact
        contact_data = {
            "Last_Name": f"{user.first_name} {user.last_name}".strip(),
            "Email": user.email or '',
            "Mobile": user.mobile,
            "Lead_Source": user.lead_source,
            "Company": user.company_name or '',
            "Primary_Doctor": user.primary_doctor_id or '',
            "Doctor_Email": user.doctor_email or '',
            "Doctor_Mobile": user.doctor_mobile or '',
            "Gender": user.gender or '',
            "Age_in_Yrs": user.age_in_years,
            "Mailing_Street": user.mailing_street or '',
            "Mailing_City": user.mailing_city or '',
            "Mailing_Zip": user.mailing_zip or '',
            "Mailing_State": user.mailing_state or '',
        }

        zoho_contact_id = ZohoService.create_contact(contact_data)
        if zoho_contact_id:
            user.zoho_contact_id = zoho_contact_id
            user.profile_completed = True
            user.save(update_fields=['zoho_contact_id', 'profile_completed'])
            return Response({"message": "Profile completed and synced to Zoho."})
        else:
            return Response({"error": "Profile saved but Zoho sync failed."},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class UpdateProfileView(views.APIView):
    """PATCH /api/patient/update-profile/ — Update profile + sync to Zoho."""
    permission_classes = [IsPatientWithProfile]

    def patch(self, request):
        serializer = UpdateProfileSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        user = request.user
        data = serializer.validated_data

        for field in ['first_name', 'last_name', 'email', 'gender', 'age_in_years',
                      'mailing_street', 'mailing_city', 'mailing_zip', 'mailing_state']:
            if field in data:
                setattr(user, field, data[field])
        user.save()

        # Sync to Zoho
        if user.zoho_contact_id:
            zoho_data = {}
            if 'first_name' in data or 'last_name' in data:
                zoho_data['Last_Name'] = f"{user.first_name} {user.last_name}".strip()
            if 'email' in data:
                zoho_data['Email'] = data['email']
            if 'gender' in data:
                zoho_data['Gender'] = data['gender']
            if 'age_in_years' in data:
                zoho_data['Age_in_Yrs'] = data['age_in_years']
            for local, zoho in [('mailing_street', 'Mailing_Street'), ('mailing_city', 'Mailing_City'),
                                ('mailing_zip', 'Mailing_Zip'), ('mailing_state', 'Mailing_State')]:
                if local in data:
                    zoho_data[zoho] = data[local]
            if zoho_data:
                ZohoService.update_contact(user.zoho_contact_id, zoho_data)

        return Response(PatientProfileSerializer(user).data)


class UpdatePrimaryDoctorView(views.APIView):
    """POST /api/patient/update-primary-doctor/"""
    permission_classes = [IsPatientWithProfile]

    def post(self, request):
        doctor_id = request.data.get('doctor_id')
        doctor_email = request.data.get('doctor_email', '')
        doctor_mobile = request.data.get('doctor_mobile', '')

        if not doctor_id:
            return Response({"error": "doctor_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        user = request.user
        user.primary_doctor_id = doctor_id
        user.doctor_email = doctor_email
        user.doctor_mobile = doctor_mobile
        user.save(update_fields=['primary_doctor_id', 'doctor_email', 'doctor_mobile'])

        # Sync to Zoho
        if user.zoho_contact_id:
            ZohoService.update_contact(user.zoho_contact_id, {
                "Primary_Doctor": doctor_id,
                "Doctor_Email": doctor_email,
                "Doctor_Mobile": doctor_mobile,
            })

        return Response({"message": "Primary doctor updated."})


class SuggestedPrimaryDoctorView(views.APIView):
    """GET /api/patient/suggested-primary-doctor/?email=..."""
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        email = request.query_params.get('email', '')
        if not email or '@' not in email:
            return Response({"error": "Valid email is required."}, status=status.HTTP_400_BAD_REQUEST)

        domain = email.split('@')[-1]
        corporate = ZohoService.get_corporate_by_email_domain(domain)
        if not corporate:
            return Response({"is_corporate": False, "suggested_doctor": None})

        return Response({
            "is_corporate": True,
            "company_name": corporate.get("Name", ""),
            "suggested_doctor": corporate.get("Primary_Doctor_Name", ""),
        })


class CorporateDoctorsView(views.APIView):
    """GET /api/patient/corporate-doctors/"""
    permission_classes = [IsPatientUser]

    def get(self, request):
        user = request.user
        doctors = []

        if user.email and '@' in user.email:
            domain = user.email.split('@')[-1]
            corporate = ZohoService.get_corporate_by_email_domain(domain)
            if corporate:
                doctors = ZohoService.get_doctors_by_corporate(corporate.get('id'))

        # Also include EzeeHealth doctors
        eh_doctors = ZohoService.get_ezeehealth_doctors()
        # Deduplicate by id
        seen_ids = {d.get('id') for d in doctors}
        for d in eh_doctors:
            if d.get('id') not in seen_ids:
                doctors.append(d)

        return Response({"doctors": doctors})


class EzeeHealthDoctorsView(views.APIView):
    """GET /api/patient/ezeehealth-doctors/"""
    permission_classes = [IsPatientUser]

    def get(self, request):
        doctors = ZohoService.get_ezeehealth_doctors()
        return Response({"doctors": doctors})


class CheckExistingUserView(views.APIView):
    """GET /api/patient/check-existing-user/"""
    permission_classes = [IsPatientUser]

    def get(self, request):
        user = request.user
        contacts = []

        if user.email:
            contacts = ZohoService.search_contact_by_email(user.email)
        if not contacts and user.mobile:
            contacts = ZohoService.search_contact_by_phone(user.mobile)

        if contacts:
            return Response({
                "is_existing": True,
                "data": contacts,
            })
        return Response({"is_existing": False, "data": []})


class IdentifyUserView(views.APIView):
    """POST /api/patient/identify-user/ — Adopt an existing Zoho contact profile."""
    permission_classes = [IsPatientUser]

    def post(self, request):
        zoho_id = request.data.get('zoho_id')
        if not zoho_id:
            return Response({"error": "zoho_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        contact = ZohoService.get_contact(zoho_id)
        if not contact:
            return Response({"error": "No Zoho contact found."}, status=status.HTTP_404_NOT_FOUND)

        user = request.user

        def pick(field, default):
            val = contact.get(field)
            return val if val not in (None, '') else default

        user.zoho_contact_id = zoho_id
        user.profile_completed = True
        user.lead_source = pick('Lead_Source', user.lead_source)
        user.gender = pick('Gender', user.gender)
        try:
            age_val = contact.get('Age_in_Yrs')
            user.age_in_years = int(age_val) if age_val is not None else user.age_in_years
        except (TypeError, ValueError):
            pass
        user.mailing_street = pick('Mailing_Street', user.mailing_street)
        user.mailing_city = pick('Mailing_City', user.mailing_city)
        user.mailing_state = pick('Mailing_State', user.mailing_state)
        user.mailing_zip = pick('Mailing_Zip', user.mailing_zip)

        user.save()

        return Response({
            "message": "User identified and profile updated.",
            "data": PatientProfileSerializer(user).data,
        })


# ==================== PHASE 2: DOCUMENTS ====================

ALLOWED_EXTENSIONS = {'.pdf', '.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tiff', '.webp'}


class DocumentListCreateView(DependantMixin, views.APIView):
    """
    GET /api/patient/documents/ — List documents
    POST /api/patient/documents/ — Upload document + trigger AI pipeline
    """
    permission_classes = [IsPatientWithProfile]

    def get(self, request):
        filters = self.get_document_owner_filter()
        documents = UploadedDocument.objects.filter(**filters)
        return Response(UploadedDocumentSerializer(documents, many=True).data)

    def post(self, request):
        file = request.FILES.get('document')
        if not file:
            return Response({"error": "No file provided."}, status=status.HTTP_400_BAD_REQUEST)

        import os
        ext = os.path.splitext(file.name)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            return Response({"error": f"File type {ext} not allowed."}, status=status.HTTP_400_BAD_REQUEST)

        title = request.data.get('title', file.name)
        category = request.data.get('category', 'Others')
        user = request.user
        dependant = self.get_dependant()

        # Upload to S3
        import uuid as uuid_mod
        from apps.patients.s3_utils import get_s3_client
        s3 = get_s3_client()
        bucket = settings.AWS_STORAGE_BUCKET_NAME
        s3_key = f"patient_uploads/{user.id}/{uuid_mod.uuid4()}_{file.name}"

        try:
            s3.upload_fileobj(file, bucket, s3_key, ExtraArgs={'ContentType': file.content_type})
        except Exception as e:
            return Response({"error": f"S3 upload failed: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        doc = UploadedDocument.objects.create(
            patient=user,
            dependant=dependant,
            title=title,
            category=category,
            s3_key=s3_key,
            file_extension=ext,
            file_size=file.size,
        )

        # Trigger AI processing in background thread
        if doc.ai_readable:
            thread = threading.Thread(target=_process_patient_document, args=(str(doc.id),))
            thread.daemon = True
            thread.start()

        return Response(UploadedDocumentSerializer(doc).data, status=status.HTTP_201_CREATED)


class DocumentDetailView(views.APIView):
    """
    GET /api/patient/documents/{id}/ — Detail + presigned download URL
    DELETE /api/patient/documents/{id}/ — Delete doc + S3 + vectors
    """
    permission_classes = [IsPatientWithProfile]

    def get(self, request, pk):
        try:
            doc = UploadedDocument.objects.get(id=pk, patient=request.user)
        except UploadedDocument.DoesNotExist:
            return Response({"error": "Document not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(UploadedDocumentSerializer(doc).data)

    def delete(self, request, pk):
        try:
            doc = UploadedDocument.objects.get(id=pk, patient=request.user)
        except UploadedDocument.DoesNotExist:
            return Response({"error": "Document not found."}, status=status.HTTP_404_NOT_FOUND)

        # Delete from S3
        from apps.patients.s3_utils import get_s3_client
        try:
            s3 = get_s3_client()
            s3.delete_object(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=doc.s3_key)
        except Exception as e:
            print(f"S3 delete warning: {e}")

        # TODO: Delete vectors from Endee index when implemented

        doc.delete()
        return Response({"message": "Document deleted."}, status=status.HTTP_204_NO_CONTENT)


class DocumentMetadataView(views.APIView):
    """PATCH /api/patient/documents/{id}/metadata/ — Update title/category."""
    permission_classes = [IsPatientWithProfile]

    def patch(self, request, pk):
        try:
            doc = UploadedDocument.objects.get(id=pk, patient=request.user)
        except UploadedDocument.DoesNotExist:
            return Response({"error": "Document not found."}, status=status.HTTP_404_NOT_FOUND)

        if 'title' in request.data:
            doc.title = request.data['title']
        if 'category' in request.data:
            doc.category = request.data['category']
        doc.save()
        return Response(UploadedDocumentSerializer(doc).data)


class DocumentInsightsView(views.APIView):
    """GET /api/patient/documents/{id}/insights/ — AI insights."""
    permission_classes = [IsPatientWithProfile]

    def get(self, request, pk):
        try:
            doc = UploadedDocument.objects.get(id=pk, patient=request.user)
        except UploadedDocument.DoesNotExist:
            return Response({"error": "Document not found."}, status=status.HTTP_404_NOT_FOUND)

        insights = doc.insights.all()
        if insights.exists():
            return Response(DocumentInsightSerializer(insights, many=True).data)

        # If no insights yet and AI readable, try generating on-demand
        if doc.ai_readable and doc.ai_processed:
            return Response({"message": "No insights generated yet."}, status=status.HTTP_204_NO_CONTENT)

        return Response({"message": "Document not yet processed."}, status=status.HTTP_202_ACCEPTED)


class AIToggleView(views.APIView):
    """POST /api/patient/documents/{id}/ai-toggle/ — Toggle AI readability."""
    permission_classes = [IsPatientWithProfile]

    def post(self, request, pk):
        try:
            doc = UploadedDocument.objects.get(id=pk, patient=request.user)
        except UploadedDocument.DoesNotExist:
            return Response({"error": "Document not found."}, status=status.HTTP_404_NOT_FOUND)

        doc.ai_readable = not doc.ai_readable
        doc.save(update_fields=['ai_readable'])

        # TODO: Update Endee vector metadata when implemented

        return Response({
            "message": f"AI readability {'enabled' if doc.ai_readable else 'disabled'}.",
            "ai_readable": doc.ai_readable,
        })


class CriticalInsightsView(DependantMixin, views.APIView):
    """GET /api/patient/critical-insights/ — Aggregated critical insights."""
    permission_classes = [IsPatientWithProfile]

    def get(self, request):
        filters = self.get_document_owner_filter()
        insights = DocumentInsight.objects.filter(
            document__in=UploadedDocument.objects.filter(**filters),
            tags__contains=['high'],
        )
        return Response(DocumentInsightSerializer(insights, many=True).data)


class DoctorDocumentsView(views.APIView):
    """GET /api/patient/doctor-documents/ — Docs uploaded BY doctor FOR this patient."""
    permission_classes = [IsPatientWithProfile]

    def get(self, request):
        from apps.patients.models import PatientDocument, Patient

        # Find local patient record by phone match
        patient = Patient.objects.filter(phone=request.user.mobile).first()
        if not patient:
            return Response([])

        docs = PatientDocument.objects.filter(patient=patient)
        result = []
        for doc in docs:
            from apps.patients.s3_utils import generate_presigned_url
            try:
                url = generate_presigned_url(doc.s3_key, expiration=3600)
            except Exception:
                url = None
            result.append({
                "id": str(doc.id),
                "title": doc.original_filename or doc.s3_key.split('/')[-1],
                "category": "Doctor Upload",
                "uploaded_at": doc.uploaded_at.isoformat() if doc.uploaded_at else None,
                "download_url": url,
                "source": "doctor",
            })
        return Response(result)


def _process_patient_document(document_id):
    """Background AI processing for a patient portal document."""
    try:
        doc = UploadedDocument.objects.get(id=document_id)

        from apps.patients.s3_utils import get_s3_client
        from apps.patients.ai_service import extract_text, chunk_and_embed_document, generate_insights
        import tempfile
        import os

        # Download from S3 to get file bytes
        s3 = get_s3_client()
        bucket = settings.AWS_STORAGE_BUCKET_NAME

        with tempfile.NamedTemporaryFile(delete=False, suffix=doc.file_extension) as tmp:
            s3.download_fileobj(bucket, doc.s3_key, tmp)
            tmp_path = tmp.name

        try:
            with open(tmp_path, 'rb') as f:
                file_bytes = f.read()

            # Extract text (expects bytes + extension)
            extracted_text = extract_text(file_bytes, doc.file_extension)

            if extracted_text:
                # Chunk, embed, and store in Endee index
                patient_id = str(doc.patient_id)
                doc_id = str(doc.id)

                chunk_and_embed_document(
                    text=extracted_text,
                    patient_id=patient_id,
                    doc_id=doc_id,
                    title=doc.title,
                    category=doc.category,
                )

                # Generate insights from Endee vectors + Gemini
                insights_data = generate_insights(doc_id, patient_id)

                if insights_data:
                    DocumentInsight.objects.update_or_create(
                        document=doc,
                        defaults={
                            'title': insights_data.get('title', doc.title),
                            'summary': insights_data.get('summary', ''),
                            'key_findings': insights_data.get('key_findings', []),
                            'risk_flags': insights_data.get('risk_flags', []),
                            'tags': insights_data.get('tags', []),
                        }
                    )

            doc.ai_processed = True
            doc.save(update_fields=['ai_processed'])

            # Create alert
            Alert.objects.create(
                user=doc.patient,
                message=f"Document '{doc.title}' uploaded and processed successfully.",
                alert_type='Report Generated',
            )

        finally:
            os.unlink(tmp_path)

    except Exception as e:
        print(f"Error processing patient document {document_id}: {e}")
        import traceback
        traceback.print_exc()


# ==================== PHASE 3: DOCUMENT SHARING ====================

class ShareDocumentView(views.APIView):
    """POST /api/patient/documents/{id}/share/ — Share with primary doctor."""
    permission_classes = [IsPatientWithProfile]

    def post(self, request, pk):
        user = request.user
        if not user.doctor_email:
            return Response({"error": "No primary doctor configured."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            doc = UploadedDocument.objects.get(id=pk, patient=user)
        except UploadedDocument.DoesNotExist:
            return Response({"error": "Document not found."}, status=status.HTTP_404_NOT_FOUND)

        # Check if already shared
        existing = DocumentShare.objects.filter(document=doc, doctor_email=user.doctor_email, is_active=True).first()
        if existing:
            return Response({"message": "Document already shared."})

        # Reactivate if previously revoked
        revoked = DocumentShare.objects.filter(document=doc, doctor_email=user.doctor_email, is_active=False).first()
        if revoked:
            revoked.is_active = True
            revoked.revoked_at = None
            revoked.save(update_fields=['is_active', 'revoked_at'])
            share = revoked
        else:
            share = DocumentShare.objects.create(
                document=doc,
                patient=user,
                doctor_email=user.doctor_email,
                doctor_mobile=user.doctor_mobile or '',
            )

        # Create SharedPatientDocument for doctor app
        from apps.patients.models import SharedPatientDocument
        SharedPatientDocument.objects.update_or_create(
            id=doc.id,
            defaults={
                'patient_zoho_id': user.zoho_contact_id or '',
                'patient_name': f"{user.first_name} {user.last_name}".strip(),
                'patient_email': user.email or '',
                'patient_phone': user.mobile,
                'title': doc.title,
                'category': doc.category,
                'uploaded_at': doc.uploaded_at,
                's3_key': doc.s3_key,
                'file_extension': doc.file_extension,
                'file_size': doc.file_size,
                'doctor_email': user.doctor_email,
                'doctor_mobile': user.doctor_mobile or '',
                'shared_at': timezone.now(),
                'is_active': True,
                'revoked_at': None,
            }
        )

        # Sync insights if they exist
        _sync_shared_insights(doc)

        return Response({"message": "Document shared with your doctor."}, status=status.HTTP_201_CREATED)


class RevokeShareView(views.APIView):
    """DELETE /api/patient/documents/{id}/share/revoke/ — Revoke access."""
    permission_classes = [IsPatientWithProfile]

    def delete(self, request, pk):
        user = request.user
        try:
            share = DocumentShare.objects.get(document_id=pk, patient=user, is_active=True)
        except DocumentShare.DoesNotExist:
            return Response({"error": "No active share found."}, status=status.HTTP_404_NOT_FOUND)

        share.is_active = False
        share.revoked_at = timezone.now()
        share.save(update_fields=['is_active', 'revoked_at'])

        # Update SharedPatientDocument
        from apps.patients.models import SharedPatientDocument
        SharedPatientDocument.objects.filter(id=pk).update(is_active=False, revoked_at=timezone.now())

        return Response({"message": "Document share revoked."})


class ShareStatusView(views.APIView):
    """GET /api/patient/documents/{id}/share/status/ — Check if shared."""
    permission_classes = [IsPatientWithProfile]

    def get(self, request, pk):
        is_shared = DocumentShare.objects.filter(
            document_id=pk, patient=request.user, is_active=True
        ).exists()
        return Response({"is_shared": is_shared})


class SharedDocumentsListView(views.APIView):
    """GET /api/patient/documents/shared/ — List all shared documents."""
    permission_classes = [IsPatientWithProfile]

    def get(self, request):
        shares = DocumentShare.objects.filter(patient=request.user, is_active=True)
        return Response(DocumentShareSerializer(shares, many=True).data)


def _sync_shared_insights(doc):
    """Sync DocumentInsight to SharedDocumentInsight for doctor app."""
    from apps.patients.models import SharedDocumentInsight
    for insight in doc.insights.all():
        SharedDocumentInsight.objects.update_or_create(
            shared_document_id=doc.id,
            defaults={
                'title': insight.title,
                'summary': insight.summary,
                'key_findings': insight.key_findings,
                'risk_flags': insight.risk_flags,
                'tags': insight.tags,
            }
        )


# ==================== PHASE 4: DASHBOARD + JOURNEYS ====================

class PatientDashboardView(DependantMixin, views.APIView):
    """GET /api/patient/dashboard/ — Zoho contact data + summary."""
    permission_classes = [IsPatientWithProfile]

    def get(self, request):
        zoho_id = self.get_zoho_contact_id()
        if not zoho_id:
            return Response({"error": "No Zoho contact linked."}, status=status.HTTP_400_BAD_REQUEST)

        contact = ZohoService.get_contact(zoho_id)
        if not contact:
            return Response({"error": "Failed to fetch contact from Zoho."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({"data": contact})


class JourneyListView(DependantMixin, views.APIView):
    """GET /api/patient/journeys/ — List all Zoho Deals for patient."""
    permission_classes = [IsPatientWithProfile]

    def get(self, request):
        zoho_id = self.get_zoho_contact_id()
        if not zoho_id:
            return Response({"error": "No Zoho contact linked."}, status=status.HTTP_400_BAD_REQUEST)

        deals = ZohoService.get_deals_by_contact(zoho_id)
        return Response({"data": deals})


class JourneyDetailView(DependantMixin, views.APIView):
    """GET /api/patient/journeys/{deal_id}/ — Deal detail + stage history + timeline."""
    permission_classes = [IsPatientWithProfile]

    def get(self, request, deal_id):
        deal = ZohoService.get_deal(deal_id)
        if not deal:
            return Response({"error": "Journey not found."}, status=status.HTTP_404_NOT_FOUND)

        stage_history = ZohoService.get_deal_stage_history(deal_id)

        # Load stage definitions and build timeline
        stages_timeline = []
        try:
            stages_path = Path(settings.BASE_DIR) / 'apps' / 'patients' / 'stages.json'
            with open(stages_path, 'r') as f:
                stages_data = json.load(f).get('stages', [])

            # Build context for placeholder filling
            contact_name = ''
            if deal.get('Contact_Name'):
                if isinstance(deal['Contact_Name'], dict):
                    contact_name = deal['Contact_Name'].get('name', '')
                else:
                    contact_name = str(deal['Contact_Name'])

            ssh_name = ''
            if deal.get('Registered_SSH'):
                if isinstance(deal['Registered_SSH'], dict):
                    ssh_name = deal['Registered_SSH'].get('name', '')
                else:
                    ssh_name = str(deal['Registered_SSH'])

            context = {
                "patient_name": contact_name,
                "Patient Name": contact_name,
                "Registered SSH": ssh_name,
                "Treatment": deal.get('Treatment', ''),
                "Provisional Diagnosis": deal.get('Provisional_Diagnosis_3', ''),
            }

            # Find reached stages from history
            reached_stages = set()
            for h in stage_history:
                reached_stages.add(h.get('Stage') or h.get('Stage_Name', ''))

            current_stage = deal.get('Stage', '')

            for stage_def in stages_data:
                stage_name = stage_def.get('stage', '')
                heading = fill_placeholders(stage_def.get('heading', ''), context)
                description = fill_placeholders(stage_def.get('description', ''), context)

                is_reached = stage_name in reached_stages or stage_name == current_stage
                is_current = stage_name == current_stage

                stages_timeline.append({
                    "stage": stage_name,
                    "heading": heading,
                    "description": description,
                    "is_reached": is_reached,
                    "is_current": is_current,
                })
        except Exception as e:
            print(f"Error building stage timeline: {e}")

        return Response({
            "deal": deal,
            "stage_history": stage_history,
            "stages_timeline": stages_timeline,
        })


class SSHDetailsView(views.APIView):
    """GET /api/patient/ssh-details/?name=... — Hospital details."""
    permission_classes = [IsPatientWithProfile]

    def get(self, request):
        name = request.query_params.get('name', '')
        if not name:
            return Response({"error": "name parameter is required."}, status=status.HTTP_400_BAD_REQUEST)

        details = ZohoService.get_ssh_details(name)
        if not details:
            return Response({"error": "Hospital not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response({"data": details})


# ==================== PHASE 5: DEPENDANTS ====================

class DependantListCreateView(views.APIView):
    """
    GET /api/patient/dependants/ — List dependants + self as "Family Head"
    POST /api/patient/dependants/ — Add dependant + create Zoho Contact
    """
    permission_classes = [IsPatientWithProfile]

    def get(self, request):
        user = request.user
        dependants = Dependant.objects.filter(patient=user)

        # Include self as "Family Head"
        user_details = {
            "id": str(user.id),
            "full_name": f"{user.first_name} {user.last_name}".strip(),
            "relationship": "Family Head",
            "age": user.age_in_years,
            "gender": user.gender or '',
            "zoho_contact_id": user.zoho_contact_id,
            "is_self": True,
        }

        dep_list = DependantSerializer(dependants, many=True).data
        for d in dep_list:
            d['is_self'] = False

        return Response({
            "user_details": user_details,
            "dependants": dep_list,
        })

    def post(self, request):
        serializer = CreateDependantSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        user = request.user
        full_name = f"{data['first_name']} {data['last_name']}".strip()

        dependant = Dependant.objects.create(
            patient=user,
            full_name=full_name,
            relationship=data['relationship'],
            age=data.get('age'),
            gender=data.get('gender', ''),
        )

        # Create Zoho Contact for dependant
        contact_data = {
            "Last_Name": full_name,
            "Mobile": user.mobile,
            "Email": user.email or '',
            "Lead_Source": user.lead_source or 'Direct',
            "Gender": data.get('gender', ''),
            "Age_in_Yrs": data.get('age'),
            "Relationship_with_Patient": data['relationship'],
        }
        zoho_id = ZohoService.create_contact(contact_data)
        if zoho_id:
            dependant.zoho_contact_id = zoho_id
            dependant.save(update_fields=['zoho_contact_id'])

        return Response(DependantSerializer(dependant).data, status=status.HTTP_201_CREATED)


# ==================== PHASE 6: AI CHAT ====================

MAX_AI_CHAT_TOKENS = 30


class AIChatView(DependantMixin, views.APIView):
    """POST /api/patient/ai-chat/ — Send message with RAG."""
    permission_classes = [IsPatientWithProfile]

    def post(self, request):
        prompt = request.data.get('prompt', '').strip()
        if not prompt:
            return Response({"error": "prompt is required."}, status=status.HTTP_400_BAD_REQUEST)

        user = request.user
        dependant = self.get_dependant()

        # Get or create session
        session = ChatSession.objects.filter(patient=user, dependant=dependant).order_by('-session_start').first()
        if not session or not session.is_active():
            # Delete expired session
            if session:
                session.delete()
            session = ChatSession.objects.create(patient=user, dependant=dependant)

        # Check message limit
        message_count = session.messages.filter(sender='user').count()
        if message_count >= MAX_AI_CHAT_TOKENS:
            return Response({
                "error": "Chat session has reached maximum message limit. Please wait for a new session."
            }, status=status.HTTP_429_TOO_MANY_REQUESTS)

        # Save user message
        ChatMessage.objects.create(session=session, sender='user', message=prompt)

        # TODO: RAG - query Endee index for relevant document chunks
        # For now, use a basic Gemini call with medical context
        try:
            import os
            from google import genai
            client = genai.Client(api_key=os.getenv('GOOGLE_GENAI_API_KEY'))

            # Build chat history for context
            history = session.messages.order_by('timestamp')[:20]  # last 20 messages
            history_text = ""
            for msg in history:
                role = "Patient" if msg.sender == 'user' else "Assistant"
                history_text += f"{role}: {msg.message}\n"

            system_prompt = (
                "You are a helpful medical assistant for a patient health portal. "
                "Answer medical questions in simple, easy-to-understand language. "
                "Do NOT provide specific diagnoses or treatment plans. "
                "If the question is not medical, politely redirect. "
                "If you're unsure, suggest consulting a healthcare professional."
            )

            full_prompt = f"{system_prompt}\n\nConversation History:\n{history_text}\nPatient: {prompt}\nAssistant:"

            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=full_prompt,
            )

            ai_response = response.text or "I'm sorry, I couldn't generate a response. Please try again."

        except Exception as e:
            print(f"AI chat error: {e}")
            ai_response = "I'm sorry, there was an error processing your question. Please try again later."

        # Save AI response
        ChatMessage.objects.create(session=session, sender='ai', message=ai_response)

        return Response({
            "response": ai_response,
            "session_id": str(session.id),
        })


class ChatHistoryView(DependantMixin, views.APIView):
    """GET /api/patient/ai-chat/history/ — Chat message history."""
    permission_classes = [IsPatientWithProfile]

    def get(self, request):
        user = request.user
        dependant = self.get_dependant()

        session = ChatSession.objects.filter(patient=user, dependant=dependant).order_by('-session_start').first()
        if not session or not session.is_active():
            return Response({"messages": [], "session_active": False})

        messages = session.messages.all()
        return Response({
            "messages": ChatMessageSerializer(messages, many=True).data,
            "session_active": True,
            "session_id": str(session.id),
        })


class SpeechToTextView(views.APIView):
    """POST /api/patient/speech-to-text/ — Audio transcription via Sarvam AI."""
    permission_classes = [IsPatientWithProfile]

    def post(self, request):
        import requests as http_requests
        import base64

        audio_file = request.FILES.get('audio')
        if not audio_file:
            return Response({"error": "No audio file provided."}, status=status.HTTP_400_BAD_REQUEST)

        language = request.data.get('language', 'en-IN')

        import os as _os
        sarvam_key = _os.getenv('SARVAM_API_KEY', '')
        if not sarvam_key:
            return Response({"error": "Speech-to-text service not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        try:
            audio_bytes = audio_file.read()
            audio_base64 = base64.b64encode(audio_bytes).decode('utf-8')

            resp = http_requests.post(
                "https://api.sarvam.ai/speech-to-text",
                headers={
                    "Content-Type": "application/json",
                    "API-Subscription-Key": sarvam_key,
                },
                json={
                    "input": audio_base64,
                    "language_code": language,
                    "model": "saarika:v2.5",
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            return Response({"text": data.get("transcript", "")})
        except Exception as e:
            return Response({"error": f"Transcription failed: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ==================== PHASE 7: ALERTS + MEETINGS ====================

class AlertsListView(views.APIView):
    """GET /api/patient/alerts/ — All alerts."""
    permission_classes = [IsPatientWithProfile]

    def get(self, request):
        alerts = Alert.objects.filter(user=request.user)
        return Response(AlertSerializer(alerts, many=True).data)


class UnreadAlertCountView(views.APIView):
    """GET /api/patient/alerts/unread-count/ — Unread count."""
    permission_classes = [IsPatientWithProfile]

    def get(self, request):
        count = Alert.objects.filter(user=request.user, is_read=False).count()
        return Response({"unread_count": count})


class MarkAlertsReadView(views.APIView):
    """PUT /api/patient/alerts/mark-read/ — Mark alerts as read."""
    permission_classes = [IsPatientWithProfile]

    def put(self, request):
        alert_id = request.data.get('alert_id')
        if alert_id:
            Alert.objects.filter(id=alert_id, user=request.user).update(is_read=True)
        else:
            Alert.objects.filter(user=request.user, is_read=False).update(is_read=True)
        return Response({"message": "Alerts marked as read."})


class MeetingsView(DependantMixin, views.APIView):
    """GET /api/patient/meetings/ — Fetch from Zoho Events."""
    permission_classes = [IsPatientWithProfile]

    def get(self, request):
        zoho_id = self.get_zoho_contact_id()
        if not zoho_id:
            return Response({"error": "No Zoho contact linked."}, status=status.HTTP_400_BAD_REQUEST)

        meetings = ZohoService.get_events_for_contact(zoho_id)
        return Response({"data": meetings})
