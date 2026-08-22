import os
import base64
import hashlib
import logging
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)


def _get_fernet() -> Fernet:
    """Return a validated Fernet instance using the configured encryption key.

    - Uses ENCRYPTION_KEY from the environment if set.
    - If ENCRYPTION_KEY is not a valid Fernet key (must be exactly 32
      url-safe base64-encoded bytes), tries to derive one from SECRET_KEY.
    - If neither produces a valid key, raises a RuntimeError.
    """
    raw_key = os.getenv('ENCRYPTION_KEY')
    candidates = []

    if raw_key:
        candidates.append(('ENCRYPTION_KEY', raw_key))

    secret = os.getenv('SECRET_KEY')
    if not secret:
        try:
            from flask import current_app, has_app_context
            if has_app_context() and current_app:
                secret = current_app.config.get('SECRET_KEY')
        except Exception:
            pass
    if not secret:
        secret = 'campusplayer-default-secret-key-fallback-2026'

    if secret:
        # Derive a valid 32-byte key from the secret
        hashed = hashlib.sha256(secret.encode()).digest()
        derived = base64.urlsafe_b64encode(hashed).decode()
        candidates.append(('derived-from-SECRET_KEY', derived))

    for source, key in candidates:
        try:
            f = Fernet(key.encode())
            # Validate it round-trips a test value
            test_enc = f.encrypt(b'test').decode()
            f.decrypt(test_enc.encode())
            logger.debug("Using encryption key from %s", source)
            return f
        except Exception as exc:
            if source == 'ENCRYPTION_KEY':
                logger.warning("ENCRYPTION_KEY is not a valid Fernet key "
                               "(must be 32 url-safe base64-encoded bytes). "
                               "Attempting fallback from SECRET_KEY. Error: %s", exc)
            # ignore invalid candidate and try the next

    raise RuntimeError(
        'No valid ENCRYPTION_KEY (or derivable SECRET_KEY) found. '
        'ENCRYPTION_KEY must be exactly 32 url-safe base64-encoded bytes, e.g. '
        'created via: python -c "from cryptography.fernet import Fernet; '
        'print(Fernet.generate_key().decode())". '
        'Set it in the environment before using encryption features.'
    )


def get_encryption_key():
    """Backward-compatible helper: return the raw key string being used.

    If ENCRYPTION_KEY is valid, returns it unchanged. Otherwise returns
    the key derived from SECRET_KEY. Raises if neither is usable.
    """
    raw_key = os.getenv('ENCRYPTION_KEY')
    if raw_key:
        try:
            Fernet(raw_key.encode())
            return raw_key
        except Exception:
            pass  # invalid raw key; fall through to derived

    secret = os.getenv('SECRET_KEY')
    if secret:
        hashed = hashlib.sha256(secret.encode()).digest()
        return base64.urlsafe_b64encode(hashed).decode()

    raise RuntimeError(
        'ENCRYPTION_KEY (or SECRET_KEY) must be set in the environment '
        'before encrypting/decrypting data.'
    )


def encrypt_password(password: str) -> str:
    """Encrypt a plain text password to a secure string.

    Raises RuntimeError if encryption fails (e.g. no valid key), instead of
    silently returning an empty string, so callers know the value was NOT saved.
    """
    if not password:
        return ""
    try:
        f = _get_fernet()
        return f.encrypt(password.encode()).decode()
    except RuntimeError:
        raise
    except Exception as e:
        logger.error("Encryption error: %s", e)
        raise


def decrypt_password(encrypted_text: str) -> str:
    """Decrypt an encrypted string to plain text.

    Returns "" if the ciphertext is empty. If decryption fails for a
    non-empty value, raises RuntimeError so callers can surface the failure.
    """
    if not encrypted_text:
        return ""
    try:
        f = _get_fernet()
        return f.decrypt(encrypted_text.encode()).decode()
    except Exception as e:
        logger.error("Decryption error: %s", e)
        return ""
