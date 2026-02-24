import boto3
from django.conf import settings
from botocore.exceptions import ClientError
import logging

logger = logging.getLogger(__name__)

def get_s3_client():
    """Returns a boto3 S3 client using settings."""
    return boto3.client(
        's3',
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_S3_REGION_NAME
    )

def get_patient_s3_prefix(patient_id):
    """Standardized prefix for patient folders."""
    return f"patients/{patient_id}/"

def ensure_patient_folder(patient_id):
    """
    Ensures a 'folder' exists in S3 for the given patient ID.
    In S3, folders are just prefixes, so we create a 0-byte placeholder object.
    """
    s3 = get_s3_client()
    bucket_name = settings.AWS_STORAGE_BUCKET_NAME
    prefix = get_patient_s3_prefix(patient_id)
    placeholder_key = f"{prefix}.keep"

    try:
        # Check if folder (prefix) already exists by listing objects
        response = s3.list_objects_v2(Bucket=bucket_name, Prefix=prefix, MaxKeys=1)
        if 'Contents' not in response:
            # Create placeholder
            s3.put_object(Bucket=bucket_name, Key=placeholder_key, Body='')
            logger.info(f"Created S3 folder prefix: {prefix}")
            return True
        return False
    except ClientError as e:
        logger.error(f"Error ensuring S3 folder: {e}")
        return False

def upload_patient_document(patient_id, file_obj, filename):
    """Uploads a file to the patient's S3 folder."""
    s3 = get_s3_client()
    bucket_name = settings.AWS_STORAGE_BUCKET_NAME
    key = f"{get_patient_s3_prefix(patient_id)}{filename}"

    try:
        s3.upload_fileobj(file_obj, bucket_name, key)
        logger.info(f"Uploaded {filename} to {key}")
        return key
    except ClientError as e:
        logger.error(f"Error uploading to S3: {e}")
        return None

def generate_presigned_url(patient_id, filename, expiration=3600):
    """
    Generates a presigned URL to share an S3 object securely.
    :param expiration: Time in seconds for the presigned URL to remain valid
    """
    s3 = get_s3_client()
    bucket_name = settings.AWS_STORAGE_BUCKET_NAME
    key = f"{get_patient_s3_prefix(patient_id)}{filename}"

    try:
        response = s3.generate_presigned_url('get_object',
                                            Params={'Bucket': bucket_name,
                                                    'Key': key},
                                            ExpiresIn=expiration)
        return response
    except ClientError as e:
        logger.error(f"Error generating presigned URL: {e}")
        return None

def delete_patient_document(patient_id, filename):
    """Deletes a file from the patient's S3 folder."""
    s3 = get_s3_client()
    bucket_name = settings.AWS_STORAGE_BUCKET_NAME
    key = f"{get_patient_s3_prefix(patient_id)}{filename}"

    try:
        s3.delete_object(Bucket=bucket_name, Key=key)
        logger.info(f"Deleted {key} from S3")
        return True
    except ClientError as e:
        logger.error(f"Error deleting from S3: {e}")
        return False

def generate_presigned_url_for_key(s3_key, expiration=3600):
    """Generates a presigned URL for an S3 object given its full key."""
    s3 = get_s3_client()
    bucket_name = settings.AWS_STORAGE_BUCKET_NAME
    try:
        response = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket_name, 'Key': s3_key},
            ExpiresIn=expiration
        )
        return response
    except ClientError as e:
        logger.error(f"Error generating presigned URL for key {s3_key}: {e}")
        return None


def delete_s3_key(s3_key):
    """Deletes an S3 object given its full key."""
    s3 = get_s3_client()
    bucket_name = settings.AWS_STORAGE_BUCKET_NAME
    try:
        s3.delete_object(Bucket=bucket_name, Key=s3_key)
        logger.info(f"Deleted S3 object: {s3_key}")
        return True
    except ClientError as e:
        logger.error(f"Error deleting S3 key {s3_key}: {e}")
        return False


def list_patient_documents(patient_id):
    """Lists all files in the patient's S3 folder, excluding the .keep file."""
    s3 = get_s3_client()
    bucket_name = settings.AWS_STORAGE_BUCKET_NAME
    prefix = get_patient_s3_prefix(patient_id)

    try:
        response = s3.list_objects_v2(Bucket=bucket_name, Prefix=prefix)
        documents = []
        if 'Contents' in response:
            for obj in response['Contents']:
                key = obj['Key']
                # Extract filename from key and skip .keep file
                filename = key.replace(prefix, '', 1)
                if filename and filename != '.keep':
                    documents.append({
                        'filename': filename,
                        'key': key,
                        'size': obj['Size'],
                        'last_modified': obj['LastModified']
                    })
        return documents
    except ClientError as e:
        logger.error(f"Error listing S3 documents: {e}")
        return []


# ==================== User Profile Picture Functions ====================

def get_user_profile_picture_key(user_id, filename):
    """
    Generate S3 key for user profile picture.
    Format: profile_pictures/{user_id}/avatar{extension}
    """
    import os
    ext = os.path.splitext(filename)[1]  # Get file extension
    return f"profile_pictures/{user_id}/avatar{ext}"


def upload_user_profile_picture(user_id, file_obj, filename):
    """
    Upload a user's profile picture to S3.
    Returns the S3 key on success, None on failure.
    """
    s3 = get_s3_client()
    bucket_name = settings.AWS_STORAGE_BUCKET_NAME
    key = get_user_profile_picture_key(user_id, filename)

    # Determine content type based on file extension
    content_type_map = {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif'
    }
    import os
    ext = os.path.splitext(filename)[1].lower()
    content_type = content_type_map.get(ext, 'application/octet-stream')

    try:
        s3.upload_fileobj(
            file_obj,
            bucket_name,
            key,
            ExtraArgs={'ContentType': content_type}
        )
        logger.info(f"Uploaded profile picture for user {user_id} to {key}")
        return key
    except ClientError as e:
        logger.error(f"Error uploading profile picture to S3: {e}")
        return None


def delete_user_profile_picture(profile_picture_key):
    """
    Delete a user's profile picture from S3.
    Takes the full S3 key as stored in User.profile_picture.
    """
    if not profile_picture_key:
        return True

    s3 = get_s3_client()
    bucket_name = settings.AWS_STORAGE_BUCKET_NAME

    try:
        s3.delete_object(Bucket=bucket_name, Key=profile_picture_key)
        logger.info(f"Deleted profile picture: {profile_picture_key}")
        return True
    except ClientError as e:
        logger.error(f"Error deleting profile picture from S3: {e}")
        return False


def generate_profile_picture_url(profile_picture_key, expiration=86400):
    """
    Generate a presigned URL for a user's profile picture.
    Default expiration is 24 hours (86400 seconds).
    """
    if not profile_picture_key:
        return None

    s3 = get_s3_client()
    bucket_name = settings.AWS_STORAGE_BUCKET_NAME

    try:
        response = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket_name, 'Key': profile_picture_key},
            ExpiresIn=expiration
        )
        return response
    except ClientError as e:
        logger.error(f"Error generating presigned URL for profile picture: {e}")
        return None
