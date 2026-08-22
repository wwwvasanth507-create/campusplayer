"""
Storage Backend Abstraction Layer for CampusPlayer.

Supports Local File System and S3-Compatible Object Storage backends.
"""
import os
import shutil
import logging
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)

class StorageBackend(ABC):
    @abstractmethod
    def save_bytes(self, relative_path: str, data: bytes) -> str:
        """Save bytes payload to storage path."""
        pass

    @abstractmethod
    def save_stream(self, relative_path: str, stream, chunk_size: int = 4096) -> str:
        """Stream data to storage path without loading entire stream into memory."""
        pass

    @abstractmethod
    def delete_file(self, relative_path: str) -> bool:
        """Delete file at relative path."""
        pass

    @abstractmethod
    def delete_directory(self, relative_dir_path: str) -> bool:
        """Delete directory and all its contents."""
        pass

    @abstractmethod
    def exists(self, relative_path: str) -> bool:
        """Check if relative path exists."""
        pass

    @abstractmethod
    def get_file_size(self, relative_path: str) -> int:
        """Get file size in bytes."""
        pass


class LocalStorageBackend(StorageBackend):
    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir).resolve()
        os.makedirs(self.base_dir, exist_ok=True)

    def _resolve_full_path(self, relative_path: str) -> Path:
        clean_rel = relative_path.lstrip('/\\').replace('\\', '/')
        full_path = (self.base_dir / clean_rel).resolve()
        if not str(full_path).startswith(str(self.base_dir)):
            raise ValueError(f"Path traversal detected: {relative_path}")
        return full_path

    def save_bytes(self, relative_path: str, data: bytes) -> str:
        full_path = self._resolve_full_path(relative_path)
        os.makedirs(full_path.parent, exist_ok=True)
        with open(full_path, 'wb') as f:
            f.write(data)
        return str(full_path)

    def save_stream(self, relative_path: str, stream, chunk_size: int = 65536) -> str:
        full_path = self._resolve_full_path(relative_path)
        os.makedirs(full_path.parent, exist_ok=True)
        with open(full_path, 'wb') as f:
            while True:
                chunk = stream.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
        return str(full_path)

    def delete_file(self, relative_path: str) -> bool:
        try:
            full_path = self._resolve_full_path(relative_path)
            if full_path.is_file():
                os.remove(full_path)
                return True
        except Exception as e:
            logger.warning(f"Failed to delete file {relative_path}: {e}")
        return False

    def delete_directory(self, relative_dir_path: str) -> bool:
        try:
            full_path = self._resolve_full_path(relative_dir_path)
            if full_path.is_dir():
                shutil.rmtree(full_path, ignore_errors=True)
                return True
        except Exception as e:
            logger.warning(f"Failed to delete directory {relative_dir_path}: {e}")
        return False

    def exists(self, relative_path: str) -> bool:
        try:
            full_path = self._resolve_full_path(relative_path)
            return full_path.exists()
        except Exception:
            return False

    def get_file_size(self, relative_path: str) -> int:
        try:
            full_path = self._resolve_full_path(relative_path)
            return os.path.getsize(full_path) if full_path.is_file() else 0
        except Exception:
            return 0


class S3StorageBackend(StorageBackend):
    def __init__(self, bucket_name: str, endpoint_url: str = None, aws_access_key_id: str = None, aws_secret_access_key: str = None, region_name: str = None):
        self.bucket_name = bucket_name
        self.endpoint_url = endpoint_url
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key
        self.region_name = region_name

    def _get_client(self):
        import boto3
        return boto3.client(
            's3',
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.aws_access_key_id,
            aws_secret_access_key=self.aws_secret_access_key,
            region_name=self.region_name
        )

    def save_bytes(self, relative_path: str, data: bytes) -> str:
        s3 = self._get_client()
        clean_key = relative_path.lstrip('/\\').replace('\\', '/')
        s3.put_object(Bucket=self.bucket_name, Key=clean_key, Body=data)
        return f"s3://{self.bucket_name}/{clean_key}"

    def save_stream(self, relative_path: str, stream, chunk_size: int = 65536) -> str:
        s3 = self._get_client()
        clean_key = relative_path.lstrip('/\\').replace('\\', '/')
        s3.upload_fileobj(stream, self.bucket_name, clean_key)
        return f"s3://{self.bucket_name}/{clean_key}"

    def delete_file(self, relative_path: str) -> bool:
        try:
            s3 = self._get_client()
            clean_key = relative_path.lstrip('/\\').replace('\\', '/')
            s3.delete_object(Bucket=self.bucket_name, Key=clean_key)
            return True
        except Exception as e:
            logger.warning(f"S3 delete error for {relative_path}: {e}")
            return False

    def delete_directory(self, relative_dir_path: str) -> bool:
        try:
            s3 = self._get_client()
            clean_prefix = relative_dir_path.lstrip('/\\').replace('\\', '/').rstrip('/') + '/'
            objects = s3.list_objects_v2(Bucket=self.bucket_name, Prefix=clean_prefix)
            if 'Contents' in objects:
                delete_keys = [{'Key': obj['Key']} for obj in objects['Contents']]
                s3.delete_objects(Bucket=self.bucket_name, Delete={'Objects': delete_keys})
            return True
        except Exception as e:
            logger.warning(f"S3 directory delete error for {relative_dir_path}: {e}")
            return False

    def exists(self, relative_path: str) -> bool:
        try:
            s3 = self._get_client()
            clean_key = relative_path.lstrip('/\\').replace('\\', '/')
            s3.head_object(Bucket=self.bucket_name, Key=clean_key)
            return True
        except Exception:
            return False

    def get_file_size(self, relative_path: str) -> int:
        try:
            s3 = self._get_client()
            clean_key = relative_path.lstrip('/\\').replace('\\', '/')
            res = s3.head_object(Bucket=self.bucket_name, Key=clean_key)
            return res.get('ContentLength', 0)
        except Exception:
            return 0


def get_storage_backend(base_dir: str = None) -> StorageBackend:
    """Factory helper returning appropriate StorageBackend based on environment."""
    storage_type = os.getenv('STORAGE_BACKEND', 'local').lower()
    if storage_type == 's3':
        bucket = os.getenv('S3_BUCKET', 'campusplayer-media')
        endpoint = os.getenv('S3_ENDPOINT', None)
        key_id = os.getenv('S3_ACCESS_KEY', None)
        secret_key = os.getenv('S3_SECRET_KEY', None)
        region = os.getenv('S3_REGION', 'us-east-1')
        return S3StorageBackend(
            bucket_name=bucket,
            endpoint_url=endpoint,
            aws_access_key_id=key_id,
            aws_secret_access_key=secret_key,
            region_name=region
        )
    else:
        root_dir = base_dir or os.path.join(os.path.dirname(os.path.dirname(__file__)), 'storage')
        return LocalStorageBackend(base_dir=root_dir)
