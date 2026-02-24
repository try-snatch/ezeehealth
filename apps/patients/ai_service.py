"""
AI service for processing patient documents:
- Text extraction (PyMuPDF for PDF, Gemini Vision for images/scanned PDFs)
- Chunking + embedding with Gemini Embeddings → Endee
- Insight generation with Gemini 2.5 Flash
"""
import os
import logging
import tempfile
import json
import base64

logger = logging.getLogger(__name__)


def _get_genai_client():
    """Return a configured google.genai Client."""
    from google import genai
    return genai.Client(api_key=os.getenv('GOOGLE_GENAI_API_KEY'))


def _get_endee_index(index_name):
    from endee import Endee, Precision

    # print(f"Token: {os.getenv('ENDEE_API_KEY')}")

    nd = Endee(token=os.getenv('ENDEE_API_KEY'))

    # Create the index if it doesn't exist yet
    response = nd.list_indexes()
    existing = [idx["name"] for idx in response["indexes"]]
    if index_name not in existing:
        logger.info(f"Endee index '{index_name}' not found — creating it now (dim=3072, cosine)...")
        nd.create_index(
            name=index_name,
            dimension=3072,      # gemini-embedding-001 output dimension
            space_type='cosine',
            precision=Precision.INT8D
        )
        # Wait until the index is ready
        # import time
        # for _ in range(30):
        #     info = nd.describe(index_name)
        #     if info.name===index_name:
        #         break
        #     time.sleep(2)
        # logger.info(f"Endee index '{index_name}' is ready.")

    return nd.get_index(index_name)


def extract_text(file_bytes, file_ext):
    """
    Extract text from file bytes.
    - PDF: PyMuPDF first; fall back to Gemini Vision OCR if < 100 readable chars
    - Images: Gemini Vision OCR with preprocessing
    """
    file_ext = file_ext.lower().lstrip('.')

    if file_ext == 'pdf':
        try:
            import fitz
            doc = fitz.open(stream=file_bytes, filetype='pdf')
            text = ''
            for page in doc:
                text += page.get_text()
            doc.close()
            if len([c for c in text if c.isprintable() and not c.isspace()]) >= 100:
                logger.info(f"PyMuPDF extracted {len(text)} chars from PDF")
                return text
            logger.info("PyMuPDF text too short, falling back to OCR")
        except Exception as e:
            logger.warning(f"PyMuPDF extraction failed: {e}")

    return _gemini_ocr(file_bytes, file_ext)


def _gemini_ocr(file_bytes, file_ext):
    """Use Gemini Vision for OCR on images or scanned PDFs."""
    try:
        if file_ext == 'pdf':
            import fitz
            doc = fitz.open(stream=file_bytes, filetype='pdf')
            all_text = []
            for page_num in range(min(len(doc), 10)):
                page = doc[page_num]
                mat = fitz.Matrix(2, 2)
                pix = page.get_pixmap(matrix=mat)
                all_text.append(_ocr_image_bytes(pix.tobytes('png')))
            doc.close()
            return '\n\n'.join(filter(None, all_text))
        else:
            return _ocr_image_bytes(file_bytes)
    except Exception as e:
        logger.error(f"Gemini OCR failed: {e}")
        return ''


def _ocr_image_bytes(img_bytes):
    """Run Gemini Vision OCR on raw image bytes with preprocessing."""
    try:
        from PIL import Image, ImageEnhance
        import io

        img = Image.open(io.BytesIO(img_bytes)).convert('RGB')
        # Upscale 2x and enhance contrast/sharpness
        img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
        img = ImageEnhance.Contrast(img).enhance(1.5)
        img = ImageEnhance.Sharpness(img).enhance(1.5)

        buf = io.BytesIO()
        img.save(buf, format='PNG')
        enhanced_bytes = buf.getvalue()

        client = _get_genai_client()
        from google.genai import types

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                types.Part.from_text(text=
                    'Extract all text from this medical document. '
                    'Return only the text content, preserving structure. '
                    'Do not add any commentary.'
                ),
                types.Part.from_bytes(data=enhanced_bytes, mime_type='image/png'),
            ],
        )
        return response.text or ''
    except Exception as e:
        logger.error(f"OCR image failed: {e}")
        return ''


def _embed_texts(texts):
    """
    Embed a list of texts using Gemini Embedding model.
    Returns list of float vectors.
    """
    client = _get_genai_client()
    vectors = []
    for text in texts:
        result = client.models.embed_content(
            model='models/gemini-embedding-001',
            contents=text,
        )
        vectors.append(result.embeddings[0].values)
    return vectors


def chunk_and_embed_document(text, patient_id, doc_id, title='', category=''):
    """Split text into chunks, embed with Gemini, store in Endee."""
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=50)
        chunks = splitter.split_text(text)
        if not chunks:
            logger.warning(f"No chunks produced for doc {doc_id}")
            return 0

        logger.info(f"Embedding {len(chunks)} chunks for doc {doc_id}")
        embeddings = _embed_texts(chunks)

        index = _get_endee_index(f"patient_{patient_id}")

        vectors = [
            {
                'id': f'{doc_id}_chunk_{i}',
                'vector': emb,
                'meta': {
                    'document_id': str(doc_id),
                    'chunk_index': i,
                    'text': chunk,
                    'title': title,
                    'category': category,
                },
                'filter': {'document_id': str(doc_id)}
            }
            for i, (chunk, emb) in enumerate(zip(chunks, embeddings))
        ]

        # Upsert in batches of 100
        batch_size = 100
        for i in range(0, len(vectors), batch_size):
            index.upsert(vectors[i:i + batch_size])

        logger.info(f"Upserted {len(vectors)} chunks for doc {doc_id}")
        return len(vectors)

    except Exception as e:
        logger.error(f"Chunk and embed failed for doc {doc_id}: {e}")
        import traceback
        traceback.print_exc()
        return 0


def generate_insights(doc_id, patient_id):
    """Generate AI insights for a document using Endee vectors + Gemini."""
    try:
        index = _get_endee_index(f'patient_{patient_id}')

        # Use a zero vector + filter to fetch all chunks for this document
        dummy_vector = [0.0] * 3072  # gemini-embedding-001 dimension
        results = index.query(
            vector=dummy_vector,
            top_k=200,
            filter=[{'document_id': {'$eq': str(doc_id)}}]
        )

        # print(results)

        if not len(results):
            logger.warning(f"No Endee chunks found for doc {doc_id}")
            return None

        sorted_matches = sorted(results, key=lambda x: x['meta'].get('chunk_index', 0))
        full_text = '\n'.join([m['meta'].get('text', '') for m in sorted_matches])

        client = _get_genai_client()
        prompt = f"""Analyze the following medical document and return a JSON object with exactly these fields:
{{
  "title": "brief document title (string)",
  "summary": "2-3 sentence summary (string)",
  "key_findings": ["array of key medical findings (strings)"],
  "risk_flags": ["array of concerning findings or risk factors (strings)"],
  "tags": ["severity/priority tags — choose only from: high, medium, low"]
}}

Document text:
{full_text[:15000]}

Return only valid JSON with no markdown fences or extra text."""

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )

        text = response.text.strip()
        # Strip markdown code fences if present
        if text.startswith('```'):
            text = '\n'.join(text.split('\n')[1:])
            if '```' in text:
                text = text[:text.rfind('```')]

        return json.loads(text.strip())

    except Exception as e:
        logger.error(f"Generate insights failed for doc {doc_id}: {e}")
        import traceback
        traceback.print_exc()
        return None


def process_document(document_id):
    """Full pipeline: fetch PatientDocument → extract text → embed → generate insights."""
    from .models import PatientDocument, PatientDocumentInsight
    from .s3_utils import get_s3_client
    from django.conf import settings

    try:
        doc = PatientDocument.objects.get(id=document_id)
    except PatientDocument.DoesNotExist:
        logger.error(f"PatientDocument {document_id} not found")
        return

    try:
        s3 = get_s3_client()
        bucket = settings.AWS_STORAGE_BUCKET_NAME

        file_bytes = b''
        with tempfile.NamedTemporaryFile(suffix=f'.{doc.file_extension}', delete=False) as tmp:
            s3.download_fileobj(bucket, doc.s3_key, tmp)
            tmp_path = tmp.name

        with open(tmp_path, 'rb') as f:
            file_bytes = f.read()

        import os as _os
        _os.unlink(tmp_path)

        text = extract_text(file_bytes, doc.file_extension)
        if not text:
            logger.warning(f"No text extracted from document {document_id}")
            return

        chunk_and_embed_document(
            text=text,
            patient_id=str(doc.patient_id),
            doc_id=str(doc.id),
            title=doc.title,
            category=doc.category
        )

        insights_data = generate_insights(str(doc.id), str(doc.patient_id))
        if insights_data:
            PatientDocumentInsight.objects.update_or_create(
                document=doc,
                defaults={
                    'title': insights_data.get('title', doc.title or 'Document Insights'),
                    'summary': insights_data.get('summary', ''),
                    'key_findings': insights_data.get('key_findings', []),
                    'risk_flags': insights_data.get('risk_flags', []),
                    'tags': insights_data.get('tags', []),
                }
            )

        doc.ai_processed = True
        doc.save(update_fields=['ai_processed'])
        logger.info(f"Successfully processed document {document_id}")

    except Exception as e:
        logger.error(f"Error processing document {document_id}: {e}")
        import traceback
        traceback.print_exc()
