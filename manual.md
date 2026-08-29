# Campus Player — Developer & Source Code Master Manual

Welcome to the **Campus Player** Developer & Source Code Master Manual. This comprehensive reference document provides developers, software architects, sysadmins, and maintainers with complete, in-depth insight into the internal architecture, database schema, media processing pipeline, real-time features, security enforcement, API specifications, exact source code implementations, and step-by-step development and deployment workflows of the Campus Player platform.

---

## 1. Executive Overview & Architectural Philosophy

### 1.1 System Vision & Purpose
**Campus Player** is an enterprise-grade, multi-tenant learning management system (LMS) and video streaming platform engineered for educational institutions (schools, colleges, universities, and tutoring centers). It handles high-capacity multi-bitrate HLS video streaming, automated FFmpeg transcoding, interactive live classroom chat, gamification (XP, levels, quests, rewards), native PDF reporting, time-locked attendance tracking, interactive video quizzes, digital e-book library, and AI-assisted lecture copilot capabilities.

### 1.2 Tech Stack & Core Dependencies
- **Backend Framework**: Python 3.10+ / Flask 3.1.3
- **ORM & Database Engine**: Flask-SQLAlchemy 3.1.1, Flask-Migrate (Alembic), SQLite (WAL mode default for local dev) / PostgreSQL 15+ (production grade)
- **Real-Time Communication**: Flask-SocketIO 5.6.1 (Socket.IO engine with Eventlet / Gevent async workers)
- **Asynchronous Task Queue**: Celery 5.6.3 + Redis 8.0.0 (Broker & Result Backend)
- **Media Processing**: FFmpeg / FFprobe (CLI binary invocation via Python `subprocess`)
- **PDF Generation Engine**: ReportLab 4.5.1
- **AI Copilot & Assessment**: Google Generative AI (`google-generativeai` 0.8.3 / Gemini 1.5/2.0 API)
- **Security & Authentication**: Flask-Login 0.6.3, Cryptography 42.0.5 (Fernet symmetric key encryption), Flask-Limiter 4.1.1
- **Frontend & UI Engine**: HTML5, Vanilla CSS3 (Cyber-Glass UI design system with CSS custom properties), Modern JS (ES6+), HLS.js, Chart.js

---

## 2. Directory Structure & Key Code Entry Points

```
c:\campusplayer\
├── app.py                      # Production application entry point & WSGI initialization
├── factory.py                  # Application Factory (`create_app`), extension initialization & middleware
├── extensions.py               # Centralized Flask extensions (db, login_manager, socketio, cache, limiter, etc.)
├── models.py                   # Complete SQLAlchemy models & multi-tenancy query interceptors
├── migrate_db.py               # Safe, non-destructive schema migration engine & legacy multi-tenant backfiller
├── wsgi.py                     # WSGI server loader (Gunicorn / uWSGI)
├── celery_config.py            # Celery broker, result backend, and task queue configurations
├── celery_tasks.py             # Asynchronous task definitions (transcoding, emails, cleanup, reports)
├── crypto_helper.py            # Fernet key encryption & credential obfuscation helpers
├── attendance_utils.py         # Attendance calculation engine & native PDF document renderer
├── audit_platform.py           # Integrity audit & table verification script
├── deploy.sh                   # Hardened Linux/Ubuntu production deployment script
├── deploy_cp1.sh               # CP1 production server deployment & PostgreSQL initialization script
├── setup_ubuntu24.sh           # Automated Ubuntu 24.04 server bootstrapper
├── docker-compose.yml          # Multi-container orchestration (Web, Celery Worker, Celery Beat, Redis, Postgres)
├── Dockerfile                  # Container build instructions for Flask + FFmpeg environment
├── requirements.txt            # Python dependencies
│
├── routes/                     # Modular Flask Blueprints
│   ├── auth.py                 # User authentication (Login, Register, Logout, Password reset)
│   ├── core.py                 # Core dashboards (Student, Teacher, Admin, System Admin)
│   ├── video.py                # Video player pages, watch analytics, comments, playlists, notes, bookmarks
│   ├── upload.py               # Resumable chunk upload endpoints & initialization API
│   ├── search.py               # Multi-entity search engine (Videos, E-Books, Playlists, Subjects)
│   └── api.py                  # RESTful API endpoints for mobile & external telemetry
│
├── services/                   # Business Logic & Infrastructure Engines
│   ├── upload_engine.py        # 20 GB resumable chunk upload manager & chunk assembler
│   ├── conversion_engine.py    # Single-pass FFmpeg multi-bitrate HLS transcoding system
│   ├── ultra_parallel_processor.py # Segmented parallel multi-threaded HLS processing engine
│   ├── security.py             # Security headers, CSRF enforcement, HTTPS redirection
│   ├── session_store.py        # Server-side database session handler (`UserSession`)
│   ├── storage_backend.py      # Abstract storage interface (Local disk / Cloud S3 backend)
│   ├── institution_service.py  # Institution CRUD & domain resolution logic
│   ├── report_engine.py        # ReportLab PDF report generation utilities
│   ├── ai_lecture_copilot.py   # AI doubt answering & flashcard generator via Gemini API
│   └── video_cleanup.py        # Storage cleanup & orphan file removal tasks
```

---

## 3. System Initialization Source Code

### 3.1 Central Extensions Initialization (`extensions.py`)
`extensions.py` instantiates all shared Flask extension singletons used across blueprints, services, and models.

```python
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_caching import Cache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_socketio import SocketIO
from flask_migrate import Migrate

db = SQLAlchemy()
login_manager = LoginManager()
cache = Cache()
limiter = Limiter(key_func=get_remote_address)
socketio = SocketIO(cors_allowed_origins="*")
migrate = Migrate()

# Optional Extensions with Graceful Fallbacks
try:
    from flask_mail import Mail
    mail = Mail()
except ImportError:
    mail = None

try:
    from flasgger import Swagger
    swagger = Swagger()
except ImportError:
    swagger = None

try:
    from flask_assets import Environment
    assets_env = Environment()
except ImportError:
    assets_env = None
```

---

### 3.2 Application Factory Source Code (`factory.py`)
The `create_app` factory initializes configuration variables, mounts database interfaces, registers blueprints, configures session stores, and applies SQLite WAL mode performance pragmas.

```python
import os
import sqlite3
from datetime import timedelta
from flask import Flask
from dotenv import load_dotenv
from extensions import db, login_manager, cache, limiter, socketio, mail, swagger, assets_env, migrate
from services.utils import get_or_create_persistent_secret_key
from services.session_store import SqlAlchemySessionInterface
from sqlalchemy import event
from sqlalchemy.engine import Engine

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

def create_app(test_config=None):
    app = Flask(__name__, static_folder='static', template_folder='templates')
    secret_key = get_or_create_persistent_secret_key(BASE_DIR)

    raw_db_url = os.getenv('DATABASE_URL')
    if not raw_db_url:
        raw_db_url = f'sqlite:///{os.path.join(BASE_DIR, "app.db").replace(chr(92), "/")}'

    if raw_db_url.startswith('postgres://'):
        raw_db_url = raw_db_url.replace('postgres://', 'postgresql://', 1)

    engine_options = {}
    if raw_db_url.startswith('postgresql'):
        engine_options = {
            'pool_size': int(os.getenv('DB_POOL_SIZE', 30)),
            'max_overflow': int(os.getenv('DB_MAX_OVERFLOW', 50)),
            'pool_timeout': int(os.getenv('DB_POOL_TIMEOUT', 30)),
            'pool_pre_ping': True,
            'pool_recycle': 1800
        }
    else:
        engine_options = {'connect_args': {'check_same_thread': False, 'timeout': 60}}

    app.config.update(
        SECRET_KEY=secret_key,
        SQLALCHEMY_DATABASE_URI=raw_db_url,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS=engine_options,
        MAX_CONTENT_LENGTH=1024 * 1024 * 1024 * 1024 * 10,  # Unlimited upload buffer
        CACHE_TYPE='SimpleCache',
        CACHE_DEFAULT_TIMEOUT=300,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(days=30)
    )

    app.session_interface = SqlAlchemySessionInterface()

    register_extensions(app)
    register_blueprints(app)
    register_request_handlers(app)

    return app

def register_extensions(app):
    db.init_app(app)
    migrate.init_app(app, db)

    # SQLite WAL & High-Concurrency Performance Optimization Pragmas
    @event.listens_for(Engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        if isinstance(dbapi_connection, sqlite3.Connection):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA cache_size=-64000")
                cursor.execute("PRAGMA temp_store=MEMORY")
                cursor.execute("PRAGMA mmap_size=268435456")
                cursor.execute("PRAGMA foreign_keys=ON")
            except Exception:
                pass
            finally:
                cursor.close()

    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    socketio.init_app(app)

def register_blueprints(app):
    from routes.auth import auth_bp
    from routes.core import core_bp
    from routes.search import search_bp
    from routes.video import video_bp
    from routes.upload import upload_bp
    from routes.api import api_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(core_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(video_bp)
    app.register_blueprint(upload_bp)
    app.register_blueprint(api_bp)

def register_request_handlers(app):
    from services.security import enforce_https, csrf_protect_request, set_security_headers, update_last_active
    app.before_request(enforce_https)
    app.before_request(csrf_protect_request)
    app.before_request(update_last_active)
    app.after_request(set_security_headers)
```

---

### 3.3 WSGI Server Entry Point Source Code (`wsgi.py`)
`wsgi.py` standardizes application loading across Gunicorn, uWSGI, and PaaS hosts.

```python
"""
WSGI Entrypoint.
Re-exports the canonical app instance from app.py and starts SocketIO server when run as script.
"""
import os
from app import app, socketio  # noqa: F401

if __name__ == '__main__':
    debug = os.getenv('FLASK_DEBUG', 'False').lower() in ('1', 'true', 'yes')
    socketio.run(app, host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=debug)
```

---

### 3.4 Celery Task Queue Initialization (`celery_config.py`)

```python
import os
from celery import Celery
from dotenv import load_dotenv

load_dotenv()

def make_celery(app=None):
    """Factory function for creating Celery background worker instances."""
    celery = Celery(
        'campusplayer',
        broker=os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0'),
        backend=os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0'),
        include=['celery_tasks']
    )
    
    celery.conf.update(
        task_serializer='json',
        accept_content=['json'],
        result_serializer='json',
        timezone='UTC',
        enable_utc=True,
        task_track_started=True,
        task_time_limit=None,       # Prevent worker termination on multi-hour video encodes
        task_soft_time_limit=None,
        worker_max_tasks_per_child=1000,
        worker_prefetch_multiplier=1,
        task_acks_late=True,
    )
    
    if app:
        class ContextTask(celery.Task):
            def __call__(self, *args, **kwargs):
                with app.app_context():
                    return self.run(*args, **kwargs)
        celery.Task = ContextTask
    
    return celery

celery = make_celery()
```

---

### 3.5 Database Migration Initialization Script (`migrate_db.py`)

```python
import os
import sys
from factory import create_app
from extensions import db
from models import *

app = create_app()

def migrate():
    """Add new columns and tables to existing database idempotently with pre-migration backup."""
    with app.app_context():
        sys.stdout.reconfigure(encoding='utf-8')
        
        print("=" * 60)
        print("CampusPlayer Database Migration Engine")
        print("=" * 60)

        # Pre-migration backup execution
        is_testing = app.config.get('TESTING') or os.getenv('TESTING') or os.getenv('FLASK_TESTING')
        if not is_testing:
            from services.backup_engine import create_backup
            ok, backup_res = create_backup(app)
            if not ok:
                print(f"❌ [Migration Error] Pre-migration database backup failed: {backup_res}")
                sys.exit(1)
            print(f"[Backup] Pre-migration backup verified: {backup_res}\n")

        # Run additive table & column synchronization
        db.create_all()
        print("✅ Core table structures synchronized successfully.")

if __name__ == '__main__':
    migrate()
```

---

## 4. Multi-Tenant Query Interceptor Source Code (`models.py`)

Campus Player automatically intercepts and scopes queries and insertions with `institution_id`:

```python
from flask import has_request_context, g
from flask_login import current_user
from sqlalchemy.event import listens_for
from sqlalchemy.orm import Query

def _get_request_tenant_context():
    """Cache and return (is_auth, role, inst_id) on flask.g to prevent redundant user lookups."""
    cached = getattr(g, '_cached_tenant_context', None)
    if cached is not None:
        return cached
    if getattr(g, 'loading_user', False):
        return False, None, None
    g.loading_user = True
    try:
        is_auth = current_user.is_authenticated
        role = current_user.role if is_auth else None
        inst_id = current_user.institution_id if is_auth else None
    except Exception:
        is_auth, role, inst_id = False, None, None
    finally:
        g.loading_user = False
    result = (is_auth, role, inst_id)
    g._cached_tenant_context = result
    return result

@listens_for(Query, "before_compile", retval=True)
def before_compile_listener(query):
    if has_request_context():
        if getattr(g, 'ignore_tenant_filter', False):
            return query
            
        is_auth, role, inst_id = _get_request_tenant_context()
        if is_auth and role != 'system_admin' and inst_id is not None:
            entities_to_filter = set()
            for desc in query.column_descriptions:
                entity = desc.get('entity')
                if not entity and 'expr' in desc:
                    expr = desc['expr']
                    entity = getattr(expr, 'entity', None)
                if entity and hasattr(entity, 'institution_id'):
                    entities_to_filter.add(entity)
            
            for entity in entities_to_filter:
                orig_limit = query._limit_clause
                orig_offset = query._offset_clause
                query = query._clone()
                query._limit_clause = None
                query._offset_clause = None
                query = query.filter(entity.institution_id == inst_id)
                query._limit_clause = orig_limit
                query._offset_clause = orig_offset
    return query

@listens_for(db.Model, 'before_insert', propagate=True)
def before_insert_listener(mapper, connection, target):
    if hasattr(target, 'institution_id') and getattr(target, 'institution_id') is None:
        if has_request_context():
            is_auth, role, inst_id = _get_request_tenant_context()
            if is_auth and role != 'system_admin' and inst_id is not None:
                target.institution_id = inst_id

@listens_for(db.Model, 'before_update', propagate=True)
def before_update_listener(mapper, connection, target):
    if hasattr(target, 'institution_id'):
        if has_request_context():
            is_auth, role, inst_id = _get_request_tenant_context()
            if is_auth and role != 'system_admin' and inst_id is not None:
                target.institution_id = inst_id
```

---

## 5. Security & CSRF Protection Source Code (`services/security.py`)

```python
import secrets
from flask import request, abort, redirect, session, current_app

def enforce_https():
    if current_app.config.get('FORCE_HTTPS'):
        if request.headers.get('X-Forwarded-Proto', 'http') != 'https' and not request.is_secure:
            url = request.url.replace('http://', 'https://', 1)
            return redirect(url, code=301)

def generate_csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return session['csrf_token']

def csrf_protect_request():
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        if request.endpoint and 'static' in request.endpoint:
            return
        token = session.get('csrf_token')
        header_token = request.headers.get('X-CSRFToken') or request.form.get('csrf_token')
        if not token or not header_token or not secrets.compare_digest(token, header_token):
            current_app.logger.warning(f"CSRF validation failed for path {request.path}")
            abort(400, description="Invalid CSRF token")

def set_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response
```

---

## 6. Resumable Upload Engine Source Implementation (`services/upload_engine.py`)

```python
import os
import uuid
import hashlib
import logging
from datetime import datetime
from extensions import db
from models import UploadSession, UploadPart, VideoProcessingJob, Video

logger = logging.getLogger('upload_engine')
DEFAULT_PART_SIZE = 33554432  # 32 MB default chunk size

def init_upload_session(institution_id: int, uploader_id: int, original_filename: str, title: str, total_bytes: int, part_size: int = None, description: str = None, subject: str = None, grade_level: str = None, content_type: str = 'video/mp4') -> UploadSession:
    if part_size is None:
        part_size = DEFAULT_PART_SIZE

    total_parts = (total_bytes + part_size - 1) // part_size if total_bytes > 0 else 1
    upload_id = f"upl_{uuid.uuid4().hex}"
    inst_storage_path = f"institutions/inst_{institution_id}/uploads/{upload_id}"

    session = UploadSession(
        upload_id=upload_id,
        institution_id=institution_id,
        uploader_id=uploader_id,
        original_filename=original_filename,
        title=title or original_filename,
        description=description,
        subject=subject,
        grade_level=grade_level,
        content_type=content_type or 'video/mp4',
        total_bytes=total_bytes,
        part_size=part_size,
        total_parts=total_parts,
        received_parts=0,
        status='initialized',
        storage_path=inst_storage_path,
        created_at=datetime.utcnow()
    )

    db.session.add(session)
    db.session.commit()
    logger.info(f"Initialized UploadSession upload_id={upload_id}, total_parts={total_parts}, total_bytes={total_bytes}")
    return session

def save_upload_part(upload_id: str, part_number: int, part_stream, part_size: int = None) -> UploadPart:
    session = UploadSession.query.filter_by(upload_id=upload_id).first()
    if not session:
        raise ValueError(f"UploadSession {upload_id} not found")

    existing_part = UploadPart.query.filter_by(upload_id=upload_id, part_number=part_number).first()
    if existing_part:
        return existing_part

    part_filename = f"part_{part_number:05d}.tmp"
    part_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], session.storage_path)
    os.makedirs(part_dir, exist_ok=True)
    part_full_path = os.path.join(part_dir, part_filename)

    hasher = hashlib.md5()
    bytes_written = 0

    with open(part_full_path, 'wb') as f:
        while True:
            chunk = part_stream.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            hasher.update(chunk)
            bytes_written += len(chunk)

    part = UploadPart(
        upload_id=upload_id,
        part_number=part_number,
        part_size=bytes_written,
        etag_checksum=hasher.hexdigest(),
        storage_path=f"{session.storage_path}/{part_filename}",
        uploaded_at=datetime.utcnow()
    )

    db.session.add(part)
    session.received_parts = UploadPart.query.filter_by(upload_id=upload_id).count() + 1
    if session.status == 'initialized':
        session.status = 'uploading'

    db.session.commit()
    return part

def assemble_and_finalize_upload(upload_id: str) -> str:
    session = UploadSession.query.filter_by(upload_id=upload_id).first()
    if not session:
        raise ValueError(f"UploadSession {upload_id} not found")

    parts = UploadPart.query.filter_by(upload_id=upload_id).order_by(UploadPart.part_number.asc()).all()
    if len(parts) < session.total_parts:
        raise ValueError(f"Missing parts: expected {session.total_parts}, got {len(parts)}")

    assembled_filename = f"{upload_id}_source.mp4"
    assembled_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], f"institutions/inst_{session.institution_id}/raw")
    os.makedirs(assembled_dir, exist_ok=True)
    assembled_path = os.path.join(assembled_dir, assembled_filename)

    with open(assembled_path, 'wb') as outfile:
        for part in parts:
            part_full_path = os.path.join(current_app.config['UPLOAD_FOLDER'], part.storage_path)
            with open(part_full_path, 'rb') as infile:
                shutil.copyfileobj(infile, outfile)

    session.status = 'completed'
    session.completed_at = datetime.utcnow()
    db.session.commit()
    return assembled_path
```

---

## 7. Transcoding Engine Source Implementation (`services/conversion_engine.py`)

```python
def transcode_to_hls(input_path: str, output_hls_dir: str, video_id: int) -> str:
    os.makedirs(output_hls_dir, exist_ok=True)
    ffmpeg_bin = get_ffmpeg_bin()
    master_playlist_path = os.path.join(output_hls_dir, "master.m3u8")

    meta = probe_video_metadata(input_path)
    h_target = meta['height']

    filters = []
    stream_maps = []
    cmd_args = [ffmpeg_bin, '-y', '-i', input_path]

    selected_renditions = [r for r in RENDITIONS_LADDER if r[2] <= h_target]
    if not selected_renditions:
        selected_renditions = [RENDITIONS_LADDER[0]]  # Fallback 144p

    split_count = len(selected_renditions)
    split_str = f"[0:v]split={split_count}" + "".join([f"[v{i}]" for i in range(split_count)]) + ";"
    filters.append(split_str)

    for i, r in enumerate(selected_renditions):
        name, w, h, b_v, b_max, b_buf, b_a = r
        filters.append(f"[v{i}]scale=w={w}:h={h}[v{i}out]")
        cmd_args.extend([
            '-map', f'[v{i}out]', f'-c:v:{i}', 'libx264', f'-b:v:{i}', b_v, f'-maxrate:v:{i}', b_max, f'-bufsize:v:{i}', b_buf,
            '-map', 'a:0', f'-c:a:{i}', 'aac', f'-b:a:{i}', b_a
        ])
        stream_maps.append(f"v:{i},a:{i},name:{name}")

    cmd_args.extend([
        '-filter_complex', " ".join(filters),
        '-f', 'hls',
        '-hls_time', '4',
        '-hls_playlist_type', 'vod',
        '-hls_flags', 'independent_segments',
        '-hls_segment_filename', os.path.join(output_hls_dir, 'stream_%v_%03d.ts'),
        '-master_pl_name', 'master.m3u8',
        '-var_stream_map', " ".join(stream_maps),
        os.path.join(output_hls_dir, 'stream_%v.m3u8')
    ])

    subprocess.run(cmd_args, check=True)
    return master_playlist_path
```

---

## 8. Asynchronous Celery Worker Source Implementation (`celery_tasks.py`)

```python
import os
import logging
from celery_config import celery
from factory import create_app
from extensions import db
from models import ConversionJob, Video
from services.conversion_engine import transcode_to_hls

app = create_app()
logger = logging.getLogger('celery_tasks')

@celery.task(name='tasks.transcode_video_task', bind=True, max_retries=3)
def transcode_video_task(self, job_id):
    with app.app_context():
        job = ConversionJob.query.filter_by(job_id=job_id).first()
        if not job:
            logger.error(f"ConversionJob {job_id} not found")
            return False

        job.status = 'processing'
        db.session.commit()

        try:
            video = Video.query.get(job.video_id)
            input_path = job.input_file_path
            hls_dir = os.path.join(app.config['HLS_FOLDER'], f"video_{video.id}")

            master_m3u8 = transcode_to_hls(input_path, hls_dir, video.id)
            video.video_path = f"/static/hls/video_{video.id}/master.m3u8"
            video.is_published = True

            job.status = 'completed'
            job.progress_pct = 100
            db.session.commit()
            return True
        except Exception as exc:
            job.status = 'failed'
            job.error_log = str(exc)
            db.session.commit()
            raise self.retry(exc=exc, countdown=60)
```

---

## 9. Ubuntu Production Server Architecture & Execution

### 9.1 Ubuntu Server Topology & Path Specs
- **Production Operating System**: Ubuntu 24.04 LTS Server
- **Primary Deployment Path**: `/opt/campusplayer/cp1` or `/opt/campusplayer`
- **Application User**: `vasanth-v` / `www-data`
- **PostgreSQL Database Target**: Database `campusplayer_cp1`, User `cp1user`
- **Connection URI Format**: `postgresql://cp1user:<PASSWORD>@localhost:5432/campusplayer_cp1`

---

### 9.2 Automated Deployment Script (`deploy_cp1.sh`)
Production deployments follow a 10-step zero-downtime execution script (`deploy_cp1.sh`):

```bash
#!/bin/bash
# =============================================================
#  CampusPlayer CP1 — Hardened Deployment Script
#  Run: sudo bash /opt/campusplayer/cp1/deploy_cp1.sh
# =============================================================
set -e

APP_DIR="/opt/campusplayer/cp1"
VENV="$APP_DIR/venv"
APP_USER="vasanth-v"

echo "[1/10] Verifying PostgreSQL DATABASE_URL in .env..."
DB_URL=$(grep -E '^DATABASE_URL=' "$APP_DIR/.env" | head -1 | cut -d= -f2-)
if [[ "$DB_URL" != postgresql://* ]]; then
    echo "❌ ABORT: cp1 requires PostgreSQL."
    exit 1
fi

echo "[2/10] Verifying storage directories..."
mkdir -p "$APP_DIR/backups" "$APP_DIR/static/uploads" "$APP_DIR/static/hls" "$APP_DIR/generated_pdfs"

echo "[3/10] Running pre-deployment database backup..."
$VENV/bin/python3 -c "from services.backup_engine import create_backup; ok, res = create_backup(); exit(0 if ok else 1)"

echo "[4/10] Pulling latest code changes from origin/main..."
git pull origin main

echo "[5/10] Updating Python virtual environment dependencies..."
$VENV/bin/pip install -r requirements.txt --quiet

echo "[6/10] Capturing pre-migration baseline data audit..."
$VENV/bin/python3 "$APP_DIR/audit_platform.py" --save-baseline

echo "[7/10] Executing database schema migrations..."
$VENV/bin/python3 "$APP_DIR/migrate_db.py"

echo "[8/10] Verifying post-migration data integrity against baseline..."
$VENV/bin/python3 "$APP_DIR/audit_platform.py" --verify-baseline

echo "[9/10] Restarting systemd production services..."
sudo systemctl daemon-reload
sudo systemctl restart campusplayer.service campusplayer-worker.service campusplayer-beat.service

echo "[10/10] Running health check verification..."
$VENV/bin/python3 -c "
import urllib.request, json
req = urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=5)
data = json.loads(req.read().decode())
print('[OK] Application health status:', data.get('status'))
"
```

---

### 9.3 Nginx Reverse Proxy Server Block (`/etc/nginx/sites-available/campusplayer`)

```nginx
server {
    listen 80;
    server_name campusplayer.yourdomain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name campusplayer.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/campusplayer.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/campusplayer.yourdomain.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    client_max_body_size 20G;

    # Static HLS Video Segment Delivery & Browser Caching
    location /static/hls/ {
        alias /opt/campusplayer/cp1/static/hls/;
        add_header Access-Control-Allow-Origin *;
        add_header Cache-Control "public, max-age=31536000, immutable";
        types {
            application/vnd.apple.mpegurl m3u8;
            video/mp2t ts;
        }
    }

    # Static Application Assets
    location /static/ {
        alias /opt/campusplayer/cp1/static/;
        expires 30d;
        add_header Cache-Control "public, no-transform";
    }

    # WebSocket Proxy for Flask-SocketIO
    location /socket.io/ {
        proxy_pass http://127.0.0.1:5000/socket.io/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "Upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Gunicorn App Server Proxy
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 600s;
        proxy_connect_timeout 600s;
    }
}
```

---

### 9.4 Systemd Production Service Files

#### 1. Web Application (`/etc/systemd/system/campusplayer.service`)
```ini
[Unit]
Description=Campus Player Web Application
After=network.target postgresql.service redis.service

[Service]
User=vasanth-v
Group=www-data
WorkingDirectory=/opt/campusplayer/cp1
Environment="PATH=/opt/campusplayer/cp1/venv/bin"
ExecStart=/opt/campusplayer/cp1/venv/bin/gunicorn --worker-class eventlet -w 4 --bind 127.0.0.1:5000 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

#### 2. Celery Worker (`/etc/systemd/system/campusplayer-worker.service`)
```ini
[Unit]
Description=Campus Player Celery Worker
After=network.target redis.service

[Service]
User=vasanth-v
Group=www-data
WorkingDirectory=/opt/campusplayer/cp1
Environment="PATH=/opt/campusplayer/cp1/venv/bin"
ExecStart=/opt/campusplayer/cp1/venv/bin/celery -A celery_tasks.celery worker --loglevel=info -P eventlet -c 4
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

#### 3. Celery Beat Scheduler (`/etc/systemd/system/campusplayer-beat.service`)
```ini
[Unit]
Description=Campus Player Celery Beat Scheduler
After=network.target redis.service

[Service]
User=vasanth-v
Group=www-data
WorkingDirectory=/opt/campusplayer/cp1
Environment="PATH=/opt/campusplayer/cp1/venv/bin"
ExecStart=/opt/campusplayer/cp1/venv/bin/celery -A celery_tasks.celery beat --loglevel=info
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

© 2026 Campus Player Team. All rights reserved.
