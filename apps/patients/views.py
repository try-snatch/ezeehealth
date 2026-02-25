from rest_framework import views, status, generics
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.decorators import api_view, permission_classes
from .models import Patient, Referral, SharedPatientDocument, SharedDocumentInsight, PatientDocument, PatientDocumentInsight, DocumentUploadLink
from .serializers import PatientSerializer, PatientDetailSerializer, ReferralSerializer
from .s3_utils import upload_patient_document, generate_presigned_url_for_key, delete_s3_key
from apps.authentication.models import User
from apps.integrations.zoho_service import ZohoService
import json
from pathlib import Path
from django.conf import settings
from django.db.models import Q
from boto3 import client
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
import os
import logging
import threading

logger = logging.getLogger(__name__)


class PatientListCreateView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Get all patients (Zoho Deals + local DB OPD/referred patients)"""
        user = request.user

        if user.role in ['owner', 'doctor', 'receptionist', 'nurse']:
            try:
                primary_user = User.objects.filter(clinic=user.clinic, role='owner').first() or \
                               User.objects.filter(clinic=user.clinic, role='doctor').first()
                doc_mobile = primary_user.mobile if primary_user else user.mobile

                # Get Zoho Deals (converted patients)
                zoho_deals = ZohoService.get_patients(doc_mobile)
                logger.info("PatientList: %d Zoho deals for %s", len(zoho_deals), doc_mobile)

                # Get Zoho Leads (referred patients not yet converted)
                zoho_leads = ZohoService.get_leads(doc_mobile)
                logger.info("PatientList: %d Zoho leads for %s", len(zoho_leads), doc_mobile)

                # Hide revenue if user can't view financial
                if not user.can_view_financial:
                    for p in zoho_deals:
                        p['revenue'] = 0

                # Get local DB patients
                local_patients = Patient.objects.filter(
                    clinic=user.clinic
                ).order_by('-created_at')

                local_serializer = PatientSerializer(local_patients, many=True, context={'request': request})
                local_data = local_serializer.data

                for p in local_data:
                    p['source'] = 'local'

                # Combine: local patients + Zoho leads + Zoho deals
                combined = local_data + zoho_leads + zoho_deals

                return Response(combined, status=status.HTTP_200_OK)

            except Exception as e:
                logger.error("Error fetching patients: %s", e, exc_info=True)
                return Response([], status=status.HTTP_200_OK)

        # For other roles, fallback to local DB
        if not user.clinic:
            return Response([], status=status.HTTP_200_OK)

        patients = Patient.objects.filter(clinic=user.clinic).order_by('-created_at')
        serializer = PatientSerializer(patients, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request):
        """Create a new referral (Lead)"""
        data = request.data.copy()

        name = data.get('name', '')
        if name:
            data['full_name'] = name.strip()

        serializer = PatientSerializer(data=data, context={'request': request})
        if serializer.is_valid():
            user = request.user
            validated_data = serializer.validated_data

            # Get Zoho Doctor ID
            zoho_doctor_id = None
            try:
                primary_user = User.objects.filter(clinic=user.clinic, role='owner').first() or \
                               User.objects.filter(clinic=user.clinic, role='doctor').first()
                doc_mobile = primary_user.mobile if primary_user else user.mobile

                zoho_doc = ZohoService.search_doctor(doc_mobile)
                if zoho_doc:
                    zoho_doctor_id = zoho_doc.get('id')
            except Exception as e:
                logger.error("Error fetching Zoho Doctor ID: %s", e)

            lead_data = {
                'name': name,
                'phone': validated_data.get('phone'),
                'email': validated_data.get('email'),
                'diagnosis': validated_data.get('diagnosis'),
                'suggested_specialty': request.data.get('suggested_specialty'),
                'age': validated_data.get('age'),
                'gender': validated_data.get('gender'),
                'doctor_id': zoho_doctor_id
            }

            zoho_lead_id = ZohoService.create_lead(lead_data)

            if zoho_lead_id:
                patient = serializer.save(clinic=user.clinic, status='opd')
                Referral.objects.create(
                    patient=patient,
                    clinic=user.clinic,
                    zoho_lead_id=zoho_lead_id,
                    status='referred',
                    diagnosis=validated_data.get('diagnosis'),
                    suggested_specialty=request.data.get('suggested_specialty'),
                )
                return Response(PatientSerializer(patient, context={'request': request}).data, status=status.HTTP_201_CREATED)
            else:
                return Response(
                    {"error": "Failed to create referral in Zoho"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class PatientDetailView(generics.RetrieveAPIView):
    """
    Retrieve patient/referral details.
    Note: Patients are read-only after creation for data integrity.
    """
    serializer_class = PatientDetailSerializer
    permission_classes = [IsAuthenticated]
    queryset = Patient.objects.all()

    def get_queryset(self):
        return Patient.objects.filter(clinic=self.request.user.clinic)


class OPDPatientRegistrationView(views.APIView):
    """Register a new OPD patient (local DB only, no Zoho yet).
    Accepts JSON or multipart/form-data.  When multipart, any files
    attached under 'documents' (or 'documents[]') are uploaded to S3
    after the patient record is created.
    """
    permission_classes = [IsAuthenticated]

    ALLOWED_EXTENSIONS = {'pdf', 'jpg', 'jpeg', 'png', 'bmp', 'tiff', 'webp'}
    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

    def post(self, request):
        user = request.user
        if not user.clinic:
            return Response({"error": "User not associated with a clinic"}, status=status.HTTP_400_BAD_REQUEST)

        data = request.data.copy()
        data['status'] = 'opd'

        # Accept 'name' or 'full_name'
        if 'name' in data and 'full_name' not in data:
            data['full_name'] = data['name']

        serializer = PatientSerializer(data=data, context={'request': request})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        patient = serializer.save(clinic=user.clinic, status='opd')

        # ---- Handle optional document uploads ----
        files = request.FILES.getlist('documents') or request.FILES.getlist('documents[]')
        uploaded_docs = []
        errors = []

        for file_obj in files:
            original_filename = file_obj.name
            ext = original_filename.rsplit('.', 1)[-1].lower() if '.' in original_filename else ''

            if ext not in self.ALLOWED_EXTENSIONS:
                errors.append(f"{original_filename}: file type not allowed")
                continue
            if file_obj.size > self.MAX_FILE_SIZE:
                errors.append(f"{original_filename}: exceeds 10 MB limit")
                continue

            import uuid as uuid_module
            doc_id = uuid_module.uuid4()
            s3_filename = f"{doc_id}.{ext}" if ext else str(doc_id)

            s3_key = upload_patient_document(patient.id, file_obj, s3_filename)
            if not s3_key:
                errors.append(f"{original_filename}: upload to storage failed")
                continue

            title = original_filename.rsplit('.', 1)[0]
            doc = PatientDocument.objects.create(
                id=doc_id,
                patient=patient,
                clinic=user.clinic,
                uploaded_by=user,
                s3_key=s3_key,
                title=title,
                category='Others',
                file_extension=ext,
                file_size=file_obj.size,
            )
            uploaded_docs.append({'id': str(doc.id), 'title': doc.title})

            # Kick off async AI processing
            from .ai_service import process_document
            threading.Thread(target=process_document, args=(doc.id,), daemon=True).start()

        response_data = PatientSerializer(patient, context={'request': request}).data
        if uploaded_docs:
            response_data['documents'] = uploaded_docs
        if errors:
            response_data['document_errors'] = errors

        return Response(response_data, status=status.HTTP_201_CREATED)


class CreateReferralView(views.APIView):
    """Create a new Zoho Lead (referral episode) for an existing patient."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        user = request.user
        try:
            patient = Patient.objects.get(pk=pk, clinic=user.clinic)
        except Patient.DoesNotExist:
            return Response({"error": "Patient not found"}, status=status.HTTP_404_NOT_FOUND)

        # Get referral details from request (fall back to patient's last referral or base record)
        latest = patient.referrals.first()
        diagnosis = request.data.get('diagnosis') or patient.diagnosis or ''
        suggested_specialty = request.data.get('suggested_specialty') or (latest.suggested_specialty if latest else '')
        suggested_sshs = request.data.get('suggested_sshs') or (latest.suggested_sshs if latest else '')

        if not diagnosis:
            return Response({"error": "Diagnosis is required"}, status=status.HTTP_400_BAD_REQUEST)

        # Get Zoho Doctor ID
        zoho_doctor_id = None
        try:
            primary_user = User.objects.filter(clinic=user.clinic, role='owner').first() or \
                           User.objects.filter(clinic=user.clinic, role='doctor').first()
            doc_mobile = primary_user.mobile if primary_user else user.mobile
            zoho_doc = ZohoService.search_doctor(doc_mobile)
            if zoho_doc:
                zoho_doctor_id = zoho_doc.get('id')
        except Exception as e:
            logger.error("Error fetching Zoho Doctor ID: %s", e)

        lead_data = {
            'name': patient.full_name,
            'phone': patient.phone,
            'email': patient.email,
            'diagnosis': diagnosis,
            'suggested_specialty': suggested_specialty or suggested_sshs,
            'age': patient.age,
            'gender': patient.gender,
            'doctor_id': zoho_doctor_id,
            'suggested_sshs': suggested_sshs
        }

        zoho_lead_id = ZohoService.create_lead(lead_data)

        if zoho_lead_id:
            referral = Referral.objects.create(
                patient=patient,
                clinic=user.clinic,
                zoho_lead_id=zoho_lead_id,
                status='referred',
                diagnosis=diagnosis,
                suggested_specialty=suggested_specialty,
                suggested_sshs=suggested_sshs,
            )
            return Response(
                {
                    **PatientSerializer(patient, context={'request': request}).data,
                    'referral': ReferralSerializer(referral).data,
                },
                status=status.HTTP_200_OK
            )
        else:
            return Response(
                {"error": "Failed to create referral in Zoho"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )



class DashboardStatsView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user

        if not user.clinic:
            return Response({"error": "User not associated with a clinic"}, status=400)

        recent_referrals_data = []
        stages_list = []
        total_converted = 0

        # OPD counts from local DB
        opd_count = Patient.objects.filter(clinic=user.clinic, status='opd').count()
        local_referred_count = Patient.objects.filter(clinic=user.clinic, status='referred').count()

        if user.role in ['owner', 'doctor', 'receptionist', 'nurse']:
            try:
                primary_user = User.objects.filter(clinic=user.clinic, role='owner').first() or \
                               User.objects.filter(clinic=user.clinic, role='doctor').first()
                doc_mobile = primary_user.mobile if primary_user else user.mobile

                # 1. Load Stages
                headings_order = []
                formatted_stats = {}

                try:
                    stages_path = Path(settings.BASE_DIR) / 'apps' / 'patients' / 'stages.json'
                    with open(stages_path, 'r') as f:
                        stages_data = json.load(f)
                        seen_headings = set()
                        sorted_stages = sorted(
                            stages_data.get('stages', []),
                            key=lambda x: x.get('sequence_number', 0)
                        )

                        for stage in sorted_stages:
                            h = stage.get('heading')
                            if h and h not in seen_headings:
                                headings_order.append(h)
                                seen_headings.add(h)
                                formatted_stats[h] = {"count": 0, "latest_date": ""}
                except Exception as e:
                        logger.error("Error loading stages.json for dashboard: %s", e)

                # 2. Fetch Deals (converted patients)
                patients = ZohoService.get_patients(doc_mobile)
                total_converted = len(patients)

                # 3. Fetch Leads (for referral count and recent referrals)
                leads = ZohoService.get_leads(doc_mobile)
                leads.sort(key=lambda x: x.get('date', ''), reverse=True)
                recent_referrals_data = leads[:5]

                total_referred = total_converted + len(leads)

                # 4. Aggregate counts from Deals
                total_revenue = 0
                for p in patients:
                    status_heading = p.get('status')
                    patient_date = p.get('date')

                    if status_heading:
                        if status_heading not in formatted_stats:
                            formatted_stats[status_heading] = {"count": 0, "latest_date": ""}
                            headings_order.append(status_heading)

                        if isinstance(formatted_stats[status_heading], int):
                            formatted_stats[status_heading] = {"count": 0, "latest_date": ""}

                        formatted_stats[status_heading]["count"] += 1

                        if patient_date:
                            current_latest = formatted_stats[status_heading]["latest_date"]
                            if not current_latest or patient_date > current_latest:
                                formatted_stats[status_heading]["latest_date"] = patient_date

                    # print(f"Patients for Dashboard: {patients}")

                    total_revenue += float(p.get('revenue') or 0)

                admitted_val = formatted_stats.get('ADMITTED', 0)
                total_admitted = admitted_val.get('count', 0) if isinstance(admitted_val, dict) else admitted_val

                overview_stats = {
                    "total_referred": total_referred,
                    "total_converted": total_converted,
                    "total_admitted": total_admitted,
                    "total_revenue": total_revenue if user.can_view_financial else 0,
                    "total_opd": opd_count,
                    "total_local_leads": local_referred_count,
                }

                colors = ["primary", "warning", "info", "success", "danger", "secondary"]
                color_idx = 0

                for h in headings_order:
                    stat = formatted_stats.get(h, {})
                    stages_list.append({
                        "title": h,
                        "value": stat.get('count', 0) if isinstance(stat, dict) else stat,
                        "latest_date": stat.get('latest_date', '') if isinstance(stat, dict) else '',
                        "color": colors[color_idx % len(colors)],
                        "icon": "Activity"
                    })
                    color_idx += 1

            except Exception as e:
                logger.error("Error fetching dashboard data from Zoho: %s", e, exc_info=True)
                overview_stats = {
                    "total_referred": 0, "total_converted": 0, "total_revenue": 0,
                    "total_opd": opd_count, "total_local_leads": local_referred_count,
                }
                stages_list = []

        else:
            local_patients = Patient.objects.filter(clinic=user.clinic)
            total_referred = local_patients.count()
            overview_stats = {
                "total_referred": total_referred,
                "total_converted": total_referred,
                "total_revenue": 0,
                "total_opd": opd_count,
                "total_local_leads": local_referred_count,
            }
            stages_list = []

            recent_referrals = local_patients.order_by('-created_at')[:5]
            recent_serializer = PatientSerializer(recent_referrals, many=True, context={'request': request})
            recent_referrals_data = recent_serializer.data

        return Response({
            "overview": overview_stats,
            "stages": stages_list,
            "recent_referrals": recent_referrals_data
        })


# ============================================================================
# SHARED PATIENT DOCUMENTS ENDPOINTS
# ============================================================================

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def list_shared_documents(request):
    """
    List all documents shared with this doctor.
    GET /api/patient-documents/
    Optional query param: ?patient_zoho_id=123
    """
    # Get doctor's email from authenticated user
    doctor_email = request.user.email

    if not doctor_email:
        return Response({
            'error': 'No email configured for your account'
        }, status=status.HTTP_400_BAD_REQUEST)

    # Base query
    query = Q(doctor_email=doctor_email, is_active=True)

    # Filter by patient if provided
    patient_zoho_id = request.query_params.get('patient_zoho_id')
    if patient_zoho_id:
        query &= Q(patient_zoho_id=patient_zoho_id)

    try:
        # Fetch documents with insights
        documents = SharedPatientDocument.objects.filter(query).prefetch_related('insights').order_by('-shared_at')

        # Flatten structure for frontend
        data = [{
            'id': str(doc.id),
            'patient_zoho_id': doc.patient_zoho_id,
            'patient_name': doc.patient_name,
            'patient_email': doc.patient_email,
            'patient_phone': doc.patient_phone,
            'title': doc.title,
            'category': doc.category,
            'uploaded_at': doc.uploaded_at.isoformat(),
            'shared_at': doc.shared_at.isoformat(),
            'file_size': doc.file_size,
            'file_extension': doc.file_extension,
            'has_insights': doc.insights.exists()
        } for doc in documents]

        return Response(data, status=status.HTTP_200_OK)

    except Exception as e:
        logger.exception("Error fetching shared documents")
        return Response({
            'error': 'Failed to fetch documents',
            'details': str(e)
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_document_details(request, document_id):
    """
    Get document details including insights and download URL.
    GET /api/patient-documents/<uuid:document_id>/
    """
    doctor_email = request.user.email

    if not doctor_email:
        return Response({
            'error': 'No email configured for your account'
        }, status=status.HTTP_400_BAD_REQUEST)

    try:
        document = SharedPatientDocument.objects.get(
            id=document_id,
            doctor_email=doctor_email,
            is_active=True
        )
    except SharedPatientDocument.DoesNotExist:
        return Response({
            'error': 'Document not found or access denied'
        }, status=status.HTTP_404_NOT_FOUND)

    # Get insights
    insights = document.insights.first()

    # Generate presigned download URL
    download_url = None
    try:
        s3_client = client(
            's3',
            aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
            aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
            region_name=os.getenv('AWS_S3_REGION_NAME', 'ap-south-1')
        )

        download_url = s3_client.generate_presigned_url(
            'get_object',
            Params={
                'Bucket': os.getenv('AWS_STORAGE_BUCKET_NAME', 'patientdocumentsezeehealth'),
                'Key': document.s3_key
            },
            ExpiresIn=3600  # 1 hour
        )
    except Exception as e:
        logger.warning(f"Failed to generate download URL: {str(e)}")
        download_url = None

    # Flatten structure to match frontend expectations
    data = {
        # Document fields
        'id': str(document.id),
        'title': document.title,
        'category': document.category,
        'uploaded_at': document.uploaded_at.isoformat(),
        'shared_at': document.shared_at.isoformat(),
        'file_size': document.file_size,
        'file_extension': document.file_extension,
        'presigned_url': download_url,

        # Patient fields
        'patient_zoho_id': document.patient_zoho_id,
        'patient_name': document.patient_name,
        'patient_email': document.patient_email,
        'patient_phone': document.patient_phone,

        # Insights
        'insights': {
            'title': insights.title,
            'summary': insights.summary,
            'key_findings': insights.key_findings,
            'risk_flags': insights.risk_flags,
            'tags': insights.tags
        } if insights else None
    }

    return Response(data, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def list_patients_with_shared_documents(request):
    """
    List all patients who have shared documents with this doctor.
    GET /api/patient-documents/patients/
    """
    doctor_email = request.user.email

    if not doctor_email:
        return Response({
            'error': 'No email configured for your account'
        }, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Get distinct patients
        patients = SharedPatientDocument.objects.filter(
            doctor_email=doctor_email,
            is_active=True
        ).values('patient_zoho_id', 'patient_name', 'patient_email', 'patient_phone').distinct()

        # Count documents per patient
        result = []
        for patient in patients:
            doc_count = SharedPatientDocument.objects.filter(
                doctor_email=doctor_email,
                patient_zoho_id=patient['patient_zoho_id'],
                is_active=True
            ).count()

            result.append({
                'zoho_id': patient['patient_zoho_id'],
                'name': patient['patient_name'],
                'email': patient['patient_email'],
                'phone': patient['patient_phone'],
                'document_count': doc_count
            })

        return Response({
            'patients': result,
            'count': len(result)
        }, status=status.HTTP_200_OK)

    except Exception as e:
        logger.exception("Error fetching patients")
        return Response({
            'error': 'Failed to fetch patients',
            'details': str(e)
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ============================================================================
# PATIENT DOCUMENTS (Doctor-uploaded)
# ============================================================================

class PatientDocumentListUploadView(views.APIView):
    """GET + POST /api/patients/{pk}/documents/"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        user = request.user
        try:
            patient = Patient.objects.get(pk=pk, clinic=user.clinic)
        except Patient.DoesNotExist:
            return Response({"error": "Patient not found"}, status=status.HTTP_404_NOT_FOUND)

        docs = PatientDocument.objects.filter(
            patient=patient, clinic=user.clinic
        ).select_related('insight').order_by('-uploaded_at')

        data = []
        for doc in docs:
            presigned_url = generate_presigned_url_for_key(doc.s3_key)
            insight_data = None
            try:
                insight = doc.insight
                insight_data = {
                    'title': insight.title,
                    'summary': insight.summary,
                    'key_findings': insight.key_findings,
                    'risk_flags': insight.risk_flags,
                    'tags': insight.tags,
                    'created_at': insight.created_at.isoformat(),
                }
            except PatientDocumentInsight.DoesNotExist:
                pass

            data.append({
                'id': str(doc.id),
                'title': doc.title,
                'category': doc.category,
                'file_extension': doc.file_extension,
                'file_size': doc.file_size,
                'uploaded_at': doc.uploaded_at.isoformat(),
                'ai_processed': doc.ai_processed,
                'presigned_url': presigned_url,
                'insight': insight_data,
            })

        return Response(data, status=status.HTTP_200_OK)

    def post(self, request, pk):
        user = request.user
        try:
            patient = Patient.objects.get(pk=pk, clinic=user.clinic)
        except Patient.DoesNotExist:
            return Response({"error": "Patient not found"}, status=status.HTTP_404_NOT_FOUND)

        file_obj = request.FILES.get('file')
        if not file_obj:
            return Response({"error": "No file provided"}, status=status.HTTP_400_BAD_REQUEST)

        allowed_extensions = {'pdf', 'jpg', 'jpeg', 'png', 'bmp', 'tiff', 'webp'}
        original_filename = file_obj.name
        ext = original_filename.rsplit('.', 1)[-1].lower() if '.' in original_filename else ''
        if ext not in allowed_extensions:
            return Response(
                {"error": f"File type not allowed. Allowed: {', '.join(sorted(allowed_extensions))}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if file_obj.size > 10 * 1024 * 1024:
            return Response({"error": "File too large. Maximum size is 10MB."}, status=status.HTTP_400_BAD_REQUEST)

        # Pre-generate the document UUID so the S3 key is unique and tied to the DB record
        import uuid as uuid_module
        doc_id = uuid_module.uuid4()
        s3_filename = f"{doc_id}.{ext}" if ext else str(doc_id)

        s3_key = upload_patient_document(patient.id, file_obj, s3_filename)
        if not s3_key:
            return Response({"error": "Failed to upload file to storage"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        title = (request.data.get('title') or '').strip() or original_filename.rsplit('.', 1)[0]
        category = request.data.get('category', 'Others')

        doc = PatientDocument.objects.create(
            id=doc_id,
            patient=patient,
            clinic=user.clinic,
            uploaded_by=user,
            s3_key=s3_key,
            title=title,
            category=category,
            file_extension=ext,
            file_size=file_obj.size,
        )

        # Kick off async AI processing
        from .ai_service import process_document
        threading.Thread(target=process_document, args=(doc.id,), daemon=True).start()

        return Response({
            'id': str(doc.id),
            'title': doc.title,
            'category': doc.category,
            'file_extension': doc.file_extension,
            'file_size': doc.file_size,
            'uploaded_at': doc.uploaded_at.isoformat(),
            'ai_processed': doc.ai_processed,
        }, status=status.HTTP_201_CREATED)


class PatientDocumentDetailView(views.APIView):
    """DELETE /api/patients/{pk}/documents/{doc_id}/"""
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk, doc_id):
        user = request.user
        try:
            patient = Patient.objects.get(pk=pk, clinic=user.clinic)
        except Patient.DoesNotExist:
            return Response({"error": "Patient not found"}, status=status.HTTP_404_NOT_FOUND)

        try:
            doc = PatientDocument.objects.get(id=doc_id, patient=patient, clinic=user.clinic)
        except PatientDocument.DoesNotExist:
            return Response({"error": "Document not found"}, status=status.HTTP_404_NOT_FOUND)

        delete_s3_key(doc.s3_key)
        doc.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PatientDocumentInsightView(views.APIView):
    """GET /api/patients/{pk}/documents/{doc_id}/insights/"""
    permission_classes = [IsAuthenticated]
    _processing = set()  # tracks doc IDs with active background threads

    def get(self, request, pk, doc_id):
        user = request.user
        try:
            patient = Patient.objects.get(pk=pk, clinic=user.clinic)
        except Patient.DoesNotExist:
            return Response({"error": "Patient not found"}, status=status.HTTP_404_NOT_FOUND)

        try:
            doc = PatientDocument.objects.get(id=doc_id, patient=patient, clinic=user.clinic)
        except PatientDocument.DoesNotExist:
            return Response({"error": "Document not found"}, status=status.HTTP_404_NOT_FOUND)

        # Return cached insight if available
        try:
            insight = doc.insight
            self._processing.discard(str(doc.id))
            return Response({
                'title': insight.title,
                'summary': insight.summary,
                'key_findings': insight.key_findings,
                'risk_flags': insight.risk_flags,
                'tags': insight.tags,
                'created_at': insight.created_at.isoformat(),
            }, status=status.HTTP_200_OK)
        except PatientDocumentInsight.DoesNotExist:
            pass

        # Already being processed in a background thread — just return 202
        if str(doc.id) in self._processing:
            return Response(
                {"status": "processing", "message": "Document is being analyzed. Please check back shortly."},
                status=status.HTTP_202_ACCEPTED,
            )

        # If already processed but no insight exists, it failed — allow retry
        if doc.ai_processed:
            doc.ai_processed = False
            doc.save(update_fields=['ai_processed'])

        # Kick off processing in a background thread and return 202
        from .ai_service import process_document
        self._processing.add(str(doc.id))

        def _run():
            try:
                process_document(doc.id)
            except Exception:
                logger.exception("Background process_document failed for doc %s", doc.id)
            finally:
                self._processing.discard(str(doc.id))

        threading.Thread(target=_run, daemon=True).start()

        return Response(
            {"status": "processing", "message": "Document is being analyzed. Please check back shortly."},
            status=status.HTTP_202_ACCEPTED,
        )


# ============================================================================
# HOSPITALS (SSH) LIST
# ============================================================================

class HospitalListView(views.APIView):
    """GET /api/hospitals/ — list Super Specialty Hospitals from Zoho."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            hospitals = ZohoService.list_hospitals()
            return Response(hospitals, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error("HospitalListView error: %s", e)
            return Response({"error": "Failed to fetch hospitals"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ============================================================================
# DOCUMENT UPLOAD LINK (Patient-facing)
# ============================================================================

class GenerateDocumentUploadLinkView(views.APIView):
    """POST /api/patients/{pk}/document-upload-link/ — generate and send upload link."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        user = request.user
        try:
            patient = Patient.objects.get(pk=pk, clinic=user.clinic)
        except Patient.DoesNotExist:
            return Response({"error": "Patient not found"}, status=status.HTTP_404_NOT_FOUND)

        link = DocumentUploadLink.objects.create(
            patient=patient,
            clinic=user.clinic,
            created_by=user,
            token=DocumentUploadLink.generate_token(),
            expires_at=timezone.now() + timezone.timedelta(days=7),
        )

        # Doctor name for email/SMS
        doctor_name = f"{user.first_name} {user.last_name}".strip() or 'Your Doctor'
        clinic_name = user.clinic.name if user.clinic else 'EzeeHealth'

        email_sent = False
        sms_sent = False

        # Send email
        if patient.email:
            from apps.authentication.email_utils import send_document_upload_link_email
            email_sent = send_document_upload_link_email(
                email=patient.email,
                token=link.token,
                patient_name=patient.full_name,
                clinic_name=clinic_name,
                doctor_name=doctor_name,
            )

        # Send SMS
        if patient.phone:
            from apps.integrations.msg91_service import MSG91Service
            from django.conf import settings as django_settings
            upload_url = f"{django_settings.FRONTEND_URL}/document-upload/{link.token}"
            sms_message = f"Dr. {doctor_name} from {clinic_name} has requested you to upload your medical documents. Upload here: {upload_url}"
            sms_sent = MSG91Service.send_sms(patient.phone, sms_message)

        return Response({
            "message": "Upload link generated",
            "email_sent": email_sent,
            "sms_sent": sms_sent,
        }, status=status.HTTP_200_OK)


@method_decorator(csrf_exempt, name='dispatch')
class VerifyDocumentUploadTokenView(views.APIView):
    """POST /api/document-upload/verify/{token}/ — verify upload token (unauthenticated)."""
    permission_classes = [AllowAny]

    def post(self, request, token):
        try:
            link = DocumentUploadLink.objects.select_related('patient', 'clinic').get(token=token)
        except DocumentUploadLink.DoesNotExist:
            return Response({"error": "Invalid upload link"}, status=status.HTTP_404_NOT_FOUND)

        if not link.is_valid:
            error_msg = "This upload link has expired" if link.is_expired else "This upload link has already been used"
            return Response({"error": error_msg}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "patient_name": link.patient.full_name,
            "clinic_name": link.clinic.name,
            "expires_at": link.expires_at.isoformat(),
        }, status=status.HTTP_200_OK)


@method_decorator(csrf_exempt, name='dispatch')
class DocumentUploadViaTokenView(views.APIView):
    """POST /api/document-upload/{token}/ — upload files via token (unauthenticated)."""
    permission_classes = [AllowAny]

    def post(self, request, token):
        try:
            link = DocumentUploadLink.objects.select_related('patient', 'clinic', 'created_by').get(token=token)
        except DocumentUploadLink.DoesNotExist:
            return Response({"error": "Invalid upload link"}, status=status.HTTP_404_NOT_FOUND)

        if not link.is_valid:
            error_msg = "This upload link has expired" if link.is_expired else "This upload link has already been used"
            return Response({"error": error_msg}, status=status.HTTP_400_BAD_REQUEST)

        files = request.FILES.getlist('files')
        if not files:
            return Response({"error": "No files provided"}, status=status.HTTP_400_BAD_REQUEST)

        category = request.data.get('category', 'Others')
        allowed_extensions = {'pdf', 'jpg', 'jpeg', 'png', 'bmp', 'tiff', 'webp'}
        max_size = 10 * 1024 * 1024

        uploaded = []
        errors = []

        for file_obj in files:
            original_filename = file_obj.name
            ext = original_filename.rsplit('.', 1)[-1].lower() if '.' in original_filename else ''

            if ext not in allowed_extensions:
                errors.append({"file": original_filename, "error": f"File type not allowed. Allowed: {', '.join(sorted(allowed_extensions))}"})
                continue

            if file_obj.size > max_size:
                errors.append({"file": original_filename, "error": "File too large. Maximum size is 10MB."})
                continue

            import uuid as uuid_module
            doc_id = uuid_module.uuid4()
            s3_filename = f"{doc_id}.{ext}" if ext else str(doc_id)

            s3_key = upload_patient_document(link.patient.id, file_obj, s3_filename)
            if not s3_key:
                errors.append({"file": original_filename, "error": "Failed to upload file to storage"})
                continue

            title = original_filename.rsplit('.', 1)[0]

            doc = PatientDocument.objects.create(
                id=doc_id,
                patient=link.patient,
                clinic=link.clinic,
                uploaded_by=link.created_by,
                s3_key=s3_key,
                title=title,
                category=category,
                file_extension=ext,
                file_size=file_obj.size,
            )

            # Kick off async AI processing
            from .ai_service import process_document
            threading.Thread(target=process_document, args=(doc.id,), daemon=True).start()

            uploaded.append({
                'id': str(doc.id),
                'title': doc.title,
                'category': doc.category,
                'file_extension': doc.file_extension,
                'file_size': doc.file_size,
            })

        return Response({
            "uploaded": uploaded,
            "errors": errors,
        }, status=status.HTTP_201_CREATED if uploaded else status.HTTP_400_BAD_REQUEST)


# ============================================================================
# ZOHO WEBHOOK + PATIENT INVITE
# ============================================================================

@method_decorator(csrf_exempt, name='dispatch')
class ZohoWebhookView(views.APIView):
    """POST /api/webhooks/zoho/ — called by Zoho when a Deal is created."""
    permission_classes = [AllowAny]

    def post(self, request):
        # secret = request.headers.get('X-Zoho-Webhook-Secret', '')
        # expected = os.getenv('ZOHO_WEBHOOK_SECRET', '')
        # if expected and secret != expected:
        #     return Response({"error": "Invalid secret"}, status=status.HTTP_403_FORBIDDEN)

        data = request.data
        logger.info(f"Zoho webhook received. Keys: {list(data.keys())} Data: {dict(data)}")

        # Zoho can use various field name styles — check common variations
        def _get(data, *keys):
            for k in keys:
                v = data.get(k)
                if v:
                    return str(v).strip()
            return None

        contact_id = _get(data, 'contact_id', 'Contact_ID', 'contactId', 'contact')
        deal_id    = _get(data, 'deal_id', 'Deal_ID', 'dealId', 'deal')
        lead_id    = _get(data, 'lead_id', 'Lead_ID', 'leadId', 'lead')
        mobile     = _get(data, 'mobile', 'phone', 'Mobile', 'Phone', 'mobile_number')
        email      = _get(data, 'email', 'Email', 'email_address')

        logger.info(f"Zoho webhook parsed: lead_id={lead_id}, deal_id={deal_id}, contact_id={contact_id}, mobile={mobile}")

        # Find the referral episode by zoho_lead_id
        referral = None
        patient = None

        if lead_id:
            referral = Referral.objects.select_related('patient').filter(zoho_lead_id=lead_id).first()
            if referral:
                patient = referral.patient

        # Fallback: find patient by phone/email, use their latest referral
        if not patient and mobile:
            patient = Patient.objects.filter(phone=mobile).first()
        if not patient and email:
            patient = Patient.objects.filter(email=email).first()
        if patient and not referral:
            referral = patient.referrals.first()

        if not patient:
            logger.warning(f"Zoho webhook: no patient found for lead_id={lead_id}, mobile={mobile}, email={email}")
            return Response({"message": "Patient not found, ignored"}, status=status.HTTP_200_OK)

        logger.info(f"Zoho webhook: updating referral for patient {patient.id} ({patient.full_name})")

        if referral:
            if contact_id:
                referral.zoho_contact_id = contact_id
            if deal_id:
                referral.zoho_deal_id = deal_id
            referral.status = 'converted'
            referral.converted_date = timezone.now()
            referral.save()
        else:
            # No referral exists yet — create one to record the deal
            referral = Referral.objects.create(
                patient=patient,
                clinic=patient.clinic,
                zoho_lead_id=lead_id or '',
                zoho_deal_id=deal_id or '',
                zoho_contact_id=contact_id or '',
                status='converted',
                converted_date=timezone.now(),
            )

        _send_patient_invite(patient)

        return Response({"message": "OK"}, status=status.HTTP_200_OK)


class SendPatientInviteView(views.APIView):
    """POST /api/patients/{pk}/send-invite/ — manual invite trigger."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        user = request.user
        try:
            patient = Patient.objects.get(pk=pk, clinic=user.clinic)
        except Patient.DoesNotExist:
            return Response({"error": "Patient not found"}, status=status.HTTP_404_NOT_FOUND)

        invite = _send_patient_invite(patient)
        return Response({
            "message": "Invite sent",
            "invite_code": invite.invitation_code,
        }, status=status.HTTP_200_OK)


def _send_patient_invite(patient):
    """Create PatientInvite and send invitation email. Returns the invite object."""
    from apps.patient_portal.models import PatientInvite
    from apps.authentication.email_utils import generate_invitation_code, send_patient_invitation_email

    clinic_name = patient.clinic.name if patient.clinic else 'EzeeHealth'

    # Find the referring doctor name from the clinic
    from apps.authentication.models import User as AuthUser
    referring_doctor = AuthUser.objects.filter(
        clinic=patient.clinic, role__in=['owner', 'doctor']
    ).first()
    referred_by = (
        f"{referring_doctor.first_name} {referring_doctor.last_name}".strip()
        if referring_doctor else clinic_name
    )

    invite = PatientInvite.objects.create(
        patient=patient,
        invitation_code=generate_invitation_code(),
        phone=patient.phone or '',
        email=patient.email or '',
        name=patient.full_name,
        clinic_name=clinic_name,
        expires_at=timezone.now() + timezone.timedelta(days=30),
    )

    if patient.email:
        send_patient_invitation_email(
            email=patient.email,
            invitation_code=invite.invitation_code,
            patient_name=patient.full_name,
            clinic_name=clinic_name,
            referred_by=referred_by,
        )
    else:
        logger.warning(f"Patient {patient.id} has no email — invitation not sent.")

    return invite
