"""
CampusPlayer - Database Backup & Integrity Engine.

PostgreSQL-only. Uses pg_dump for atomic, verified database backups.
Backup is mandatory before migrations — a missing pg_dump binary causes a
hard failure rather than writing a fake marker file and pretending success.
"""

import os
import sys
import shutil
import subprocess
import urllib.parse
from datetime import datetime

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')


def ensure_backup_dir():
    """Ensure the backups directory exists."""
    os.makedirs(BACKUP_DIR, exist_ok=True)


def create_backup(app=None, prefix="campusplayer"):
    """
    Create a timestamped PostgreSQL database backup using pg_dump.

    Returns (True, backup_path) on success, or (False, error_message) on failure.

    IMPORTANT: pg_dump is mandatory. If pg_dump is not available in the system PATH,
    this function returns (False, ...) — it does NOT write a fake marker file.
    A fake-success marker is not a backup and would allow deploy.sh to proceed
    with migrations without any real data protection.
    """
    ensure_backup_dir()

    db_uri = ""
    if app:
        db_uri = str(app.config.get('SQLALCHEMY_DATABASE_URI', ''))
    else:
        db_uri = str(os.getenv('DATABASE_URL', ''))

    is_testing = (app and app.config.get('TESTING')) or os.getenv('TESTING') or os.getenv('FLASK_TESTING')

    if not db_uri.startswith('postgres'):
        return False, f"[Backup] Unsupported database URI scheme '{db_uri[:20]}'. PostgreSQL is strictly required."

    pg_dump_bin = shutil.which('pg_dump')
    if not pg_dump_bin:
        msg = (
            "[Backup] CRITICAL: pg_dump not found in system PATH. "
            "Cannot create a real database backup. "
            "Install postgresql-client (e.g. apt-get install -y postgresql-client) and retry. "
            "Aborting to protect data integrity."
        )
        print(msg)
        return False, msg

    timestamp = datetime.utcnow().strftime("%Y-%m-%d_%H%M%S")
    backup_filename = f"{prefix}_pg_{timestamp}.sql"
    backup_path = os.path.join(BACKUP_DIR, backup_filename)

    try:
        env = os.environ.copy()
        parsed = urllib.parse.urlparse(db_uri)
        if parsed.password:
            env['PGPASSWORD'] = parsed.password

        cmd = [pg_dump_bin, db_uri, '-f', backup_path]
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
            env=env
        )

        if res.returncode == 0 and os.path.exists(backup_path) and os.path.getsize(backup_path) > 0:
            print(f"[Backup] Successfully created PostgreSQL dump: {backup_path} "
                  f"({os.path.getsize(backup_path)} bytes)")
            return True, backup_path
        else:
            err_msg = res.stderr[:500] if res.stderr else f"pg_dump exited with code {res.returncode}"
            # Clean up partial file if created
            if os.path.exists(backup_path):
                try:
                    os.remove(backup_path)
                except Exception:
                    pass
            print(f"[Backup] pg_dump failed: {err_msg}")
            return False, f"pg_dump failed: {err_msg}"

    except subprocess.TimeoutExpired:
        if os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except Exception:
                pass
        return False, "[Backup] pg_dump timed out after 300 seconds."
    except Exception as e:
        if os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except Exception:
                pass
        return False, f"[Backup] pg_dump execution error: {e}"


def list_backups():
    """List all available PostgreSQL database backups ordered by modification time descending."""
    ensure_backup_dir()
    backups = []
    for fname in os.listdir(BACKUP_DIR):
        if fname.startswith("campusplayer_") and fname.endswith(".sql"):
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
