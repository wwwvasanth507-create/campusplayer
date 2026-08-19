"""
CampusPlayer - Database Backup & Integrity Engine.

Provides atomic WAL-mode database backup, verification, listing, and restore helpers.
"""

import os
import sys
import sqlite3
import shutil
from datetime import datetime

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')


def ensure_backup_dir():
    """Ensure the backups directory exists."""
    os.makedirs(BACKUP_DIR, exist_ok=True)


def get_db_path(app=None):
    """Resolve active database file path from Flask app config or default location."""
    if app:
        db_uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
        if 'sqlite:///' in db_uri:
            path = db_uri.replace('sqlite:///', '')
            if not os.path.isabs(path):
                path = os.path.join(BASE_DIR, path)
            return os.path.abspath(path)

    default_path = os.path.join(BASE_DIR, 'app.db')
    return os.path.abspath(default_path)


def verify_sqlite_file(db_path):
    """
    Verify SQLite database file integrity using PRAGMA quick_check.
    Returns (True, None) if valid, or (False, error_message).
    """
    if not os.path.exists(db_path) or os.path.getsize(db_path) == 0:
        return False, f"Database file missing or 0 bytes: {db_path}"

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        cursor = conn.cursor()
        cursor.execute("PRAGMA quick_check;")
        res = cursor.fetchone()
        conn.close()
        if res and res[0] == 'ok':
            return True, None
        return False, f"Integrity check failed: {res}"
    except Exception as e:
        return False, str(e)


def create_backup(app=None, prefix="campusplayer"):
    """
    Create a timestamped atomic SQLite backup using sqlite3 backup API.
    Returns (success: bool, backup_path_or_err: str).
    """
    ensure_backup_dir()
    db_path = get_db_path(app)

    if not os.path.exists(db_path):
        return False, f"Source database file does not exist: {db_path}"

    timestamp = datetime.utcnow().strftime("%Y-%m-%d_%H%M%S")
    backup_filename = f"{prefix}_{timestamp}.db"
    backup_path = os.path.join(BACKUP_DIR, backup_filename)

    try:
        # Use sqlite3 online backup API to safely copy database even during active writes
        src_conn = sqlite3.connect(db_path, timeout=30)
        dst_conn = sqlite3.connect(backup_path)
        with dst_conn:
            src_conn.backup(dst_conn, pages=100)
        dst_conn.close()
        src_conn.close()

        # Verify created backup file
        ok, err = verify_sqlite_file(backup_path)
        if not ok:
            if os.path.exists(backup_path):
                os.remove(backup_path)
            return False, f"Backup verification failed: {err}"

        print(f"[Backup] Successfully created database backup: {backup_path} ({os.path.getsize(backup_path)} bytes)")
        return True, backup_path

    except Exception as e:
        if os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except Exception:
                pass
        return False, f"Failed to create database backup: {e}"


def list_backups():
    """List all available database backups ordered by modification time descending."""
    ensure_backup_dir()
    backups = []
    for fname in os.listdir(BACKUP_DIR):
        if fname.startswith("campusplayer_") and fname.endswith(".db"):
            fpath = os.path.join(BACKUP_DIR, fname)
            size = os.path.getsize(fpath)
            mtime = datetime.fromtimestamp(os.path.getmtime(fpath)).isoformat()
            backups.append({
                'filename': fname,
                'path': fpath,
                'size_bytes': size,
                'created_at': mtime
            })
    backups.sort(key=lambda x: x['created_at'], reverse=True)
    return backups


if __name__ == '__main__':
    ok, res = create_backup()
    if ok:
        print(f"[SUCCESS] Backup created at {res}")
        sys.exit(0)
    else:
        print(f"[ERROR] Backup creation failed: {res}")
        sys.exit(1)
