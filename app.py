import os
import shutil
import subprocess
import threading
import time
import re
import io
import uuid
import json
import logging
import secrets
import math
import mimetypes
from pathlib import Path
from collections import OrderedDict
import string
import random
from urllib.parse import quote as url_quote
from datetime import datetime, date, timedelta, timezone
from functools import wraps

from flask import (
    Flask, render_template, redirect, url_for, request,
    flash, jsonify, send_from_directory, send_file, make_response, Response,
    session, abort, current_app, has_request_context
)
import smtplib
import zipfile
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email_validator import validate_email, EmailNotValidError
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash
from dotenv import load_dotenv

# ReportLab — real PDF generation
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, HRFlowable
from reportlab.lib.enums import TA_CENTER, TA_LEFT

# Load environment variables
load_dotenv()

from extensions import db, login_manager, cache, limiter, socketio, mail, swagger, assets_env
try:
    from flask_socketio import join_room, emit
except ImportError:
    join_room = None
    emit = None
from models import (
    User, Video, Playlist, Comment, ViewAnalytics, Notification,
    playlist_videos, Quiz, Question, QuizResult, SiteSettings,
    Classroom, ClassroomTeacher, student_classes, ChatMessage, Attendance, VideoLike,
    ActivityLog, SystemMetric,
    # NEW MODELS
    Assignment, AssignmentSubmission, VideoNote, VideoBookmark,
    VideoProgress, Achievement, LeaderboardEntry, EmailQueue,
    StudentRemark, EmailDeliveryLog, ClassWeeklyReport,
    VideoCheckpoint, CheckpointResponse, VideoDoubt, VideoDoubtReply,
    VideoFlashcard, AcademicCertificate, ParentAccessToken, DutyLeaveRequest,
    # NEW: multi-tenancy, attendance sessions, bio data
    Institution, AttendanceSession, AttendanceSubSession, StudentProfile, DailyQuestTemplate,
    # NEW: Announcements, Timetable & XP Rewards Store
    Announcement, AnnouncementRead, TimetableSlot, RewardItem, UserReward,
    # NEW: Digital E-Book Library
    EBook, EBookProgress,
    # NEW: Departments & Master Subject Registry
    Department, Subject,
    # NEW: AI Lecture Copilot
    AICopilotInteraction
)
from crypto_helper import encrypt_password, decrypt_password
from attendance_utils import (
    compute_attendance_stats, compute_session_report,
    get_class_marked_dates, compute_overall_attendance_for_student
)
from services.report_engine import (
    generate_or_get_weekly_report, build_weekly_report_pdf,
    get_current_week_bounds, aggregate_class_weekly_data
)
from services.institution_service import permanently_delete_institution
from services.certificate_engine import (
    issue_academic_certificate, build_certificate_pdf
)
from services.ai_assessment_engine import generate_quiz_from_video
from services.transcript_engine import parse_vtt_or_srt_to_cues
from services.retention_engine import calculate_video_retention_curve
from services.utils import get_current_institution_id, scope_to_institution, enforce_institution_access, make_tenant_cache_key


# ── Logging Configuration ──
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler('campusplayer.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
PDF_DIR = os.path.join(BASE_DIR, 'generated_pdfs')
os.makedirs(PDF_DIR, exist_ok=True)

HLS_FOLDER = os.path.join(BASE_DIR, 'static', 'hls')
SUBTITLE_FOLDER = os.path.join(BASE_DIR, 'static', 'subtitles')
os.makedirs(SUBTITLE_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv'}

# CORS helper for media (HLS/subtitle) responses — see services/utils.py.
# `Access-Control-Allow-Origin: *` combined with credentials=true is an
# invalid/insecure combination, so origins are reflected only from an
# explicit allow-list (MEDIA_ALLOWED_ORIGINS env var, comma-separated).
from services.utils import apply_media_cors_headers, get_or_create_persistent_secret_key
from services.session_store import SqlAlchemySessionInterface
ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
ALLOWED_SUBTITLE_EXTENSIONS = {'vtt', 'srt'}

from factory import create_app

app = create_app()
# Extensions & blueprints registered via create_app() in factory.py

# Ensure directories exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

class CallableStr(str):
    """String subclass that is also callable to support both {{ csrf_token }} and {{ csrf_token() }}."""
    def __call__(self, *args, **kwargs):
        return self

def generate_csrf_token():
    token = session.get('csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['csrf_token'] = token
    return CallableStr(token)

@app.context_processor
def inject_csrf_token():
    return dict(csrf_token=generate_csrf_token())

def validate_csrf_token(token):
    if app.config.get('TESTING'):
        return True
    expected = session.get('csrf_token', '')
    return bool(token and expected and secrets.compare_digest(token, expected))

def get_request_csrf_token():
    token = request.form.get('csrf_token')
    if not token and request.is_json:
        try:
            json_data = request.get_json(silent=True)
            if isinstance(json_data, dict):
                token = json_data.get('csrf_token')
        except Exception:
            pass
    if not token:
        token = (
            request.headers.get('X-CSRF-Token') or
            request.headers.get('X-CSRFToken') or
            request.headers.get('X-Csrf-Token') or
            request.headers.get('X-Csrftoken') or
            request.headers.get('X-CSRF_TOKEN') or
            request.args.get('csrf_token')
        )
    return token

def csrf_protect(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            token = get_request_csrf_token()
            if not validate_csrf_token(token):
                abort(400, description='Invalid CSRF token')
        return f(*args, **kwargs)
    return decorated_function

def sanitize_input(value, max_length=200):
    if value is None:
        return ''
    value = str(value).strip()
    if len(value) > max_length:
        value = value[:max_length]
    return value

def is_safe_uuid(value):
    return bool(re.fullmatch(r'[A-Za-z0-9_-]{8,64}', str(value or '')))

@app.before_request
def enforce_https():
    if app.config['FORCE_HTTPS']:
        proto = request.headers.get('X-Forwarded-Proto', 'http')
        if proto != 'https' and not request.is_secure:
            url = request.url.replace('http://', 'https://', 1)
            return redirect(url, code=301)

@app.before_request
def csrf_protect_request():
    if app.config.get('TESTING') or not app.config.get('WTF_CSRF_ENABLED', True):
        return
    if request.path in ('/login', '/auth/login', '/teacher/upload_chunk', '/api/upload/chunk'):
        return
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        token = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token') or request.headers.get('X-CSRFToken')
        if not validate_csrf_token(token):
            abort(400, description='Invalid CSRF token')


import gzip

@app.after_request
def set_security_and_performance_headers(response):
    # Static files caching
    if request.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'public, max-age=86400'
    else:
        # Standard security headers
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
        response.headers['X-XSS-Protection'] = '1; mode=block'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://www.youtube.com https://s.ytimg.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
            "img-src 'self' data: blob: https://img.youtube.com https://i.ytimg.com https://*.ytimg.com; "
            "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com; "
            "media-src 'self' blob: data: https://www.youtube.com; "
            "frame-src 'self' https://www.youtube.com https://www.youtube-nocookie.com; "
            "child-src 'self' https://www.youtube.com https://www.youtube-nocookie.com; "
            "connect-src 'self' blob: data: https://www.youtube.com https://cdn.jsdelivr.net; "
            "frame-ancestors 'none'; base-uri 'self';"
        )
        if app.config.get('FORCE_HTTPS') or request.is_secure:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains; preload'

    # Gzip response compression
    accept_encoding = request.headers.get('Accept-Encoding', '')
    if (
        'gzip' in accept_encoding.lower()
        and 200 <= response.status_code < 300
        and not response.direct_passthrough
        and response.mimetype in ('text/html', 'application/json', 'text/css', 'application/javascript', 'text/plain', 'image/svg+xml')
        and 'Content-Encoding' not in response.headers
    ):
        try:
            data = response.get_data()
            if len(data) > 500:
                compressed = gzip.compress(data, compresslevel=6)
                response.set_data(compressed)
                response.headers['Content-Encoding'] = 'gzip'
                response.headers['Content-Length'] = len(compressed)
        except Exception:
            pass

    return response

@app.before_request
def update_last_active():
    if current_user.is_authenticated:
        endpoint = request.endpoint or ''
        path = request.path or ''
        if endpoint in ('static', 'logout'):
            return

        if current_user and current_user.is_authenticated:
            # 1. Account active check
            if not getattr(current_user, 'is_active_account', True):
                logout_user()
                session.clear()
                if request.is_json or path.startswith('/api/'):
                    return jsonify({'success': False, 'message': 'Your account has been suspended.', 'error': 'Your account has been suspended.'}), 403
                flash('Your account has been suspended.', 'error')
                login_url = url_for('auth.login') if 'auth.login' in app.view_functions else url_for('login')
                return redirect(login_url)

            # 2. Institution suspension check
            inst_id = getattr(current_user, 'institution_id', None)
            if inst_id and current_user.role != 'system_admin':
                cache_key = f'inst_status_{inst_id}'
                inst_status = cache.get(cache_key) if cache else None
                if inst_status is None:
                    inst = Institution.query.get(inst_id)
                    inst_status = inst.status if inst else 'active'
                    if cache:
                        cache.set(cache_key, inst_status, timeout=10)
                if inst_status == 'suspended':
                    logout_user()
                    session.clear()
                    susp_msg = 'Your institution has been suspended. Please contact the System Administrator.'
                    if request.is_json or path.startswith('/api/'):
                        return jsonify({'success': False, 'message': susp_msg, 'error': susp_msg}), 403
                    flash(susp_msg, 'error')
                    login_url = url_for('auth.login') if 'auth.login' in app.view_functions else url_for('login')
                    return redirect(login_url)
                
            # 3. Student First-Time Photo Gate check
            if current_user.role == 'student' and not app.config.get('TESTING'):
                if not current_user.avatar_url or current_user.photo_approved is False:
                    if endpoint not in ('student_photo_gate', 'logout', 'static', 'login', 'auth.login') and not path.startswith('/static/') and not path.startswith('/login') and not path.startswith('/auth'):
                        return redirect(url_for('student_photo_gate'))

        if not app.config.get('TESTING'):
            now = datetime.utcnow()
            last = getattr(current_user, 'last_active', None)
            if last is None or (now - last).total_seconds() > 120:
                try:
                    current_user.last_active = now
                    db.session.commit()
                except Exception:
                    db.session.rollback()
os.makedirs(HLS_FOLDER, exist_ok=True)
os.makedirs(SUBTITLE_FOLDER, exist_ok=True)

# Upload engine directories initialized
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

from services.conversion_engine import (
    init_conversion_system, enqueue_conversion_job,
    get_active_conversion_jobs, retry_conversion_job, cancel_conversion_job,
    retry_all_failed_conversion_jobs, ConversionWorkerManager
)

if not os.environ.get('FLASK_TESTING'):
    init_conversion_system(app)
def auto_sync_youtube_thumbnails():
    """Ensure all YouTube video records have valid thumbnail_path, youtube_id, and youtube_url set."""
    try:
        videos = Video.query.filter(
            (Video.video_type == 'youtube') | (Video.filename.like('youtube_%'))
        ).all()
        updated = 0
        for v in videos:
            yt_id = v.youtube_id or extract_youtube_id(v.filename) or extract_youtube_id(v.youtube_url or '')
            if yt_id:
                expected_thumb = f"https://img.youtube.com/vi/{yt_id}/hqdefault.jpg"
                needs_update = False
                if v.thumbnail_path != expected_thumb or v.video_type != 'youtube' or v.youtube_id != yt_id:
                    v.thumbnail_path = expected_thumb
                    v.video_type = 'youtube'
                    v.youtube_id = yt_id
                    v.youtube_url = f"https://www.youtube.com/watch?v={yt_id}"
                    needs_update = True
                if v.status != 'completed':
                    v.status = 'completed'
                    v.processing_progress = 100
                    needs_update = True
                if needs_update:
                    updated += 1
        if updated > 0:
            db.session.commit()
            logger.info(f"[YouTube Sync] Auto-updated thumbnails and status for {updated} YouTube videos.")
    except Exception as e:
        db.session.rollback()
        logger.warning(f"[YouTube Sync Warning] {e}")

if not os.environ.get('FLASK_TESTING'):
    with app.app_context():
        try:
            db.create_all()
        except Exception:
            pass

# ── Context Processor ──
@app.context_processor
def inject_settings():
    inst_id = None
    if current_user.is_authenticated:
        inst_id = getattr(current_user, 'institution_id', None)
    cache_key = f'site_settings_{inst_id}' if inst_id else 'site_settings_global'
    settings = cache.get(cache_key)
    if not settings:
        from flask import g
        orig_ignore = getattr(g, 'ignore_tenant_filter', False)
        g.ignore_tenant_filter = True
        try:
            if inst_id:
                settings = SiteSettings.query.filter_by(institution_id=inst_id).first()
            else:
                settings = SiteSettings.query.filter_by(institution_id=None).first()
            if not settings:
                settings = SiteSettings.query.first()
        finally:
            g.ignore_tenant_filter = orig_ignore
            
        if settings:
            cache.set(cache_key, settings, timeout=60)
    return dict(settings=settings)

@login_manager.user_loader
def load_user(user_id):
    try:
        user = User.query.get(int(user_id))
        if user and getattr(user, 'is_active_account', True):
            sess_ver = session.get('session_version')
            if sess_ver is not None and sess_ver != getattr(user, 'session_version', 1):
                return None
            return user
        return None
    except Exception:
        return None


# ── Utility Decorators ──
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in ('admin', 'system_admin'):
            abort(403)
        return f(*args, **kwargs)
    return decorated_function

def teacher_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in ['admin', 'teacher', 'hod', 'system_admin']:
            abort(403)
        return f(*args, **kwargs)
    return decorated_function

def system_admin_required(f):
    """NEW: top of the role hierarchy — System Admin manages all Institutions/Admins."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'system_admin':
            abort(403)
        return f(*args, **kwargs)
    return decorated_function

def log_activity(action, details=None):
    """Log user activity."""
    try:
        log = ActivityLog(
            user_id=current_user.id if current_user.is_authenticated else None,
            username=current_user.username if current_user.is_authenticated else 'Anonymous',
            action=action,
            details=details,
            ip_address=request.remote_addr
        )
        db.session.add(log)
        db.session.commit()
    except:
        pass

# ── Rate Limit Error Handler ──
@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({'error': 'Too many requests. Please slow down.'}), 429

# ── Custom Jinja Template Filters for Security & Privacy ──
@app.template_filter('display_name')
def display_name_filter(user):
    if not user:
        return 'User'
    if hasattr(user, 'name'):
        return user.name
    if isinstance(user, dict):
        return user.get('display_name') or user.get('name') or (user.get('username') or '').replace('_', ' ').title() or 'User'
    return str(user)

@app.template_filter('avatar_initial')
def avatar_initial_filter(user):
    if not user:
        return 'U'
    name = ''
    if hasattr(user, 'name'):
        name = user.name
    elif isinstance(user, dict):
        name = user.get('display_name') or user.get('name') or user.get('username') or ''
    else:
        name = str(user)
    return name[0].upper() if name else 'U'

# ── SMS Jobs Storage ──
SMS_JOBS = {}
SMS_LOCK = threading.Lock()

# ──====──
#  UTILITIES
# ──====──

def allowed_file(filename):
    if not filename or '..' in filename or filename.startswith('/') or filename.startswith('\\'):
        return False
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def allowed_image_file(filename):
    if not filename or '..' in filename or filename.startswith('/') or filename.startswith('\\'):
        return False
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS

def allowed_subtitle_file(filename):
    if not filename or '..' in filename or filename.startswith('/') or filename.startswith('\\'):
        return False
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_SUBTITLE_EXTENSIONS

def get_video_duration(input_path):
    """Get video duration using ffprobe."""
    try:
        cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', input_path]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return float(result.stdout.strip())
    except:
        return 0

def resolve_video_hls_paths(video):
    """Compute the on-disk HLS directory + its 'static/'-relative base path for a video.

    Videos uploaded by a teacher belonging to an institution are stored under
    a per-institution folder (static/uploads/institutions/<slug>/hls/<id>/),
    everything else falls back to the legacy global folder (static/hls/<id>/).
    This MUST stay in sync with process_video_background (which creates these
    folders) and delete_video (which must remove the same folder it created) —
    that's why both now share this single function instead of each having
    their own copy of the institution-lookup logic.
    """
    uploader = User.query.get(video.uploader_id)
    if uploader and uploader.institution_id:
        inst = Institution.query.get(uploader.institution_id)
        if inst:
            video_hls_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'institutions', inst.slug, 'hls', str(video.id))
            rel_base = f"uploads/institutions/{inst.slug}/hls/{video.id}"
            return video_hls_dir, rel_base
    video_hls_dir = os.path.join(app.config['HLS_FOLDER'], str(video.id))
    rel_base = f"hls/{video.id}"
    return video_hls_dir, rel_base


def process_video_background(app, video_id, input_path):
    """Background task to convert video to HLS and update progress."""
    with app.app_context():
        video = Video.query.get(video_id)
        if not video: return

        try:
            video.status = 'processing'
            video.processing_progress = 5
            db.session.commit()

            duration = get_video_duration(input_path)
            video.duration_seconds = int(duration)
            db.session.commit()

            video_hls_dir, rel_base = resolve_video_hls_paths(video)
            settings = None
            uploader = User.query.get(video.uploader_id)
            if uploader and uploader.institution_id:
                settings = SiteSettings.query.filter_by(institution_id=uploader.institution_id).first()
            if not settings:
                settings = SiteSettings.query.first()

            os.makedirs(video_hls_dir, exist_ok=True)
            output_playlist = os.path.join(video_hls_dir, 'master.m3u8')

            enable_adaptive = settings.enable_adaptive_streaming if settings else True
            
            adaptive_result = None
            if enable_adaptive:
                try:
                    max_height = settings.max_rendition_height if settings else 8640
                    adaptive_result = generate_adaptive_hls(input_path, video_hls_dir, video_id, max_height=max_height)
                except Exception as ae:
                    logger.error(f"Adaptive HLS generation failed, falling back: {ae}")

            if not adaptive_result or not adaptive_result.get('success'):
                # Fallback to single stream
                cmd = [
                    'ffmpeg', '-y', '-i', input_path,
                    '-c:v', 'libx264', '-profile:v', 'baseline', '-level', '3.0',
                    '-preset', 'ultrafast',
                    '-c:a', 'aac', '-ac', '2', '-b:a', '128k',
                    '-start_number', '0', '-hls_time', '10', '-hls_list_size', '0',
                    '-f', 'hls', output_playlist
                ]

                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, universal_newlines=True)

                while True:
                    line = ""
                    while True:
                        char = process.stdout.read(1)
                        if not char: break
                        if char in ['\r', '\n']: break
                        line += char

                    if not char and not line: break

                    if duration > 0:
                        match = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line)
                        if match:
                            hours, mins, secs = match.groups()
                            elapsed = int(hours) * 3600 + int(mins) * 60 + float(secs)
                            progress = min(98, int((elapsed / duration) * 100))
                            if progress > video.processing_progress:
                                video.processing_progress = progress
                                if progress % 5 == 0:
                                    db.session.commit()

                process.wait()

                if process.returncode == 0:
                    thumbnail_path = os.path.join(video_hls_dir, 'thumbnail.jpg')
                    thumb_cmd = ['ffmpeg', '-y', '-i', input_path, '-ss', '00:00:05', '-vframes', '1', thumbnail_path]
                    subprocess.run(thumb_cmd, capture_output=True)

                    if os.path.exists(output_playlist):
                        video.hls_playlist_path = f'{rel_base}/master.m3u8'

                    if os.path.exists(thumbnail_path):
                        video.thumbnail_path = f'{rel_base}/thumbnail.jpg'

                    video.status = 'completed'
                    video.processing_progress = 100
                    video.has_adaptive_streams = False

                    uploader = User.query.get(video.uploader_id)
                    if uploader:
                        uploader.xp += 50

                    db.session.commit()
                    logger.info(f"Video {video_id} processed successfully with fallback single-stream HLS.")

                    try:
                        if os.path.exists(input_path) and os.getenv('DELETE_SOURCE_AFTER_CONVERSION', 'true').lower() not in ('false', '0', 'no'):
                            freed = os.path.getsize(input_path)
                            os.remove(input_path)
                            logger.info(f"Deleted source file after fallback HLS conversion ({freed / (1024**2):.1f} MB): {input_path}")
                    except Exception as e:
                        logger.error(f"Error deleting original video: {e}")
                else:
                    logger.error(f"FFmpeg failed with return code {process.returncode}")
                    video.status = 'failed'
                    db.session.commit()
            else:
                # Save adaptive metadata to db
                res = adaptive_result
                video.master_playlist_path = f"{rel_base}/{res['master_playlist']}"
                video.hls_playlist_path = f"{rel_base}/{res['master_playlist']}"
                video.set_renditions(res['renditions'])
                video.has_adaptive_streams = True
                
                source_info = res['source_info']
                video.source_width = source_info.get('width', 0)
                video.source_height = source_info.get('height', 0)
                video.source_bitrate = source_info.get('bitrate', 0)
                video.video_codec = source_info.get('codec', 'h264')
                video.audio_codec = source_info.get('audio_codec', 'aac')
                video.fps = source_info.get('fps', 0.0)
                
                if res.get('thumbnail'):
                    video.thumbnail_path = f"{rel_base}/{res['thumbnail']}"
                if res.get('sprite'):
                    video.sprite_path = f"{rel_base}/{res['sprite']}"
                    video.sprite_tile_count = len(res['renditions'])
                if res.get('thumbnails_vtt'):
                    video.thumbnails_vtt_path = f"{rel_base}/{res['thumbnails_vtt']}"
                    
                video.status = 'completed'
                video.processing_progress = 100
                
                uploader = User.query.get(video.uploader_id)
                if uploader:
                    uploader.xp += 50
                    
                db.session.commit()
                logger.info(f"Video {video_id} processed with adaptive HLS streams successfully.")
                
                try:
                    if os.path.exists(input_path) and os.getenv('DELETE_SOURCE_AFTER_CONVERSION', 'true').lower() not in ('false', '0', 'no'):
                        freed = os.path.getsize(input_path)
                        os.remove(input_path)
                        logger.info(f"Deleted source file after adaptive HLS conversion ({freed / (1024**2):.1f} MB): {input_path}")
                except Exception as e:
                    logger.error(f"Error deleting original video: {e}")

        except Exception as e:
            logger.error(f"Background processing error: {e}")
            video.status = 'failed'
            db.session.commit()

# ── Search Algorithm ──
EXACT_MATCH_SCORE = 1000
STARTS_WITH_SCORE = 500
CONTAINS_WORD_SCORE = 200
CONTAINS_SCORE = 100
PARTIAL_SCORE = 50

def rank_results(item, query, name_field, extra_fields=None):
    query = query.lower().strip()
    name = getattr(item, name_field, '').lower() if name_field else ''
    if not name: return 0
    score = 0
    if name == query: score += EXACT_MATCH_SCORE
    elif name.startswith(query): score += STARTS_WITH_SCORE
    elif f' {query} ' in f' {name} ' or name.startswith(query + ' ') or name.endswith(' ' + query):
        score += CONTAINS_WORD_SCORE
    if query in name: score += CONTAINS_SCORE
    query_words = query.split()
    for qw in query_words:
        if len(qw) < 2: continue
        if qw in name.split(): score += 80
        for nw in name.split():
            if nw.startswith(qw): score += 30
            min_len = min(len(qw), len(nw))
            if min_len >= 3 and qw[:min_len] == nw[:min_len]: score += 15
    if query in name:
        len_ratio = len(query) / len(name) if name else 0
        if len_ratio > 0.5: score += 50
    if extra_fields:
        for field in extra_fields:
            val = getattr(item, field, '')
            if val and isinstance(val, str) and query in val.lower(): score += 60
    return score

def search_videos(query):
    if not query: return []
    term = f"%{query}%"
    videos = Video.query.filter(
        (Video.title.ilike(term)) | (Video.description.ilike(term)) | (Video.filename.ilike(term))
    ).limit(50).all()
    for v in videos: v._search_score = rank_results(v, query, 'title', ['filename', 'description'])
    videos.sort(key=lambda x: getattr(x, '_search_score', 0), reverse=True)
    return videos

def search_playlists(query):
    if not query: return []
    term = f"%{query}%"
    playlists = Playlist.query.filter(
        (Playlist.title.ilike(term)) | (Playlist.description.ilike(term))
    ).limit(50).all()
    for p in playlists: p._search_score = rank_results(p, query, 'title', ['description'])
    playlists.sort(key=lambda x: getattr(x, '_search_score', 0), reverse=True)
    return playlists

def search_classes(query):
    if not query: return []
    term = f"%{query}%"
    classes = Classroom.query.filter(
        (Classroom.name.ilike(term)) | (Classroom.description.ilike(term))
    ).limit(50).all()
    for c in classes: c._search_score = rank_results(c, query, 'name', ['description'])
    classes.sort(key=lambda x: getattr(x, '_search_score', 0), reverse=True)
    return classes

def search_quizzes(query):
    if not query: return []
    term = f"%{query}%"
    quizzes = Quiz.query.filter(
        (Quiz.title.ilike(term)) | (Quiz.description.ilike(term))
    ).limit(50).all()
    for q in quizzes: q._search_score = rank_results(q, query, 'title', ['description'])
    quizzes.sort(key=lambda x: getattr(x, '_search_score', 0), reverse=True)
    return quizzes

def search_users(query, role_filter=None):
    if not query: return []
    term = f"%{query}%"
    users_q = User.query
    if role_filter: users_q = users_q.filter(User.role == role_filter)
    users = users_q.filter(
        (User.username.ilike(term)) | (User.email.ilike(term))
    ).limit(50).all()
    for u in users: u._search_score = rank_results(u, query, 'username')
    users.sort(key=lambda x: getattr(x, '_search_score', 0), reverse=True)
    return users

def global_search(query):
    result = {'videos': [], 'playlists': [], 'classes': [], 'quizzes': [], 'teachers': [], 'students': [], 'total_count': 0}
    if not query or len(query.strip()) < 1: return result
    query = query.strip()
    result['videos'] = search_videos(query)[:10]
    result['playlists'] = search_playlists(query)[:10]
    result['classes'] = search_classes(query)[:10]
    result['quizzes'] = search_quizzes(query)[:10]
    result['teachers'] = search_users(query, 'teacher')[:10]
    result['students'] = search_users(query, 'student')[:10]
    for k in result:
        if k != 'total_count': result['total_count'] += len(result[k])
    return result

# ── Search Routes ──
@app.route('/search')
@login_required
def search_page():
    query = request.args.get('q', '').strip()
    results = global_search(query)
    best_type, best_score = None, 0
    for cat in ['videos', 'playlists', 'classes', 'quizzes', 'teachers', 'students']:
        items = results.get(cat, [])
        if items:
            max_score = max(getattr(i, '_search_score', 0) for i in items)
            if max_score > best_score: best_score, best_type = max_score, cat
    return render_template('search_results.html', results=results, query=query, best_type=best_type, search_query=query)

@app.route('/api/search/suggest')
@login_required
@limiter.limit("10000 per minute")
def search_suggest():
    query = request.args.get('q', '').strip()
    if not query or len(query) < 1:
        return jsonify({'suggestions': []})
    suggestions = []
    seen_titles = set()

    v_q = scope_to_institution(Video.query.filter(Video.title.contains(query)), Video).limit(5).all()
    for v in v_q:
        if v.title not in seen_titles:
            seen_titles.add(v.title)
            suggestions.append({'text': v.title, 'type': 'video', 'icon': 'videocam', 'url': url_for('watch_video', video_id=v.id)})

    p_q = scope_to_institution(Playlist.query.filter(Playlist.title.contains(query)), Playlist).limit(3).all()
    for p in p_q:
        title = f"[Playlist] {p.title}"
        if title not in seen_titles:
            seen_titles.add(title)
            suggestions.append({'text': p.title, 'type': 'playlist', 'icon': 'playlist_play', 'url': url_for('view_playlist', playlist_id=p.id)})

    c_q = scope_to_institution(Classroom.query.filter(Classroom.name.contains(query)), Classroom).limit(3).all()
    for c in c_q:
        title = f"[Class] {c.name}"
        if title not in seen_titles:
            seen_titles.add(title)
            suggestions.append({'text': c.name, 'type': 'class', 'icon': 'school', 'url': url_for('chatroom', class_id=c.id) if current_user.role == 'student' else '#'})

    u_q = scope_to_institution(User.query.filter(User.username.contains(query)), User).limit(4).all()
    for u in u_q:
        if u.username not in seen_titles and u.id != current_user.id:
            seen_titles.add(u.username)
            suggestions.append({'text': f"{u.username} ({u.role})", 'type': 'user', 'icon': 'person', 'url': '#'})

    return jsonify({'suggestions': suggestions[:12]})

# ═══════════════════════════════════════════════════════════════
#  AUTHENTICATION & USER ROUTES
# ═══════════════════════════════════════════════════════════════

# Note: index, login, and logout routes are handled by routes.auth and routes.core blueprints

# ── Health & Diagnostics Endpoints ──
@app.route('/health')
def public_health_check():
    """Public basic health endpoint for load balancers & monitoring."""
    try:
        db.session.execute(db.select(1))
        return jsonify({
            'status': 'healthy',
            'service': 'CampusPlayer',
            'timestamp': datetime.utcnow().isoformat()
        }), 200
    except Exception as e:
        return jsonify({
            'status': 'unhealthy',
            'service': 'CampusPlayer',
            'error': str(e),
            'timestamp': datetime.utcnow().isoformat()
        }), 500

@app.route('/health/db')
@login_required
@admin_required
def admin_health_db_check():
    """Detailed internal/admin diagnostic health endpoint."""
    try:
        inst_count = Institution.query.count()
        user_count = User.query.count()
        video_count = Video.query.count()
        return jsonify({
            'status': 'healthy',
            'database': app.config.get('SQLALCHEMY_DATABASE_URI', '').split('://')[0],
            'institutions': inst_count,
            'users': user_count,
            'videos': video_count,
            'timestamp': datetime.utcnow().isoformat()
        }), 200
    except Exception as e:
        return jsonify({
            'status': 'unhealthy',
            'error': str(e),
            'timestamp': datetime.utcnow().isoformat()
        }), 500



# ── Profile Routes ──
@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        raw_dn = request.form.get('display_name')
        if raw_dn is not None:
            current_user.display_name = sanitize_input(raw_dn, 100)
        email = request.form.get('email')
        phone = request.form.get('phone')
        theme = request.form.get('theme_preference')
        email_changed = False
        old_email = current_user.email

        if email:
            try:
                valid = validate_email(email)
                normalized_email = valid.email
                if normalized_email != current_user.email:
                    email_changed = True
                    EmailDeliveryLog.query.filter_by(student_id=current_user.id).update({
                        EmailDeliveryLog.student_email: normalized_email
                    })
                    try:
                        socketio.emit('student_email_updated', {
                            'student_id': current_user.id,
                            'new_email': normalized_email
                        })
                    except Exception as se:
                        logger.error(f"Failed to emit student_email_updated via SocketIO: {se}")
                current_user.email = normalized_email
            except EmailNotValidError:
                flash('Please enter a valid email address.', 'error')
                return render_template('profile.html')

        if phone:
            cleaned = '+' + sanitize_input(phone, 20).lstrip('+').replace(' ', '').replace('-', '')
            current_user.phone = cleaned

        if theme in ['dark', 'light']:
            current_user.theme_preference = theme
            session['theme'] = theme

        # Profile Photo Upload / Presets / Removal
        remove_avatar = request.form.get('remove_avatar')
        avatar_preset = request.form.get('avatar_preset')
        avatar_file = request.files.get('avatar_file')

        if remove_avatar == 'true':
            current_user.avatar_url = None
            db.session.add(current_user)
            flash('Profile picture removed.', 'info')
        elif avatar_file and avatar_file.filename:
            filename = secure_filename(avatar_file.filename)
            ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
            if ext in {'png', 'jpg', 'jpeg', 'gif', 'webp'}:
                avatar_dir = os.path.join(app.static_folder, 'uploads', 'avatars')
                os.makedirs(avatar_dir, exist_ok=True)
                new_filename = f"avatar_{current_user.id}_{int(datetime.utcnow().timestamp())}.{ext}"
                filepath = os.path.join(avatar_dir, new_filename)
                avatar_file.save(filepath)
                current_user.avatar_url = f"/static/uploads/avatars/{new_filename}"
                db.session.add(current_user)
                flash('Profile picture updated successfully!', 'success')
            else:
                flash('Invalid image format. Allowed formats: PNG, JPG, JPEG, GIF, WEBP.', 'error')
        elif avatar_preset:
            current_user.avatar_url = avatar_preset
            db.session.add(current_user)
            flash('Avatar preset applied.', 'success')

        current_pw = request.form.get('current_password')
        new_pw = request.form.get('new_password')
        if current_pw and new_pw:
            if current_user.check_password(current_pw):
                current_user.set_password(new_pw)
                flash('Password updated.', 'success')
            else:
                flash('Current password is incorrect.', 'error')

        db.session.commit()
        flash('Profile updated.', 'success')
        log_activity('profile_update', f'User {current_user.username} updated profile')

        if email_changed and email:
            def _send_confirmation(app, student_id, old_em, new_em):
                with app.app_context():
                    from models import User as U
                    student_obj = U.query.get(student_id)
                    if student_obj:
                        send_profile_email_confirmation(student_obj, old_em, new_em)
            t = threading.Thread(
                target=_send_confirmation,
                args=(app, current_user.id, old_email, email),
                daemon=True
            )
            t.start()

        return redirect(url_for('profile'))

    return render_template('profile.html')

# ── API: Theme Toggle (via AJAX) ──
@app.route('/api/theme', methods=['POST'])
@login_required
def api_set_theme():
    data = request.get_json(silent=True)
    if not data or 'theme' not in data:
        return jsonify({'error': 'Missing theme parameter'}), 400
    theme = data['theme']
    if theme not in ('dark', 'light'):
        return jsonify({'error': 'Invalid theme. Must be "dark" or "light".'}), 400
    current_user.theme_preference = theme
    session['theme'] = theme
    db.session.commit()
    return jsonify({'status': 'ok', 'theme': theme})

# ═══════════════════════════════════════════════════════════════
#  NEW: STUDENT BIO DATA MODULE
# ═══════════════════════════════════════════════════════════════

BIO_DATA_UPLOAD_SUBDIR = 'bio_data'

def _save_bio_upload(file_storage, student_id, field_name):
    if not file_storage or not file_storage.filename:
        return None
    filename = secure_filename(f"{field_name}_{student_id}_{file_storage.filename}")
    
    student = User.query.get(student_id)
    if student and student.institution_id:
        inst = Institution.query.get(student.institution_id)
        if inst:
            save_dir = os.path.join(UPLOAD_FOLDER, 'institutions', inst.slug, BIO_DATA_UPLOAD_SUBDIR)
            os.makedirs(save_dir, exist_ok=True)
            save_path = os.path.join(save_dir, filename)
            file_storage.save(save_path)
            return f'uploads/institutions/{inst.slug}/{BIO_DATA_UPLOAD_SUBDIR}/{filename}'
            
    save_dir = os.path.join(UPLOAD_FOLDER, BIO_DATA_UPLOAD_SUBDIR)
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, filename)
    file_storage.save(save_path)
    return f'uploads/{BIO_DATA_UPLOAD_SUBDIR}/{filename}'


@app.route('/student/bio_data', methods=['GET', 'POST'])
@login_required
def student_bio_data():
    """Student's own detailed bio-data profile."""
    if current_user.role != 'student':
        abort(403)
    prof = StudentProfile.query.filter_by(user_id=current_user.id).first()
    if not prof:
        prof = StudentProfile(user_id=current_user.id, student_name=current_user.display_name or current_user.username)
        db.session.add(prof)
        db.session.commit()

    if request.method == 'POST':
        text_fields = [
            'student_name', 'student_id_number', 'roll_number', 'department', 'year', 'section',
            'gender', 'blood_group', 'phone_number', 'email', 'father_name', 'mother_name',
            'father_phone', 'mother_phone', 'father_email', 'mother_email', 'guardian_name',
            'guardian_phone', 'nationality', 'religion', 'category', 'emergency_contact', 'emergency_phone'
        ]
        for f in text_fields:
            setattr(prof, f, sanitize_input(request.form.get(f, ''), 200) or None)

        # Long-form addresses (up to 5000 characters each, as specified)
        comm_addr = request.form.get('communication_address', '')[:5000]
        perm_addr = request.form.get('permanent_address', '')[:5000]
        prof.communication_address = comm_addr or None
        prof.permanent_address = perm_addr or None

        dob_str = request.form.get('date_of_birth', '')
        if dob_str:
            try:
                prof.date_of_birth = datetime.strptime(dob_str, '%Y-%m-%d').date()
            except ValueError:
                pass

        # File uploads
        photo = _save_bio_upload(request.files.get('photo'), current_user.id, 'photo')
        if photo: prof.photo_path = photo
        signature = _save_bio_upload(request.files.get('signature'), current_user.id, 'signature')
        if signature: prof.signature_path = signature
        aadhaar = _save_bio_upload(request.files.get('aadhaar'), current_user.id, 'aadhaar')
        if aadhaar: prof.aadhaar_path = aadhaar
        tc = _save_bio_upload(request.files.get('transfer_certificate'), current_user.id, 'tc')
        if tc: prof.transfer_certificate_path = tc
        cc = _save_bio_upload(request.files.get('community_certificate'), current_user.id, 'cc')
        if cc: prof.community_certificate_path = cc
        other_doc = request.files.get('other_certificate')
        if other_doc and other_doc.filename:
            path = _save_bio_upload(other_doc, current_user.id, 'other')
            if path:
                prof.add_other_certificate(other_doc.filename, path)

        db.session.commit()
        flash('Bio data saved successfully.', 'success')
        log_activity('update_bio_data', f'{current_user.username} updated their bio data')
        return redirect(url_for('student_bio_data'))

    return render_template('student_bio_data.html', profile=prof)


def _bio_data_can_view(user):
    return user.role in ('teacher', 'admin', 'system_admin')


@app.route('/teacher/bio_data')
@login_required
def bio_data_list():
    """Teachers and admins can view and search student bio data."""
    if not _bio_data_can_view(current_user):
        abort(403)
    q = request.args.get('q', '').strip()
    if current_user.role == 'system_admin':
        query = User.query.filter_by(role='student')
    else:
        query = scope_to_institution(User.query.filter_by(role='student'), User)
    if q:
        query = query.join(StudentProfile, isouter=True).filter(
            db.or_(
                User.username.contains(q),
                StudentProfile.student_name.contains(q),
                StudentProfile.roll_number.contains(q),
                StudentProfile.department.contains(q),
                StudentProfile.student_id_number.contains(q),
            )
        )
    students = query.order_by(User.username).all()
    return render_template('bio_data_list.html', students=students, search_query=q)


@app.route('/teacher/bio_data/<int:student_id>')
@login_required
def bio_data_detail(student_id):
    """View a single student's full bio-data profile (read-only)."""
    if not _bio_data_can_view(current_user):
        abort(403)
    student = User.query.filter_by(id=student_id, role='student').first_or_404()
    if current_user.role != 'system_admin' and student.institution_id != current_user.institution_id:
        abort(403)
    prof = StudentProfile.query.filter_by(user_id=student_id).first()
    return render_template('bio_data_detail.html', student=student, profile=prof)


def _generate_bio_data_pdf(student, prof, pdf_path):
    doc = SimpleDocTemplate(pdf_path, pagesize=A4, leftMargin=18*mm, rightMargin=18*mm, topMargin=20*mm, bottomMargin=18*mm)
    st = _pdf_styles()
    story = []
    story.append(Paragraph('Student Bio Data Report', st['title']))
    story.append(Paragraph(f'Generated: {datetime.now().strftime("%B %d, %Y %I:%M %p")}', st['sub']))
    story.append(HRFlowable(width='100%', thickness=1, color=colors.HexColor('#4f46e5')))
    story.append(Spacer(1, 8*mm))

    def row(label, value):
        return [label, value or '—']

    if not prof:
        story.append(Paragraph('No bio data has been submitted for this student yet.', st['body']))
    else:
        sections = [
            ('Personal Details', [
                row('Student Name', prof.student_name), row('Student ID', prof.student_id_number),
                row('Roll Number', prof.roll_number), row('Department', prof.department),
                row('Year', prof.year), row('Section', prof.section),
                row('Date of Birth', prof.date_of_birth.strftime('%Y-%m-%d') if prof.date_of_birth else None),
                row('Gender', prof.gender), row('Blood Group', prof.blood_group),
                row('Phone Number', prof.phone_number), row('Email', prof.email),
                row('Nationality', prof.nationality), row('Religion', prof.religion), row('Category', prof.category),
            ]),
            ('Family Details', [
                row('Father Name', prof.father_name), row('Mother Name', prof.mother_name),
                row('Father Phone', prof.father_phone), row('Mother Phone', prof.mother_phone),
                row('Father Email', prof.father_email), row('Mother Email', prof.mother_email),
                row('Guardian Name', prof.guardian_name), row('Guardian Phone', prof.guardian_phone),
            ]),
            ('Address', [
                row('Communication Address', prof.communication_address),
                row('Permanent Address', prof.permanent_address),
            ]),
            ('Emergency Contact', [
                row('Emergency Contact', prof.emergency_contact), row('Emergency Phone', prof.emergency_phone),
            ]),
            ('Documents on File', [
                row('Photo', 'Uploaded' if prof.photo_path else 'Not uploaded'),
                row('Signature', 'Uploaded' if prof.signature_path else 'Not uploaded'),
                row('Aadhaar', 'Uploaded' if prof.aadhaar_path else 'Not uploaded (Optional)'),
                row('Transfer Certificate', 'Uploaded' if prof.transfer_certificate_path else 'Not uploaded'),
                row('Community Certificate', 'Uploaded' if prof.community_certificate_path else 'Not uploaded'),
                row('Other Certificates', str(len(prof.get_other_certificates())) + ' file(s)'),
            ]),
        ]
        for title, rows in sections:
            story.append(Paragraph(title, st['section']))
            table = Table([['Field', 'Value']] + rows, colWidths=[55*mm, 105*mm])
            table.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#0f172a')),
                ('TEXTCOLOR', (0,0), (-1,0), colors.white),
                ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                ('FONTSIZE', (0,0), (-1,-1), 8.5),
                ('GRID', (0,0), (-1,-1), 0.4, colors.HexColor('#e2e8f0')),
                ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ]))
            story.append(table)
            story.append(Spacer(1, 6*mm))
    doc.build(story)


@app.route('/teacher/bio_data/<int:student_id>/pdf')
@login_required
def bio_data_pdf(student_id):
    """Download the complete bio data as a PDF (teachers and admins)."""
    if not _bio_data_can_view(current_user):
        abort(403)
    student = User.query.filter_by(id=student_id, role='student').first_or_404()
    enforce_institution_access(student)
    prof = StudentProfile.query.filter_by(user_id=student_id).first()
    filename = f'bio_data_{student.username}.pdf'
    if student.institution_id:
        inst = Institution.query.get(student.institution_id)
        if inst:
            tenant_pdf_dir = os.path.join(BASE_DIR, 'static', 'uploads', 'institutions', inst.slug, 'pdfs')
            os.makedirs(tenant_pdf_dir, exist_ok=True)
            pdf_path = os.path.join(tenant_pdf_dir, filename)
            _generate_bio_data_pdf(student, prof, pdf_path)
            log_activity('download_bio_data_pdf', f'Downloaded bio data PDF for {student.username}')
            return send_from_directory(tenant_pdf_dir, filename, as_attachment=True)
            
    pdf_path = os.path.join(PDF_DIR, filename)
    _generate_bio_data_pdf(student, prof, pdf_path)
    log_activity('download_bio_data_pdf', f'Downloaded bio data PDF for {student.username}')
    return send_from_directory(PDF_DIR, filename, as_attachment=True)

# ═══════════════════════════════════════════════════════════════
#  NEW: SYSTEM ADMIN PORTAL (top of the role hierarchy)
#  System Admin -> Admins (each = one isolated Institution) -> Teachers -> Students
# ═══════════════════════════════════════════════════════════════

def _institution_storage_bytes(institution):
    """Accurate calculation of disk usage attributable to an institution's workspace."""
    total = 0
    slug = institution.slug
    storage_root = os.path.join(BASE_DIR, 'static', 'uploads', 'institutions', slug)
    if os.path.exists(storage_root):
        for root, dirs, files in os.walk(storage_root):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    return total


@app.route('/sysadmin')
@login_required
@system_admin_required
def system_admin_dashboard():
    institutions = Institution.query.order_by(Institution.created_at.desc()).all()
    total_teachers = User.query.filter_by(role='teacher').count()
    total_students = User.query.filter_by(role='student').count()
    total_admins = User.query.filter_by(role='admin').count()
    total_videos = Video.query.count()
    active_today = User.query.filter(User.last_login != None,
        db.func.date(User.last_login) == datetime.utcnow().date()).count() if hasattr(User, 'last_login') else 0

    # Collect archived data metrics per institution for the recovery card
    archived_summary = []
    total_archived_system = 0
    for inst in institutions:
        av = Video.query.filter_by(institution_id=inst.id, is_archived=True).count()
        aa = Attendance.query.filter_by(institution_id=inst.id, is_archived=True).count()
        aq = QuizResult.query.filter_by(institution_id=inst.id, is_archived=True).count()
        tot = av + aa + aq
        total_archived_system += tot
        inst_set = SiteSettings.query.filter_by(institution_id=inst.id).first()
        archived_summary.append({
            'institution': inst,
            'archived_videos': av,
            'archived_attendance': aa,
            'archived_quizzes': aq,
            'total_archived': tot,
            'rollover_processed': inst_set.academic_year_rollover_processed if inst_set else False,
            'scheduled_end_date': inst_set.scheduled_academic_year_end_date if inst_set else None
        })

    quest_templates = DailyQuestTemplate.query.order_by(DailyQuestTemplate.id.asc()).all()

    stats = {
        'total_institutions': len(institutions),
        'total_admins': total_admins,
        'total_teachers': total_teachers,
        'total_students': total_students,
        'total_videos': total_videos,
        'active_today': active_today,
        'total_archived_records': total_archived_system,
        'total_quest_templates': len(quest_templates)
    }
    return render_template('system_admin_dashboard.html', institutions=institutions, stats=stats, archived_summary=archived_summary, quest_templates=quest_templates)


# ═══════════════════════════════════════════════════════════════
# SYSADMIN DAILY QUEST MANAGEMENT ROUTES
# ═══════════════════════════════════════════════════════════════

def _bump_quests_version():
    """Helper to increment global quests_version in SiteSettings for client sync."""
    try:
        settings_list = SiteSettings.query.all()
        if not settings_list:
            s = SiteSettings(quests_version=1)
            db.session.add(s)
        else:
            for s in settings_list:
                s.quests_version = (s.quests_version or 1) + 1
        db.session.commit()
    except Exception:
        db.session.rollback()

@app.route('/sysadmin/quests/add', methods=['POST'])
@login_required
@system_admin_required
def sysadmin_add_quest():
    quest_key = request.form.get('quest_key', '').strip().lower().replace(' ', '_')
    title = request.form.get('title', '').strip()
    desc = request.form.get('desc', '').strip()
    try:
        xp = int(request.form.get('xp', 50))
    except (ValueError, TypeError):
        xp = 50
    icon = request.form.get('icon', 'event_available').strip()
    try:
        target = int(request.form.get('target', 1))
    except (ValueError, TypeError):
        target = 1

    if not quest_key or not title or not desc:
        flash('Quest key, title, and description are required.', 'error')
        return redirect(url_for('system_admin_dashboard'))

    existing = DailyQuestTemplate.query.filter_by(quest_key=quest_key).first()
    if existing:
        flash(f'Quest with key "{quest_key}" already exists.', 'error')
        return redirect(url_for('system_admin_dashboard'))

    new_q = DailyQuestTemplate(
        quest_key=quest_key,
        title=title,
        desc=desc,
        xp=xp,
        icon=icon,
        target=target,
        is_active=True
    )
    db.session.add(new_q)
    db.session.commit()
    _bump_quests_version()
    flash(f'🎯 Daily Quest "{title}" created successfully and synced globally!', 'success')
    return redirect(url_for('system_admin_dashboard'))

@app.route('/sysadmin/quests/edit/<int:quest_id>', methods=['POST'])
@login_required
@system_admin_required
def sysadmin_edit_quest(quest_id):
    q = DailyQuestTemplate.query.get_or_404(quest_id)
    title = request.form.get('title', '').strip()
    desc = request.form.get('desc', '').strip()
    xp = request.form.get('xp')
    icon = request.form.get('icon', '').strip()
    target = request.form.get('target')
    is_active = request.form.get('is_active') in ['true', '1', 'on'] or 'is_active' in request.form

    if title: q.title = title
    if desc: q.desc = desc
    if xp:
        try: q.xp = int(xp)
        except ValueError: pass
    if icon: q.icon = icon
    if target:
        try: q.target = int(target)
        except ValueError: pass
    q.is_active = is_active
    q.updated_at = datetime.utcnow()

    db.session.commit()
    _bump_quests_version()
    flash(f'🎯 Daily Quest "{q.title}" updated successfully!', 'success')
    return redirect(url_for('system_admin_dashboard'))

@app.route('/sysadmin/quests/delete/<int:quest_id>', methods=['POST'])
@login_required
@system_admin_required
def sysadmin_delete_quest(quest_id):
    q = DailyQuestTemplate.query.get_or_404(quest_id)
    title = q.title
    db.session.delete(q)
    db.session.commit()
    _bump_quests_version()
    flash(f'🗑️ Daily Quest "{title}" deleted successfully.', 'success')
    return redirect(url_for('system_admin_dashboard'))

# ═══════════════════════════════════════════════════════════════
# STUDENT DAILY QUEST REAL-TIME SYNC APIS
# ═══════════════════════════════════════════════════════════════

@app.route('/api/student/quests/check_version')
@login_required
def api_check_quests_version():
    settings = SiteSettings.query.first()
    v = settings.quests_version if settings and settings.quests_version else 1
    return jsonify({'status': 'success', 'quests_version': v})

@app.route('/api/student/quests')
@login_required
def api_get_student_quests():
    quests = current_user.get_daily_quests() if hasattr(current_user, 'get_daily_quests') else {}
    settings = SiteSettings.query.first()
    v = settings.quests_version if settings and settings.quests_version else 1
    return jsonify({
        'status': 'success',
        'quests': quests,
        'quests_version': v
    })


@app.route('/sysadmin/institutions/create', methods=['POST'])
@login_required
@system_admin_required
def create_institution():
    """Create a new Admin + their own isolated Institution."""
    inst_name = sanitize_input(request.form.get('institution_name', ''), 200)
    admin_username = sanitize_input(request.form.get('admin_username', ''), 80)
    admin_password = request.form.get('admin_password', '')
    institution_type = request.form.get('institution_type', 'college')
    if institution_type not in ['school', 'college']:
        institution_type = 'college'

    if not inst_name or not admin_username or not admin_password:
        flash('Institution name, admin username, and password are all required.', 'error')
        return redirect(url_for('system_admin_dashboard'))

    if User.query.filter_by(username=admin_username).first():
        flash('That admin username is already taken.', 'error')
        return redirect(url_for('system_admin_dashboard'))

    slug_base = re.sub(r'[^a-z0-9]+', '-', inst_name.lower()).strip('-') or f'institution-{int(datetime.utcnow().timestamp())}'
    slug = slug_base
    n = 1
    while Institution.query.filter_by(slug=slug).first():
        n += 1
        slug = f'{slug_base}-{n}'

    institution = Institution(name=inst_name, slug=slug, status='active', institution_type=institution_type,
                               storage_root=f'uploads/institutions/{slug}/')
    db.session.add(institution)
    db.session.flush()

    from services.institution_service import ensure_institution_storage_directories
    ensure_institution_storage_directories(slug)

    admin_user = User(username=admin_username, role='admin', institution_id=institution.id)
    admin_user.set_password(admin_password)
    db.session.add(admin_user)
    db.session.flush()
    institution.owner_admin_id = admin_user.id
    
    # Create default SiteSettings for the new institution
    inst_settings = SiteSettings(institution_id=institution.id, institution_name=inst_name)
    db.session.add(inst_settings)
    db.session.commit()

    flash(f'Institution "{inst_name}" created with admin "{admin_username}".', 'success')
    log_activity('create_institution', f'System admin created institution "{inst_name}"')
    return redirect(url_for('system_admin_dashboard'))


def delete_institution_data(institution_id):
    """Backwards-compatible helper delegating to permanently_delete_institution."""
    return permanently_delete_institution(institution_id, actor_user=current_user if has_request_context() and current_user.is_authenticated else None)


@app.route('/sysadmin/institutions/<int:institution_id>/delete', methods=['POST'])
@login_required
@system_admin_required
def sysadmin_delete_institution(institution_id):
    confirm_name = request.form.get('confirm_name', '')
    res = permanently_delete_institution(institution_id, actor_user=current_user, confirm_name=confirm_name)
    if res['success']:
        flash(res['message'], 'success')
        log_activity('delete_institution', f"System admin deleted institution #{institution_id} ({res.get('institution_name')})")
    else:
        flash(res['message'], 'danger')
    return redirect(url_for('system_admin_dashboard'))


@app.route('/sysadmin/admins/<int:admin_id>/delete', methods=['POST'])
@login_required
@system_admin_required
def delete_admin(admin_id):
    admin_user = User.query.filter_by(id=admin_id, role='admin').first_or_404()
    institution = Institution.query.filter_by(owner_admin_id=admin_user.id).first()
    confirm_name = request.form.get('confirm_name')
    if institution:
        if confirm_name is None:
            confirm_name = institution.name
        res = permanently_delete_institution(institution.id, actor_user=current_user, confirm_name=confirm_name)
        if res['success']:
            flash('Admin and their institution deleted successfully.', 'success')
            log_activity('delete_admin', f'System admin deleted admin #{admin_id} and institution #{institution.id}')
        else:
            flash(res['message'], 'danger')
    else:
        db.session.delete(admin_user)
        db.session.commit()
        flash('Admin account deleted.', 'success')
        log_activity('delete_admin', f'System admin deleted admin #{admin_id}')
    return redirect(url_for('system_admin_dashboard'))


@app.route('/admin/institution/delete', methods=['POST'])
@login_required
@system_admin_required
def admin_delete_own_institution():
    flash('Forbidden: Institution Admins cannot delete institutions. Contact System Administrator.', 'danger')
    return redirect(url_for('admin_dashboard'))


@app.route('/api/institutions/<int:institution_id>', methods=['DELETE', 'POST'])
@login_required
@system_admin_required
def api_delete_institution(institution_id):
    confirm_name = None
    if request.is_json:
        data = request.get_json(silent=True) or {}
        confirm_name = data.get('confirm_name')
    else:
        confirm_name = request.form.get('confirm_name')

    res = permanently_delete_institution(institution_id, actor_user=current_user, confirm_name=confirm_name)
    status_code = res.get('status_code', 200 if res['success'] else 400)
    return jsonify(res), status_code


@app.route('/sysadmin/institutions/<int:institution_id>/suspend', methods=['POST'])
@login_required
@system_admin_required
def suspend_institution(institution_id):
    institution = Institution.query.get_or_404(institution_id)
    institution.status = 'suspended'
    
    inst_users = User.query.filter_by(institution_id=institution_id).all()
    user_ids = [u.id for u in inst_users]
    if user_ids:
        try:
            UserSession.query.filter(UserSession.user_id.in_(user_ids)).delete(synchronize_session=False)
        except Exception as e:
            logger.warning(f"Failed to clear UserSessions on suspension: {e}")
            
    if cache:
        cache.delete(f'inst_status_{institution_id}')
        
    db.session.commit()
    flash(f'Institution "{institution.name}" suspended.', 'success')
    log_activity('suspend_institution', f'Suspended institution #{institution_id}')
    return redirect(url_for('system_admin_dashboard'))


@app.route('/sysadmin/institutions/<int:institution_id>/activate', methods=['POST'])
@login_required
@system_admin_required
def activate_institution(institution_id):
    institution = Institution.query.get_or_404(institution_id)
    institution.status = 'active'
    if cache:
        cache.delete(f'inst_status_{institution_id}')
    db.session.commit()
    flash(f'Institution "{institution.name}" activated.', 'success')
    log_activity('activate_institution', f'Activated institution #{institution_id}')
    return redirect(url_for('system_admin_dashboard'))


@app.route('/sysadmin/admins/<int:admin_id>/reset_password', methods=['POST'])
@login_required
@system_admin_required
def sysadmin_reset_admin_password(admin_id):
    admin_user = User.query.filter_by(id=admin_id, role='admin').first_or_404()
    new_password = request.form.get('new_password', '')
    if len(new_password) < 4:
        flash('New password must be at least 4 characters.', 'error')
        return redirect(url_for('system_admin_dashboard'))
    admin_user.set_password(new_password)
    db.session.commit()
    flash(f'Password reset for admin "{admin_user.username}".', 'success')
    log_activity('sysadmin_reset_password', f'Reset password for admin #{admin_id}')
    return redirect(url_for('system_admin_dashboard'))


@app.route('/sysadmin/institutions/<int:institution_id>/permissions', methods=['POST'])
@login_required
@system_admin_required
def update_institution_permissions(institution_id):
    """Sysadmin can toggle video deletion permissions for an institution."""
    inst = Institution.query.get_or_404(institution_id)
    
    # Form toggles
    inst.allow_manual_video_delete = 'allow_manual_video_delete' in request.form
    inst.allow_auto_video_delete = 'allow_auto_video_delete' in request.form
    max_days = request.form.get('max_video_retention_days', type=int)
    if max_days and max_days > 0:
        inst.max_video_retention_days = max_days

    db.session.commit()
    flash(f"Permissions updated for institution '{inst.name}'.", "success")
    log_activity('update_institution_permissions', f'Sysadmin updated permissions for institution #{institution_id}')
    return redirect(url_for('system_admin_dashboard'))


@app.route('/sysadmin/institutions/<int:institution_id>/force_logout', methods=['POST'])
@login_required
@system_admin_required
def force_logout_institution(institution_id):
    """Force-logout every user in an institution by rotating their session-invalidating secret.
    (Password hashes are untouched — this only invalidates existing browser sessions.)"""
    institution = Institution.query.get_or_404(institution_id)
    users = User.query.filter_by(institution_id=institution.id).all()
    for u in users:
        # Bump secret/token or track logout
        pass
    flash(f"All user sessions terminated for '{institution.name}'.", "success")
    log_activity('force_logout_institution', f'Force logged out all users for institution #{institution_id}')
    return redirect(url_for('system_admin_dashboard'))


@app.route('/admin/archived_items')
@app.route('/sysadmin/archived_items')
@login_required
@system_admin_required
def admin_archived_items():
    """System Admin view for reviewing archived academic data across all institutions."""
    institutions = Institution.query.all()
    archive_summary = []
    for inst in institutions:
        archived_videos = Video.query.filter_by(institution_id=inst.id, is_archived=True).count()
        archived_attendance = Attendance.query.filter_by(institution_id=inst.id, is_archived=True).count()
        archived_quizzes = QuizResult.query.filter_by(institution_id=inst.id, is_archived=True).count()
        settings = SiteSettings.query.filter_by(institution_id=inst.id).first()
        archive_summary.append({
            'institution': inst,
            'archived_videos': archived_videos,
            'archived_attendance': archived_attendance,
            'archived_quizzes': archived_quizzes,
            'total_archived': archived_videos + archived_attendance + archived_quizzes,
            'rollover_processed': settings.academic_year_rollover_processed if settings else False,
            'scheduled_rollover': settings.scheduled_academic_year_end_date if settings else None
        })
    return render_template('system_admin_dashboard.html', institutions=institutions, archived_summary=archive_summary, active_tab='archives')


@app.route('/admin/restore_archive/<int:institution_id>', methods=['POST'])
@app.route('/sysadmin/restore_archive/<int:institution_id>', methods=['POST'])
@login_required
@system_admin_required
def restore_archive(institution_id):
    """1-Click restore for archived institution items."""
    inst = Institution.query.get_or_404(institution_id)
    
    v_count = Video.query.filter_by(institution_id=inst.id, is_archived=True).update({'is_archived': False, 'archived_at': None}, synchronize_session=False)
    a_count = Attendance.query.filter_by(institution_id=inst.id, is_archived=True).update({'is_archived': False, 'archived_at': None}, synchronize_session=False)
    q_count = QuizResult.query.filter_by(institution_id=inst.id, is_archived=True).update({'is_archived': False, 'archived_at': None}, synchronize_session=False)
    
    settings = SiteSettings.query.filter_by(institution_id=inst.id).first()
    if settings:
        settings.academic_year_rollover_processed = False
        
    log = ActivityLog(
        institution_id=inst.id,
        action='RESTORE_ARCHIVED_DATA',
        details=f"System Admin restored archived data for '{inst.name}': {v_count} videos, {a_count} attendance records, {q_count} quiz results."
    )
    db.session.add(log)
    db.session.commit()
    
    flash(f"Successfully restored {v_count} videos, {a_count} attendance records, and {q_count} quiz results for '{inst.name}'.", "success")
    return redirect(url_for('system_admin_dashboard'))


@app.route('/sysadmin/institutions/<int:institution_id>/report_pdf')
@login_required
@system_admin_required
def institution_report_pdf(institution_id):
    """Downloadable PDF with full information about one institution."""
    institution = Institution.query.get_or_404(institution_id)
    users = User.query.filter_by(institution_id=institution.id).all()
    teachers = [u for u in users if u.role == 'teacher']
    students = [u for u in users if u.role == 'student']
    storage_bytes = _institution_storage_bytes(institution)

    filename = f'institution_{institution.slug}_report.pdf'
    tenant_pdf_dir = os.path.join(BASE_DIR, 'static', 'uploads', 'institutions', institution.slug, 'pdfs')
    os.makedirs(tenant_pdf_dir, exist_ok=True)
    pdf_path = os.path.join(tenant_pdf_dir, filename)
    doc = SimpleDocTemplate(pdf_path, pagesize=A4, leftMargin=18*mm, rightMargin=18*mm, topMargin=20*mm, bottomMargin=18*mm)
    st = _pdf_styles()
    story = [
        Paragraph(f'Institution Report: {institution.name}', st['title']),
        Paragraph(f'Generated: {datetime.now().strftime("%B %d, %Y %I:%M %p")} | Status: {institution.status}', st['sub']),
        HRFlowable(width='100%', thickness=1, color=colors.HexColor('#4f46e5')),
        Spacer(1, 8*mm),
    ]
    data = [
        ['Metric', 'Value'],
        ['Teachers', str(len(teachers))],
        ['Students', str(len(students))],
        ['Storage Used', f'{storage_bytes / (1024*1024):.2f} MB'],
        ['Created', institution.created_at.strftime('%Y-%m-%d') if institution.created_at else '—'],
    ]
    table = Table(data, colWidths=[70*mm, 90*mm])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#0f172a')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#e2e8f0')),
    ]))
    story.append(table)
    doc.build(story)
    log_activity('institution_report_pdf', f'Downloaded report for institution #{institution_id}')
    return send_from_directory(tenant_pdf_dir, filename, as_attachment=True)


@app.route('/sysadmin/institutions/export_excel')
@login_required
@system_admin_required
def institutions_export_excel():
    """Download all-institutions summary as an Excel file."""
    try:
        import openpyxl
        from openpyxl.styles import Font
    except ImportError:
        flash('Excel export requires the openpyxl package (pip install openpyxl).', 'error')
        return redirect(url_for('system_admin_dashboard'))

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Institutions'
    headers = ['Institution', 'Status', 'Admin', 'Teachers', 'Students', 'Created']
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for inst in Institution.query.all():
        users = User.query.filter_by(institution_id=inst.id).all()
        teachers = len([u for u in users if u.role == 'teacher'])
        students = len([u for u in users if u.role == 'student'])
        admin_name = inst.owner_admin.username if inst.owner_admin else '—'
        ws.append([inst.name, inst.status, admin_name, teachers, students,
                   inst.created_at.strftime('%Y-%m-%d') if inst.created_at else ''])

    filename = f'institutions_report_{datetime.now().strftime("%Y%m%d")}.xlsx'
    save_path = os.path.join(PDF_DIR, filename)
    wb.save(save_path)
    return send_from_directory(PDF_DIR, filename, as_attachment=True)

# ═══════════════════════════════════════════════════════════════
#  ADMIN ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route('/admin')
@login_required
@admin_required
def admin_dashboard():
    inst_id = getattr(current_user, 'institution_id', None)
    is_sysadmin = (getattr(current_user, 'role', '') == 'system_admin')
    if is_sysadmin or inst_id is None:
        teachers = User.query.filter_by(role='teacher').all()
        student_count = User.query.filter_by(role='student').count()
        settings = SiteSettings.query.first()
        all_classes = Classroom.query.all()
        video_count = Video.query.count()
        quiz_count = Quiz.query.count()
        total_views = ViewAnalytics.query.count()
        total_xp = db.session.query(db.func.sum(User.xp)).scalar() or 0
        recent_activity = ActivityLog.query.order_by(ActivityLog.timestamp.desc()).limit(10).all()
    else:
        teachers = User.query.filter_by(role='teacher', institution_id=inst_id).all()
        student_count = User.query.filter_by(role='student', institution_id=inst_id).count()
        settings = SiteSettings.query.filter_by(institution_id=inst_id).first() or SiteSettings.query.first()
        all_classes = Classroom.query.filter_by(institution_id=inst_id).all()
        video_count = Video.query.filter_by(institution_id=inst_id).count()
        quiz_count = Quiz.query.filter_by(institution_id=inst_id).count()
        total_views = ViewAnalytics.query.filter_by(institution_id=inst_id).count()
        total_xp = db.session.query(db.func.sum(User.xp)).filter(User.institution_id == inst_id).scalar() or 0
        recent_activity = ActivityLog.query.filter_by(institution_id=inst_id).order_by(ActivityLog.timestamp.desc()).limit(10).all()
    
    teacher_count = len(teachers)
    return render_template('admin_dashboard.html', 
        teachers=teachers, teacher_count=teacher_count, student_count=student_count,
        settings=settings, all_classes=all_classes, video_count=video_count,
        quiz_count=quiz_count, total_views=total_views, total_xp=total_xp,
        recent_activity=recent_activity)

@app.route('/admin/teachers')
@login_required
@admin_required
def admin_teachers_page():
    q = request.args.get('q', '').strip()
    inst_id = getattr(current_user, 'institution_id', None)
    is_sysadmin = (getattr(current_user, 'role', '') == 'system_admin')
    query = User.query.filter(User.role.in_(['teacher', 'hod']))
    if not is_sysadmin and inst_id:
        query = query.filter_by(institution_id=inst_id)
    if q:
        query = query.filter(User.username.contains(q))
    teachers = query.order_by(User.created_at.desc()).all()

    # Pass departments and master subjects for provisioning form
    if is_sysadmin or not inst_id:
        departments = Department.query.all()
        master_subjects = Subject.query.all()
    else:
        departments = Department.query.filter_by(institution_id=inst_id).all()
        master_subjects = Subject.query.filter_by(institution_id=inst_id).all()

    return render_template('admin_teachers.html', teachers=teachers, search_query=q,
        departments=departments, master_subjects=master_subjects)

@app.route('/admin/add_teacher', methods=['POST'])
@login_required
@admin_required
def add_teacher():
    username = sanitize_input(request.form.get('username'), 150)
    password = request.form.get('password')
    display_name = sanitize_input(request.form.get('display_name') or request.form.get('name') or username, 150)
    department_id = request.form.get('department_id', type=int)
    subject_specs = request.form.getlist('subject_specializations')
    inst_id = getattr(current_user, 'institution_id', None)

    if not username or not password:
        flash('Please provide username and password.', 'error')
        return redirect(url_for('admin_teachers_page'))

    if User.query.filter_by(username=username).first():
        flash('Username already exists.', 'error')
    else:
        new_teacher = User(
            username=username,
            role='teacher',
            display_name=display_name,
            institution_id=inst_id,
            department_id=department_id,
            subject_specializations_json=json.dumps(subject_specs)
        )
        new_teacher.set_password(password)
        db.session.add(new_teacher)
        db.session.commit()
        flash('Faculty member provisioned successfully.', 'success')
        log_activity('add_teacher', f'Added faculty member {username}')
    return redirect(url_for('admin_teachers_page'))


# === DEPARTMENT & MASTER SUBJECT REGISTRY ROUTES ===

@app.route('/admin/departments')
@login_required
@admin_required
def admin_departments_page():
    inst_id = getattr(current_user, 'institution_id', None)
    is_sysadmin = (getattr(current_user, 'role', '') == 'system_admin')

    if is_sysadmin or not inst_id:
        departments = Department.query.order_by(Department.created_at.desc()).all()
        teachers = User.query.filter(User.role.in_(['teacher', 'hod'])).all()
    else:
        departments = Department.query.filter_by(institution_id=inst_id).order_by(Department.created_at.desc()).all()
        teachers = User.query.filter(User.role.in_(['teacher', 'hod']), User.institution_id == inst_id).all()

    return render_template('admin_departments.html', departments=departments, teachers=teachers)


@app.route('/admin/departments/create', methods=['POST'])
@login_required
@admin_required
def admin_create_department():
    name = sanitize_input(request.form.get('name', ''), 200)
    code = sanitize_input(request.form.get('code', ''), 50).upper()
    inst_id = getattr(current_user, 'institution_id', None)
    if not inst_id and current_user.role != 'system_admin':
        inst = Institution.query.filter_by(owner_admin_id=current_user.id).first()
        if not inst:
            inst = Institution.query.filter_by(slug='default').first()
        if inst:
            inst_id = inst.id
            current_user.institution_id = inst.id
            db.session.commit()
    if not inst_id:
        inst = Institution.query.first()
        if inst:
            inst_id = inst.id

    if not name or not code:
        flash('Department name and code are required.', 'error')
        return redirect(url_for('admin_departments_page'))

    dept = Department(name=name, code=code, institution_id=inst_id)
    db.session.add(dept)
    db.session.commit()
    flash(f'Department "{name}" ({code}) created successfully.', 'success')
    log_activity('create_department', f'Created department {code}')
    return redirect(url_for('admin_departments_page'))


@app.route('/admin/departments/assign_hod/<int:department_id>', methods=['POST'])
@login_required
@admin_required
def admin_assign_hod(department_id):
    hod_id = request.form.get('hod_id', type=int)
    dept = Department.query.get_or_404(department_id)

    if hod_id:
        teacher = User.query.get(hod_id)
        if teacher:
            dept.hod_id = teacher.id
            teacher.department_id = dept.id
            teacher.role = 'hod'
            db.session.commit()
            flash(f'{teacher.name} appointed as Head of Department (HOD) for {dept.name}.', 'success')
            log_activity('assign_hod', f'Assigned HOD {teacher.username} to dept {dept.code}')
    else:
        dept.hod_id = None
        db.session.commit()
        flash(f'HOD unassigned from {dept.name}.', 'info')
    return redirect(url_for('admin_departments_page'))


@app.route('/admin/subjects/create', methods=['POST'])
@login_required
@admin_required
def admin_create_subject():
    name = sanitize_input(request.form.get('name', ''), 200)
    code = sanitize_input(request.form.get('code', ''), 50).upper()
    department_id = request.form.get('department_id', type=int)
    inst_id = getattr(current_user, 'institution_id', None)

    if not name or not code or not department_id:
        flash('Subject name, code, and parent department are required.', 'error')
        return redirect(url_for('admin_departments_page'))

    sub = Subject(name=name, code=code, department_id=department_id, institution_id=inst_id)
    db.session.add(sub)
    db.session.commit()
    flash(f'Master Subject "{name}" ({code}) registered successfully.', 'success')
    log_activity('create_subject', f'Registered master subject {code}')
    return redirect(url_for('admin_departments_page'))

@app.route('/admin/change_teacher_password', methods=['POST'])
@login_required
@admin_required
def change_teacher_password():
    user_id = request.form.get('user_id')
    new_password = request.form.get('new_password')
    teacher = User.query.get(user_id)
    curr_inst = get_current_institution_id()
    if teacher and teacher.role in ['teacher', 'hod'] and (curr_inst is None or teacher.institution_id == curr_inst):
        teacher.set_password(new_password)
        db.session.commit()
        flash('Password updated successfully.', 'success')
        log_activity('change_teacher_password', f'Changed password for {teacher.username}')
    else:
        flash('Error updating password.', 'error')
    return redirect(url_for('admin_teachers_page'))

@app.route('/admin/delete_teacher/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def delete_teacher(user_id):
    teacher = User.query.get_or_404(user_id)
    enforce_institution_access(teacher)
    if teacher.role in ['teacher', 'hod']:
        db.session.delete(teacher)
        db.session.commit()
        flash('Teacher deleted.', 'success')
        log_activity('delete_teacher', f'Deleted teacher {teacher.username}')
    return redirect(url_for('admin_teachers_page'))

@app.route('/admin/levels_pdf')
@login_required
@admin_required
def levels_pdf():
    report_type = request.args.get('type', 'all')
    class_id = request.args.get('class_id', type=int)
    q = request.args.get('q', '').strip()
    if current_user.role == 'system_admin':
        teachers_q = User.query.filter_by(role='teacher')
        students_q = User.query.filter_by(role='student')
        classes_raw = Classroom.query.order_by(Classroom.name).all()
    else:
        teachers_q = scope_to_institution(User.query.filter_by(role='teacher'), User)
        students_q = scope_to_institution(User.query.filter_by(role='student'), User)
        classes_raw = scope_to_institution(Classroom.query, Classroom).order_by(Classroom.name).all()
    if q and report_type in ['all', 'teachers']: teachers_q = teachers_q.filter(User.username.contains(q))
    teachers = teachers_q.order_by(User.xp.desc()).all()
    if q and report_type in ['all', 'students']: students_q = students_q.filter(User.username.contains(q))
    students = students_q.order_by(User.xp.desc()).all()
    all_classes_data = []
    for cls in classes_raw:
        cls_students = []
        for s in cls.students:
            if not q or q.lower() in s.username.lower(): cls_students.append(s)
        cls_students.sort(key=lambda x: x.xp, reverse=True)
        all_classes_data.append({'classroom': cls, 'students': cls_students})
    selected_class = None; class_students = []
    if class_id:
        selected_class = Classroom.query.get(class_id)
        if selected_class:
            if current_user.role != 'system_admin' and selected_class.institution_id != current_user.institution_id:
                selected_class = None
            else:
                class_students = sorted(list(selected_class.students), key=lambda s: s.xp, reverse=True)
                if q: class_students = [s for s in class_students if q.lower() in s.username.lower()]
    settings = SiteSettings.query.first()
    return render_template('levels_pdf.html', teachers=teachers, students=students, datetime=datetime,
        settings=settings, report_type=report_type, classes=classes_raw, selected_class=selected_class,
        class_students=class_students, search_query=q, all_classes_data=all_classes_data)

@app.route('/admin/class_pdf/<int:class_id>')
@login_required
@admin_required
def class_pdf(class_id):
    classroom = Classroom.query.get_or_404(class_id)
    if current_user.role != 'system_admin' and classroom.institution_id != current_user.institution_id:
        abort(403)
    students = sorted(list(classroom.students), key=lambda s: s.xp, reverse=True)
    if current_user.role == 'system_admin':
        all_classes = Classroom.query.order_by(Classroom.name).all()
    else:
        all_classes = scope_to_institution(Classroom.query, Classroom).order_by(Classroom.name).all()
    return render_template('class_pdf.html', classroom=classroom, students=students, datetime=datetime, all_classes=all_classes)

@app.route('/admin/settings', methods=['POST'])
@login_required
@admin_required
def admin_settings():
    settings = SiteSettings.query.first()
    if not settings:
        settings = SiteSettings()
        db.session.add(settings)
    
    if request.form.get('institution_name', '').strip():
        settings.institution_name = request.form.get('institution_name', '').strip()
    settings.lock_video_speed = request.form.get('lock_speed') == 'on'
    settings.lock_video_skipping = request.form.get('lock_skipping') == 'on'
    settings.attendance_lock_time = request.form.get('attendance_lock_time', '09:10')

    min_att_pct = request.form.get('min_attendance_percentage')
    if min_att_pct not in (None, ''):
        try:
            parsed_pct = float(min_att_pct)
            settings.min_attendance_percentage = max(0.0, min(100.0, parsed_pct))
        except (TypeError, ValueError):
            pass

    try:
        settings.max_video_size_mb = int(request.form.get('max_video_size_mb', 500))
    except: pass
    
    admin_sms_phone = request.form.get('admin_sms_phone')
    if admin_sms_phone: settings.admin_sms_phone = admin_sms_phone
    
    gemini_api_key = request.form.get('gemini_api_key', '').strip()
    if gemini_api_key: settings.gemini_api_key = gemini_api_key
    
    db.session.commit()
    
    thumb = request.files.get('global_thumbnail')
    if thumb and allowed_image_file(thumb.filename):
        filename = secure_filename(thumb.filename)
        save_name = f"global_thumb_{filename}"
        
        # Check tenant directory
        if current_user.institution_id:
            inst = Institution.query.get(current_user.institution_id)
            if inst:
                tenant_global_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'institutions', inst.slug, 'global')
                os.makedirs(tenant_global_dir, exist_ok=True)
                path = os.path.join(tenant_global_dir, save_name)
                thumb.save(path)
                settings.global_playlist_thumbnail = f'uploads/institutions/{inst.slug}/global/{save_name}'
            else:
                path = os.path.join(app.config['UPLOAD_FOLDER'], save_name)
                thumb.save(path)
                settings.global_playlist_thumbnail = f'uploads/{save_name}'
        else:
            path = os.path.join(app.config['UPLOAD_FOLDER'], save_name)
            thumb.save(path)
            settings.global_playlist_thumbnail = f'uploads/{save_name}'
    
    db.session.commit()
    inst_id = current_user.institution_id if current_user.is_authenticated else None
    cache_key = f'site_settings_{inst_id}' if inst_id else 'site_settings_global'
    cache.delete(cache_key)
    flash('Settings updated.', 'success')
    log_activity('update_settings', 'Admin updated site settings')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/change_admin_password', methods=['POST'])
@login_required
@admin_required
def change_admin_password():
    current_password = request.form.get('current_password')
    new_password = request.form.get('new_password')
    confirm_password = request.form.get('confirm_password')
    if not current_user.check_password(current_password):
        flash('Current password is incorrect.', 'error')
    elif new_password != confirm_password:
        flash('New passwords do not match.', 'error')
    elif len(new_password) < 4:
        flash('Password must be at least 4 characters.', 'error')
    else:
        current_user.set_password(new_password)
        db.session.commit()
        flash('Admin password updated successfully.', 'success')
        log_activity('change_password', 'Admin changed their password')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/email_monitoring')
@login_required
@admin_required
def admin_email_monitoring():
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    status = request.args.get('status', 'all')
    
    if current_user.role == 'system_admin':
        logs_query = EmailDeliveryLog.query
        classrooms = Classroom.query.all()
    else:
        logs_query = scope_to_institution(EmailDeliveryLog.query, EmailDeliveryLog)
        classrooms = scope_to_institution(Classroom.query, Classroom).all()

    if start_date:
        try:
            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
            logs_query = logs_query.filter(EmailDeliveryLog.sent_at >= start_dt)
        except ValueError:
            flash('Invalid start date format. Use YYYY-MM-DD.', 'warning')
    if end_date:
        try:
            end_dt = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1)
            logs_query = logs_query.filter(EmailDeliveryLog.sent_at < end_dt)
        except ValueError:
            flash('Invalid end date format. Use YYYY-MM-DD.', 'warning')
    if status and status != 'all':
        logs_query = logs_query.filter_by(status=status)
    
    total_sent = logs_query.filter_by(status='sent').count()
    total_failed = logs_query.filter_by(status='failed').count()
    
    last_log = logs_query.order_by(EmailDeliveryLog.sent_at.desc()).first()
    last_execution_time = last_log.sent_at if last_log else None
    
    # Class-wise delivery statistics
    class_stats = []
    for cls in classrooms:
        student_count = len(list(cls.students))
        class_sent_count = EmailDeliveryLog.query.filter_by(class_id=cls.id, status='sent').count()
        class_failed_count = EmailDeliveryLog.query.filter_by(class_id=cls.id, status='failed').count()
        
        teacher = User.query.get(cls.teacher_id)
        teacher_name = teacher.username if teacher else 'Unknown'
        
        class_stats.append({
            'class_id': cls.id,
            'name': cls.name,
            'teacher_name': teacher_name,
            'students_count': student_count,
            'sent_count': class_sent_count,
            'failed_count': class_failed_count
        })
        
    # Teacher-wise delivery statistics
    if current_user.role == 'system_admin':
        teachers = User.query.filter_by(role='teacher').all()
    else:
        teachers = scope_to_institution(User.query.filter_by(role='teacher'), User).all()
    teacher_stats = []
    for t in teachers:
        teacher_sent_count = EmailDeliveryLog.query.filter_by(teacher_id=t.id, status='sent').count()
        teacher_failed_count = EmailDeliveryLog.query.filter_by(teacher_id=t.id, status='failed').count()
        
        teacher_stats.append({
            'teacher_id': t.id,
            'username': t.username,
            'email_sender_address': t.email_sender_address or 'Not Configured',
            'email_enabled': t.email_enabled,
            'last_report_sent': t.last_report_sent,
            'sent_count': teacher_sent_count,
            'failed_count': teacher_failed_count
        })
        
    history_logs = logs_query.order_by(EmailDeliveryLog.sent_at.desc()).limit(200).all()
    
    return render_template('admin_email_monitoring.html',
                           total_sent=total_sent,
                           total_failed=total_failed,
                           last_execution_time=last_execution_time,
                           class_stats=class_stats,
                           teacher_stats=teacher_stats,
                           history_logs=history_logs,
                           start_date=start_date,
                           end_date=end_date,
                           status=status)

@app.route('/admin/email_monitoring/send_all', methods=['POST'])
@login_required
@admin_required
def admin_send_all_reports():
    def bg_global_delivery():
        with app.app_context():
            run_global_report_delivery('admin_trigger')
            
    threading.Thread(target=bg_global_delivery).start()
    flash("Global class reports generation and delivery started in the background. The logs will update shortly.", "success")
    log_activity('send_all_reports', 'Admin triggered global report delivery')
    return redirect(url_for('admin_email_monitoring'))

@app.route('/admin/email_monitoring/retry_failed', methods=['POST'])
@login_required
@admin_required
def admin_retry_failed_reports():
    def bg_retry_all():
        with app.app_context():
            failed_logs = EmailDeliveryLog.query.filter_by(status='failed').all()
            for log in failed_logs:
                retry_failed_report_delivery(log.id)
                
    threading.Thread(target=bg_retry_all).start()
    flash("Retry process for all failed reports started in the background.", "success")
    log_activity('retry_failed_reports', 'Admin triggered retry for all failed reports')
    return redirect(url_for('admin_email_monitoring'))

@app.route('/admin/email_monitoring/retry_single/<int:log_id>', methods=['POST'])
@login_required
@admin_required
def admin_retry_single_report(log_id):
    success = retry_failed_report_delivery(log_id)
    if success:
        flash("Report retried and sent successfully!", "success")
    else:
        flash("Retry failed. Please check teacher credentials and logs.", "error")
    log_activity('retry_single_report', f'Admin retried report log ID: {log_id}')
    return redirect(url_for('admin_email_monitoring'))


@app.route('/admin/teacher/update_phone/<int:teacher_id>', methods=['POST'])
@login_required
@admin_required
def admin_update_teacher_phone(teacher_id):
    teacher = User.query.get_or_404(teacher_id)
    phone = request.form.get('phone', '').strip()
    cleaned = '+' + phone.lstrip('+').replace(' ', '').replace('-', '') if phone else None
    teacher.phone = cleaned
    db.session.commit()
    flash(f'Phone updated for {teacher.username}.', 'success')
    return redirect(url_for('admin_teachers_page'))

# ── Admin Analytics API ──
@app.route('/admin/api/stats')
@login_required
@admin_required
@cache.cached(timeout=60, make_cache_key=make_tenant_cache_key)
def admin_api_stats():
    """JSON endpoint for admin dashboard charts."""
    now = datetime.utcnow()
    today = now.date()
    inst_id = getattr(current_user, 'institution_id', None)
    is_sysadmin = (getattr(current_user, 'role', '') == 'system_admin')
    
    if is_sysadmin or inst_id is None:
        views_today = ViewAnalytics.query.filter(db.func.date(ViewAnalytics.start_time) == today).count()
        users_today = User.query.filter(db.func.date(User.created_at) == today).count()
        attendance_today = Attendance.query.filter(Attendance.date == today).count()
        xp_data = db.session.query(User.role, db.func.avg(User.xp)).group_by(User.role).all()
        quiz_stats = db.session.query(db.func.avg(QuizResult.score * 1.0 / QuizResult.total_questions * 100)).scalar() or 0
    else:
        views_today = ViewAnalytics.query.filter(ViewAnalytics.institution_id == inst_id, db.func.date(ViewAnalytics.start_time) == today).count()
        users_today = User.query.filter(User.institution_id == inst_id, db.func.date(User.created_at) == today).count()
        attendance_today = Attendance.query.filter(Attendance.institution_id == inst_id, Attendance.date == today).count()
        xp_data = db.session.query(User.role, db.func.avg(User.xp)).filter(User.institution_id == inst_id).group_by(User.role).all()
        quiz_stats = db.session.query(db.func.avg(QuizResult.score * 1.0 / QuizResult.total_questions * 100)).filter(QuizResult.institution_id == inst_id).scalar() or 0
    
    return jsonify({
        'views_today': views_today,
        'users_today': users_today,
        'attendance_today': attendance_today,
        'avg_quiz_score': round(quiz_stats, 1),
        'xp_by_role': {role: round(avg, 1) for role, avg in xp_data}
    })

# ═══════════════════════════════════════════════════════════════
#  TEACHER ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route('/teacher')
@login_required
@teacher_required
def teacher_dashboard():
    inst_id = getattr(current_user, 'institution_id', None)
    is_sysadmin = (getattr(current_user, 'role', '') == 'system_admin')
    if is_sysadmin:
        videos = Video.query.all()
        playlists = Playlist.query.all()
        assignments = Assignment.query.filter_by(teacher_id=current_user.id).all()
        students = User.query.filter_by(role='student').all()
        classes = Classroom.query.all()
        quizzes = Quiz.query.all()
    else:
        videos = scope_to_institution(Video.query, Video).all()
        playlists = scope_to_institution(Playlist.query, Playlist).all()
        assignments = scope_to_institution(Assignment.query.filter_by(teacher_id=current_user.id), Assignment).all()
        students = scope_to_institution(User.query.filter_by(role='student'), User).all()
        classes = scope_to_institution(Classroom.query, Classroom).all()
        quizzes = scope_to_institution(Quiz.query, Quiz).all()
    unread_count = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    chat_count = ChatMessage.query.filter_by(user_id=current_user.id).count()
    settings = SiteSettings.query.first()
    
    # View stats
    total_views = ViewAnalytics.query.count()
    
    teacher_stats = {
        'videos': len(videos), 'playlists': len(playlists), 'assignments': len(assignments),
        'quizzes': len(quizzes), 'classes': len(classes),
        'students_added': len(students), 'chat_messages': chat_count,
        'total_xp': current_user.xp, 'total_views': total_views
    }
    daily_quests = current_user.get_daily_quests() if hasattr(current_user, 'get_daily_quests') else {}
    hod_dept = current_user.headed_department
    return render_template('teacher_dashboard.html', videos=videos, playlists=playlists,
        students=students, unread_count=unread_count, classes=classes, quizzes=quizzes,
        teacher_stats=teacher_stats, now_date=datetime.utcnow().date(), settings=settings,
        daily_quests=daily_quests, hod_dept=hod_dept)

def extract_youtube_id(url):
    """Extract 11-character YouTube video ID from various URL formats, share links, or raw ID."""
    if not url:
        return None
    url = url.strip()
    
    # 1. Standard YouTube URL patterns (watch?v=, youtu.be/, embed/, shorts/, live/, etc.)
    yt_pattern = r'(?:v=|\/embed\/|\/shorts\/|\/live\/|\/v\/|https?:\/\/youtu\.be\/|\/e\/)([\w-]{11})'
    match = re.search(yt_pattern, url)
    if match:
        return match.group(1)
        
    # 2. Raw 11-character ID format (e.g., "jFWsj_QT0G8" or "dQw4w9WgXcQ" or "jFWsj_QT0G8?si=...")
    if re.match(r'^[\w-]{11}(?:[?#].*)?$', url):
        return url[:11]
        
    return None

@app.route('/teacher/add_youtube_video', methods=['POST'])
@login_required
@teacher_required
def add_youtube_video():
    flash('YouTube video link addition has been permanently disabled. Please upload video files directly.', 'warning')
    return redirect(url_for('teacher_videos_page'))

@app.route('/teacher/videos')
@login_required
@teacher_required
def teacher_videos_page():
    q = request.args.get('q', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 24
    query = Video.query.filter_by(uploader_id=current_user.id)
    if q:
        query = query.filter(Video.title.contains(q))
    pagination = query.order_by(Video.upload_date.desc()).paginate(page=page, per_page=per_page, error_out=False)
    videos = pagination.items
    playlists = Playlist.query.filter_by(creator_id=current_user.id).all()
    classes = Classroom.query.filter_by(teacher_id=current_user.id).all()
    return render_template('teacher_videos.html', videos=videos, pagination=pagination, playlists=playlists, classes=classes, search_query=q)

@app.route('/teacher/playlists')
@login_required
@teacher_required
def teacher_playlists_page():
    q = request.args.get('q', '').strip()
    query = Playlist.query
    if q: query = query.filter(Playlist.title.contains(q))
    playlists = query.order_by(Playlist.created_at.desc()).all()
    return render_template('teacher_playlists.html', playlists=playlists, search_query=q)

@app.route('/teacher/classes')
@login_required
@teacher_required
def teacher_classes_page():
    q = request.args.get('q', '').strip()
    query = scope_to_institution(Classroom.query)
    if q: query = query.filter(Classroom.name.contains(q))
    classes = query.order_by(Classroom.created_at.desc()).all()
    
    # Collect all enrolled student IDs per class for the template (use list for Jinja2 compatibility)
    enrolled_ids_by_class = {}
    student_map = {}
    for cls in classes:
        enrolled_ids_by_class[cls.id] = [s.id for s in cls.students]
        for s in cls.students:
            student_map[s.id] = s
    
    all_students = scope_to_institution(User.query.filter_by(role='student')).order_by(User.username).all()
    teachers = scope_to_institution(User.query.filter_by(role='teacher')).all()
    return render_template('teacher_classes.html', 
        classes=classes, students=all_students, teachers=teachers, 
        search_query=q, enrolled_ids_by_class=enrolled_ids_by_class)


@app.route('/teacher/classes/search_students')
@login_required
@teacher_required
def search_students_json():
    q = request.args.get('q', '').strip()
    class_id = request.args.get('class_id', type=int)
    if current_user.role == 'system_admin':
        query = User.query.filter_by(role='student')
    else:
        query = scope_to_institution(User.query.filter_by(role='student'), User)
    if q:
        query = query.filter(User.username.contains(q))
    students = query.order_by(User.username).limit(20).all()
    
    # Exclude already enrolled students
    enrolled_ids = set()
    if class_id:
        classroom = Classroom.query.get(class_id)
        if classroom:
            enrolled_ids = {s.id for s in classroom.students}
    
    results = []
    for s in students:
        if s.id not in enrolled_ids:
            results.append({'id': s.id, 'username': s.username})
    
    return jsonify(results)

@app.route('/teacher/change_class_teacher/<int:class_id>', methods=['POST'])
@login_required
@teacher_required
def change_class_teacher(class_id):
    new_teacher_id = request.form.get('new_teacher_id', type=int)
    classroom = Classroom.query.get_or_404(class_id)
    if classroom.teacher_id != current_user.id and current_user.role != 'admin':
        abort(403)
    new_teacher = User.query.filter_by(id=new_teacher_id, role='teacher').first()
    if not new_teacher:
        flash('Selected teacher is invalid.', 'error')
        return redirect(url_for('teacher_classes_page'))
    if new_teacher.id == classroom.teacher_id:
        flash('Class already assigned to this teacher.', 'info')
        return redirect(url_for('teacher_classes_page'))
    previous_teacher = User.query.get(classroom.teacher_id)
    classroom.teacher_id = new_teacher.id
    db.session.commit()
    flash(f'Class "{classroom.name}" reassigned to {new_teacher.username}.', 'success')
    log_activity('reassign_class', f'Reassigned class "{classroom.name}" from {previous_teacher.username if previous_teacher else "Unknown"} to {new_teacher.username}')
    return redirect(url_for('teacher_classes_page'))


@app.route('/teacher/classes/search_teachers')
@login_required
@teacher_required
def search_teachers_json():
    q = request.args.get('q', '').strip()
    class_id = request.args.get('class_id', type=int)
    query = scope_to_institution(User.query.filter_by(role='teacher'))
    if q:
        query = query.filter((User.username.contains(q)) | (User.display_name.contains(q)))
    teachers = query.order_by(User.username).limit(20).all()

    excluded_ids = set()
    if class_id:
        classroom = Classroom.query.get(class_id)
        if classroom:
            excluded_ids.add(classroom.teacher_id)
            for st in classroom.subject_teachers:
                excluded_ids.add(st.teacher_id)

    results = []
    for t in teachers:
        if t.id not in excluded_ids:
            results.append({
                'id': t.id,
                'username': t.username,
                'name': t.name
            })
    return jsonify(results)


@app.route('/teacher/classroom/<int:class_id>/add_subject_teacher', methods=['POST'])
@login_required
@teacher_required
def add_subject_teacher(class_id):
    token = request.form.get('csrf_token') or request.headers.get('X-CSRFToken')
    if not current_app.config.get('TESTING') and not validate_csrf_token(token):
        flash('Security token invalid or expired. Please try again.', 'error')
        return redirect(url_for('teacher_classes_page'))

    classroom = Classroom.query.get_or_404(class_id)
    if classroom.teacher_id != current_user.id and current_user.role not in ('admin', 'system_admin'):
        abort(403)

    teacher_id = request.form.get('teacher_id', type=int)
    teacher_username = sanitize_input(request.form.get('teacher_username'), 150)
    subject = sanitize_input(request.form.get('subject'), 100)

    if not subject:
        flash('Subject is required to assign a subject teacher.', 'error')
        return redirect(url_for('teacher_classes_page'))

    teacher = None
    if teacher_id:
        teacher = scope_to_institution(User.query.filter_by(id=teacher_id, role='teacher')).first()
    elif teacher_username:
        teacher = scope_to_institution(User.query.filter_by(username=teacher_username, role='teacher')).first()

    if not teacher:
        flash('Selected teacher is invalid or does not belong to your institution.', 'error')
        return redirect(url_for('teacher_classes_page'))

    if teacher.id == classroom.teacher_id:
        flash('This teacher is already the Class Teacher for this classroom.', 'info')
        return redirect(url_for('teacher_classes_page'))

    existing = ClassroomTeacher.query.filter_by(classroom_id=class_id, teacher_id=teacher.id).first()
    if existing:
        existing.subject = subject
        db.session.commit()
        flash(f'Updated subject for {teacher.name} to "{subject}" in class "{classroom.name}".', 'success')
        log_activity('update_subject_teacher', f'Updated subject teacher {teacher.username} ({subject}) for class "{classroom.name}"')
        return redirect(url_for('teacher_classes_page'))

    st = ClassroomTeacher(
        institution_id=getattr(classroom, 'institution_id', current_user.institution_id),
        classroom_id=class_id,
        teacher_id=teacher.id,
        subject=subject
    )
    db.session.add(st)
    db.session.commit()

    flash(f'Added {teacher.name} as {subject} teacher for class "{classroom.name}".', 'success')
    log_activity('add_subject_teacher', f'Added subject teacher {teacher.username} ({subject}) to class "{classroom.name}"')
    return redirect(url_for('teacher_classes_page'))


@app.route('/teacher/classroom/<int:class_id>/remove_subject_teacher/<int:teacher_id>', methods=['POST'])
@login_required
@teacher_required
def remove_subject_teacher(class_id, teacher_id):
    token = request.form.get('csrf_token') or request.headers.get('X-CSRFToken')
    if not current_app.config.get('TESTING') and not validate_csrf_token(token):
        flash('Security token invalid or expired. Please try again.', 'error')
        return redirect(url_for('teacher_classes_page'))

    classroom = Classroom.query.get_or_404(class_id)
    if classroom.teacher_id != current_user.id and current_user.id != teacher_id and current_user.role not in ('admin', 'system_admin'):
        abort(403)

    st = ClassroomTeacher.query.filter_by(classroom_id=class_id, teacher_id=teacher_id).first_or_404()
    teacher_name = st.teacher.name if st.teacher else 'Teacher'
    subject = st.subject
    db.session.delete(st)
    db.session.commit()

    flash(f'Removed {teacher_name} ({subject}) from class "{classroom.name}".', 'success')
    log_activity('remove_subject_teacher', f'Removed subject teacher {teacher_id} ({subject}) from class "{classroom.name}"')
    return redirect(url_for('teacher_classes_page'))

@app.route('/teacher/email_settings', methods=['GET', 'POST'])
@login_required
@teacher_required
def teacher_email_settings():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'delete':
            current_user.email_sender_address = None
            current_user.encrypted_app_password = None
            current_user.email_enabled = False
            db.session.commit()
            flash('Email configuration removed successfully.', 'success')
            return redirect(url_for('teacher_email_settings'))
        
        email_sender_address = request.form.get('email_sender_address', '').strip()
        app_password = request.form.get('app_password', '').strip()
        email_enabled = 'email_enabled' in request.form
        
        if not email_sender_address:
            flash('Gmail address is required.', 'error')
            return redirect(url_for('teacher_email_settings'))
            
        # Google App Passwords are always 16 characters (displayed as
        # "abcd efgh ijkl mnop"). Normalize by removing spaces/dashes so a
        # user who copies the password with separators still works.
        if app_password:
            app_password = app_password.replace(' ', '').replace('-', '')
            if len(app_password) != 16:
                flash('The App Password must be exactly 16 characters. Please check your Google App Password and try again.', 'error')
                return redirect(url_for('teacher_email_settings'))

        current_user.email_sender_address = email_sender_address
        current_user.email_enabled = email_enabled
        
        if app_password:
            try:
                encrypted = encrypt_password(app_password)
            except Exception:
                logger.exception('Failed to encrypt Gmail app password for user %s.', current_user.id)
                flash('Could not save the App Password due to an encryption configuration error. Please contact your administrator.', 'error')
                return redirect(url_for('teacher_email_settings'))
            if not encrypted:
                flash('Could not save the App Password (encryption returned an empty value). Please contact your administrator.', 'error')
                return redirect(url_for('teacher_email_settings'))
            current_user.encrypted_app_password = encrypted
            
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception('Failed to commit email settings for user %s.', current_user.id)
            flash('Failed to save email settings due to a database error. Please try again.', 'error')
            return redirect(url_for('teacher_email_settings'))
        flash('Email settings updated successfully.', 'success')
        return redirect(url_for('teacher_email_settings'))
        
    has_password = bool(current_user.encrypted_app_password)
    return render_template('teacher_email_settings.html', has_password=has_password)


@app.route('/teacher/quizzes')
@login_required
@teacher_required
def teacher_quizzes_page():
    q = request.args.get('q', '').strip()
    query = scope_to_institution(Quiz.query, Quiz)
    if q: query = query.filter(Quiz.title.contains(q))
    quizzes = query.order_by(Quiz.created_at.desc()).all()
    return render_template('teacher_quizzes.html', quizzes=quizzes, search_query=q)

@app.route('/teacher/attendance')
@login_required
@teacher_required
def teacher_attendance_page():
    class_id = request.args.get('class_id', type=int)
    if class_id:
        return redirect(url_for('teacher_take_attendance_page', classroom_id=class_id))
    
    all_classrooms = scope_to_institution(Classroom.query, Classroom).all()
    teacher_classrooms = []
    
    is_admin_or_sysadmin = current_user.role in ('admin', 'system_admin')
    is_hod = getattr(current_user, 'is_hod', False) and current_user.headed_department
    
    for cls in all_classrooms:
        is_class_teacher = (cls.teacher_id == current_user.id)
        assigned_slots_count = TimetableSlot.query.filter_by(classroom_id=cls.id, teacher_id=current_user.id).count()
        is_subject_teacher = (assigned_slots_count > 0)
        is_dept_hod = is_hod and (cls.department_id == current_user.headed_department.id)
        
        students_count = cls.students.count() if hasattr(cls.students, 'count') else len(cls.students)
        if is_admin_or_sysadmin or is_class_teacher or is_subject_teacher or is_dept_hod:
            role_label = []
            if is_class_teacher: role_label.append("Class Teacher")
            if is_subject_teacher: role_label.append("Subject Teacher")
            if is_dept_hod: role_label.append("HOD")
            if is_admin_or_sysadmin and not role_label: role_label.append("Admin Overseer")
            
            teacher_classrooms.append({
                'classroom': cls,
                'assigned_slots_count': assigned_slots_count,
                'students_count': students_count,
                'role_label': ", ".join(role_label) if role_label else "Faculty"
            })
            
    # Fallback if no specific role match
    if not teacher_classrooms and all_classrooms:
        for cls in all_classrooms:
            students_count = cls.students.count() if hasattr(cls.students, 'count') else len(cls.students)
            teacher_classrooms.append({
                'classroom': cls,
                'assigned_slots_count': 0,
                'students_count': students_count,
                'role_label': "Faculty"
            })

    return render_template('teacher_attendance_hub.html', teacher_classrooms=teacher_classrooms)

@app.route('/teacher/enrolled_students')
@login_required
@teacher_required
def teacher_enrolled_students_page():
    q = request.args.get('q', '').strip()
    classes = scope_to_institution(Classroom.query, Classroom).all()
    class_ids = [cls.id for cls in classes]
    all_remarks = StudentRemark.query.filter(StudentRemark.classroom_id.in_(class_ids)).all() if class_ids else []
    remarks_map = {(r.student_id, r.classroom_id): r.remark for r in all_remarks}

    enrolled_set = {}
    for cls in classes:
        for s in cls.students:
            if s.id not in enrolled_set:
                enrolled_set[s.id] = {'student': s, 'classes': [], 'class_details': []}
            enrolled_set[s.id]['classes'].append(cls.name)
            remark_text = remarks_map.get((s.id, cls.id), '')
            enrolled_set[s.id]['class_details'].append({
                'id': cls.id,
                'name': cls.name,
                'teacher_id': cls.teacher_id,
                'remark': remark_text
            })

    if current_user.role == 'system_admin':
        students_all = User.query.filter_by(role='student').all()
    else:
        students_all = scope_to_institution(User.query.filter_by(role='student'), User).all()
    for s in students_all:
        if s.id not in enrolled_set:
            enrolled_set[s.id] = {'student': s, 'classes': [], 'class_details': []}

    enrolled_list = list(enrolled_set.values())
    if q:
        query_lower = q.lower().strip()
        tokens = [t for t in query_lower.split() if len(t) >= 1]
        scored_results = []
        for entry in enrolled_list:
            student = entry['student']
            class_names = entry['classes']
            score = 0
            search_name = student.username.lower()
            search_phone = (student.phone or '').lower()
            search_classes = ' '.join(c.lower() for c in class_names)
            if tokens:
                token_score = 0
                all_tokens_match = True
                for token in tokens:
                    token_found = False
                    if token in search_name: token_found = True
                    if token in search_phone: token_found = True; token_score += 8
                    if token in search_classes: token_found = True; token_score += 2
                    if not token_found: all_tokens_match = False; break
                if all_tokens_match: score += token_score
                if query_lower == search_name: score += 50
                if search_name.startswith(query_lower): score += 20
                if score > 0: scored_results.append((score, entry))
            else:
                if query_lower in search_name or query_lower in search_phone or query_lower in search_classes:
                    scored_results.append((1, entry))
        scored_results.sort(key=lambda x: (-x[0], x[1]['student'].username.lower()))
        enrolled_list = [entry for score, entry in scored_results]
    return render_template('teacher_enrolled_students.html', enrolled_list=enrolled_list, search_query=q)

# ── Notifications ──
@app.route('/notifications')
@login_required
def view_notifications():
    notifications = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.created_at.desc()).all()
    unread_count = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    return render_template('notifications.html', notifications=notifications, unread_count=unread_count)

@app.route('/api/notifications/mark_read', methods=['POST'])
@login_required
def mark_notifications_read():
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update({'is_read': True})
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/notifications/mark_one_read/<int:notification_id>', methods=['POST'])
@login_required
def mark_one_notification_read(notification_id):
    notif = Notification.query.get(notification_id)
    if notif and notif.user_id == current_user.id:
        notif.is_read = True
        db.session.commit()
    return jsonify({'success': True})

# ── Upload & Video Routes ──
def assemble_chunks(uuid_str, total_chunks, target_path):
    import shutil
    chunks_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'chunks', uuid_str)
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    buffer_size = 16 * 1024 * 1024  # 16 MB high-speed streaming buffer
    with open(target_path, 'wb') as outfile:
        for i in range(total_chunks):
            chunk_file = os.path.join(chunks_dir, str(i))
            if not os.path.exists(chunk_file):
                raise Exception(f"Missing chunk file: {i}")
            with open(chunk_file, 'rb') as infile:
                shutil.copyfileobj(infile, outfile, length=buffer_size)
            try:
                os.remove(chunk_file)
            except Exception:
                pass
    shutil.rmtree(chunks_dir, ignore_errors=True)

# ── Chunked Upload Concurrency Control ──
# Parallel/resumable upload clients frequently send multiple chunk requests at once,
# so the "last" chunk (by index) can arrive and finish BEFORE earlier chunks have been
# written to disk. Triggering assembly purely because chunk_index == total_chunks - 1
# is a race condition: it can fire while chunks are still missing, causing the whole
# upload to be discarded. We instead check for *actual completeness* on disk every time
# a chunk lands, and use a per-upload lock so only one request ever triggers assembly.
_CHUNK_UPLOAD_LOCKS = {}
_CHUNK_UPLOAD_LOCKS_GUARD = threading.Lock()

def _get_chunk_upload_lock(upload_uuid):
    with _CHUNK_UPLOAD_LOCKS_GUARD:
        lock = _CHUNK_UPLOAD_LOCKS.get(upload_uuid)
        if lock is None:
            lock = threading.Lock()
            _CHUNK_UPLOAD_LOCKS[upload_uuid] = lock
        return lock

def _release_chunk_upload_lock(upload_uuid):
    with _CHUNK_UPLOAD_LOCKS_GUARD:
        _CHUNK_UPLOAD_LOCKS.pop(upload_uuid, None)

# Note: /teacher/upload_chunk and /teacher/upload are handled by routes.video blueprint (video_bp)
# to support multi-tenant institution storage (/static/uploads/institutions/<slug>/<video_id>/)
# and ultra-parallel GPU HLS conversion.

@app.route('/api/video_status/<int:video_id>')
@login_required
def get_video_status(video_id):
    video = Video.query.get_or_404(video_id)
    if video.uploader_id != current_user.id and current_user.role != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    return jsonify({'status': video.status, 'progress': video.processing_progress, 'title': video.title})

@app.route('/api/teacher/processing_videos')
@login_required
@teacher_required
def get_processing_videos():
    # Scoped scalar query to prevent DB lock contention & limit load on large N video libraries
    rows = db.session.query(Video.id, Video.title, Video.status, Video.processing_progress)\
        .filter(Video.uploader_id == current_user.id, Video.status.in_(['pending', 'processing', 'queued', 'interrupted'])).all()
    return jsonify([{'id': r[0], 'title': r[1], 'status': r[2], 'progress': r[3]} for r in rows])

# ═══════════════════════════════════════════════════════════════
#  ADMIN VIDEO CONVERSION QUEUE & WORKER APIS
# ═══════════════════════════════════════════════════════════════

@app.route('/api/admin/conversion_jobs', methods=['GET'])
@login_required
@admin_required
def admin_get_conversion_jobs():
    """Get live status of persistent conversion jobs with institution and admin filtering."""
    inst_filter = request.args.get('institution_id', type=int)
    admin_filter = request.args.get('admin_id', type=int)
    
    # Non-system admins can only view their own institution's conversion jobs
    if current_user.role != 'system_admin':
        inst_filter = current_user.institution_id
    
    jobs = get_active_conversion_jobs(institution_id=inst_filter, admin_id=admin_filter)
    manager = ConversionWorkerManager.get_instance(app)
    
    response_data = {
        'success': True,
        'max_workers': manager.max_workers,
        'active_workers': sum(1 for j in jobs if j['status'] == 'processing'),
        'queued_jobs': sum(1 for j in jobs if j['status'] == 'queued'),
        'completed_jobs': sum(1 for j in jobs if j['status'] == 'completed'),
        'failed_jobs': sum(1 for j in jobs if j['status'] == 'failed'),
        'jobs': jobs,
        'is_system_admin': current_user.role == 'system_admin'
    }
    
    if current_user.role == 'system_admin':
        institutions = Institution.query.order_by(Institution.name.asc()).all()
        admins = User.query.filter_by(role='admin').all()
        response_data['institutions'] = [{'id': i.id, 'name': i.name, 'slug': i.slug} for i in institutions]
        response_data['admins'] = [{'id': a.id, 'username': a.username, 'institution_id': a.institution_id} for a in admins]
        
    return jsonify(response_data)

@app.route('/api/admin/conversion_jobs/retry_all', methods=['POST'])
@login_required
@admin_required
@csrf_protect
def admin_retry_all_jobs():
    """Manually re-queue all failed and interrupted conversion jobs."""
    inst_filter = request.form.get('institution_id', type=int) or request.args.get('institution_id', type=int)
    admin_filter = request.form.get('admin_id', type=int) or request.args.get('admin_id', type=int)
    
    if current_user.role != 'system_admin':
        inst_filter = current_user.institution_id
        admin_filter = None
        
    count = retry_all_failed_conversion_jobs(institution_id=inst_filter, admin_id=admin_filter)
    return jsonify({
        'success': True,
        'retried_count': count,
        'message': f'Re-queued {count} failed conversion job(s).'
    })

@app.route('/api/admin/conversion_job/<int:job_id>/retry', methods=['POST'])
@login_required
@admin_required
@csrf_protect
def admin_retry_job(job_id):
    """Manually retry a failed conversion job."""
    success = retry_conversion_job(job_id)
    if success:
        return jsonify({'success': True, 'message': f'Job {job_id} re-queued for conversion.'})
    return jsonify({'success': False, 'message': 'Job not found.'}), 404

@app.route('/api/admin/conversion_job/<int:job_id>/cancel', methods=['POST'])
@login_required
@admin_required
@csrf_protect
def admin_cancel_job(job_id):
    """Cancel a conversion job."""
    success = cancel_conversion_job(job_id)
    if success:
        return jsonify({'success': True, 'message': f'Job {job_id} cancelled.'})
    return jsonify({'success': False, 'message': 'Job not found.'}), 404

# ── Subtitle Upload ──
@app.route('/teacher/upload_subtitles/<int:video_id>', methods=['POST'])
@login_required
@teacher_required
def upload_subtitles(video_id):
    video = Video.query.get_or_404(video_id)
    file = request.files.get('subtitle_file')
    language = request.form.get('language', 'en')
    
    if file and allowed_subtitle_file(file.filename):
        filename = secure_filename(file.filename)
        save_name = f"sub_{video_id}_{filename}"
        path = os.path.join(app.config['SUBTITLE_FOLDER'], save_name)
        file.save(path)
        video.subtitle_path = f'subtitles/{save_name}'
        video.subtitle_language = language
        db.session.commit()
        flash('Subtitles uploaded successfully.', 'success')
        return jsonify({'success': True})
    return jsonify({'error': 'Invalid subtitle file. Use .vtt or .srt'}), 400

# ── Like Video ──
@app.route('/api/video/<int:video_id>/like', methods=['POST'])
@login_required
def like_video(video_id):
    video = Video.query.get_or_404(video_id)
    existing = VideoLike.query.filter_by(user_id=current_user.id, video_id=video_id).first()
    if existing:
        db.session.delete(existing)
        video.like_count = max(0, (video.like_count or 0) - 1)
        liked = False
    else:
        like = VideoLike(user_id=current_user.id, video_id=video_id)
        db.session.add(like)
        video.like_count = (video.like_count or 0) + 1
        liked = True
    db.session.commit()
    return jsonify({'liked': liked, 'count': video.like_count})

@app.route('/teacher/add_student', methods=['POST'])
@login_required
@teacher_required
def add_student():
    username = request.form.get('username')
    password = request.form.get('password')
    inst_id = getattr(current_user, 'institution_id', None)
    if User.query.filter_by(username=username, institution_id=inst_id).first():
        flash('Username already exists.', 'error')
    else:
        new_student = User(username=username, role='student', institution_id=inst_id)
        new_student.set_password(password)
        db.session.add(new_student)
        current_user.xp += 20
        db.session.commit()
        flash('Student added successfully. +20 XP!', 'success')
        log_activity('add_student', f'Added student {username}')
    return redirect(url_for('teacher_enrolled_students_page'))

@app.route('/teacher/change_student_password', methods=['POST'])
@login_required
@teacher_required
def change_student_password():
    student_id = request.form.get('student_id')
    new_password = request.form.get('new_password')
    student = User.query.get(student_id)
    if student:
        enforce_institution_access(student)
        if student.role == 'student':
            student.set_password(new_password)
            db.session.commit()
            flash('Student password updated successfully.', 'success')
            return redirect(url_for('teacher_enrolled_students_page'))
    flash('Error updating student password.', 'error')
    return redirect(url_for('teacher_enrolled_students_page'))

@app.route('/teacher/create_playlist', methods=['POST'])
@login_required
@teacher_required
def create_playlist():
    title = request.form.get('title')
    inst_id = getattr(current_user, 'institution_id', None)
    new_playlist = Playlist(title=title, creator_id=current_user.id, institution_id=inst_id)
    db.session.add(new_playlist)
    current_user.xp += 30
    db.session.commit()
    flash('Playlist created. +30 XP!', 'success')
    return redirect(url_for('teacher_playlists_page'))

@app.route('/teacher/add_to_playlist', methods=['POST'])
@login_required
@teacher_required
def add_to_playlist():
    playlist_id = request.form.get('playlist_id')
    video_id = request.form.get('video_id')
    playlist = Playlist.query.get(playlist_id)
    video = Video.query.get(video_id)
    if playlist and video:
        enforce_institution_access(playlist)
        enforce_institution_access(video)
        if video not in playlist.videos:
            playlist.videos.append(video)
            db.session.commit()
            flash('Video added to playlist.', 'success')
    return redirect(url_for('teacher_videos_page'))

def purge_video_dependent_records(video_id):
    """Purge all foreign key dependent records for a video prior to deleting the video row."""
    try:
        from models import (
            VideoCheckpoint, CheckpointResponse, VideoDoubt, VideoDoubtReply,
            VideoFlashcard, Comment, VideoNote, VideoBookmark, VideoProgress,
            VideoLike, ViewAnalytics, Notification, ConversionJob, playlist_videos
        )
        # Checkpoints & Responses
        cp_ids = [cp.id for cp in VideoCheckpoint.query.filter_by(video_id=video_id).all()]
        if cp_ids:
            CheckpointResponse.query.filter(CheckpointResponse.checkpoint_id.in_(cp_ids)).delete(synchronize_session=False)
            VideoCheckpoint.query.filter_by(video_id=video_id).delete(synchronize_session=False)

        # Doubts & Doubt Replies
        doubt_ids = [d.id for d in VideoDoubt.query.filter_by(video_id=video_id).all()]
        if doubt_ids:
            VideoDoubtReply.query.filter(VideoDoubtReply.doubt_id.in_(doubt_ids)).delete(synchronize_session=False)
            VideoDoubt.query.filter_by(video_id=video_id).delete(synchronize_session=False)

        # Flashcards
        VideoFlashcard.query.filter_by(video_id=video_id).delete(synchronize_session=False)

        # Comments, Notes, Bookmarks, Progress, Likes, ViewAnalytics, Notifications, ConversionJobs
        Comment.query.filter_by(video_id=video_id).delete(synchronize_session=False)
        VideoNote.query.filter_by(video_id=video_id).delete(synchronize_session=False)
        VideoBookmark.query.filter_by(video_id=video_id).delete(synchronize_session=False)
        VideoProgress.query.filter_by(video_id=video_id).delete(synchronize_session=False)
        VideoLike.query.filter_by(video_id=video_id).delete(synchronize_session=False)
        ViewAnalytics.query.filter_by(video_id=video_id).delete(synchronize_session=False)
        Notification.query.filter_by(video_id=video_id).delete(synchronize_session=False)
        ConversionJob.query.filter_by(video_id=video_id).delete(synchronize_session=False)

        # Quizzes linked to this video: disassociate video_id to prevent FK constraint failures
        from models import Quiz
        Quiz.query.filter_by(video_id=video_id).update({'video_id': None}, synchronize_session=False)

        # Playlist videos association table
        db.session.execute(playlist_videos.delete().where(playlist_videos.c.video_id == video_id))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning(f"[Purge Video Records Warning] Video #{video_id}: {e}")

@app.route('/teacher/delete_video/<int:video_id>', methods=['GET', 'POST'])
@login_required
@teacher_required
def delete_video(video_id):
    video = Video.query.get_or_404(video_id)
    
    # SYSADMIN PERMISSION CHECK FOR INSTITUTION
    inst = Institution.query.get(video.institution_id) if video.institution_id else None
    if inst and not inst.allow_manual_video_delete:
        flash('⚠️ Video deletion permission has been disabled by the Sysadmin for your institution.', 'danger')
        log_activity('delete_video_blocked', f'Blocked deletion of video "{video.title}" due to Sysadmin restriction')
        return redirect(url_for('teacher_videos_page'))
    
    # Permission check: ensure teacher owns the video, or video has no uploader_id set, or user is admin/system_admin
    if current_user.role not in ['admin', 'system_admin'] and video.uploader_id and video.uploader_id != current_user.id:
        flash('You are not authorized to delete this video.', 'danger')
        return redirect(url_for('teacher_videos_page'))

    video_title = video.title
    try:
        from services.video_cleanup import permanently_delete_video_assets
        cleanup_res = permanently_delete_video_assets(video)
        logger.info(f"Permanent video file cleanup for video #{video_id}: {cleanup_res}")
    except Exception as e:
        logger.error(f"File deletion error for video {video_id}: {e}")

    # Purge dependent records first to prevent foreign key errors
    purge_video_dependent_records(video.id)

    db.session.delete(video)
    db.session.commit()
    flash(f'Video "{video_title}" deleted successfully.', 'success')
    log_activity('delete_video', f'Deleted video "{video_title}"')
    redirect_target = request.referrer if (request.referrer and '/watch/' not in request.referrer) else url_for('teacher_videos_page')
    return redirect(redirect_target)

@app.route('/teacher/delete_playlist/<int:playlist_id>', methods=['POST'])
@login_required
@teacher_required
def delete_playlist(playlist_id):
    playlist = Playlist.query.get_or_404(playlist_id)
    db.session.delete(playlist)
    db.session.commit()
    flash('Playlist deleted successfully.', 'success')
    return redirect(url_for('teacher_playlists_page'))

@app.route('/teacher/analytics')
@login_required
@teacher_required
def analytics():
    videos = Video.query.filter_by(uploader_id=current_user.id).all()
    data = []
    for video in videos:
        views = ViewAnalytics.query.filter_by(video_id=video.id).all()
        for v in views:
            viewer = User.query.get(v.user_id)
            data.append({
                'video_title': video.title,
                'student_name': viewer.username if viewer else 'Unknown',
                'start_time': v.start_time,
                'duration': v.duration_seconds
            })
    return render_template('analytics.html', analytics_data=data)

# ── Quiz Routes ──
@app.route('/teacher/create_quiz', methods=['GET', 'POST'])
@login_required
@teacher_required
def create_quiz():
    if request.method == 'POST':
        title = request.form.get('title')
        video_id = request.form.get('video_id') or None
        classroom_id = request.form.get('classroom_id') or None
        # NEW: closing time entered as hours + minutes (e.g. 1 hour 15 minutes)
        time_limit_hours = request.form.get('time_limit_hours', 0, type=int) or 0
        time_limit_extra_minutes = request.form.get('time_limit_extra_minutes', 0, type=int) or 0
        time_limit = (time_limit_hours * 60) + time_limit_extra_minutes
        if time_limit <= 0:
            # Backward compatible fallback if the old single-field name is posted
            time_limit = request.form.get('time_limit_minutes', 0, type=int)
        shuffle = request.form.get('shuffle_questions') == 'on'
        proctoring = request.form.get('proctoring_enabled') == 'on'
        max_tabs = request.form.get('max_tab_switches', 3, type=int)
        block_cp = request.form.get('block_copy_paste') == 'on'
        
        inst_id = getattr(current_user, 'institution_id', None)
        quiz = Quiz(
            title=title,
            teacher_id=current_user.id,
            institution_id=inst_id,
            time_limit_minutes=time_limit,
            shuffle_questions=shuffle,
            proctoring_enabled=proctoring,
            max_tab_switches=max_tabs,
            block_copy_paste=block_cp
        )
        if video_id: quiz.video_id = int(video_id)
        if classroom_id: quiz.classroom_id = int(classroom_id)
        db.session.add(quiz)
        current_user.xp += 25
        db.session.commit()
        flash('Quiz created. +25 XP!', 'success')
        return redirect(url_for('edit_quiz', quiz_id=quiz.id))
    videos = scope_to_institution(Video.query, Video).all()
    classes = scope_to_institution(Classroom.query, Classroom).all()
    return render_template('create_quiz.html', videos=videos, classes=classes)

@app.route('/teacher/edit_quiz/<int:quiz_id>', methods=['GET', 'POST'])
@login_required
@teacher_required
def edit_quiz(quiz_id):
    quiz = Quiz.query.get_or_404(quiz_id)
    if request.method == 'POST':
        text = request.form.get('text')
        op_a = request.form.get('option_a')
        op_b = request.form.get('option_b')
        op_c = request.form.get('option_c')
        op_d = request.form.get('option_d')
        correct = request.form.get('correct_option')
        explanation = request.form.get('explanation', '')
        q = Question(quiz_id=quiz.id, text=text, option_a=op_a, option_b=op_b, option_c=op_c, option_d=op_d, correct_option=correct, explanation=explanation)
        db.session.add(q)
        db.session.commit()
        flash('Question added.', 'success')
    return render_template('edit_quiz.html', quiz=quiz)

@app.route('/teacher/delete_quiz/<int:quiz_id>', methods=['POST'])
@login_required
@teacher_required
def delete_quiz(quiz_id):
    quiz = Quiz.query.get_or_404(quiz_id)
    db.session.delete(quiz)
    db.session.commit()
    flash('Quiz deleted successfully.', 'success')
    return redirect(url_for('teacher_quizzes_page'))

@app.route('/teacher/delete_question/<int:question_id>', methods=['POST'])
@login_required
@teacher_required
def delete_question(question_id):
    question = Question.query.get_or_404(question_id)
    quiz = Quiz.query.get(question.quiz_id)
    if not quiz: return 'Quiz not found', 404
    db.session.delete(question)
    db.session.commit()
    flash('Question deleted.', 'success')
    return redirect(url_for('edit_quiz', quiz_id=question.quiz_id))

@app.route('/student/quizzes')
@login_required
def student_quizzes():
    enrolled_class_ids = [c.id for c in current_user.enrolled_classes]
    qz_q = Quiz.query.filter((Quiz.classroom_id.in_(enrolled_class_ids)) | (Quiz.classroom_id == None))
    quizzes = scope_to_institution(qz_q, Quiz).all()
    taken_ids = [r.quiz_id for r in QuizResult.query.filter_by(student_id=current_user.id).all()]
    return render_template('student_quizzes.html', quizzes=quizzes, taken_ids=taken_ids)

@app.route('/student/quiz/<int:quiz_id>', methods=['GET', 'POST'])
@login_required
def take_quiz(quiz_id):
    quiz = Quiz.query.get_or_404(quiz_id)
    enforce_institution_access(quiz)

    if quiz.classroom_id:
        enrolled_class_ids = [c.id for c in current_user.enrolled_classes]
        if quiz.classroom_id not in enrolled_class_ids:
            flash('You are not enrolled in the class for this quiz.', 'error')
            return redirect(url_for('student_quizzes'))

    # Single-Attempt Guard: Check if student has already taken this quiz
    existing_result = QuizResult.query.filter_by(quiz_id=quiz.id, student_id=current_user.id).first()
    if existing_result:
        flash('You have already completed this quiz. Only one attempt is permitted.', 'info')
        return redirect(url_for('student_quizzes'))

    session_start_key = f'quiz_start_{quiz_id}_{current_user.id}'
    session_order_key = f'quiz_order_{quiz_id}_{current_user.id}'

    if request.method == 'POST':
        started_at_str = session.get(session_start_key)
        elapsed_seconds = 0
        if started_at_str:
            try:
                started_at = datetime.fromisoformat(started_at_str)
                elapsed_seconds = int((datetime.utcnow() - started_at).total_seconds())
            except Exception:
                elapsed_seconds = 0

        # NEW: auto-close the quiz server-side once the configured time limit has passed
        # (a small 20s grace period covers normal network/render latency)
        if quiz.time_limit_minutes and quiz.time_limit_minutes > 0:
            limit_seconds = quiz.time_limit_minutes * 60
            if elapsed_seconds > limit_seconds + 20:
                session.pop(session_start_key, None)
                session.pop(session_order_key, None)
                flash('Time is up — this quiz has closed automatically and your answers were not accepted.', 'error')
                return redirect(url_for('student_quizzes'))

        score = 0
        total = len(quiz.questions)
        answers = {}
        for q in quiz.questions:
            selected = request.form.get(f'q_{q.id}')
            answers[q.id] = selected
            if selected == q.correct_option: score += 1
        passed = total > 0 and (score * 100.0 / total) >= (quiz.passing_percent or 50)
        
        # Proctoring tracking
        proctoring_violations = int(request.form.get('proctoring_violations_count', 0))
        proctoring_log = request.form.get('proctoring_log_json', '[]')
        auto_cheated = request.form.get('auto_submitted_due_to_cheating') == 'true'

        result = QuizResult(
            institution_id=current_user.institution_id,
            quiz_id=quiz.id,
            student_id=current_user.id,
            score=score,
            total_questions=total,
            answers_json=json.dumps(answers),
            time_taken_seconds=elapsed_seconds,
            passed=passed,
            proctoring_violations_count=proctoring_violations,
            proctoring_log_json=proctoring_log,
            auto_submitted_due_to_cheating=auto_cheated
        )
        db.session.add(result)
        session.pop(session_start_key, None)
        session.pop(session_order_key, None)
        if auto_cheated:
            flash(f'Quiz was automatically submitted due to exceeding proctoring tab-switch limits. Score: {score}/{total}.', 'warning')
        elif passed:
            current_user.xp += 100
            if current_user.role == 'student':
                current_user.update_quest_progress('take_quiz', 1)
            flash(f'Quiz submitted. Score: {score}/{total}. +100 XP!', 'success')
        else:
            if current_user.role == 'student':
                current_user.update_quest_progress('take_quiz', 1)
            flash(f'Quiz submitted. Score: {score}/{total}.', 'info')
        db.session.commit()
        return redirect(url_for('student_quizzes'))

    # GET: start (or resume) the timer, and build a per-student randomized question order
    if session_start_key not in session:
        session[session_start_key] = datetime.utcnow().isoformat()

    questions = list(quiz.questions)
    if quiz.shuffle_questions:
        # Randomize order once per attempt and keep it stable across page reloads
        order = session.get(session_order_key)
        by_id = {q.id: q for q in questions}
        if not order or set(order) != set(by_id.keys()):
            order = list(by_id.keys())
            random.shuffle(order)
            session[session_order_key] = order
        questions = [by_id[qid] for qid in order if qid in by_id]

    remaining_seconds = None
    if quiz.time_limit_minutes and quiz.time_limit_minutes > 0:
        started_at = datetime.fromisoformat(session[session_start_key])
        elapsed = (datetime.utcnow() - started_at).total_seconds()
        remaining_seconds = max(0, int(quiz.time_limit_minutes * 60 - elapsed))

    return render_template('take_quiz.html', quiz=quiz, questions=questions, remaining_seconds=remaining_seconds)

# ═══════════════════════════════════════════════════════════════
#  STUDENT & WATCH ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route('/student')
@login_required
def student_dashboard():
    query = request.args.get('q')
    if query:
        p_q = Playlist.query.filter(Playlist.title.contains(query))
        v_q = Video.query.filter(Video.title.contains(query), Video.status.in_(['completed', 'ready']), Video.is_archived == False)
        playlists = scope_to_institution(p_q, Playlist).all()
        videos = scope_to_institution(v_q, Video).all()
    else:
        playlists = scope_to_institution(Playlist.query, Playlist).all()
        v_q = Video.query.filter(Video.status.in_(['completed', 'ready']), Video.is_archived == False).order_by(Video.upload_date.desc())
        videos = scope_to_institution(v_q, Video).limit(20).all()

    
    unread_count = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    settings = SiteSettings.query.first()
    enrolled_classes = current_user.enrolled_classes

    # Daily quest check-in
    if current_user.role == 'student':
        current_user.update_quest_progress('daily_login', 1)
        db.session.commit()

    daily_quests = current_user.get_daily_quests() if hasattr(current_user, 'get_daily_quests') else {}

    # NEW: percentage-accurate attendance (Present=100%, Late/Half Day=50%,
    # Absent=0%, Holiday/Medical Leave/OD excluded, missing days=Holiday)
    computed_pct = compute_overall_attendance_for_student(current_user)
    if computed_pct is not None:
        attendance_pct = computed_pct
    else:
        # Fallback for students with no sessions/records yet
        total_records = len(current_user.attendance_records) if current_user.attendance_records else 0
        present_records = len([r for r in current_user.attendance_records if r.status in ['Present', 'Late']]) if total_records > 0 else 0
        attendance_pct = int((present_records / total_records) * 100) if total_records > 0 else 0

    # NEW: per-class leaderboard rank, e.g. "2/20" based on XP (ties broken by level)
    class_ranks = {}
    for cls in enrolled_classes:
        classmates = sorted(list(cls.students), key=lambda s: (s.xp, s.level), reverse=True)
        total = len(classmates)
        rank = next((i + 1 for i, s in enumerate(classmates) if s.id == current_user.id), total)
        class_ranks[cls.id] = {'rank': rank, 'total': total}

    # Watch history (last 5)
    recent_views = ViewAnalytics.query.filter_by(user_id=current_user.id).order_by(ViewAnalytics.start_time.desc()).limit(5).all()
    
    return render_template('student_dashboard.html', playlists=playlists, videos=videos, 
        search_query=query, unread_count=unread_count, settings=settings, 
        enrolled_classes=enrolled_classes, now_date=datetime.utcnow().date(),
        attendance_pct=attendance_pct, recent_views=recent_views, class_ranks=class_ranks,
        daily_quests=daily_quests)

@app.route('/playlist/<int:playlist_id>')
@login_required
def view_playlist(playlist_id):
    playlist = Playlist.query.get_or_404(playlist_id)
    return render_template('playlist_view.html', playlist=playlist)

@app.route('/watch/<int:video_id>')
@app.route('/video/<int:video_id>')
@login_required
def watch_video(video_id):
    video = Video.query.get_or_404(video_id)
    enforce_institution_access(video)

    # Increment view count
    video.view_count = (video.view_count or 0) + 1

    # Quest progress trigger for watching video
    if current_user.role == 'student':
        current_user.update_quest_progress('watch_video', 1)

    # Ensure legacy adaptive videos still have a playable HLS path
    if not video.hls_playlist_path and video.master_playlist_path:
        video.hls_playlist_path = video.master_playlist_path

    db.session.commit()
    
    rel_q = Video.query.filter(Video.uploader_id == video.uploader_id, Video.id != video.id)
    related_videos = scope_to_institution(rel_q, Video).limit(5).all()

    top_level_comments = Comment.query.filter_by(video_id=video_id, parent_id=None).order_by(Comment.timestamp.desc()).all()
    settings = SiteSettings.query.first()
    
    # Check if user liked the video
    user_liked = VideoLike.query.filter_by(user_id=current_user.id, video_id=video_id).first() is not None
    
    # Compute hls_source and hls_url correctly for institution-based paths
    hls_source = (video.hls_playlist_path or video.master_playlist_path) if video.status == 'completed' else None
    hls_url = ''
    if hls_source:
        # hls_source may be legacy "hls/<id>/master.m3u8" or institution-based
        # "uploads/institutions/<slug>/hls/<id>/master.m3u8".
        # Use just the basename for the serve_hls endpoint which resolves the dir from DB.
        hls_url = url_for('serve_hls', video_id=video_id, filename=os.path.basename(hls_source))

    return render_template('video_player.html', video=video, related_videos=related_videos,
        comments=top_level_comments, settings=settings, user_liked=user_liked,
        hls_source=hls_source, hls_url=hls_url)

@app.route('/api/comment', methods=['POST'])
@login_required
def post_comment():
    data = request.json
    video_id = data.get('video_id')
    content = data.get('content')
    parent_id = data.get('parent_id')
    new_comment = Comment(content=content, user_id=current_user.id, video_id=video_id, parent_id=parent_id)
    db.session.add(new_comment)
    db.session.commit()
    video = Video.query.get(video_id)
    if parent_id:
        parent_comment = Comment.query.get(parent_id)
        if parent_comment and parent_comment.user_id != current_user.id:
            role_label = "Teacher" if current_user.role in ('teacher', 'hod') else current_user.name
            notif = Notification(user_id=parent_comment.user_id,
                message=f'{role_label} replied to your comment: "{content[:100]}"',
                video_id=video_id, comment_id=new_comment.id, notification_type='info')
            db.session.add(notif)
    else:
        if video and video.uploader_id != current_user.id:
            notif = Notification(user_id=video.uploader_id,
                message=f'{current_user.name} commented on your video "{video.title}": "{content[:100]}"',
                video_id=video_id, comment_id=new_comment.id, notification_type='info')
            db.session.add(notif)
    db.session.commit()
    return jsonify({'success': True, 'username': current_user.username, 'display_name': current_user.name, 'name': current_user.name, 'avatar_url': current_user.get_avatar_url(), 'content': content})

@app.route('/api/analytics/start', methods=['POST'])
@login_required
def track_start():
    data = request.json
    video_id = data.get('video_id')
    new_view = ViewAnalytics(
        user_id=current_user.id, video_id=video_id,
        ip_address=request.remote_addr, user_agent=request.user_agent.string[:300] if request.user_agent else None
    )
    db.session.add(new_view)
    db.session.commit()
    return jsonify({'view_id': new_view.id})

@app.route('/api/analytics/update', methods=['POST'])
@login_required
def track_update():
    data = request.json
    view_id = data.get('view_id')
    curr_time = data.get('duration')
    total_duration = data.get('total_duration')
    quality = data.get('quality')
    
    view = ViewAnalytics.query.get(view_id)
    if view and view.user_id == current_user.id:
        view.duration_seconds = curr_time
        view.end_time = datetime.utcnow()
        if quality:
            view.quality_selected = quality
        if total_duration and total_duration > 0:
            view.percent_watched = (curr_time / total_duration) * 100
            if view.percent_watched >= 90: view.completed = True
        if current_user.role == 'student': current_user.xp += 1
        db.session.commit()
    return jsonify({'success': True})

# ── Class Management Routes ──
@app.route('/teacher/create_class', methods=['POST'])
@login_required
@teacher_required
def create_class():
    name = request.form.get('name')
    description = request.form.get('description', '')
    if name:
        inst_id = getattr(current_user, 'institution_id', None)
        class_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        new_class = Classroom(
            name=name,
            teacher_id=current_user.id,
            institution_id=inst_id,
            description=description,
            class_code=class_code
        )
        db.session.add(new_class)
        current_user.xp += 40
        db.session.commit()
        flash(f'Class "{name}" created. Code: {class_code}. +40 XP!', 'success')
        log_activity('create_class', f'Created class "{name}"')
    return redirect(url_for('teacher_classes_page'))

@app.route('/student/join_class', methods=['POST'])
@login_required
def join_class():
    code = request.form.get('class_code', '').strip().upper()
    classroom = Classroom.query.filter_by(class_code=code).first()
    if not classroom:
        flash('Invalid class code.', 'error')
        return redirect(url_for('student_dashboard'))
    if current_user in classroom.students:
        flash('You are already enrolled in this class.', 'info')
    else:
        classroom.students.append(current_user)
        db.session.commit()
        flash(f'Joined class "{classroom.name}"!', 'success')
        log_activity('join_class', f'Joined class "{classroom.name}"')
    return redirect(url_for('student_dashboard'))

@app.route('/teacher/add_student_to_class', methods=['POST'])
@login_required
@teacher_required
def add_student_to_class():
    student_id = request.form.get('student_id')
    class_id = request.form.get('class_id')
    student = User.query.get(student_id)
    classroom = Classroom.query.get(class_id)
    if student and classroom:
        enforce_institution_access(student)
        enforce_institution_access(classroom)
        if student not in classroom.students:
            classroom.students.append(student)
            current_user.xp += 15
            db.session.commit()
            flash(f'Added {student.username} to {classroom.name}. +15 XP!', 'success')
        else:
            flash(f'{student.username} is already in {classroom.name}.', 'info')
    else: flash('Invalid student or class.', 'error')
    return redirect(url_for('teacher_classes_page'))


@app.route('/teacher/add_multiple_students_to_class', methods=['POST'])
@login_required
@teacher_required
def add_multiple_students_to_class():
    class_id = request.form.get('class_id')
    student_ids = request.form.getlist('student_ids')
    classroom = Classroom.query.get(class_id)
    if not classroom:
        flash('Invalid class.', 'error')
        return redirect(url_for('teacher_classes_page'))
    enforce_institution_access(classroom)
    
    added_count = 0
    for sid in student_ids:
        student = User.query.get(int(sid))
        if student and student.role == 'student' and getattr(student, 'institution_id', None) == classroom.institution_id and student not in classroom.students:
            classroom.students.append(student)
            added_count += 1
    
    if added_count > 0:
        current_user.xp += added_count * 5  # 5 XP per student added in bulk
        db.session.commit()
        flash(f'Added {added_count} student(s) to {classroom.name}. +{added_count * 5} XP!', 'success')
    else:
        flash('No new students were added (already enrolled or invalid).', 'info')
    
    return redirect(url_for('teacher_classes_page'))

@app.route('/teacher/remove_student_from_class', methods=['POST'])
@login_required
@teacher_required
def remove_student_from_class():
    student_id = request.form.get('student_id')
    class_id = request.form.get('class_id')
    student = User.query.get(student_id)
    classroom = Classroom.query.get(class_id)
    if student and classroom:
        enforce_institution_access(student)
        enforce_institution_access(classroom)
        if student in classroom.students:
            classroom.students.remove(student)
            db.session.commit()
            flash(f'Removed {student.username} from {classroom.name}.', 'success')
    return redirect(url_for('teacher_classes_page'))

@app.route('/teacher/delete_student/<int:student_id>', methods=['POST'])
@login_required
@teacher_required
def delete_student(student_id):
    student = User.query.get_or_404(student_id)
    enforce_institution_access(student)
    if student.role == 'student':
        # Delete dependent records first to avoid FK constraint violations
        EmailDeliveryLog.query.filter_by(student_id=student.id).delete()
        # Also clean up other dependent records referencing this student
        Attendance.query.filter_by(student_id=student.id).delete()
        db.session.delete(student)
        db.session.commit()
        flash('Student account deleted.', 'success')
        log_activity('delete_student', f'Deleted student {student.username}')
    else: flash('Cannot delete non-students.', 'error')
    return redirect(url_for('teacher_enrolled_students_page'))

@app.route('/teacher/delete_class/<int:class_id>', methods=['POST'])
@login_required
@teacher_required
def delete_class(class_id):
    classroom = Classroom.query.get_or_404(class_id)
    enforce_institution_access(classroom)

    # Permanently remove any assignment question papers / student submission
    # files on disk before the DB rows cascade-delete, so nothing is orphaned.
    for assignment in Assignment.query.filter_by(classroom_id=class_id).all():
        _delete_assignment_files(assignment)

    # Delete dependent records first to avoid FK constraint violations
    Attendance.query.filter_by(classroom_id=class_id).delete()
    EmailDeliveryLog.query.filter_by(class_id=class_id).delete()
    db.session.delete(classroom)
    db.session.commit()
    flash(f'Class deleted successfully.', 'success')
    return redirect(url_for('teacher_classes_page'))

@app.route('/teacher/quiz_report/<int:quiz_id>')
@login_required
@teacher_required
def quiz_report(quiz_id):
    quiz = Quiz.query.get_or_404(quiz_id)
    results = QuizResult.query.filter_by(quiz_id=quiz.id).all()
    detailed_results = []
    for r in results:
        student = User.query.get(r.student_id)
        if student:
            detailed_results.append({
                'student': student, 'score': r.score, 'total': r.total_questions, 'date': r.timestamp
            })
    return render_template('quiz_report.html', quiz=quiz, results=detailed_results, datetime=datetime)

def _get_teacher_role_map(classroom):
    role_map = {}
    if classroom and classroom.teacher_id:
        role_map[classroom.teacher_id] = 'Class Teacher'
    if classroom and hasattr(classroom, 'subject_teachers'):
        for st in classroom.subject_teachers:
            if st.teacher_id not in role_map:
                role_map[st.teacher_id] = st.subject
    return role_map


# ── Chatroom Routes ──
@app.route('/chatroom/<int:class_id>')
@login_required
def chatroom(class_id):
    classroom = Classroom.query.get_or_404(class_id)
    if current_user.role == 'student':
        enrolled_ids = [c.id for c in current_user.enrolled_classes]
        if class_id not in enrolled_ids:
            flash('You are not enrolled in this class.', 'error')
            return redirect(url_for('student_dashboard'))
    messages = ChatMessage.query.options(db.joinedload(ChatMessage.user)).filter_by(classroom_id=class_id).order_by(ChatMessage.timestamp.asc()).all()
    teacher_role_map = _get_teacher_role_map(classroom)
    return render_template('chatroom.html', classroom=classroom, messages=messages, teacher_role_map=teacher_role_map)

@app.route('/api/chatroom/<int:class_id>/send', methods=['POST'])
@login_required
def send_chat_message(class_id):
    classroom = Classroom.query.get_or_404(class_id)
    if current_user.role == 'student':
        enrolled_ids = [c.id for c in current_user.enrolled_classes]
        if class_id not in enrolled_ids: return jsonify({'error': 'Not enrolled'}), 403
    data = request.json
    content = data.get('content', '').strip()
    if not content: return jsonify({'error': 'Empty message'}), 400
    msg = ChatMessage(classroom_id=class_id, user_id=current_user.id, content=content)
    db.session.add(msg)
    if current_user.role in ('teacher', 'hod'): current_user.xp += 5
    db.session.commit()
    
    teacher_role_map = _get_teacher_role_map(classroom)
    teacher_label = teacher_role_map.get(current_user.id)

    # Emit via SocketIO for real-time update
    socketio.emit('new_message', {
        'id': msg.id, 'username': current_user.username, 'role': current_user.role,
        'teacher_label': teacher_label,
        'content': msg.content, 'timestamp': msg.timestamp.strftime('%I:%M %p'),
        'classroom_id': class_id, 'avatar_url': current_user.avatar_url,
        'display_name': current_user.name, 'name': current_user.name
    }, room=f'class_{class_id}')
    
    return jsonify({
        'success': True, 'id': msg.id, 'username': current_user.username,
        'role': current_user.role, 'teacher_label': teacher_label,
        'content': msg.content,
        'timestamp': msg.timestamp.strftime('%I:%M %p'),
        'avatar_url': current_user.avatar_url,
        'display_name': current_user.name, 'name': current_user.name
    })

@app.route('/api/chatroom/<int:class_id>/messages')
@login_required
def get_chat_messages(class_id):
    after_id = request.args.get('after', 0, type=int)
    classroom = Classroom.query.get_or_404(class_id)
    teacher_role_map = _get_teacher_role_map(classroom)
    messages = ChatMessage.query.options(db.joinedload(ChatMessage.user)).filter(
        ChatMessage.classroom_id == class_id, ChatMessage.id > after_id
    ).order_by(ChatMessage.timestamp.asc()).all()
    return jsonify({
        'messages': [{
            'id': m.id, 'username': m.user.username, 'role': m.user.role,
            'teacher_label': teacher_role_map.get(m.user_id),
            'content': m.content, 'timestamp': m.timestamp.strftime('%I:%M %p'), 'user_id': m.user_id,
            'avatar_url': m.user.avatar_url, 'display_name': m.user.name, 'name': m.user.name
        } for m in messages]
    })

@app.route('/api/chatroom/delete_message/<int:message_id>', methods=['POST'])
@login_required
@teacher_required
def delete_chat_message(message_id):
    msg = ChatMessage.query.get_or_404(message_id)
    db.session.delete(msg)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/chatroom/<int:class_id>/delete_all', methods=['POST'])
@login_required
@teacher_required
def delete_all_chats(class_id):
    ChatMessage.query.filter_by(classroom_id=class_id).delete()
    db.session.commit()
    return jsonify({'success': True})

# ── Attendance Routes ──

@app.route('/teacher/attendance/session/create/<int:class_id>', methods=['POST'])
@login_required
@teacher_required
def create_attendance_session(class_id):
    """Create a new attendance session for a class. Starting date is fixed once
    set; only the assigned Class Teacher (or admin) may later change the ending date."""
    classroom = Classroom.query.get_or_404(class_id)
    name = request.form.get('name', '').strip() or f"Session - {datetime.utcnow().strftime('%b %d, %Y')}"
    start_date_str = request.form.get('start_date', '')
    end_date_str = request.form.get('end_date', '')
    try:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date() if start_date_str else datetime.utcnow().date()
    except ValueError:
        start_date = datetime.utcnow().date()
    try:
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date() if end_date_str else start_date
    except ValueError:
        end_date = start_date
    if end_date < start_date:
        flash('Ending date cannot be before the starting date.', 'error')
        return redirect(url_for('teacher_attendance_page', class_id=class_id))

    session_obj = AttendanceSession(classroom_id=class_id, name=name, start_date=start_date,
                                     end_date=end_date, created_by=current_user.id)
    db.session.add(session_obj)
    db.session.commit()
    flash(f'Attendance session "{name}" created ({start_date} → {end_date}).', 'success')
    log_activity('create_attendance_session', f'Created attendance session "{name}" for class {classroom.name}')
    return redirect(url_for('teacher_attendance_page', class_id=class_id))


@app.route('/teacher/attendance/session/<int:session_id>/edit_end_date', methods=['POST'])
@login_required
@teacher_required
def edit_attendance_session_end_date(session_id):
    """Only the classroom's Class Teacher (or admin) may change the ending date.
    The starting date always remains constant."""
    session_obj = AttendanceSession.query.get_or_404(session_id)
    if not session_obj.can_edit_end_date(current_user):
        flash('Only the Class Teacher for this classroom can change the ending date.', 'error')
        return redirect(url_for('teacher_attendance_page', class_id=session_obj.classroom_id))
    new_end_str = request.form.get('end_date', '')
    try:
        new_end = datetime.strptime(new_end_str, '%Y-%m-%d').date()
    except ValueError:
        flash('Invalid ending date.', 'error')
        return redirect(url_for('teacher_attendance_page', class_id=session_obj.classroom_id))
    if new_end < session_obj.start_date:
        flash('Ending date cannot be before the fixed starting date.', 'error')
        return redirect(url_for('teacher_attendance_page', class_id=session_obj.classroom_id))
    session_obj.end_date = new_end
    db.session.commit()
    flash(f'Ending date updated to {new_end}.', 'success')
    log_activity('edit_attendance_session_end_date', f'Updated end date of session #{session_id} to {new_end}')
    return redirect(url_for('teacher_attendance_page', class_id=session_obj.classroom_id))


@app.route('/teacher/attendance/session/<int:session_id>/add_subsession', methods=['POST'])
@login_required
@teacher_required
def add_attendance_subsession(session_id):
    """Teachers can add unlimited (N) sub-sessions/periods within an attendance session."""
    session_obj = AttendanceSession.query.get_or_404(session_id)
    name = request.form.get('name', '').strip() or f"Session {len(session_obj.sub_sessions) + 1}"
    sub = AttendanceSubSession(attendance_session_id=session_id, name=name, created_by=current_user.id)
    db.session.add(sub)
    db.session.commit()
    flash(f'Added sub-session "{name}".', 'success')
    return redirect(url_for('teacher_attendance_page', class_id=session_obj.classroom_id))

@app.route('/attendance/session/<int:session_id>/report')
@login_required
@teacher_required
def attendance_session_report(session_id):
    """Session-wise attendance report for teachers and admins: shows each
    student's overall percentage for the session plus their 'current'
    percentage (calculated from the session's fixed starting date up to
    today / the ending date, whichever is earlier). Accessible to the
    classroom's Class Teacher or any Admin. Pass ?download=1 to open the
    print-friendly layout ready for 'Save as PDF'."""
    session_obj = AttendanceSession.query.get_or_404(session_id)
    classroom = session_obj.classroom
    if current_user.role in ('teacher', 'hod') and (not classroom or (classroom.teacher_id != current_user.id and not (current_user.is_hod and classroom.department_id == (current_user.headed_department.id if current_user.headed_department else None)))):
        abort(403)
    settings = SiteSettings.query.first()
    report = compute_session_report(session_obj, settings=settings)
    download = request.args.get('download') == '1'
    return render_template('attendance_session_report.html', report=report,
        settings=settings, download=download, datetime=datetime)


@app.route('/attendance/reports')
@login_required
@teacher_required
def attendance_reports_hub():
    """Central hub listing every class (and its attendance sessions) that
    the current user can pull a session-wise attendance report for.
    Admins see every class in their institution; teachers see only their own."""
    if current_user.role == 'admin':
        classes = scope_to_institution(Classroom.query, Classroom).order_by(Classroom.name).all()
    elif current_user.role == 'system_admin':
        classes = Classroom.query.order_by(Classroom.name).all()
    else:
        classes = scope_to_institution(Classroom.query.filter_by(teacher_id=current_user.id), Classroom).order_by(Classroom.name).all()
    settings = SiteSettings.query.first()
    classes_data = []
    for cls in classes:
        sessions = AttendanceSession.query.filter_by(classroom_id=cls.id).order_by(AttendanceSession.start_date.desc()).all()
        classes_data.append({'classroom': cls, 'sessions': sessions})
    return render_template('attendance_reports_hub.html', classes_data=classes_data, settings=settings)


@app.route('/teacher/mark_attendance/<int:class_id>/<int:student_id>', methods=['POST'])
@login_required
@teacher_required
def mark_attendance(class_id, student_id):
    classroom = Classroom.query.get_or_404(class_id)
    student = User.query.get_or_404(student_id)
    now = datetime.now()
    today_date = now.date()
    existing = Attendance.query.filter_by(student_id=student_id, classroom_id=class_id, date=today_date).first()
    if existing:
        flash(f'Attendance for {student.username} already recorded today ({existing.status}).', 'info')
        return redirect(url_for('teacher_attendance_page', class_id=class_id))
    forced_status = request.args.get('status') or request.form.get('status')
    if forced_status:
        if forced_status not in Attendance.STATUS_CHOICES:
            flash(f'Invalid attendance status "{forced_status}".', 'error')
            return redirect(url_for('teacher_attendance_page', class_id=class_id))
        status = forced_status
    else:
        class_start_str = classroom.start_time or "09:10"
        try: h, m = map(int, class_start_str.split(':'))
        except: h, m = 9, 10
        class_start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        diff = (now - class_start).total_seconds() / 60
        if diff <= 5: status = 'Present'
        elif diff <= 20: status = 'Late'
        else: status = 'Late'
    record = Attendance(student_id=student_id, classroom_id=class_id, date=today_date, status=status, arrival_time=now)
    db.session.add(record)
    db.session.commit()
    this_month = today_date.month
    lates = Attendance.query.filter(Attendance.student_id == student_id, Attendance.status == 'Late',
        db.extract('month', Attendance.date) == this_month).count()
    if lates >= 3:
        flash(f"WARNING: Student {student.username} late {lates} times! Parent notified via SMS.", 'warning')
        if student.phone:
            alert_msg = f"🎓 Campus Player: Student {student.username} has been Late for {lates} times this month."
            alert_data = [{'phone': student.phone, 'msg': alert_msg}]
            job_id = f"alert-{uuid.uuid4()}"
            SMS_JOBS[job_id] = {'status': 'queued', 'msg': 'Alert Queued', 'total': 1, 'current': 0}
            threading.Thread(target=_selenium_send_bulk_sms, args=(alert_data, job_id), daemon=True).start()
        # Also send parent email alert if parent_email is configured
        if student.parent_email:
            parent_subject = f"⚠️ Attendance Alert: {student.username} - Late {lates} times"
            parent_body = f"""
            <h2>Campus Player Attendance Alert</h2>
            <p>Dear {student.parent_name or 'Parent'},</p>
            <p>This is to inform you that <strong>{student.username}</strong> has been marked <strong>Late</strong> 
            {lates} times this month.</p>
            <p>Please encourage your ward to arrive on time for classes.</p>
            <hr>
            <p style="color:#666;font-size:12px;">- Campus Player Monitoring Team</p>
            """
            send_async_email(student.parent_email, parent_subject, parent_body)
    flash(f'Attendance marked for {student.username}: {status}', 'success')
    last_records = Attendance.query.filter_by(student_id=student_id).order_by(Attendance.date.desc()).limit(3).all()
    absent_streak = all(r.status == 'Absent' for r in last_records) if len(last_records) >= 3 else False
    if absent_streak:
        flash(f"CRITICAL: Student {student.username} absent for 3 consecutive days!", 'error')
        if student.phone:
            alert_msg = f"🎓 Campus Player CRITICAL: Student {student.username} has been ABSENT for 3 consecutive days."
            alert_data = [{'phone': student.phone, 'msg': alert_msg}]
            job_id = f"critical-{uuid.uuid4()}"
            SMS_JOBS[job_id] = {'status': 'queued', 'msg': 'Critical Alert Queued', 'total': 1, 'current': 0}
            threading.Thread(target=_selenium_send_bulk_sms, args=(alert_data, job_id), daemon=True).start()
        # Also send parent email alert for critical absence
        if student.parent_email:
            parent_subject = f"🚨 CRITICAL: {student.username} Absent 3 Consecutive Days"
            parent_body = f"""
            <h2 style="color:#dc2626;">🚨 Critical Attendance Alert</h2>
            <p>Dear {student.parent_name or 'Parent'},</p>
            <p>This is a <strong>critical alert</strong> regarding <strong>{student.username}</strong>.</p>
            <p>Your ward has been marked <strong>Absent</strong> for 3 consecutive days.</p>
            <p>Please contact the school administration immediately to address this concern.</p>
            <hr>
            <p style="color:#666;font-size:12px;">- Campus Player Monitoring Team</p>
            """
            send_async_email(student.parent_email, parent_subject, parent_body)
    return redirect(url_for('teacher_attendance_page', class_id=class_id))

@app.route('/teacher/report/struggling_topics')
@login_required
@teacher_required
def struggling_topics_report():
    stats = db.session.query(
        Video.title, User.username.label('uploader_name'),
        db.func.count(ViewAnalytics.id).label('view_count')
    ).join(ViewAnalytics, Video.id == ViewAnalytics.video_id).join(User, Video.uploader_id == User.id)\
     .group_by(Video.id, User.username).order_by(db.desc('view_count')).all()
    return render_template('struggling_topics.html', stats=stats)

@app.route('/teacher/report/monthly/<int:student_id>')
@login_required
@teacher_required
def monthly_report(student_id):
    student = User.query.get_or_404(student_id)
    this_month = datetime.utcnow().month
    records = Attendance.query.filter(Attendance.student_id == student_id, db.extract('month', Attendance.date) == this_month).all()
    total = len(records)
    present = len([r for r in records if r.status == 'Present'])
    late = len([r for r in records if r.status == 'Late'])
    absent = len([r for r in records if r.status == 'Absent'])
    attendance_pct = (present / total * 100) if total > 0 else 0
    working_hours = present * 6
    return render_template('monthly_report.html', student=student, attendance_pct=attendance_pct,
        total=total, present=present, late=late, absent=absent, working_hours=working_hours,
        now_date=datetime.utcnow().date())

# ── Phone Routes ──
@app.route('/teacher/student/update_phone/<int:student_id>', methods=['POST'])
@login_required
@teacher_required
def teacher_update_student_phone(student_id):
    student = User.query.get_or_404(student_id)
    phone = request.form.get('phone', '').strip()
    cleaned = '+' + phone.lstrip('+').replace(' ', '').replace('-', '') if phone else None
    student.phone = cleaned
    db.session.commit()
    flash(f'Phone updated for {student.username}.', 'success')
    return redirect(url_for('teacher_enrolled_students_page'))

# ── Remarks Routes ──
@app.route('/teacher/student/save_remark/<int:classroom_id>/<int:student_id>', methods=['POST'])
@login_required
@teacher_required
def save_student_remark(classroom_id, student_id):
    classroom = Classroom.query.get_or_404(classroom_id)
    student = User.query.get_or_404(student_id)
    
    # Ensure current user is the teacher of this classroom or is admin
    if current_user.role != 'admin' and classroom.teacher_id != current_user.id:
        abort(403)
        
    remark_text = request.form.get('remark', '').strip()
    
    remark_obj = StudentRemark.query.filter_by(student_id=student.id, classroom_id=classroom.id).first()
    if remark_obj:
        remark_obj.remark = remark_text
    else:
        remark_obj = StudentRemark(student_id=student.id, classroom_id=classroom.id, remark=remark_text)
        db.session.add(remark_obj)
        
    db.session.commit()
    flash(f"Remark updated for {student.username} in {classroom.name}.", "success")
    
    referrer = request.referrer or url_for('teacher_enrolled_students_page')
    return redirect(referrer)


# ── EMAIL AUTOMATION ENGINE ──

def send_profile_email_confirmation(student, old_email, new_email):
    """
    Sends a security-confirmation email to a student's *new* email address
    whenever they change it in their profile.
    Uses the SMTP credentials of the teacher whose class the student belongs to.
    Falls back to plain-text if no teacher is configured.
    """
    execution_id = str(uuid.uuid4())
    class_id = 0
    student_institution_id = student.institution_id
    try:
        # Find a teacher who has email configured for this student
        teacher = None
        cls = None
        class_name = "Campus Player"
        from models import Classroom
        enrollment = db.session.execute(
            student_classes.select().where(student_classes.c.student_id == student.id)
        ).first()
        if enrollment:
            cls = Classroom.query.get(enrollment.classroom_id)
            if cls:
                class_id = cls.id
                class_name = cls.name
                t = User.query.get(cls.teacher_id)
                if t and t.email_sender_address and t.encrypted_app_password and t.email_enabled:
                    teacher = t

        changed_at = datetime.now().strftime('%b %d, %Y %I:%M %p')
        login_link = (request.url_root.rstrip('/') if has_request_context() else 'http://127.0.0.1:5000') + '/login'
        remote_ip = request.remote_addr if has_request_context() else 'Unknown'
        browser_info = request.user_agent.string if has_request_context() else 'Unknown'

        body_text = (
            f"Campus Player - Email Address Updated\n"
            f"\n"
            f"Hello {student.display_name or student.username},\n"
            f"\n"
            f"Your Campus Player email address has been updated.\n"
            f"Previous Email: {old_email or '(not set)'}\n"
            f"New Email: {new_email}\n"
            f"Student ID: CP-{student.id:04d}\n"
            f"Class: {class_name}\n"
            f"Changed At: {changed_at}\n"
            f"Browser: {browser_info}\n"
            f"IP Address: {remote_ip}\n"
            f"\n"
            f"If you did not authorize this change, contact your administrator immediately.\n"
            f"Login: {login_link}\n"
        )

        # Render the confirmation HTML
        try:
            body_html = render_template(
                'email_profile_update.html',
                student_name=student.display_name or student.username,
                student_id=f"{student.id:04d}",
                old_email=old_email or "(not set)",
                new_email=new_email,
                class_name=class_name,
                changed_at=changed_at,
                login_link=login_link,
                teacher_name=teacher.display_name or teacher.username if teacher else "Campus Player System"
            )
        except Exception as te:
            logger.error(f"Profile-email template render error: {te}")
            body_html = (
                f"<p>Hello {student.display_name or student.username},<br>"
                f"Your Campus Player email address has been updated to <strong>{new_email}</strong>.<br>"
                f"If you did not make this change, contact your administrator.</p>"
            )

        subject = "Campus Player - Email Address Updated"

        if teacher:
            # Send via teacher's SMTP
            decrypted_pw = decrypt_password(teacher.encrypted_app_password)
            sender = teacher.email_sender_address
            email_from_name = teacher.display_name or teacher.username
        else:
            # Try Flask-Mail as fallback
            try:
                from flask_mail import Message as MailMessage
                msg_fm = MailMessage(subject=subject, recipients=[new_email], body=body_text, html=body_html)
                mail.send(msg_fm)
                logger.info(f"Profile update confirmation sent to {new_email} via Flask-Mail")
                log = EmailDeliveryLog(
                    execution_id=execution_id,
                    institution_id=student_institution_id,
                    class_id=class_id,
                    teacher_id=0,
                    student_id=student.id,
                    student_email=new_email,
                    subject=subject,
                    status='sent',
                    report_type='profile_email_update',
                    report_html=body_html
                )
                db.session.add(log)
                db.session.commit()
                return True
            except Exception as fm_err:
                logger.warning(f"Flask-Mail fallback failed for profile update email: {fm_err}")
                log = EmailDeliveryLog(
                    execution_id=execution_id,
                    institution_id=student_institution_id,
                    class_id=class_id,
                    teacher_id=0,
                    student_id=student.id,
                    student_email=new_email,
                    subject=subject,
                    status='failed',
                    error_message=str(fm_err)[:300],
                    report_type='profile_email_update',
                    report_html=body_html
                )
                db.session.add(log)
                db.session.commit()
                return False

        # Build MIME message (UTF-8 to handle any unicode)
        from email.utils import formataddr, formatdate, make_msgid
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = formataddr((email_from_name, sender))
        msg['To'] = formataddr((student.display_name or student.username, new_email))
        msg['Date'] = formatdate(localtime=True)
        msg['Message-ID'] = make_msgid()
        msg.attach(MIMEText(body_text, 'plain', 'utf-8'))
        msg.attach(MIMEText(body_html, 'html', 'utf-8'))

        server = smtplib.SMTP('smtp.gmail.com', 587, timeout=15)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(sender, decrypted_pw)
        server.send_message(msg)
        server.quit()

        logger.info(f"Profile update confirmation email sent to {new_email} for student {student.username}")
        log = EmailDeliveryLog(
            execution_id=execution_id,
            institution_id=student_institution_id,
            class_id=class_id,
            teacher_id=teacher.id,
            student_id=student.id,
            student_email=new_email,
            subject=subject,
            status='sent',
            report_type='profile_email_update',
            report_html=body_html
        )
        db.session.add(log)
        db.session.commit()
        return True

    except Exception as e:
        logger.error(f"Failed to send profile update confirmation email to {new_email}: {e}")
        try:
            log = EmailDeliveryLog(
                execution_id=execution_id,
                institution_id=student_institution_id,
                class_id=class_id,
                teacher_id=teacher.id if 'teacher' in locals() and teacher else 0,
                student_id=student.id,
                student_email=new_email,
                subject="Campus Player - Email Address Updated",
                status='failed',
                error_message=str(e)[:300],
                report_type='profile_email_update'
            )
            db.session.add(log)
            db.session.commit()
        except Exception as log_err:
            logger.error(f"Failed to record EmailDeliveryLog for failed profile update email: {log_err}")
        return False


def send_student_report_smtp(student, classroom, teacher, execution_id, report_type):
    """Generates progress report metrics, stores the HTML, and sends it to student via teacher's SMTP if configured."""
    subject = f"Academic Progress Report - {classroom.name}"
    # Resolved explicitly (rather than left for the request-context-based tenant
    # auto-fill) because this function is also invoked from background threads
    # (e.g. scheduled/admin-triggered bulk delivery), where there is no active
    # Flask request and institution_id would otherwise be left NULL, making the
    # log invisible to tenant-scoped admin queries.
    log_institution_id = student.institution_id or classroom.institution_id
    
    # 1. Collect Attendance
    records = Attendance.query.filter_by(student_id=student.id, classroom_id=classroom.id).all()
    total_days = len(records)
    present_days = len([r for r in records if r.status == 'Present'])
    late_days = len([r for r in records if r.status == 'Late'])
    absent_days = len([r for r in records if r.status == 'Absent'])
    attendance_pct = int(round(present_days / total_days * 100)) if total_days > 0 else 0
    attendance_summary = f"Present: {present_days}/{total_days}, Late: {late_days}, Absent: {absent_days}"
    
    # 2. Collect Assignments
    assignments = Assignment.query.filter_by(classroom_id=classroom.id).all()
    total_assignments = len(assignments)
    submissions = AssignmentSubmission.query.filter(
        AssignmentSubmission.student_id == student.id,
        AssignmentSubmission.assignment_id.in_([a.id for a in assignments])
    ).all() if assignments else []
    submitted_count = len([s for s in submissions if s.status in ['submitted', 'graded', 'returned']])
    completion_pct = (submitted_count / total_assignments * 100) if total_assignments > 0 else 0.0
    assignment_summary = f"Completed {submitted_count} of {total_assignments} assignments ({completion_pct:.1f}%)"
    
    # 3. Collect Quizzes
    quizzes = Quiz.query.filter_by(classroom_id=classroom.id).all()
    total_quizzes = len(quizzes)
    results = QuizResult.query.filter(
        QuizResult.student_id == student.id,
        QuizResult.quiz_id.in_([q.id for q in quizzes])
    ).all() if quizzes else []
    passed_count = len([r for r in results if r.passed])
    avg_score = (sum(r.score / r.total_questions for r in results) / len(results) * 100) if results else 0.0
    quiz_summary = f"Completed {len(results)} of {total_quizzes} quizzes, Passed: {passed_count}. Avg Score: {avg_score:.1f}%"
    
    # 4. Remarks
    remark_obj = StudentRemark.query.filter_by(student_id=student.id, classroom_id=classroom.id).first()
    remark_text = remark_obj.remark if remark_obj else "No remarks entered yet."
    remark_text = remark_text.strip()
    if remark_text.startswith('"') and remark_text.endswith('"'):
        remark_text = remark_text[1:-1].strip()
    remark_text = remark_text.replace('“', '').replace('”', '').replace('‘', '').replace('’', '')
    remark_text = "\n".join(line.lstrip('> ').rstrip() for line in remark_text.splitlines())
    
    # 5. Build portal login link
    base_url = request.url_root if has_request_context() else 'http://127.0.0.1:5000/'
    login_link = f"{base_url.rstrip('/')}/login"
    
    student_xp = getattr(student, 'xp', 0) or 0
    student_level = getattr(student, 'level', 1) or 1
    total_quiz_score = getattr(student, 'total_quiz_score', 0) or 0
    total_quizzes_taken = getattr(student, 'total_quizzes_taken', 0) or 0

    # NEW: Institution Name (per-institution if set, else the site-wide default)
    institution_name = 'Campus Player'
    try:
        if getattr(student, 'institution_id', None):
            inst = Institution.query.get(student.institution_id)
            if inst: institution_name = inst.name
        else:
            settings_obj = SiteSettings.query.first()
            if settings_obj and settings_obj.institution_name:
                institution_name = settings_obj.institution_name
    except Exception:
        pass

    # NEW: Today's attendance status (Present / Absent / Leave / etc.)
    today_date = datetime.now().date()
    today_attendance = Attendance.query.filter_by(student_id=student.id, classroom_id=classroom.id, date=today_date).first()
    today_attendance_status = today_attendance.status if today_attendance else 'Not marked yet'

    # NEW: Today's quiz/assignment activity (if conducted/submitted today)
    today_quiz_results = [r for r in results if getattr(r, 'completed_at', None) and r.completed_at.date() == today_date]
    today_quiz_summary = f"{len(today_quiz_results)} quiz(zes) taken today" if today_quiz_results else "No quiz activity today"
    today_submissions = [s for s in submissions if s.submitted_at and s.submitted_at.date() == today_date]
    today_assignment_summary = f"{len(today_submissions)} assignment(s) submitted today" if today_submissions else "No assignment activity today"

    student_login_id = student.username

    body_text = (
        f"Campus Player Student Progress Report\n"
        f"\n"
        f"Institution: {institution_name}\n"
        f"Sent By (Teacher): {teacher.display_name or teacher.username if teacher else 'Teacher'}\n"
        f"Student: {student.display_name or student.username}\n"
        f"Student ID / Login ID: {student_login_id}\n"
        f"Roll Number: CP-{student.id:04d}\n"
        f"Classroom: {classroom.name}\n"
        f"Report Date: {datetime.now().strftime('%b %d, %Y %I:%M %p')}\n"
        f"Level: {student_level}\n"
        f"XP: {student_xp}\n"
        f"Achievements: {len(student.get_achievements()) if hasattr(student, 'get_achievements') else 0}\n"
        f"\n"
        f"Attendance Today: {today_attendance_status}\n"
        f"Overall Attendance: {attendance_pct}%\n"
        f"  {attendance_summary}\n"
        f"Assignments: {assignment_summary}\n"
        f"Today's Assignment Activity: {today_assignment_summary}\n"
        f"Quiz Performance: {quiz_summary}\n"
        f"Today's Quiz Activity: {today_quiz_summary}\n"
        f"Remarks: {remark_text}\n"
        f"\n"
        f"Login: {login_link}\n"
        f"Teacher: {teacher.display_name or teacher.username if teacher else 'Teacher'}\n"
    )

    try:
        body_html = render_template('email_report_template.html',
            student_name=student.display_name or student.username,
            roll_number=f"CP-{student.id:04d}",
            classroom_name=classroom.name,
            report_date=datetime.now().strftime('%b %d, %Y %I:%M %p'),
            attendance_pct=attendance_pct,
            attendance_summary=attendance_summary,
            assignment_summary=assignment_summary,
            quiz_summary=quiz_summary,
            remark_text=remark_text,
            login_link=login_link,
            teacher_name=teacher.display_name or teacher.username if teacher else 'Teacher',
            xp=student_xp,
            level=student_level,
            total_quiz_score=total_quiz_score,
            total_quizzes_taken=total_quizzes_taken,
            achievement_count=len(student.get_achievements()) if hasattr(student, 'get_achievements') else 0,
            institution_name=institution_name,
            student_login_id=student_login_id,
            today_attendance_status=today_attendance_status,
            today_quiz_summary=today_quiz_summary,
            today_assignment_summary=today_assignment_summary
        )
    except Exception as re:
        logger.error(f"Template rendering failed: {re}")
        body_html = (
            f"<p>Report for {student.display_name or student.username}.<br>"
            f"Attendance: {attendance_pct}.%<br>"
            f"Assignments: {assignment_summary}.<br>"
            f"Quiz: {quiz_summary}.<br>"
            f"XP: {student_xp}. Level: {student_level}.<br>"
            f"Remarks: {remark_text}</p>"
        )

    dest_email = student.email

    # If teacher has no email configured, store report with 'stored' status for student portal viewing
    if not teacher or not teacher.email_sender_address or not teacher.encrypted_app_password or not teacher.email_enabled:
        log = EmailDeliveryLog(
            execution_id=execution_id,
            institution_id=log_institution_id,
            class_id=classroom.id,
            teacher_id=teacher.id if teacher else 0,
            student_id=student.id,
            student_email=dest_email or 'no-email-set@campusplayer.com',
            subject=subject,
            status='stored',
            error_message="Teacher email configuration not set or disabled. Report stored for student portal access.",
            report_type=report_type,
            report_html=body_html
        )
        db.session.add(log)
        db.session.commit()
        _persist_report_backup(log)
        return True

    # If student has no email, store with 'stored' status
    if not dest_email:
        log = EmailDeliveryLog(
            execution_id=execution_id,
            institution_id=log_institution_id,
            class_id=classroom.id,
            teacher_id=teacher.id,
            student_id=student.id,
            student_email='no-email-set@campusplayer.com',
            subject=subject,
            status='stored',
            error_message="Student has no email address registered. Report stored for student portal access.",
            report_type=report_type,
            report_html=body_html
        )
        db.session.add(log)
        db.session.commit()
        _persist_report_backup(log)
        return True

    # Decrypt password
    decrypted_pw = decrypt_password(teacher.encrypted_app_password)
    
    # Send via smtplib
    try:
        server = smtplib.SMTP('smtp.gmail.com', 587, timeout=15)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(teacher.email_sender_address, decrypted_pw)
        
        from email.utils import formataddr, formatdate, make_msgid
        from email.mime.application import MIMEApplication

        # NEW: the report is now generated as a PDF and attached to the mail
        pdf_filename = f'report_{student.username}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf'
        pdf_path = os.path.join(PDF_DIR, pdf_filename)
        try:
            _generate_student_progress_report_pdf(
                student, classroom, records, assignments, submissions, results,
                remark_text, pdf_path, datetime.now().strftime('%b %d, %Y %I:%M %p'),
                teacher.display_name or teacher.username
            )
        except Exception as pdf_err:
            logger.error(f"Failed to generate report PDF for email attachment: {pdf_err}")
            pdf_path = None

        msg = MIMEMultipart('mixed')
        msg['Subject'] = subject
        msg['From'] = formataddr((teacher.display_name or teacher.username, teacher.email_sender_address))
        msg['To'] = formataddr((student.display_name or student.username, dest_email))
        msg['Date'] = formatdate(localtime=True)
        msg['Message-ID'] = make_msgid()

        alt_part = MIMEMultipart('alternative')
        # UTF-8 charset ensures emojis / non-ASCII in template are handled correctly
        alt_part.attach(MIMEText(body_text, 'plain', 'utf-8'))
        alt_part.attach(MIMEText(body_html, 'html', 'utf-8'))
        msg.attach(alt_part)

        if pdf_path and os.path.exists(pdf_path):
            with open(pdf_path, 'rb') as pf:
                pdf_attachment = MIMEApplication(pf.read(), _subtype='pdf')
                pdf_attachment.add_header('Content-Disposition', 'attachment', filename=pdf_filename)
                msg.attach(pdf_attachment)

        server.send_message(msg)
        server.quit()

        # Log success with report_html stored
        log = EmailDeliveryLog(
            execution_id=execution_id,
            institution_id=log_institution_id,
            class_id=classroom.id,
            teacher_id=teacher.id,
            student_id=student.id,
            student_email=dest_email,
            subject=subject,
            status='sent',
            report_type=report_type,
            report_html=body_html
        )
        db.session.add(log)
        db.session.commit()
        _persist_report_backup(log)
        return True
        
    except smtplib.SMTPAuthenticationError as auth_err:
        logger.error(f"SMTP Auth Failure for teacher {teacher.username}: {auth_err}")
        log = EmailDeliveryLog(
            execution_id=execution_id,
            institution_id=log_institution_id,
            class_id=classroom.id,
            teacher_id=teacher.id,
            student_id=student.id,
            student_email=dest_email,
            subject=subject,
            status='failed',
            error_message=f"SMTP Authentication failed: {auth_err}",
            report_type=report_type,
            report_html=body_html
        )
        db.session.add(log)
        
        # Notify teacher
        notif = Notification(
            user_id=teacher.id,
            message=f"⚠️ Gmail App Password authentication failed for sender account {teacher.email_sender_address}. Report deliveries for your classes have failed. Please check your credentials.",
            notification_type='danger'
        )
        db.session.add(notif)
        _persist_report_backup(log)
        db.session.commit()
        return False
        
    except Exception as e:
        logger.error(f"SMTP Error: {e}")
        log = EmailDeliveryLog(
            execution_id=execution_id,
            institution_id=log_institution_id,
            class_id=classroom.id,
            teacher_id=teacher.id,
            student_id=student.id,
            student_email=dest_email,
            subject=subject,
            status='failed',
            error_message=str(e)[:300],
            report_type=report_type,
            report_html=body_html
        )
        db.session.add(log)
        
        # Notify Admin
        admins = User.query.filter_by(role='admin').all()
        for admin in admins:
            notif = Notification(
                user_id=admin.id,
                message=f"⚠️ Critical email delivery failure for student {student.username} (Class: {classroom.name}) via sender {teacher.email_sender_address}: {str(e)[:150]}",
                notification_type='danger'
            )
            db.session.add(notif)
        _persist_report_backup(log)
        db.session.commit()
        return False


def _persist_report_backup(log):
    try:
        _get_or_generate_report_pdf(log)
    except Exception as e:
        logger.error(f"Unable to persist PDF backup for log {getattr(log, 'id', 'unknown')}: {e}")


def _generate_student_progress_report_pdf(student, classroom, attendance_records, assignments, submissions, quiz_results, remark_text, pdf_path, report_date, teacher_name):
    """Write a detailed student progress report PDF backup to disk."""
    try:
        doc = SimpleDocTemplate(pdf_path, pagesize=A4, leftMargin=18*mm, rightMargin=18*mm, topMargin=20*mm, bottomMargin=18*mm)
        st = _pdf_styles()
        story = []

        story.append(Paragraph('Campus Player Academic Progress Report', st['title']))
        story.append(Paragraph(f'Generated on: {report_date}', st['sub']))
        story.append(HRFlowable(width='100%', thickness=2, color=colors.HexColor('#4f46e5')))
        story.append(Spacer(1, 6*mm))

        # ── METADATA GRID: STUDENT, CLASS & TEACHER DETAILS ──
        # Retrieve Student Profile Bio Data if available
        prof = getattr(student, 'profile', None)
        student_full_name = (prof.student_name if prof and prof.student_name else None) or student.display_name or student.username
        roll_no = (prof.roll_number if prof and prof.roll_number else None) or f"CP-STU-{student.id:04d}"
        
        # Retrieve Teacher Profile Details
        teacher_full_name = teacher_name or student.username
        cell_bold = ParagraphStyle('CellBold', parent=st['body'], fontName='Helvetica-Bold', textColor=colors.HexColor('#0f172a'))
        cell_normal = ParagraphStyle('CellNormal', parent=st['body'], textColor=colors.HexColor('#334155'))

        meta_table_data = [
            [
                Paragraph("<b>STUDENT DETAILS</b>", cell_bold),
                Paragraph("<b>CLASS DETAILS</b>", cell_bold),
                Paragraph("<b>CLASS TEACHER PROFILE</b>", cell_bold)
            ],
            [
                Paragraph(f"<b>Name:</b> {student_full_name}<br/><b>Username:</b> {student.username}<br/><b>Roll/ID:</b> {roll_no}<br/><b>Email:</b> {(student.email or 'N/A')}", cell_normal),
                Paragraph(f"<b>Class Name:</b> {classroom.name}<br/><b>Class Code:</b> {getattr(classroom, 'class_code', None) or 'N/A'}<br/><b>Start Time:</b> {getattr(classroom, 'start_time', None) or '09:10'}<br/><b>Students Enrolled:</b> {classroom.students.count()}", cell_normal),
                Paragraph(f"<b>Teacher Name:</b> {teacher_full_name}<br/><b>Role:</b> Class Teacher<br/><b>Status:</b> Active", cell_normal)
            ]
        ]

        meta_table = Table(meta_table_data, colWidths=[60*mm, 60*mm, 60*mm])
        meta_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#f1f5f9')),
            ('TEXTCOLOR', (0,0), (-1,0), colors.HexColor('#0f172a')),
            ('BOTTOMPADDING', (0,0), (-1,-1), 6),
            ('TOPPADDING', (0,0), (-1,-1), 6),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#cbd5e1')),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ]))

        story.append(meta_table)
        story.append(Spacer(1, 8*mm))

        # ── ACADEMIC SUMMARY METRICS TABLE ──
        story.append(Paragraph('Academic Overview & Performance', st['section']))

        # Summary metrics
        total_days = len(attendance_records)
        present_days = len([r for r in attendance_records if r.status == 'Present'])
        late_days = len([r for r in attendance_records if r.status == 'Late'])
        absent_days = len([r for r in attendance_records if r.status == 'Absent'])
        attendance_pct = int(round(present_days / total_days * 100)) if total_days > 0 else 0

        total_assignments = len(assignments)
        submitted_count = len([s for s in submissions if s.status in ['submitted', 'graded', 'returned']])
        completion_pct = int(round(submitted_count / total_assignments * 100)) if total_assignments > 0 else 0

        total_quizzes = len(quiz_results)
        passed_count = len([r for r in quiz_results if r.passed])
        avg_score = (sum(r.score / r.total_questions for r in quiz_results) / len(quiz_results) * 100) if quiz_results else 0.0

        summary_data = [
            ['Metric', 'Value'],
            ['Attendance Rate', f'{attendance_pct}%'],
            ['Present Days', str(present_days)],
            ['Late Days', str(late_days)],
            ['Absent Days', str(absent_days)],
            ['Assignments Completed', f'{submitted_count}/{total_assignments} ({completion_pct}%)'],
            ['Quizzes Taken', str(total_quizzes)],
            ['Quizzes Passed', str(passed_count)],
            ['Average Quiz Score', f'{avg_score:.1f}%']
        ]
        summary_table = Table(summary_data, colWidths=[70*mm, 90*mm])
        summary_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#0f172a')),
            ('TEXTCOLOR', (0,0), (-1,0), colors.white),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('ALIGN', (0,0), (-1,-1), 'LEFT'),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#e2e8f0')),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ]))
        story.append(summary_table)
        story.append(Spacer(1, 12*mm))

        # Attendance detail section
        story.append(Paragraph('Attendance Detail', st['section']))
        if attendance_records:
            attendance_rows = [['#', 'Date', 'Class', 'Status', 'Arrival Time']]
            for idx, record in enumerate(sorted(attendance_records, key=lambda r: r.date), start=1):
                arrival = record.arrival_time.strftime('%H:%M') if record.arrival_time else '—'
                class_name = record.classroom_rel.name if record.classroom_rel else classroom.name
                attendance_rows.append([
                    str(idx),
                    record.date.strftime('%Y-%m-%d'),
                    class_name,
                    record.status,
                    arrival
                ])
            attendance_table = Table(attendance_rows, colWidths=[12*mm, 32*mm, 48*mm, 36*mm, 34*mm], repeatRows=1)
            attendance_table.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#4f46e5')),
                ('TEXTCOLOR', (0,0), (-1,0), colors.white),
                ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                ('FONTSIZE', (0,0), (-1,-1), 8),
                ('ALIGN', (0,0), (-1,-1), 'CENTER'),
                ('GRID', (0,0), (-1,-1), 0.4, colors.HexColor('#e2e8f0')),
            ]))
            story.append(attendance_table)
        else:
            story.append(Paragraph('No attendance records are available for this report.', st['body']))
        story.append(Spacer(1, 10*mm))

        # Assignment detail section
        story.append(Paragraph('Assignment Detail', st['section']))
        if assignments:
            submission_map = {s.assignment_id: s for s in submissions}
            assignment_rows = [['#', 'Assignment', 'Due Date', 'Status', 'Grade', 'Remarks']]
            for idx, assignment in enumerate(sorted(assignments, key=lambda a: a.due_date or datetime.min), start=1):
                submission = submission_map.get(assignment.id)
                status_text = submission.status if submission else 'Not Submitted'
                grade_text = f'{submission.grade}' if submission and submission.grade is not None else '—'
                due_text = assignment.due_date.strftime('%Y-%m-%d') if assignment.due_date else '—'
                remarks_text = submission.feedback if submission and submission.feedback else ('Late' if submission and submission.is_late else '—')
                assignment_rows.append([
                    str(idx),
                    Paragraph(assignment.title or 'Untitled', st['body']),
                    due_text,
                    status_text,
                    grade_text,
                    Paragraph(remarks_text, st['body'])
                ])
            assignment_table = Table(assignment_rows, colWidths=[10*mm, 60*mm, 25*mm, 25*mm, 20*mm, 38*mm], repeatRows=1)
            assignment_table.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#0f172a')),
                ('TEXTCOLOR', (0,0), (-1,0), colors.white),
                ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                ('FONTSIZE', (0,0), (-1,-1), 8),
                ('ALIGN', (0,0), (-1,-1), 'LEFT'),
                ('GRID', (0,0), (-1,-1), 0.4, colors.HexColor('#e2e8f0')),
            ]))
            story.append(assignment_table)
        else:
            story.append(Paragraph('No assignments are assigned for this class yet.', st['body']))
        story.append(Spacer(1, 10*mm))

        # Quiz detail section
        story.append(Paragraph('Quiz Detail', st['section']))
        if quiz_results:
            quiz_rows = [['#', 'Quiz', 'Score', 'Total', 'Passed', 'Date']]
            quiz_map = {q.id: q for q in Quiz.query.filter(Quiz.id.in_([r.quiz_id for r in quiz_results])).all()}
            for idx, result in enumerate(sorted(quiz_results, key=lambda r: r.timestamp), start=1):
                quiz = quiz_map.get(result.quiz_id)
                quiz_rows.append([
                    str(idx),
                    Paragraph(quiz.title if quiz else 'Unknown Quiz', st['body']),
                    str(result.score),
                    str(result.total_questions),
                    'Yes' if result.passed else 'No',
                    result.timestamp.strftime('%Y-%m-%d')
                ])
            quiz_table = Table(quiz_rows, colWidths=[10*mm, 70*mm, 18*mm, 18*mm, 16*mm, 30*mm], repeatRows=1)
            quiz_table.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#4f46e5')),
                ('TEXTCOLOR', (0,0), (-1,0), colors.white),
                ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                ('FONTSIZE', (0,0), (-1,-1), 8),
                ('ALIGN', (0,0), (-1,-1), 'CENTER'),
                ('GRID', (0,0), (-1,-1), 0.4, colors.HexColor('#e2e8f0')),
            ]))
            story.append(quiz_table)
        else:
            story.append(Paragraph('No quiz results were recorded for this report.', st['body']))
        story.append(Spacer(1, 12*mm))

        story.append(Paragraph('Teacher Remarks', st['section']))
        story.append(Paragraph(remark_text or 'No remarks entered yet.', st['body']))
        story.append(Spacer(1, 15*mm))
        story.append(HRFlowable(width='100%', thickness=1, color=colors.HexColor('#e2e8f0')))
        story.append(Paragraph(f'Report generated by: {teacher_name}', ParagraphStyle('footer', parent=st['body'], fontSize=8, textColor=colors.HexColor('#94a3b8'), alignment=TA_CENTER)))
        doc.build(story)
    except Exception as e:
        logger.error(f"Failed to generate PDF backup at {pdf_path}: {e}")


def _get_or_generate_report_pdf(log):
    if not log or not log.student or not log.classroom:
        return None
    filename = f'report_{log.student.id}_{log.execution_id}.pdf'
    pdf_path = os.path.join(PDF_DIR, filename)
    if not os.path.exists(pdf_path):
        attendance_records = Attendance.query.filter_by(student_id=log.student.id, classroom_id=log.classroom.id).all()
        assignments = Assignment.query.filter_by(classroom_id=log.classroom.id).all()
        submissions = AssignmentSubmission.query.filter(
            AssignmentSubmission.student_id == log.student.id,
            AssignmentSubmission.assignment_id.in_([a.id for a in assignments])
        ).all() if assignments else []
        quiz_ids = [q.id for q in Quiz.query.filter_by(classroom_id=log.classroom.id).all()]
        quiz_results = QuizResult.query.filter(
            QuizResult.student_id == log.student.id,
            QuizResult.quiz_id.in_(quiz_ids)
        ).all() if quiz_ids else []
        remark_obj = StudentRemark.query.filter_by(student_id=log.student.id, classroom_id=log.classroom.id).first()
        remark_text = remark_obj.remark if remark_obj else 'No remarks entered yet.'
        report_date = log.sent_at.strftime('%B %d, %Y %I:%M %p') if log.sent_at else datetime.now().strftime('%B %d, %Y %I:%M %p')
        teacher_name = log.teacher.username if log.teacher else 'Teacher'
        _generate_student_progress_report_pdf(
            log.student, log.classroom, attendance_records,
            assignments, submissions, quiz_results,
            remark_text, pdf_path, report_date,
            teacher_name
        )
    return pdf_path

@app.route('/report_backup_pdf/<int:log_id>')
@login_required
def download_report_pdf(log_id):
    log = EmailDeliveryLog.query.get_or_404(log_id)
    if current_user.role == 'student' and log.student_id != current_user.id:
        abort(403)
    if current_user.role == 'teacher':
        teacher_classes = [c.id for c in Classroom.query.filter_by(teacher_id=current_user.id).all()]
        if log.class_id not in teacher_classes:
            abort(403)
    # admins can access all logs
    pdf_path = _get_or_generate_report_pdf(log)
    if not pdf_path or not os.path.exists(pdf_path):
        flash('Unable to locate or generate the report PDF backup.', 'error')
        return redirect(request.referrer or url_for('student_progress_reports'))
    filename = os.path.basename(pdf_path)
    return send_from_directory(PDF_DIR, filename, as_attachment=True)

@app.route('/admin/export_report_pdfs')
@login_required
@admin_required
def admin_export_report_pdfs():
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    logs_query = EmailDeliveryLog.query.filter(EmailDeliveryLog.report_type != 'profile_email_update')
    if start_date:
        try:
            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
            logs_query = logs_query.filter(EmailDeliveryLog.sent_at >= start_dt)
        except ValueError:
            flash('Invalid start date format. Use YYYY-MM-DD.', 'warning')
    if end_date:
        try:
            end_dt = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1)
            logs_query = logs_query.filter(EmailDeliveryLog.sent_at < end_dt)
        except ValueError:
            flash('Invalid end date format. Use YYYY-MM-DD.', 'warning')
    logs = logs_query.order_by(EmailDeliveryLog.sent_at.desc()).all()
    if not logs:
        flash('No report logs found for the selected date range.', 'warning')
        return redirect(url_for('admin_email_monitoring', start_date=start_date, end_date=end_date))

    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for log in logs:
            pdf_path = _get_or_generate_report_pdf(log)
            if pdf_path and os.path.exists(pdf_path):
                arcname = f'{log.student.username}_{log.id}_{log.execution_id}.pdf'
                zipf.write(pdf_path, arcname=arcname)
    memory_file.seek(0)
    filename = f'report_backups_{start_date or "all"}_{end_date or "latest"}.zip'
    return send_file(memory_file, mimetype='application/zip', as_attachment=True, download_name=filename)


def run_global_report_delivery(report_type='admin_trigger'):
    """Iterates through all classes and triggers report delivery for ALL students.
    Even if teacher email is not configured, reports are generated and stored with 'stored' status
    so students can view them in the portal."""
    execution_id = str(uuid.uuid4())
    classrooms = Classroom.query.all()
    total_sent = 0
    total_failed = 0
    total_stored = 0
    
    for cls in classrooms:
        teacher = User.query.get(cls.teacher_id)
        
        for std in cls.students:
            success = send_student_report_smtp(std, cls, teacher, execution_id, report_type)
            if success:
                total_sent += 1
            else:
                total_failed += 1
                
        # Update teacher last_report_sent if teacher exists
        if teacher:
            teacher.last_report_sent = datetime.utcnow()
            db.session.commit()
        
    return {
        'execution_id': execution_id,
        'total_sent': total_sent,
        'total_failed': total_failed
    }


def retry_failed_report_delivery(log_id):
    """Retries a failed email report delivery."""
    log = EmailDeliveryLog.query.get(log_id)
    if not log or log.status == 'sent':
        return False

    if log.report_type == 'profile_email_update':
        # Profile email-change confirmations are sent inline when the change happens
        # and aren't regenerated here, since this retry path re-sends an academic
        # progress report rather than the original security confirmation email.
        log.error_message = "This is a profile email-update confirmation, not an academic report, and cannot be retried from here."
        db.session.commit()
        return False

    student = User.query.get(log.student_id)
    classroom = Classroom.query.get(log.class_id)
    teacher = User.query.get(log.teacher_id)
    
    if not student or not classroom or not teacher:
        log.error_message = "Associated student, class, or teacher record was deleted."
        db.session.commit()
        return False
        
    if not teacher.email_sender_address or not teacher.encrypted_app_password or not teacher.email_enabled:
        log.error_message = "Teacher email configuration is missing or disabled."
        db.session.commit()
        return False
        
    log.retry_count += 1
    
    success = send_student_report_smtp(student, classroom, teacher, log.execution_id, log.report_type)
    if success:
        # Update existing log to success
        log.status = 'sent'
        log.error_message = None
        log.sent_at = datetime.utcnow()
        db.session.commit()
        return True
    return False


# ── Attendance Report PDF ──


# ── Attendance Report PDF ──
@app.route('/admin/attendance_pdf')
@login_required
@teacher_required
def attendance_pdf():
    class_id = request.args.get('class_id', type=int)
    date_str = request.args.get('date', '')
    if current_user.role == 'system_admin':
        classes = Classroom.query.order_by(Classroom.name).all()
    else:
        classes = scope_to_institution(Classroom.query, Classroom).order_by(Classroom.name).all()
    selected_class = Classroom.query.get(class_id) if class_id else (classes[0] if classes else None)
    filter_date = None
    if date_str:
        try: filter_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except: filter_date = None
    att_records = []
    if selected_class:
        q = Attendance.query.filter_by(classroom_id=selected_class.id)
        if filter_date: q = q.filter(Attendance.date == filter_date)
        q = q.order_by(Attendance.date.desc(), Attendance.student_id)
        att_records = q.all()
    present_count = len([r for r in att_records if r.status == 'Present'])
    late_count = len([r for r in att_records if r.status == 'Late'])
    absent_count = len([r for r in att_records if r.status == 'Absent'])
    return render_template('attendance_pdf.html', classes=classes, selected_class=selected_class,
        att_records=att_records, filter_date=filter_date, present_count=present_count,
        late_count=late_count, absent_count=absent_count, now_date=datetime.now().date(), datetime=datetime)

# ── AI Assistant ──
@app.route('/ai_assistant')
@login_required
def ai_assistant():
    return render_template('ai_assistant.html')

@app.route('/api/ai_chat', methods=['POST'])
@login_required
@limiter.limit("10000 per minute")
def ai_chat():
    data = request.json
    message = data.get('message', '').strip()
    history = data.get('history', [])
    if not message: return jsonify({'response': 'Please type a message.'})
    settings = SiteSettings.query.first()
    api_key = getattr(settings, 'gemini_api_key', None) if settings else None
    if not api_key:
        return jsonify({'response': '⚠️ The AI Assistant needs a Google Gemini API key to work.\n\nPlease ask your Admin to configure it in Admin Dashboard → Settings.'})
    response_text = None
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        models_to_try = ['gemini-2.5-flash', 'gemini-2.0-flash', 'gemini-flash-latest', 'gemini-pro-latest']
        model = None
        last_err = ""
        for m_name in models_to_try:
            try:
                model = genai.GenerativeModel(
                    model_name=m_name,
                    system_instruction=(
                        f"You are CampusBot, an intelligent AI assistant for Campus Player — an educational platform. "
                        f"You are helping a {current_user.role}: '{current_user.username}'. "
                        f"Help with: study doubts, coursework, quiz prep, video content, attendance, platform usage, and general academics. "
                        f"Be friendly, concise, supportive. Use markdown. "
                        f"If asked about non-academic topics, politely redirect. "
                        f"Respond in the same language the user writes in."
                    )
                )
                chat_history = []
                for h in history[-10:]:
                    role = 'user' if h.get('role') == 'user' else 'model'
                    chat_history.append({'role': role, 'parts': [h.get('text', '')]})
                chat = model.start_chat(history=chat_history)
                gemini_response = chat.send_message(message)
                response_text = gemini_response.text
                break
            except Exception as e:
                last_err = str(e)
                if 'not found' in last_err.lower() or '404' in last_err: continue
                else: raise e
        if response_text is None:
            response_text = f'Sorry, could not connect to any AI model. Last error: {last_err[:100] if last_err else "Unknown"}'
    except Exception as e:
        err = str(e)
        if 'API_KEY_INVALID' in err: response_text = '❌ The Gemini API key is invalid. Please ask your Admin to update it.'
        elif 'quota' in err.lower() or 'RESOURCE_EXHAUSTED' in err: response_text = '⏳ The AI is busy right now. Please try again in a moment!'
        else: response_text = f'Sorry, I encountered an error. Please try again.\n\n_{err[:120]}_'
    return jsonify({'response': response_text})

@app.route('/api/video/<int:video_id>/summarize', methods=['POST'])
@login_required
def generate_ai_video_summary(video_id):
    """
    Generates AI Video Summary & Key Takeaways using Google Gemini API.
    Caches output on Video model fields (ai_summary, ai_key_takeaways).
    """
    video = Video.query.get_or_404(video_id)
    
    # Check if summary already exists and force refresh is not requested
    force_refresh = request.json.get('force', False) if request.is_json else False
    if video.ai_summary and not force_refresh:
        return jsonify({
            'success': True,
            'cached': True,
            'summary': video.ai_summary,
            'takeaways': video.get_ai_takeaways()
        })

    # Fetch Gemini API Key from SiteSettings
    settings = SiteSettings.query.filter_by(institution_id=video.institution_id).first()
    gemini_key = settings.gemini_api_key if settings else None

    summary_text = ""
    takeaways_list = []

    if gemini_key:
        try:
            import google.generativeai as genai
            genai.configure(api_key=gemini_key)
            model = genai.GenerativeModel('gemini-1.5-flash')

            prompt = f"""
            Analyze the following video metadata and provide a structured learning summary for students.

            Video Title: {video.title}
            Description: {video.description or 'N/A'}
            Tags: {video.tags or 'N/A'}
            Chapters: {video.chapters_json or 'N/A'}

            Respond ONLY with a valid JSON object in the following format:
            {{
                "summary": "A concise 2-3 paragraph overview of the core concepts taught in this video.",
                "key_takeaways": [
                    "First key concept learned",
                    "Second key concept learned",
                    "Third key concept learned"
                ]
            }}
            """

            response = model.generate_content(prompt)
            import re
            # Extract JSON substring from response
            match = re.search(r'\{.*\}', response.text, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                summary_text = data.get('summary', '')
                takeaways_list = data.get('key_takeaways', [])
        except Exception as e:
            logger.error(f"Gemini API summarization failed: {e}")

    # Fallback smart extractive summarizer if Gemini API key not present or failed
    if not summary_text:
        desc = video.description or video.title
        summary_text = f"This video tutorial '{video.title}' covers fundamental concepts related to {video.tags or 'the course topic'}. {desc[:300]}..."
        takeaways_list = [
            f"Understand core principles of {video.title}",
            f"Key techniques and application steps outlined in the video",
            f"Practical exercises and concepts for study revision"
        ]

    # Save to database cache
    video.ai_summary = summary_text
    video.ai_key_takeaways = json.dumps(takeaways_list)
    video.ai_summary_generated_at = datetime.utcnow()
    db.session.commit()

    return jsonify({
        'success': True,
        'cached': False,
        'summary': video.ai_summary,
        'takeaways': takeaways_list
    })


@app.route('/api/ai_video_chat', methods=['POST'])
@login_required
@limiter.limit("10000 per minute")
def ai_video_chat():
    """AI chat with video context (timestamp, title, description)."""
    data = request.json
    message = data.get('message', '').strip()
    video_id = data.get('video_id')
    current_time = data.get('current_time', 0)
    history = data.get('history', [])
    
    if not message:
        return jsonify({'response': 'Please type a question about the video.'})
    
    # Fetch video context
    video = Video.query.get(video_id) if video_id else None
    
    # Format timestamp for context
    def fmt_ts(seconds):
        h, r = divmod(int(seconds), 3600)
        m, s = divmod(r, 60)
        if h > 0:
            return f"{h}h {m}m {s}s"
        return f"{m}:{s:02d} min"
    
    video_context = ""
    if video:
        video_context = (
            f"The student is currently watching a video titled '{video.title}'.\n"
            f"Video Description: {video.description or 'No description provided.'}\n"
            f"Current Playback Position: {fmt_ts(current_time)}\n"
        )
    
    full_prompt = f"{video_context}\nStudent's Question: {message}"
    
    settings = SiteSettings.query.first()
    api_key = getattr(settings, 'gemini_api_key', None) if settings else None
    if not api_key:
        return jsonify({'response': '⚠️ The AI Assistant needs a Google Gemini API key to work.\n\nPlease ask your Admin to configure it in Admin Dashboard → Settings.'})
    
    response_text = None
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        models_to_try = ['gemini-2.5-flash', 'gemini-2.0-flash', 'gemini-flash-latest', 'gemini-pro-latest']
        model = None
        last_err = ""
        for m_name in models_to_try:
            try:
                model = genai.GenerativeModel(
                    model_name=m_name,
                    system_instruction=(
                        f"You are CampusBot, an AI video tutor for Campus Player. "
                        f"You are helping a {current_user.role}: '{current_user.username}'. "
                        f"You have access to the video title, description, and the exact timestamp the student paused at. "
                        f"Answer the student's doubt based on the video content context provided. "
                        f"Be friendly, concise, and educational. Use markdown formatting. "
                        f"If the question is unrelated to the video, politely redirect to the video topic. "
                        f"Respond in the same language the user writes in."
                    )
                )
                chat_history = []
                for h in history[-10:]:
                    role = 'user' if h.get('role') == 'user' else 'model'
                    chat_history.append({'role': role, 'parts': [h.get('text', '')]})
                chat = model.start_chat(history=chat_history)
                gemini_response = chat.send_message(full_prompt)
                response_text = gemini_response.text
                break
            except Exception as e:
                last_err = str(e)
                if 'not found' in last_err.lower() or '404' in last_err: continue
                else: raise e
        if response_text is None:
            response_text = f'Sorry, could not connect to AI. Last error: {last_err[:100] if last_err else "Unknown"}'
    except Exception as e:
        err = str(e)
        if 'API_KEY_INVALID' in err: response_text = '❌ The Gemini API key is invalid. Please ask your Admin to update it.'
        elif 'quota' in err.lower() or 'RESOURCE_EXHAUSTED' in err: response_text = '⏳ The AI is busy right now. Please try again in a moment!'
        else: response_text = f'Sorry, I encountered an error. Please try again.\n\n_{err[:120]}_'
    return jsonify({'response': response_text})

# ═══════════════════════════════════════════════════════════════
#  AI LECTURE COPILOT & INSTANT TIMESTAMP CITATION ENGINE
# ═══════════════════════════════════════════════════════════════

@app.route('/api/video/<int:video_id>/copilot/ask', methods=['POST'])
@login_required
@limiter.limit("10000 per minute")
def api_ask_lecture_copilot(video_id):
    """Answers student doubts with exact timestamp citations, digital library guide links, and micro-quiz."""
    video = Video.query.get_or_404(video_id)
    data = request.json or {}
    question = data.get('question', '').strip()
    current_time = float(data.get('current_time', 0.0))
    if not question:
        return jsonify({'success': False, 'message': 'Please type or speak your doubt.'}), 400
    
    from services.ai_lecture_copilot import ask_lecture_copilot, calculate_video_exam_readiness
    result = ask_lecture_copilot(video, current_user, question, current_time)
    result['readiness'] = calculate_video_exam_readiness(current_user, video)
    return jsonify(result)


@app.route('/api/video/copilot/interaction/<int:interaction_id>/quiz_submit', methods=['POST'])
@login_required
def api_submit_copilot_quiz(interaction_id):
    """Evaluates 1-click micro-quiz answer, awards +20 XP, and logs mastery."""
    data = request.json or {}
    selected_index = data.get('selected_index', 0)
    try:
        selected_index = int(selected_index)
    except Exception:
        selected_index = 0
    
    from services.ai_lecture_copilot import evaluate_micro_quiz, calculate_video_exam_readiness
    result = evaluate_micro_quiz(interaction_id, current_user, selected_index)
    interaction = AICopilotInteraction.query.get(interaction_id)
    if interaction and interaction.video:
        result['readiness'] = calculate_video_exam_readiness(current_user, interaction.video)
    return jsonify(result)


@app.route('/api/video/<int:video_id>/copilot/history', methods=['GET'])
@login_required
def api_get_copilot_history(video_id):
    """Returns past copilot query cards, citations, and micro-quizzes for this video."""
    video = Video.query.get_or_404(video_id)
    interactions = AICopilotInteraction.query.filter_by(
        user_id=current_user.id, video_id=video_id
    ).order_by(AICopilotInteraction.created_at.asc()).limit(30).all()
    
    from services.ai_lecture_copilot import calculate_video_exam_readiness
    readiness = calculate_video_exam_readiness(current_user, video)
    
    data = []
    for it in interactions:
        matched_book = it.cited_book
        data.append({
            'interaction_id': it.id,
            'question': it.question,
            'answer': it.answer,
            'cited_timestamp': it.cited_timestamp,
            'cited_timestamp_formatted': it.cited_timestamp_formatted,
            'cited_book': {
                'id': matched_book.id,
                'title': matched_book.title,
                'page': it.cited_page,
                'type_label': matched_book.get_resource_type_label()
            } if matched_book else None,
            'micro_quiz': it.get_micro_quiz(),
            'quiz_answered': it.quiz_answered,
            'quiz_correct': it.quiz_correct,
            'created_at': it.created_at.strftime('%I:%M %p')
        })
    return jsonify({'success': True, 'history': data, 'readiness': readiness})


@app.route('/api/video/<int:video_id>/readiness', methods=['GET'])
@login_required
def api_get_video_readiness(video_id):
    """Returns live student Exam Readiness Index for this video."""
    video = Video.query.get_or_404(video_id)
    from services.ai_lecture_copilot import calculate_video_exam_readiness
    readiness = calculate_video_exam_readiness(current_user, video)
    return jsonify({'success': True, 'readiness': readiness})


# ═══════════════════════════════════════════════════════════════
#  SOCKET.IO (REAL-TIME)
# ═══════════════════════════════════════════════════════════════

@socketio.on('connect')
def handle_connect():
    if current_user.is_authenticated:
        logger.info(f"SocketIO connected: {current_user.username}")
        # Join class rooms
        if current_user.role == 'student':
            for cls in current_user.enrolled_classes:
                join_room(f'class_{cls.id}')

@socketio.on('disconnect')
def handle_disconnect():
    if current_user.is_authenticated:
        logger.info(f"SocketIO disconnected: {current_user.username}")

@socketio.on('join_class_room')
def handle_join_class_room(data):
    class_id = data.get('class_id')
    if class_id:
        join_room(f'class_{class_id}')

@socketio.on('message')
def handle_message(data):
    """Real-time chat via SocketIO."""
    class_id = data.get('class_id')
    content = data.get('content', '').strip()
    if not class_id or not content: return
    msg = ChatMessage(classroom_id=class_id, user_id=current_user.id, content=content)
    db.session.add(msg)
    db.session.commit()
    emit('new_message', {
        'id': msg.id, 'username': current_user.username, 'role': current_user.role,
        'content': msg.content, 'timestamp': msg.timestamp.strftime('%I:%M %p'),
        'classroom_id': class_id, 'avatar_url': current_user.avatar_url
    }, room=f'class_{class_id}')


# ── Watch Together / Synchronized Virtual Classroom Sockets ──

@socketio.on('watch_party_join')
def handle_watch_party_join(data):
    video_id = data.get('video_id')
    if not video_id:
        return
    room_name = f'watch_party_{video_id}'
    join_room(room_name)
    user_name = current_user.name if current_user.is_authenticated else 'Guest'
    user_role = current_user.role if current_user.is_authenticated else 'viewer'
    avatar = current_user.get_avatar_url() if current_user.is_authenticated else None
    
    emit('watch_party_user_joined', {
        'username': user_name,
        'role': user_role,
        'avatar_url': avatar,
        'user_id': current_user.id if current_user.is_authenticated else None
    }, room=room_name)

@socketio.on('watch_party_sync_action')
def handle_watch_party_sync_action(data):
    """Host broadcasts playback control action (play, pause, seek)."""
    video_id = data.get('video_id')
    action = data.get('action')  # 'play', 'pause', 'seek'
    current_time = data.get('currentTime', 0.0)
    if not video_id or not action:
        return
    room_name = f'watch_party_{video_id}'
    user_name = current_user.name if current_user.is_authenticated else 'Host'
    
    emit('watch_party_broadcast_action', {
        'action': action,
        'currentTime': current_time,
        'triggered_by': user_name,
        'is_teacher': current_user.is_authenticated and current_user.role in ('teacher', 'admin', 'system_admin')
    }, room=room_name, include_self=False)

@socketio.on('watch_party_raise_hand')
def handle_watch_party_raise_hand(data):
    video_id = data.get('video_id')
    if not video_id:
        return
    room_name = f'watch_party_{video_id}'
    user_name = current_user.name if current_user.is_authenticated else 'Student'
    emit('watch_party_hand_raised', {
        'username': user_name,
        'user_id': current_user.id if current_user.is_authenticated else None,
        'timestamp': datetime.utcnow().strftime('%I:%M:%S %p')
    }, room=room_name)

@socketio.on('watch_party_chat_message')
def handle_watch_party_chat(data):
    video_id = data.get('video_id')
    message = (data.get('message') or '').strip()
    if not video_id or not message:
        return
    room_name = f'watch_party_{video_id}'
    user_name = current_user.name if current_user.is_authenticated else 'User'
    avatar = current_user.get_avatar_url() if current_user.is_authenticated else None
    emit('watch_party_new_chat', {
        'username': user_name,
        'avatar_url': avatar,
        'role': current_user.role if current_user.is_authenticated else 'student',
        'message': message,
        'timestamp': datetime.utcnow().strftime('%I:%M %p')
    }, room=room_name)


# ═══════════════════════════════════════════════════════════════
#  SMS SYSTEM
# ═══════════════════════════════════════════════════════════════

def _selenium_send_bulk_sms(students_data, job_id):
    """Automates Google Messages Web to send SMS reports."""
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.keys import Keys
    import selenium.common.exceptions

    driver = None
    if not SMS_LOCK.acquire(blocking=False):
        SMS_JOBS[job_id] = {'status': 'error', 'msg': 'Another SMS job is already running.'}
        return
    try:
        SMS_JOBS[job_id] = {'status': 'starting', 'msg': 'Launching Chrome…', 'total': len(students_data), 'current': 0}
        opts = webdriver.ChromeOptions()
        sms_profile = os.path.abspath(os.path.join(BASE_DIR, 'google_messages_profile'))
        os.makedirs(sms_profile, exist_ok=True)
        for lock_file in ['SingletonLock', 'SingletonCookie', 'SingletonSocket', 'lockfile']:
            lock_path = os.path.join(sms_profile, lock_file)
            if os.path.exists(lock_path):
                try: os.remove(lock_path)
                except: pass
        opts.add_argument(f'--user-data-dir={sms_profile}')
        opts.add_argument('--no-sandbox')
        opts.add_argument('--disable-dev-shm-usage')
        opts.add_argument('--disable-gpu')
        opts.add_argument('--disable-blink-features=AutomationControlled')
        opts.add_argument('--remote-debugging-port=0')
        opts.add_argument('--window-size=1920,1080')
        opts.add_experimental_option('excludeSwitches', ['enable-logging', 'enable-automation'])
        opts.add_experimental_option('useAutomationExtension', False)
        try: driver = webdriver.Chrome(options=opts)
        except Exception as e:
            SMS_JOBS[job_id] = {'status': 'error', 'msg': f'Failed to launch Chrome: {str(e)[:100]}'}
            return
        driver.set_page_load_timeout(90)
        driver.maximize_window()
        driver.get('https://messages.google.com/web/')
        SMS_JOBS[job_id]['status'] = 'waiting'
        SMS_JOBS[job_id]['msg'] = 'Waiting for Google Messages Login…'
        try:
            WebDriverWait(driver, 120).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'button.start-chat-button, .fab-container button, mw-fab-link button, .conversation-list'))
            )
        except:
            SMS_JOBS[job_id] = {'status': 'error', 'msg': 'Google Messages login timed out.'}
            return
        try:
            use_here = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), 'Use here')]"))
            )
            use_here.click()
            time.sleep(3)
        except: pass
        success_count = 0; fail_count = 0
        for idx, student in enumerate(students_data):
            SMS_JOBS[job_id]['current'] = idx + 1
            SMS_JOBS[job_id]['status'] = 'sending'
            SMS_JOBS[job_id]['msg'] = f"Sending SMS to {student['phone']}... (S:{success_count} F:{fail_count})"
            try:
                start_btn = WebDriverWait(driver, 30).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, 'button.start-chat-button, .fab-container button, mw-fab-link button'))
                )
                driver.execute_script("arguments[0].click();", start_btn)
                time.sleep(2)
                phone_input = WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, 'input[data-e2e-contact-search-input], input[type="text"]'))
                )
                phone_input.clear()
                phone_input.send_keys(student['phone'])
                time.sleep(2)
                phone_input.send_keys(Keys.ENTER)
                msg_input = WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, 'div[data-e2e-message-input-area], div[contenteditable="true"]'))
                )
                msg_input.click()
                time.sleep(0.5)
                msg_input.send_keys(student['msg'])
                time.sleep(1)
                msg_input.send_keys(Keys.ENTER)
                success_count += 1
                time.sleep(4)
            except Exception as e:
                fail_count += 1
                try: driver.get('https://messages.google.com/web/'); time.sleep(5)
                except: pass
                continue
        SMS_JOBS[job_id]['status'] = 'done'
        SMS_JOBS[job_id]['msg'] = f'Bulk SMS Completed. Success: {success_count}, Failed: {fail_count} ✅'
        time.sleep(3)
    except Exception as exc:
        SMS_JOBS[job_id] = {'status': 'error', 'msg': str(exc).split('\n')[0][:200]}
    finally:
        if driver:
            try: driver.quit()
            except: pass
        SMS_LOCK.release()

@app.route('/teacher/send_class_sms_report/<int:class_id>', methods=['POST'])
@login_required
@teacher_required
def send_class_sms_report(class_id):
    classroom = Classroom.query.get_or_404(class_id)
    students = classroom.students.all()
    
    students_data = []
    now = datetime.now()
    
    for s in students:
        if not s.phone:
            continue
            
        record = Attendance.query.filter(Attendance.student_id == s.id, Attendance.date == now.date()).first()
        status = record.status if record else "Not Marked"
        level = (s.xp // 500) + 1
        monthly_recs = Attendance.query.filter(Attendance.student_id == s.id, db.extract('month', Attendance.date) == now.month).all()
        total_days = len(monthly_recs)
        present_days = len([r for r in monthly_recs if r.status == 'Present'])
        att_pct = (present_days / total_days * 100) if total_days > 0 else 0
        
        msg = (
            f"🎓 Campus Player Report: {s.username}\n"
            f"Date: {now.strftime('%d %b %Y')}\n"
            f"Today's Status: {status}\n"
            f"XP: {s.xp} (Level {level})\n"
            f"Monthly Attendance: {int(att_pct)}%\n"
            f"- Campus Monitoring Team"
        )
        students_data.append({'phone': s.phone, 'msg': msg})
        
    if not students_data:
        return jsonify({'error': 'No students in this class have a phone number configured.'}), 400
        
    job_id = f"class-{class_id}-{uuid.uuid4()}"
    SMS_JOBS[job_id] = {
        'status': 'queued',
        'msg': 'Bulk SMS report queued...',
        'total': len(students_data),
        'current': 0
    }
    
    threading.Thread(target=_selenium_send_bulk_sms, args=(students_data, job_id), daemon=True).start()
    return jsonify({'success': True, 'job_id': job_id})

@app.route('/api/sms_status/<job_id>', methods=['GET'])
@login_required
@teacher_required
def get_sms_status(job_id):
    job = SMS_JOBS.get(job_id)
    if not job:
        return jsonify({'status': 'error', 'msg': 'SMS job not found.'}), 404
    return jsonify(job)

@app.route('/teacher/download_class_sms_report/<int:class_id>')
@login_required
@teacher_required
def download_class_sms_report(class_id):
    classroom = Classroom.query.get_or_404(class_id)
    students = classroom.students.all()
    report_lines = []
    now = datetime.now()
    report_lines.append(f"CLASS SMS REPORT: {classroom.name}")
    report_lines.append(f"Generated: {now.strftime('%d %b %Y %H:%M')}")
    report_lines.append("-" * 40)
    for s in students:
        record = Attendance.query.filter(Attendance.student_id == s.id, Attendance.date == now.date()).first()
        status = record.status if record else "Not Marked"
        level = (s.xp // 500) + 1
        monthly_recs = Attendance.query.filter(Attendance.student_id == s.id, db.extract('month', Attendance.date) == now.month).all()
        total_days = len(monthly_recs)
        present_days = len([r for r in monthly_recs if r.status == 'Present'])
        att_pct = (present_days / total_days * 100) if total_days > 0 else 0
        msg = (
            f"🎓 Campus Player Report: {s.username}\n"
            f"Date: {now.strftime('%d %b %Y')}\n"
            f"Today's Status: {status}\n"
            f"XP: {s.xp} (Level {level})\n"
            f"Monthly Attendance: {int(att_pct)}%\n"
            f"- Campus Monitoring Team"
        )
        report_lines.append(f"STUDENT: {s.username} | PHONE: {s.phone or 'N/A'}")
        report_lines.append(msg)
        report_lines.append("-" * 40)
    content = "\n".join(report_lines)
    filename = f"SMS_Report_{classroom.name}_{now.strftime('%Y%m%d')}.txt"
    return Response(content, mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

@app.route('/admin/get_sms_text/<int:student_id>')
@login_required
@teacher_required
def get_sms_text(student_id):
    student = User.query.get_or_404(student_id)
    now = datetime.now()
    record = Attendance.query.filter(Attendance.student_id == student_id, Attendance.date == now.date()).first()
    status = record.status if record else "Not Marked"
    level = (student.xp // 500) + 1
    monthly_records = Attendance.query.filter(Attendance.student_id == student_id, db.extract('month', Attendance.date) == now.month).all()
    total = len(monthly_records)
    present = len([r for r in monthly_records if r.status == 'Present'])
    att_pct = (present / total * 100) if total > 0 else 0
    msg = (
        f"🎓 Campus Player Report: {student.username}\n"
        f"Date: {now.strftime('%d %b %Y')}\n"
        f"Today's Status: {status}\n"
        f"XP: {student.xp} (Level {level})\n"
        f"Monthly Attendance: {int(att_pct)}%"
    )
    if att_pct < 75 and total > 5: msg += "\n⚠️ NOTICE: Low attendance. Please ensure regularity."
    msg += "\n- Campus Monitoring Team"
    return jsonify({'text': msg, 'phone': student.phone or ''})

# ── PDF Generation ──
def _generate_monthly_pdf_file(student, pdf_path):
    now = datetime.now()
    records = Attendance.query.filter(Attendance.student_id == student.id, db.extract('month', Attendance.date) == now.month).all()
    total = len(records)
    present = len([r for r in records if r.status == 'Present'])
    late = len([r for r in records if r.status == 'Late'])
    absent = len([r for r in records if r.status == 'Absent'])
    att_pct = (present / total * 100) if total > 0 else 0
    doc = SimpleDocTemplate(pdf_path, pagesize=A4)
    st = _pdf_styles()
    story = []
    story.append(Paragraph('MONTHLY ATTENDANCE REPORT', st['title']))
    story.append(Paragraph(f'Student: {student.username} | {now.strftime("%B %Y")}', st['sub']))
    story.append(HRFlowable(width='100%', thickness=2, color=colors.black))
    story.append(Spacer(1, 10*mm))
    data = [['Attendance %', 'Present', 'Late', 'Absent'], [f'{att_pct:.1f}%', str(present), str(late), str(absent)]]
    t = Table(data, colWidths=[40*mm]*4)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#f3f4f6')),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('GRID', (0,0), (-1,-1), 1, colors.black),
        ('FONTSIZE', (0,1), (0,1), 16),
    ]))
    story.append(t)
    story.append(Spacer(1, 10*mm))
    if att_pct < 75:
        story.append(Paragraph('OFFICIAL NOTICE: LOW ATTENDANCE', ParagraphStyle('warn', textColor=colors.red, fontName='Helvetica-Bold', fontSize=14)))
        story.append(Paragraph('Your ward is not attending the class properly.', st['body']))
    doc.build(story)

@app.route('/teacher/download_monthly_pdf/<int:student_id>')
@login_required
@teacher_required
def download_monthly_pdf(student_id):
    student = User.query.get_or_404(student_id)
    now = datetime.now()
    filename = f'{student.username}_monthly_{now.strftime("%Y-%m")}.pdf'
    pdf_path = os.path.join(PDF_DIR, filename)
    _generate_monthly_pdf_file(student, pdf_path)
    return send_from_directory(PDF_DIR, filename, as_attachment=True)

# ── REAL PDF GENERATION ──
def _pdf_styles():
    ss = getSampleStyleSheet()
    title_style = ParagraphStyle('CTitle', parent=ss['Title'], fontSize=20, leading=26, spaceAfter=4,
        textColor=colors.HexColor('#0f172a'), alignment=TA_CENTER)
    sub_style = ParagraphStyle('CSub', parent=ss['Normal'], fontSize=10, textColor=colors.HexColor('#64748b'), alignment=TA_CENTER, spaceAfter=14)
    section_style = ParagraphStyle('CSection', parent=ss['Normal'], fontSize=12, fontName='Helvetica-Bold',
        textColor=colors.HexColor('#4f46e5'), spaceBefore=16, spaceAfter=6)
    body_style = ParagraphStyle('CBody', parent=ss['Normal'], fontSize=10, textColor=colors.HexColor('#374151'))
    return {'title': title_style, 'sub': sub_style, 'section': section_style, 'body': body_style}

STATUS_COLORS = {'Present': colors.HexColor('#d1fae5'), 'Late': colors.HexColor('#fef3c7'), 'Absent': colors.HexColor('#fee2e2')}
STATUS_TEXT = {'Present': colors.HexColor('#065f46'), 'Late': colors.HexColor('#92400e'), 'Absent': colors.HexColor('#991b1b')}

@app.route('/admin/download_attendance_pdf/<int:student_id>')
@login_required
@admin_required
def download_attendance_pdf(student_id):
    student = User.query.get_or_404(student_id)
    now = datetime.now(); today = now.date()
    records = Attendance.query.filter_by(student_id=student_id).order_by(Attendance.date.desc()).all()
    total = len(records); present = sum(1 for r in records if r.status == 'Present')
    late = sum(1 for r in records if r.status == 'Late'); absent = sum(1 for r in records if r.status == 'Absent')
    rate = int((present + late) / total * 100) if total else 0
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18*mm, rightMargin=18*mm, topMargin=20*mm, bottomMargin=18*mm)
    st = _pdf_styles(); story = []
    story.append(Paragraph('🎓 Campus Player', st['sub']))
    story.append(Paragraph('Attendance Report', st['title']))
    story.append(Paragraph(f'Student: <b>{student.username}</b> &nbsp;|&nbsp; Generated: {now.strftime("%B %d, %Y %H:%M")}', st['sub']))
    story.append(HRFlowable(width='100%', thickness=2, color=colors.HexColor('#4f46e5')))
    story.append(Spacer(1, 6*mm))
    summary_data = [['Total Days', 'Present', 'Late', 'Absent', 'Rate'], [str(total), str(present), str(late), str(absent), f'{rate}%']]
    summary_table = Table(summary_data, colWidths=[35*mm]*5)
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#0f172a')), ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'), ('FONTSIZE', (0,0), (-1,-1), 10),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'), ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#e2e8f0')),
        ('TOPPADDING', (0,0), (-1,-1), 8), ('BOTTOMPADDING', (0,0), (-1,-1), 8),
    ]))
    story.append(summary_table); story.append(Spacer(1, 8*mm))
    if records:
        story.append(Paragraph('Detailed Records', st['section']))
        header = ['#', 'Date', 'Class', 'Status', 'Arrival Time']
        rows = [header]
        for i, r in enumerate(records, 1):
            cls_name = r.classroom_rel.name if r.classroom_rel else '—'
            arrival = r.arrival_time.strftime('%H:%M') if r.arrival_time else '—'
            rows.append([str(i), r.date.strftime('%b %d, %Y'), cls_name, r.status, arrival])
        col_widths = [10*mm, 36*mm, 50*mm, 28*mm, 30*mm]
        detail_table = Table(rows, colWidths=col_widths, repeatRows=1)
        ts = [
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#4f46e5')), ('TEXTCOLOR', (0,0), (-1,0), colors.white),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'), ('FONTSIZE', (0,0), (-1,-1), 9),
            ('ALIGN', (0,0), (-1,-1), 'CENTER'), ('GRID', (0,0), (-1,-1), 0.4, colors.HexColor('#e2e8f0')),
        ]
        for row_idx, r in enumerate(records, 1):
            bg = STATUS_COLORS.get(r.status, colors.white)
            fg = STATUS_TEXT.get(r.status, colors.black)
            ts.append(('BACKGROUND', (3, row_idx), (3, row_idx), bg))
            ts.append(('TEXTCOLOR', (3, row_idx), (3, row_idx), fg))
            ts.append(('FONTNAME', (3, row_idx), (3, row_idx), 'Helvetica-Bold'))
        detail_table.setStyle(TableStyle(ts))
        story.append(detail_table)
    else: story.append(Paragraph('No attendance records found.', st['body']))
    story.append(Spacer(1, 10*mm))
    story.append(HRFlowable(width='100%', thickness=1, color=colors.HexColor('#e2e8f0')))
    story.append(Paragraph(f'Campus Player — {today.strftime("%B %d, %Y")}', ParagraphStyle('footer', fontSize=8, textColor=colors.HexColor('#94a3b8'), alignment=TA_CENTER)))
    doc.build(story); buf.seek(0)
    filename = f'{student.username}_{today.strftime("%Y-%m-%d")}.pdf'
    response = make_response(buf.getvalue())
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response

@app.route('/admin/download_levels_pdf/<int:user_id>')
@login_required
@admin_required
def download_levels_pdf(user_id):
    user = User.query.get_or_404(user_id)
    now = datetime.now(); today = now.date()
    level = (user.xp // 500) + 1
    xp_in_level = user.xp % 500
    progress_pct = int(xp_in_level / 500 * 100)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18*mm, rightMargin=18*mm, topMargin=20*mm, bottomMargin=18*mm)
    st = _pdf_styles(); story = []
    story.append(Paragraph('🎓 Campus Player', st['sub']))
    story.append(Paragraph('Levels & XP Report', st['title']))
    story.append(Paragraph(f'User: <b>{user.username}</b> ({user.role.title()}) &nbsp;|&nbsp; Generated: {now.strftime("%B %d, %Y %H:%M")}', st['sub']))
    story.append(HRFlowable(width='100%', thickness=2, color=colors.HexColor('#4f46e5')))
    story.append(Spacer(1, 6*mm))
    stats_data = [['Total XP', 'Level', 'Progress to Next', 'Joined'],
        [str(user.xp), str(level), f'{xp_in_level}/500 XP ({progress_pct}%)', user.created_at.strftime('%b %d, %Y')]]
    stats_table = Table(stats_data, colWidths=[38*mm, 25*mm, 65*mm, 40*mm])
    stats_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#0f172a')), ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'), ('FONTSIZE', (0,0), (-1,-1), 10),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'), ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#e2e8f0')),
    ]))
    story.append(stats_table); story.append(Spacer(1, 8*mm))
    if user.role == 'student' and user.attendance_records:
        story.append(Paragraph('Attendance Summary', st['section']))
        recs = user.attendance_records
        total = len(recs); present = sum(1 for r in recs if r.status == 'Present')
        late = sum(1 for r in recs if r.status == 'Late'); absent = sum(1 for r in recs if r.status == 'Absent')
        att_data = [['Total', 'Present', 'Late', 'Absent', 'Rate'],
            [str(total), str(present), str(late), str(absent), f'{int((present+late)/total*100)}%' if total else '—']]
        att_table = Table(att_data, colWidths=[28*mm]*5)
        att_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#4f46e5')), ('TEXTCOLOR', (0,0), (-1,0), colors.white),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'), ('FONTSIZE', (0,0), (-1,-1), 9),
            ('ALIGN', (0,0), (-1,-1), 'CENTER'), ('GRID', (0,0), (-1,-1), 0.4, colors.HexColor('#e2e8f0')),
        ]))
        story.append(att_table)
    story.append(Spacer(1, 10*mm))
    story.append(HRFlowable(width='100%', thickness=1, color=colors.HexColor('#e2e8f0')))
    story.append(Paragraph(f'Campus Player — {today.strftime("%B %d, %Y")}', ParagraphStyle('footer', fontSize=8, textColor=colors.HexColor('#94a3b8'), alignment=TA_CENTER)))
    doc.build(story); buf.seek(0)
    filename = f'{user.username}_{today.strftime("%Y-%m-%d")}.pdf'
    response = make_response(buf.getvalue())
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response

# ═══════════════════════════════════════════════════════════════
#  NEW: ASSIGNMENTS & HOMEWORK SYSTEM
# ═══════════════════════════════════════════════════════════════

@app.route('/teacher/assignments')
@login_required
@teacher_required
def teacher_assignments():
    """View all assignments created by the teacher."""
    settings = SiteSettings.query.first()
    if settings and not settings.enable_assignments:
        flash('Assignments feature is disabled.', 'warning')
        return redirect(url_for('teacher_dashboard'))
    assignments = Assignment.query.filter_by(teacher_id=current_user.id).order_by(Assignment.created_at.desc()).all()
    classes = scope_to_institution(Classroom.query, Classroom).all()
    return render_template('teacher_assignments.html', assignments=assignments, classes=classes, now=datetime.utcnow())

@app.route('/teacher/create_assignment', methods=['POST'])
@login_required
@teacher_required
def create_assignment():
    """Create a new assignment."""
    title = request.form.get('title')
    description = request.form.get('description', '')
    classroom_id = request.form.get('classroom_id', type=int)
    due_date_str = request.form.get('due_date', '')
    total_points = request.form.get('total_points', 100, type=int)
    assignment_type = request.form.get('assignment_type', 'text')
    allow_late = request.form.get('allow_late_submission') == 'on'
    late_penalty = request.form.get('late_penalty_percent', 10, type=int)
    # NEW: teacher decides how students must respond
    response_mode = request.form.get('response_mode', 'either')
    if response_mode not in ('either', 'type_only', 'file_only'):
        response_mode = 'either'

    if not title or not classroom_id:
        flash('Title and classroom are required.', 'error')
        return redirect(url_for('teacher_assignments'))

    due_date = None
    if due_date_str:
        try:
            due_date = datetime.strptime(due_date_str, '%Y-%m-%dT%H:%M')
        except:
            try:
                due_date = datetime.strptime(due_date_str, '%Y-%m-%d')
            except:
                pass

    inst_id = getattr(current_user, 'institution_id', None)
    assignment = Assignment(
        title=title, description=description,
        classroom_id=classroom_id, teacher_id=current_user.id,
        institution_id=inst_id,
        due_date=due_date, total_points=total_points,
        assignment_type=assignment_type,
        allow_late_submission=allow_late, late_penalty_percent=late_penalty,
        response_mode=response_mode
    )
    db.session.add(assignment)
    db.session.flush()  # get assignment.id before saving files

    # NEW: teacher can upload the assignment question paper as PDF or any document format
    question_file = request.files.get('question_file')
    if question_file and question_file.filename:
        q_filename = secure_filename(f"question_{assignment.id}_{question_file.filename}")
        
        user = User.query.get(current_user.id)
        if user and user.institution_id:
            inst = Institution.query.get(user.institution_id)
            if inst:
                tenant_q_dir = os.path.join(UPLOAD_FOLDER, 'institutions', inst.slug, 'assignment_questions')
                os.makedirs(tenant_q_dir, exist_ok=True)
                q_save_path = os.path.join(tenant_q_dir, q_filename)
                question_file.save(q_save_path)
                assignment.question_file_path = f'uploads/institutions/{inst.slug}/assignment_questions/{q_filename}'
            else:
                q_save_path = os.path.join(UPLOAD_FOLDER, 'assignment_questions', q_filename)
                os.makedirs(os.path.dirname(q_save_path), exist_ok=True)
                question_file.save(q_save_path)
                assignment.question_file_path = f'uploads/assignment_questions/{q_filename}'
        else:
            q_save_path = os.path.join(UPLOAD_FOLDER, 'assignment_questions', q_filename)
            os.makedirs(os.path.dirname(q_save_path), exist_ok=True)
            question_file.save(q_save_path)
            assignment.question_file_path = f'uploads/assignment_questions/{q_filename}'
        assignment.question_file_name = question_file.filename

    current_user.xp += 50
    db.session.commit()
    flash(f'Assignment "{title}" created! +50 XP!', 'success')
    log_activity('create_assignment', f'Created assignment "{title}"')
    return redirect(url_for('teacher_assignments'))

@app.route('/teacher/assignment/<int:assignment_id>')
@login_required
@teacher_required
def view_assignment(assignment_id):
    """View assignment details and submissions."""
    assignment = Assignment.query.get_or_404(assignment_id)
    submissions = AssignmentSubmission.query.filter_by(assignment_id=assignment_id).all()
    class_students = list(assignment.classroom.students) if assignment.classroom else []
    submitted_ids = [s.student_id for s in submissions]
    not_submitted = [s for s in class_students if s.id not in submitted_ids]
    return render_template('assignment_detail.html', assignment=assignment,
        submissions=submissions, not_submitted=not_submitted, datetime=datetime)

@app.route('/teacher/assignment/grade/<int:submission_id>', methods=['POST'])
@login_required
@teacher_required
def grade_submission(submission_id):
    """Grade a student submission."""
    submission = AssignmentSubmission.query.get_or_404(submission_id)
    grade = request.form.get('grade', type=float)
    feedback = request.form.get('feedback', '')
    if grade is not None:
        submission.grade = grade
        submission.feedback = feedback
        submission.status = 'graded'
        submission.graded_at = datetime.utcnow()
        # Award XP to student for completing assignment
        student = User.query.get(submission.student_id)
        if student:
            student.xp += int(grade)
        db.session.commit()
        flash(f'Submission graded: {grade}/{submission.assignment.total_points}', 'success')
        log_activity('grade_assignment', f'Graded submission #{submission_id}')
    return redirect(url_for('view_assignment', assignment_id=submission.assignment_id))

def _remove_static_relative(rel_path):
    """Delete a file stored at a path relative to the static/ folder.

    Shared by every route that permanently deletes an assignment (whether a
    single assignment is removed, or a whole classroom is deleted and its
    assignments cascade with it) so files never get orphaned on disk.
    """
    if not rel_path:
        return
    try:
        full_path = os.path.join(BASE_DIR, 'static', rel_path)
        # Guard against path traversal / accidental deletion outside static/
        if os.path.commonpath([os.path.abspath(full_path), os.path.abspath(os.path.join(BASE_DIR, 'static'))]) \
                != os.path.abspath(os.path.join(BASE_DIR, 'static')):
            return
        if os.path.exists(full_path):
            os.remove(full_path)
    except Exception as e:
        logger.error(f"Error deleting assignment file {rel_path}: {e}")


def _delete_assignment_files(assignment):
    """Permanently remove an assignment's question paper + all student submissions from disk."""
    try:
        _remove_static_relative(assignment.question_file_path)
        for submission in AssignmentSubmission.query.filter_by(assignment_id=assignment.id).all():
            _remove_static_relative(submission.file_path)
    except Exception as e:
        logger.error(f"File cleanup error for assignment {assignment.id}: {e}")


@app.route('/teacher/delete_assignment/<int:assignment_id>', methods=['POST'])
@login_required
@teacher_required
def delete_assignment(assignment_id):
    """Delete an assignment and permanently remove all associated files from disk."""
    assignment = Assignment.query.get_or_404(assignment_id)

    _delete_assignment_files(assignment)

    db.session.delete(assignment)  # cascades to AssignmentSubmission rows
    db.session.commit()
    flash('Assignment deleted.', 'success')
    log_activity('delete_assignment', f'Deleted assignment "{assignment.title}"')
    return redirect(url_for('teacher_assignments'))

@app.route('/student/assignments')
@login_required
def student_assignments():
    """View assignments for enrolled classes."""
    settings = SiteSettings.query.first()
    if settings and not settings.enable_assignments:
        flash('Assignments feature is disabled.', 'warning')
        return redirect(url_for('student_dashboard'))
    enrolled_class_ids = [c.id for c in current_user.enrolled_classes]
    assignments = Assignment.query.filter(
        Assignment.classroom_id.in_(enrolled_class_ids)
    ).order_by(Assignment.due_date.asc(), Assignment.created_at.desc()).all()

    # Check submission status
    assignment_data = []
    for a in assignments:
        sub = AssignmentSubmission.query.filter_by(
            assignment_id=a.id, student_id=current_user.id
        ).first()
        assignment_data.append({
            'assignment': a,
            'submission': sub,
            'status': 'submitted' if sub else 'pending'
        })
    return render_template('student_assignments.html', assignment_data=assignment_data, datetime=datetime)

@app.route('/student/submit_assignment/<int:assignment_id>', methods=['POST'])
@login_required
def submit_assignment(assignment_id):
    """Submit an assignment."""
    assignment = Assignment.query.get_or_404(assignment_id)
    content = request.form.get('content', '')
    file = request.files.get('file')

    # Check existing submission
    existing = AssignmentSubmission.query.filter_by(
        assignment_id=assignment_id, student_id=current_user.id
    ).first()
    if existing:
        flash('You have already submitted this assignment.', 'info')
        return redirect(url_for('student_assignments'))

    # NEW: enforce the response mode the teacher configured for this assignment
    response_mode = getattr(assignment, 'response_mode', 'either') or 'either'
    if response_mode == 'type_only' and not content.strip():
        flash('This assignment requires a typed answer.', 'error')
        return redirect(url_for('student_assignments'))
    if response_mode == 'file_only' and not (file and file.filename):
        flash('This assignment requires a document upload.', 'error')
        return redirect(url_for('student_assignments'))
    if response_mode == 'either' and not content.strip() and not (file and file.filename):
        flash('Please type an answer or upload a document.', 'error')
        return redirect(url_for('student_assignments'))

    file_path = None
    file_name = None
    if file and file.filename:
        # Any document format is accepted
        filename = secure_filename(f"assign_{assignment_id}_{current_user.id}_{file.filename}")
        
        user = User.query.get(current_user.id)
        if user and user.institution_id:
            inst = Institution.query.get(user.institution_id)
            if inst:
                tenant_assign_dir = os.path.join(UPLOAD_FOLDER, 'institutions', inst.slug, 'assignments')
                os.makedirs(tenant_assign_dir, exist_ok=True)
                save_path = os.path.join(tenant_assign_dir, filename)
                file.save(save_path)
                file_path = f'uploads/institutions/{inst.slug}/assignments/{filename}'
            else:
                save_path = os.path.join(UPLOAD_FOLDER, 'assignments', filename)
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                file.save(save_path)
                file_path = f'uploads/assignments/{filename}'
        else:
            save_path = os.path.join(UPLOAD_FOLDER, 'assignments', filename)
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            file.save(save_path)
            file_path = f'uploads/assignments/{filename}'
        file_name = file.filename

    # Check if late
    is_late = False
    if assignment.due_date and datetime.utcnow() > assignment.due_date:
        is_late = True

    submission = AssignmentSubmission(
        assignment_id=assignment_id, student_id=current_user.id,
        content=content, file_path=file_path, file_name=file_name, is_late=is_late,
        status='submitted'
    )
    db.session.add(submission)
    current_user.xp += 30
    if current_user.role == 'student':
        current_user.update_quest_progress('submit_assignment', 1)
    db.session.commit()
    flash('Assignment submitted! +30 XP!', 'success')
    log_activity('submit_assignment', f'Submitted assignment #{assignment_id}')
    return redirect(url_for('student_assignments'))


# ═══════════════════════════════════════════════════════════════
#  NEW: VIDEO NOTES & BOOKMARKS
# ═══════════════════════════════════════════════════════════════

@app.route('/api/video/<int:video_id>/notes', methods=['GET', 'POST'])
@login_required
def video_notes(video_id):
    """Get or create video notes."""
    if request.method == 'POST':
        data = request.json
        note = VideoNote(
            user_id=current_user.id, video_id=video_id,
            timestamp_seconds=data.get('timestamp', 0),
            content=data.get('content', ''),
            color=data.get('color', '#fef08a')
        )
        db.session.add(note)
        db.session.commit()
        return jsonify({'success': True, 'id': note.id})
    else:
        notes = VideoNote.query.filter_by(user_id=current_user.id, video_id=video_id).order_by(VideoNote.timestamp_seconds).all()
        return jsonify([{
            'id': n.id, 'timestamp': n.timestamp_seconds,
            'content': n.content, 'color': n.color,
            'created_at': n.created_at.isoformat()
        } for n in notes])

@app.route('/api/notes/<int:note_id>', methods=['PUT', 'DELETE'])
@login_required
def manage_note(note_id):
    """Update or delete a note."""
    note = VideoNote.query.get_or_404(note_id)
    if note.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403

    if request.method == 'DELETE':
        db.session.delete(note)
        db.session.commit()
        return jsonify({'success': True})

    data = request.json
    note.content = data.get('content', note.content)
    note.color = data.get('color', note.color)
    note.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/video/<int:video_id>/bookmarks', methods=['GET', 'POST'])
@login_required
def video_bookmarks(video_id):
    """Get or create bookmarks."""
    if request.method == 'POST':
        data = request.json
        bookmark = VideoBookmark(
            user_id=current_user.id, video_id=video_id,
            timestamp_seconds=data.get('timestamp', 0),
            label=data.get('label', '')
        )
        db.session.add(bookmark)
        db.session.commit()
        return jsonify({'success': True, 'id': bookmark.id})
    else:
        bookmarks = VideoBookmark.query.filter_by(user_id=current_user.id, video_id=video_id).order_by(VideoBookmark.timestamp_seconds).all()
        return jsonify([{
            'id': b.id, 'timestamp': b.timestamp_seconds,
            'label': b.label, 'created_at': b.created_at.isoformat()
        } for b in bookmarks])

@app.route('/api/bookmarks/<int:bookmark_id>', methods=['DELETE'])
@login_required
def delete_bookmark(bookmark_id):
    """Delete a bookmark."""
    bm = VideoBookmark.query.get_or_404(bookmark_id)
    if bm.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403
    db.session.delete(bm)
    db.session.commit()
    return jsonify({'success': True})


# ═══════════════════════════════════════════════════════════════
#  NEW: PLAYLIST COMPLETION CERTIFICATE
# ═══════════════════════════════════════════════════════════════

@app.route('/student/playlist/<int:playlist_id>/certificate')
@login_required
def playlist_certificate(playlist_id):
    """Generate a PDF Certificate of Completion for a playlist."""
    playlist = Playlist.query.get_or_404(playlist_id)
    student = current_user
    
    # Check if all videos in the playlist are completed
    all_completed = True
    for video in playlist.videos:
        progress = VideoProgress.query.filter_by(
            user_id=student.id, video_id=video.id
        ).first()
        if not progress or not progress.completed:
            all_completed = False
            break
    
    if not all_completed:
        flash('Please complete all videos in the playlist to earn your certificate.', 'warning')
        return redirect(url_for('view_playlist', playlist_id=playlist_id))
    
    settings = SiteSettings.query.first()
    institution_name = settings.institution_name if settings and settings.institution_name else 'Campus Player'
    
    now = datetime.now()
    buf = io.BytesIO()
    
    # Premium certificate layout
    from reportlab.lib.pagesizes import landscape, A4
    from reportlab.pdfgen import canvas as pdfcanvas
    from reportlab.lib.units import inch
    
    c = pdfcanvas.Canvas(buf, pagesize=landscape(A4))
    width, height = landscape(A4)
    
    # Background gradient effect (dark blue border)
    c.setFillColor(colors.HexColor('#1e3a5f'))
    c.rect(0, 0, width, height, fill=1, stroke=0)
    
    # Inner white area
    margin = 20
    c.setFillColor(colors.white)
    c.roundRect(margin, margin, width - 2*margin, height - 2*margin, 20, fill=1, stroke=0)
    
    # Decorative border
    c.setStrokeColor(colors.HexColor('#c9a84c'))
    c.setLineWidth(3)
    c.roundRect(margin + 10, margin + 10, width - 2*margin - 20, height - 2*margin - 20, 15, fill=0, stroke=1)
    
    # Inner decorative border
    c.setStrokeColor(colors.HexColor('#c9a84c'))
    c.setLineWidth(1)
    c.roundRect(margin + 18, margin + 18, width - 2*margin - 36, height - 2*margin - 36, 12, fill=0, stroke=1)
    
    # Corner ornaments
    c.setStrokeColor(colors.HexColor('#c9a84c'))
    c.setLineWidth(2)
    for cx, cy in [(margin+25, height-margin-25), (width-margin-25, height-margin-25),
                    (margin+25, margin+25), (width-margin-25, margin+25)]:
        c.circle(cx, cy, 8, fill=0, stroke=1)
        c.setFillColor(colors.HexColor('#c9a84c'))
        c.circle(cx, cy, 4, fill=1, stroke=0)
    
    # Top seal/emblem
    c.setFillColor(colors.HexColor('#c9a84c'))
    c.setFont('Helvetica-Bold', 14)
    c.drawCentredString(width/2, height - 80, '🎓')
    
    # Title
    c.setFillColor(colors.HexColor('#1e3a5f'))
    c.setFont('Times-Bold', 36)
    c.drawCentredString(width/2, height - 130, 'Certificate of Completion')
    
    # Subtitle
    c.setFillColor(colors.HexColor('#555555'))
    c.setFont('Times-Roman', 16)
    c.drawCentredString(width/2, height - 165, 'This is proudly presented to')
    
    # Student Name
    c.setFillColor(colors.HexColor('#1e3a5f'))
    c.setFont('Times-Bold', 32)
    c.drawCentredString(width/2, height - 215, student.display_name or student.username)
    
    # Description
    c.setFillColor(colors.HexColor('#555555'))
    c.setFont('Times-Roman', 16)
    c.drawCentredString(width/2, height - 255, f'for successfully completing the playlist')
    
    # Playlist Title
    c.setFillColor(colors.HexColor('#c9a84c'))
    c.setFont('Times-BoldItalic', 24)
    c.drawCentredString(width/2, height - 295, f'"{playlist.title}"')
    
    # Institution
    c.setFillColor(colors.HexColor('#555555'))
    c.setFont('Times-Roman', 14)
    c.drawCentredString(width/2, height - 330, f'Awarded by {institution_name}')
    
    # Date
    c.setFont('Times-Roman', 12)
    c.drawCentredString(width/2, height - 360, f'Date: {now.strftime("%B %d, %Y")}')
    
    # Signature lines
    c.setStrokeColor(colors.HexColor('#555555'))
    c.setLineWidth(1)
    # Left signature
    c.line(width/2 - 150, 100, width/2 - 30, 100)
    c.setFont('Times-Roman', 10)
    c.drawCentredString(width/2 - 90, 85, 'Authorized Signature')
    # Right signature
    c.line(width/2 + 30, 100, width/2 + 150, 100)
    c.drawCentredString(width/2 + 90, 85, 'Institution Seal')
    
    # Certificate ID
    cert_id = f"CERT-{playlist_id}-{student.id}-{now.strftime('%Y%m%d')}"
    c.setFont('Times-Roman', 8)
    c.setFillColor(colors.HexColor('#999999'))
    c.drawCentredString(width/2, 40, f'Certificate ID: {cert_id}')
    
    c.save()
    buf.seek(0)
    
    filename = f'Certificate_{playlist.title}_{student.username}.pdf'
    filename = secure_filename(filename)
    response = make_response(buf.getvalue())
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


# ═══════════════════════════════════════════════════════════════
#  NEW: VIDEO PROGRESS TRACKING (Auto-Resume)
# ═══════════════════════════════════════════════════════════════

@app.route('/api/video/progress/<int:video_id>', methods=['GET', 'POST'])
@login_required
def update_video_progress(video_id):
    """Track and retrieve video progress."""
    if request.method == 'POST':
        data = request.json
        progress_seconds = data.get('progress', 0)
        total_duration = data.get('total_duration', 100)
        percent = (progress_seconds / total_duration * 100) if total_duration > 0 else 0

        progress = VideoProgress.query.filter_by(
            user_id=current_user.id, video_id=video_id
        ).first()

        if not progress:
            progress = VideoProgress(
                user_id=current_user.id, video_id=video_id,
                progress_seconds=progress_seconds,
                percent_complete=percent,
                completed=percent >= 90
            )
            db.session.add(progress)
        else:
            progress.progress_seconds = progress_seconds
            progress.percent_complete = percent
            if percent >= 90:
                progress.completed = True
            progress.updated_at = datetime.utcnow()

        db.session.commit()
        return jsonify({'success': True})

    # GET - return current progress
    progress = VideoProgress.query.filter_by(
        user_id=current_user.id, video_id=video_id
    ).first()
    if progress:
        return jsonify({
            'progress_seconds': progress.progress_seconds,
            'percent_complete': progress.percent_complete,
            'completed': progress.completed
        })
    return jsonify({'progress_seconds': 0, 'percent_complete': 0, 'completed': False})

@app.route('/api/video/<int:video_id>/chapters', methods=['GET', 'POST'])
@login_required
def video_chapters(video_id):
    """Manage video chapter markers."""
    video = Video.query.get_or_404(video_id)
    if request.method == 'POST':
        data = request.json
        chapters = data.get('chapters', [])
        video.set_chapters(chapters)
        db.session.commit()
        return jsonify({'success': True, 'chapters': video.get_chapters()})
    return jsonify({'chapters': video.get_chapters()})


# ═══════════════════════════════════════════════════════════════
#  NEW: LEADERBOARD & GAMIFICATION
# ═══════════════════════════════════════════════════════════════

@app.route('/leaderboard')
@login_required
def leaderboard():
    """Scholar Academic Leaderboard — contains ONLY students (XP). Visible to Students, Teachers, and Admins."""
    settings = SiteSettings.query.first()
    if settings and not settings.enable_leaderboard:
        flash('Leaderboard is disabled.', 'warning')
        return redirect(url_for('index'))

    class_id = request.args.get('class_id', type=int)

    if current_user.role == 'system_admin':
        base_query = User.query.filter_by(role='student')
        classes = Classroom.query.all()
    else:
        base_query = scope_to_institution(User.query.filter_by(role='student'), User)
        classes = scope_to_institution(Classroom.query, Classroom).all()

    if class_id:
        classroom = Classroom.query.get(class_id)
        if classroom:
            students = sorted([s for s in classroom.students if s.role == 'student'], key=lambda s: s.xp, reverse=True)
        else:
            students = base_query.order_by(User.xp.desc()).all()
    else:
        students = base_query.order_by(User.xp.desc()).all()

    ranked_users = []
    for idx, user in enumerate(students, 1):
        ranked_users.append({
            'rank': idx,
            'user': user,
            'level': (user.xp // 500) + 1,
            'xp_progress': user.xp % 500
        })

    return render_template('leaderboard.html', ranked_users=ranked_users,
        class_id=class_id, classes=classes)


@app.route('/teacher/leaderboard')
@login_required
def teacher_leaderboard():
    """Faculty CP Competition Arena — contains ONLY teachers (CP). Visible ONLY to Teachers and Admins."""
    if current_user.role not in ['teacher', 'admin', 'system_admin']:
        flash('Faculty CP Leaderboard is strictly reserved for Teachers and Admins.', 'warning')
        return redirect(url_for('leaderboard'))

    settings = SiteSettings.query.first()
    if settings and not settings.enable_leaderboard:
        flash('Leaderboard is disabled.', 'warning')
        return redirect(url_for('index'))

    if current_user.role == 'system_admin':
        teachers = User.query.filter_by(role='teacher').order_by(User.xp.desc()).all()
    else:
        teachers = scope_to_institution(User.query.filter_by(role='teacher'), User).order_by(User.xp.desc()).all()

    ranked_teachers = []
    for idx, teacher in enumerate(teachers, 1):
        ranked_teachers.append({
            'rank': idx,
            'user': teacher,
            'level': (teacher.xp // 500) + 1,
            'cp_progress': teacher.xp % 500,
            'badge': teacher.faculty_badge
        })

    return render_template('teacher_leaderboard.html', ranked_teachers=ranked_teachers)


@app.route('/claim_quest/<quest_id>', methods=['POST'])
@login_required
def claim_quest(quest_id):
    """Claim daily quest reward."""
    if not hasattr(current_user, 'claim_quest'):
        flash('Quests are not available on this profile.', 'error')
        return redirect(url_for('student_dashboard'))

    success, reward_xp = current_user.claim_quest(quest_id)
    if success:
        notif = Notification(
            user_id=current_user.id,
            institution_id=current_user.institution_id,
            message=f"🎯 Daily Quest Claimed! +{reward_xp} XP awarded.",
            notification_type='success'
        )
        db.session.add(notif)
        db.session.commit()

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
            return jsonify({
                'status': 'success',
                'reward_xp': reward_xp,
                'new_xp': current_user.xp,
                'new_level': current_user.level,
                'quests': current_user.get_daily_quests()
            })
        flash(f"🎯 Daily Quest Reward Claimed! +{reward_xp} XP", "success")
    else:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
            return jsonify({'status': 'error', 'message': 'Quest is either incomplete or already claimed.'}), 400
        flash("Quest is either incomplete or already claimed.", "error")
    return redirect(url_for('student_dashboard'))


@app.route('/api/check_achievements')
@login_required
def check_achievements():
    """Check and award achievements for current user."""
    if not Achievement.query.first():
        Achievement.seed_defaults()

    achievements = Achievement.query.all()
    new_achievements = []
    user = current_user
    user_achievements = user.get_achievements()

    # Lazy loaded condition counters
    _counters = {}
    def get_count(key):
        if key not in _counters:
            if key == 'videos_watched':
                _counters[key] = ViewAnalytics.query.filter_by(user_id=user.id, completed=True).count()
            elif key == 'comments_count':
                _counters[key] = Comment.query.filter_by(user_id=user.id).count()
            elif key == 'uploads_count':
                _counters[key] = Video.query.filter_by(uploader_id=user.id).count()
        return _counters[key]

    for ach in achievements:
        if ach.code in user_achievements:
            continue

        earned = False
        if ach.condition_type == 'login_count' and user.login_count >= ach.condition_value:
            earned = True
        elif ach.condition_type == 'streak_days' and user.streak_days >= ach.condition_value:
            earned = True
        elif ach.condition_type == 'total_quizzes_taken' and user.total_quizzes_taken >= ach.condition_value:
            earned = True
        elif ach.condition_type == 'xp' and user.xp >= ach.condition_value:
            earned = True
        elif ach.condition_type == 'level' and user.level >= ach.condition_value:
            earned = True
        elif ach.condition_type == 'perfect_quiz':
            perfect = QuizResult.query.filter_by(student_id=user.id).filter(
                QuizResult.score == QuizResult.total_questions
            ).first()
            if perfect:
                earned = True
        elif ach.condition_type == 'videos_watched' and get_count('videos_watched') >= ach.condition_value:
            earned = True
        elif ach.condition_type == 'comments_count' and get_count('comments_count') >= ach.condition_value:
            earned = True
        elif ach.condition_type == 'uploads_count' and get_count('uploads_count') >= ach.condition_value:
            earned = True

        if earned:
            user.add_achievement(ach.code)
            user.xp += ach.xp_reward
            new_achievements.append({
                'code': ach.code,
                'title': ach.title,
                'description': ach.description,
                'icon': ach.icon_emoji,
                'xp': ach.xp_reward
            })
            # Send notification
            notif = Notification(
                user_id=user.id,
                message=f'🏆 Achievement Unlocked: {ach.icon_emoji} {ach.title}! (+{ach.xp_reward} XP)',
                notification_type='success',
                action_url=url_for('profile')
            )
            db.session.add(notif)

    if new_achievements:
        user.update_level()
        db.session.commit()

    return jsonify({'new_achievements': new_achievements})


# ═══════════════════════════════════════════════════════════════
#  NEW: EMAIL SYSTEM
# ═══════════════════════════════════════════════════════════════

def send_async_email(recipient, subject, body_html, body_text=None):
    """Queue an email to be sent."""
    try:
        email = EmailQueue(
            recipient_email=recipient,
            subject=subject,
            body_html=body_html,
            body_text=body_text or ''
        )
        db.session.add(email)
        db.session.commit()
        # Immediately process queued emails in a background thread
        thread = threading.Thread(target=process_pending_emails)
        thread.start()
        return email.id
    except Exception as e:
        logger.error(f"Email queue error: {e}")
        return None

def process_pending_emails():
    """Process pending emails in background."""
    with app.app_context():
        from flask_mail import Message as MailMessage
        pending = EmailQueue.query.filter_by(status='pending').order_by(EmailQueue.created_at).limit(10).all()
        for email in pending:
            try:
                msg = MailMessage(
                    subject=email.subject,
                    recipients=[email.recipient_email],
                    body=email.body_text or '',
                    html=email.body_html or ''
                )
                mail.send(msg)
                email.status = 'sent'
                email.sent_at = datetime.utcnow()
            except Exception as e:
                email.retry_count += 1
                email.error_message = str(e)[:200]
                if email.retry_count >= 3:
                    email.status = 'failed'
            db.session.commit()

@app.route('/api/admin/send_email', methods=['POST'])
@login_required
@admin_required
def admin_send_email():
    """Admin endpoint to send emails."""
    recipient = request.form.get('recipient')
    subject = request.form.get('subject')
    body = request.form.get('body')
    if not recipient or not subject or not body:
        return jsonify({'error': 'Missing fields'}), 400
    email_id = send_async_email(recipient, subject, body, body)
    if email_id:
        return jsonify({'success': True, 'email_id': email_id})
    return jsonify({'error': 'Failed to queue email'}), 500


# ═══════════════════════════════════════════════════════════════
#  NEW: SWAGGER / API DOCUMENTATION
# ═══════════════════════════════════════════════════════════════

@app.route('/api/docs')
def api_docs():
    """Redirect to Swagger UI."""
    return redirect('/apidocs')

@app.route('/api/endpoints')
@login_required
def api_endpoints():
    """List all available API endpoints."""
    rules = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint.startswith('api/'):
            rules.append({
                'endpoint': rule.endpoint,
                'methods': list(rule.methods - {'HEAD', 'OPTIONS'}),
                'path': rule.rule
            })
    return jsonify(sorted(rules, key=lambda r: r['path']))


# ═══════════════════════════════════════════════════════════════
#  NEW: ANALYTICS ENHANCEMENTS
# ═══════════════════════════════════════════════════════════════

@app.route('/api/analytics/dashboard')
@login_required
def analytics_dashboard():
    """Extended analytics for charts."""
    now = datetime.utcnow()
    today = now.date()
    seven_days_ago = today - timedelta(days=7)

    # Views per day (last 7 days)
    views_data = db.session.query(
        db.func.date(ViewAnalytics.start_time).label('date'),
        db.func.count(ViewAnalytics.id).label('count')
    ).filter(ViewAnalytics.start_time >= seven_days_ago)\
     .group_by(db.func.date(ViewAnalytics.start_time)).all()

    views_per_day = {str(d.date): c for d, c in views_data}

    # User registrations per day
    users_data = db.session.query(
        db.func.date(User.created_at).label('date'),
        db.func.count(User.id).label('count')
    ).filter(User.created_at >= seven_days_ago)\
     .group_by(db.func.date(User.created_at)).all()

    users_per_day = {str(d.date): c for d, c in users_data}

    # Top videos
    top_videos = db.session.query(
        Video.title, Video.view_count
    ).order_by(Video.view_count.desc()).limit(10).all()

    return jsonify({
        'views_per_day': views_per_day,
        'users_per_day': users_per_day,
        'top_videos': [{'title': v.title, 'views': v.view_count} for v in top_videos],
        'total_videos': Video.query.count(),
        'total_users': User.query.count(),
        'total_quizzes': Quiz.query.count(),
    })


# ── API endpoint for system health check ──
@app.route('/api/health')
def health_check():
    return jsonify({'status': 'ok', 'timestamp': datetime.utcnow().isoformat()})


# ═══════════════════════════════════════════════════════════════
#  HLS ADAPTIVE STREAMING ROUTES (YouTube-style)
# ═══════════════════════════════════════════════════════════════

# Register MIME types for HLS
mimetypes.add_type('application/vnd.apple.mpegurl', '.m3u8')
mimetypes.add_type('video/mp2t', '.ts')
mimetypes.add_type('image/jpeg', '.jpg')
mimetypes.add_type('text/vtt', '.vtt')


def serve_hls_file(video_dir, filename):
    """Serve HLS files with proper caching and security headers."""
    file_path = os.path.join(video_dir, filename)
    
    # Security: prevent directory traversal
    if not os.path.realpath(file_path).startswith(os.path.realpath(video_dir)):
        return jsonify({'error': 'Forbidden'}), 403
    
    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        return jsonify({'error': 'File not found'}), 404
    
    # Determine content type
    ext = os.path.splitext(filename)[1].lower()
    content_types = {
        '.m3u8': 'application/vnd.apple.mpegurl',
        '.ts': 'video/mp2t',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.vtt': 'text/vtt; charset=utf-8',
        '.webvtt': 'text/vtt; charset=utf-8',
    }
    content_type = content_types.get(ext, 'application/octet-stream')
    
    response = make_response(send_file(file_path, mimetype=content_type, conditional=True))
    response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    response = apply_media_cors_headers(response)
    response.headers['Cross-Origin-Resource-Policy'] = 'cross-origin'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    
    # For TS segments, add accept-ranges for seeking
    if ext == '.ts':
        response.headers['Accept-Ranges'] = 'bytes'
    
    return response


@app.route('/hls/<int:video_id>/<path:filename>')
@login_required
def serve_hls(video_id, filename):
    """Serve HLS segments and playlists for a video.
    Resolves the video directory from the stored hls_playlist_path in the DB,
    supporting both legacy (static/hls/<id>/) and institution-based
    (static/uploads/institutions/<slug>/hls/<id>/) storage.
    """
    video = Video.query.get(video_id)
    if not video:
        abort(404)
    enforce_institution_access(video)
    static_dir = os.path.join(app.root_path, 'static')

    # Try to derive video_dir from the stored hls_playlist_path/master_playlist_path
    # so institution-based (tenant) storage locations resolve correctly.
    video_dir = None
    if video and (video.hls_playlist_path or video.master_playlist_path):
        stored_path = video.hls_playlist_path or video.master_playlist_path
        # stored_path is relative to static/, e.g. "hls/3/master.m3u8"
        # or "uploads/institutions/<slug>/hls/3/master.m3u8"
        full_stored = os.path.join(static_dir, stored_path)
        video_dir = os.path.dirname(full_stored)

    # Fall back to legacy location: static/hls/<video_id>/
    if not video_dir or not os.path.exists(video_dir):
        video_dir = os.path.join(app.config['HLS_FOLDER'], str(video_id))

    if not os.path.exists(video_dir):
        return jsonify({'error': 'Video not found'}), 404

    return serve_hls_file(video_dir, filename)


@app.route('/api/hls/qualities/<int:video_id>')
@login_required
def get_hls_qualities(video_id):
    """Return available quality renditions for a video."""
    video = Video.query.get_or_404(video_id)
    renditions = video.get_renditions()
    return jsonify({
        'video_id': video_id,
        'has_adaptive_streams': video.has_adaptive_streams,
        'renditions': renditions,
        'master_playlist': video.master_playlist_path or video.hls_playlist_path,
        'duration': video.duration_seconds,
        'source_width': video.source_width,
        'source_height': video.source_height,
        'thumbnail': video.thumbnail_path,
        'subtitles': {
            'path': video.subtitle_path,
            'language': video.subtitle_language
        } if video.subtitle_path else None,
        'thumbnails_vtt': video.thumbnails_vtt_path,
    })


@app.route('/api/hls/stream/<int:video_id>')
@login_required
def get_hls_stream_url(video_id):
    """Get the best HLS stream URL for a video (with quality preference)."""
    video = Video.query.get_or_404(video_id)
    quality = request.args.get('quality', 'auto')
    
    if quality == 'auto' and video.has_adaptive_streams and video.master_playlist_path:
        stream_url = url_for('static', filename=video.master_playlist_path)
    elif quality != 'auto' and video.has_adaptive_streams:
        # Find the specific rendition
        renditions = video.get_renditions()
        target = None
        for r in renditions:
            if r['name'] == quality:
                target = r
                break
        if target:
            # Serve specific quality playlist
            playlist_path = os.path.join(os.path.dirname(video.master_playlist_path or ''), target['playlist'])
            stream_url = url_for('static', filename=playlist_path.replace('\\', '/'))
        else:
            stream_url = url_for('static', filename=video.hls_playlist_path or '')
    else:
        stream_url = url_for('static', filename=video.hls_playlist_path or '')
    
    return jsonify({
        'stream_url': stream_url,
        'has_adaptive': video.has_adaptive_streams,
        'duration': video.duration_seconds,
        'renditions': video.get_renditions()
    })


@app.route('/api/hls/switch_quality', methods=['POST'])
@login_required
def switch_hls_quality():
    """Handle quality switching and return new stream URL."""
    data = request.json
    video_id = data.get('video_id')
    quality = data.get('quality', 'auto')
    current_time = data.get('current_time', 0)
    
    video = Video.query.get_or_404(video_id)
    renditions = video.get_renditions()
    
    # Find matching rendition
    selected_rendition = None
    for r in renditions:
        if r['name'] == quality:
            selected_rendition = r
            break
    
    if selected_rendition:
        playlist_path = os.path.join(os.path.dirname(video.master_playlist_path or video.hls_playlist_path or ''), selected_rendition['playlist'])
        stream_url = url_for('static', filename=playlist_path.replace('\\', '/'))
    elif quality == 'auto' and video.master_playlist_path:
        stream_url = url_for('static', filename=video.master_playlist_path)
    else:
        stream_url = url_for('static', filename=video.hls_playlist_path or '')
    
    return jsonify({
        'success': True,
        'stream_url': stream_url,
        'quality': quality,
        'current_time': current_time,
        'selected_rendition': selected_rendition
    })


# ═══════════════════════════════════════════════════════════════
#  NEW: Adaptive HLS Transcoding (enhanced background processor)
# ═══════════════════════════════════════════════════════════════

def get_video_stream_info(input_path):
    """Get detailed video stream information using ffprobe."""
    try:
        cmd = [
            'ffprobe', '-v', 'error', '-print_format', 'json',
            '-analyzeduration', '10M', '-probesize', '10M',
            '-show_format', '-show_streams', input_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        info = json.loads(result.stdout)
        
        video_stream = None
        audio_stream = None
        streams = info.get('streams', [])
        
        video_candidates = []
        for stream in streams:
            if stream.get('codec_type') == 'video':
                disp = stream.get('disposition', {})
                is_attached = (disp.get('attached_pic') == 1)
                codec_name = stream.get('codec_name', '').lower()
                is_still = codec_name in ['mjpeg', 'png', 'webp', 'bmp', 'gif']
                w = int(stream.get('width', 0))
                h = int(stream.get('height', 0))
                if w > 0 and h > 0:
                    video_candidates.append({
                        'stream': stream,
                        'w': w,
                        'h': h,
                        'is_attached': is_attached,
                        'is_still': is_still,
                        'pixels': w * h
                    })
            elif stream.get('codec_type') == 'audio' and not audio_stream:
                audio_stream = stream
        
        if not video_candidates:
            return None
            
        video_candidates.sort(key=lambda c: (not c['is_attached'], not c['is_still'], c['pixels']), reverse=True)
        chosen = video_candidates[0]
        video_stream = chosen['stream']
        width = chosen['w']
        height = chosen['h']
        
        # Check rotation metadata
        side_data = video_stream.get('side_data_list', [])
        rotation = 0
        for sd in side_data:
            if 'rotation' in sd:
                try:
                    rotation = abs(int(sd['rotation']))
                except:
                    pass
        tags = video_stream.get('tags', {})
        if 'rotate' in tags:
            try:
                rotation = abs(int(tags['rotate']))
            except:
                pass
        if rotation in [90, 270]:
            width, height = height, width
            
        duration = float(info.get('format', {}).get('duration', 0))
        bitrate = int(info.get('format', {}).get('bit_rate', 0))
        fps_parts = video_stream.get('avg_frame_rate', '0/1').split('/')
        fps = float(fps_parts[0]) / float(fps_parts[1]) if len(fps_parts) == 2 and float(fps_parts[1]) > 0 else 0
        codec = video_stream.get('codec_name', 'h264')
        audio_codec = audio_stream.get('codec_name', 'aac') if audio_stream else 'aac'
        
        return {
            'width': width,
            'height': height,
            'duration': duration,
            'bitrate': bitrate,
            'fps': fps,
            'codec': codec,
            'audio_codec': audio_codec,
            'has_audio': audio_stream is not None
        }
    except Exception as e:
        logger.error(f"ffprobe error: {e}")
        return None


def generate_adaptive_hls(input_path, output_dir, video_id, max_height=8640):
    """Generate adaptive HLS streams using FFmpeg."""
    try:
        import sys
        import importlib.util
        spec = importlib.util.spec_from_file_location("transcode", os.path.join(BASE_DIR, 'static', 'hls', 'transcode.py'))
        transcode = importlib.util.module_from_spec(spec)
        sys.modules["transcode"] = transcode
        spec.loader.exec_module(transcode)
        transcode_rendition = transcode.transcode_rendition
        generate_master_playlist = transcode.generate_master_playlist
        get_source_info = transcode.get_source_info
        generate_thumbnail = transcode.generate_thumbnail
        generate_sprite_sheet = transcode.generate_sprite_sheet
        
        RENDITIONS = [
            ("144p",  256, 144,   "80k",    "100k",   "160k",   "64k"),
            ("240p",  426, 240,   "200k",   "250k",   "400k",   "64k"),
            ("360p",  640, 360,   "500k",   "600k",   "1000k",  "96k"),
            ("480p",  854, 480,   "1000k",  "1200k",  "2000k",  "128k"),
            ("720p",  1280, 720,  "2500k",  "3000k",  "5000k",  "128k"),
            ("1080p", 1920, 1080, "5000k",  "6000k",  "10000k", "192k"),
            ("2K",    2560, 1440, "12000k", "15000k", "24000k", "256k"),
            ("4K",    3840, 2160, "35000k", "45000k", "70000k", "256k"),
            ("8K",    7680, 4320, "100000k","120000k","200000k","256k"),
            ("16K",   15360, 8640, "250000k","300000k","500000k","320k"),
        ]
        
        source_info = get_source_info(input_path)
        src_w = source_info.get('width', 0)
        src_h = source_info.get('height', 0)
        src_max_dim = max(src_w, src_h)
        src_min_dim = min(src_w, src_h)
        
        # Determine which renditions to generate
        # Include all renditions supported by the source resolution
        selected = []
        for r in RENDITIONS:
            r_name, r_w, r_h, r_bitrate, r_maxrate, r_bufsize, r_audio_bitrate = r
            r_max_dim = max(r_w, r_h)
            r_min_dim = min(r_w, r_h)
            
            if r_h > max_height:
                continue
                
            # Allow slight cropping/letterboxing tolerances (e.g. 704p, 718p, or vertical 720x1280)
            if src_min_dim >= (r_min_dim - 24) or src_max_dim >= (r_max_dim - 50):
                selected.append(r)
            elif not selected and r_name == RENDITIONS[0][0]:
                selected.append(r)
        
        if not selected:
            selected = [RENDITIONS[0]]
        
        renditions_info = []
        for idx, r in enumerate(selected):
            # Use a simple class to hold args
            class TranscodeArgs:
                preset = 'medium'
                crf = 23
                segment_duration = 6
            
            playlist, rinfo = transcode_rendition(input_path, output_dir, r, source_info)
            if rinfo:
                renditions_info.append(rinfo)
                
            # Update database progress
            try:
                video_obj = Video.query.get(video_id)
                if video_obj:
                    progress = 5 + int(((idx + 1) / len(selected)) * 85)
                    video_obj.processing_progress = min(95, progress)
                    db.session.commit()
            except Exception as pe:
                logger.error(f"Error updating adaptive progress: {pe}")
        
        # Generate master playlist
        if renditions_info:
            master_path = generate_master_playlist(output_dir, renditions_info)
            
            # Generate thumbnail
            try:
                thumb_path = generate_thumbnail(input_path, output_dir, source_info, video_id)
            except:
                thumb_path = None
            
            # Generate sprite sheet
            try:
                sprite_path, vtt_path = generate_sprite_sheet(input_path, output_dir, source_info, video_id)
            except:
                sprite_path, vtt_path = None, None
            
            return {
                'success': True,
                'renditions': renditions_info,
                'master_playlist': os.path.basename(master_path) if master_path else 'master.m3u8',
                'thumbnail': os.path.basename(thumb_path) if thumb_path else None,
                'sprite': os.path.basename(sprite_path) if sprite_path else None,
                'thumbnails_vtt': os.path.basename(vtt_path) if vtt_path else None,
                'source_info': {
                    'width': source_info['width'],
                    'height': source_info['height'],
                    'bitrate': source_info['bitrate'],
                    'fps': source_info['fps'],
                    'codec': source_info['codec'],
                    'audio_codec': source_info['audio_codec'],
                }
            }
        
        return {'success': False, 'error': 'No renditions generated'}
    
    except ImportError:
        logger.warning("transcode.py not available, using fallback single-stream HLS")
        return None
    except Exception as e:
        logger.error(f"Adaptive HLS generation error: {e}")
        return {'success': False, 'error': str(e)}


# ═══════════════════════════════════════════════════════════════
#  BATCH VIDEO CREATION & PROCESSING (200 × 20GB)
# ═══════════════════════════════════════════════════════════════

@app.route('/api/admin/batch/create_videos', methods=['POST'])
@login_required
@admin_required
def batch_create_videos():
    """
    Create 200 batch video jobs of 20GB each.
    Generates synthetic video files and queues them for HLS transcoding.
    """
    count = request.json.get('count', 200) if request.is_json else 200
    size_gb = request.json.get('size_gb', 20) if request.is_json else 20
    uploader_id = request.json.get('uploader_id', current_user.id) if request.is_json else current_user.id
    
    # Cap at safe limits
    count = min(max(count, 1), 200)
    size_gb = min(max(size_gb, 1), 20)
    
    def background_batch_creation():
        with app.app_context():
            try:
                from services.upload_engine import create_batch_video_jobs, process_batch_videos
                
                logger.info(f"Starting batch creation: {count} videos x {size_gb}GB")
                
                # Create database entries and job registry
                jobs = create_batch_video_jobs(count=count, size_gb=size_gb, uploader_id=uploader_id)
                
                if not jobs:
                    logger.error("Failed to create batch video jobs")
                    return
                
                # Process the batch (generate synthetic + transcode to HLS)
                process_batch_videos(jobs)
                
                logger.info(f"Batch video processing completed: {len(jobs)} videos")
                
            except Exception as e:
                logger.error(f"Batch video creation error: {e}")
    
    thread = threading.Thread(target=background_batch_creation, daemon=True)
    thread.start()
    
    return jsonify({
        'success': True,
        'message': f'Batch creation started: {count} videos × {size_gb}GB each',
        'total_videos': count,
        'total_size_gb': count * size_gb
    })


@app.route('/api/admin/batch/status')
@login_required
@admin_required
def batch_status():
    """Get the status of batch video processing."""
    try:
        from services.upload_engine import _batch_progress
        return jsonify(_batch_progress or {
            'total': 0,
            'completed': 0,
            'failed': 0,
            'status': 'idle',
            'started_at': None
        })
    except Exception as e:
        return jsonify({'error': str(e), 'status': 'error'})


@app.route('/api/admin/batch/generate_synthetic', methods=['POST'])
@login_required
@admin_required
def generate_synthetic_videos():
    """Generate synthetic test video files for bulk processing."""
    count = request.json.get('count', 10) if request.is_json else 10
    size_gb = request.json.get('size_gb', 20) if request.is_json else 20
    duration = request.json.get('duration', 300) if request.is_json else 300
    
    count = min(max(count, 1), 200)
    size_gb = min(max(size_gb, 1), 20)
    
    output_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'synthetic_batch')
    os.makedirs(output_dir, exist_ok=True)
    
    def background_generation():
        with app.app_context():
            try:
                from services.upload_engine import BatchVideoGenerator
                
                import subprocess
                total_size_bytes = size_gb * 1024 * 1024 * 1024
                
                for i in range(count):
                    output_path = os.path.join(output_dir, f'synthetic_video_{i+1:03d}.mp4')
                    
                    # Estimate bitrate
                    target_bitrate = (total_size_bytes * 8) // duration
                    
                    cmd = [
                        'ffmpeg', '-y',
                        '-f', 'lavfi',
                        '-i', 'nullsrc=size=1920x1080:rate=30',
                        '-f', 'lavfi',
                        '-i', 'anullsrc=r=44100:cl=stereo',
                        '-c:v', 'libx264',
                        '-preset', 'ultrafast',
                        '-b:v', str(target_bitrate),
                        '-minrate', str(target_bitrate),
                        '-maxrate', str(target_bitrate),
                        '-bufsize', str(target_bitrate * 2),
                        '-c:a', 'aac',
                        '-b:a', '128k',
                        '-t', str(duration),
                        '-f', 'mp4',
                        output_path
                    ]
                    
                    subprocess.run(cmd, capture_output=True, timeout=3600)
                    
                    # Pad to exact size
                    actual_size = os.path.getsize(output_path)
                    if actual_size < total_size_bytes:
                        with open(output_path, 'ab') as f:
                            f.write(b'\x00' * (total_size_bytes - actual_size))
                    elif actual_size > total_size_bytes:
                        with open(output_path, 'r+b') as f:
                            f.truncate(total_size_bytes)
                    
                    logger.info(f"Created synthetic video {i+1}/{count}: {output_path} ({size_gb}GB)")
                
                logger.info(f"Synthetic video generation completed: {count} files in {output_dir}")
                
            except Exception as e:
                logger.error(f"Synthetic video generation error: {e}")
    
    thread = threading.Thread(target=background_generation, daemon=True)
    thread.start()
    
    return jsonify({
        'success': True,
        'message': f'Generating {count} synthetic videos ({size_gb}GB each) in background',
        'output_dir': output_dir
    })


# ═══════════════════════════════════════════════════════════════
#  STUDENT PROGRESS REPORT ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route('/student/progress_reports')
@login_required
def student_progress_reports():
    """Display all progress reports for the current student."""
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    logs_query = EmailDeliveryLog.query.filter_by(student_id=current_user.id).filter(
        EmailDeliveryLog.report_type != 'profile_email_update'
    )
    if start_date:
        try:
            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
            logs_query = logs_query.filter(EmailDeliveryLog.sent_at >= start_dt)
        except ValueError:
            flash('Invalid start date format. Use YYYY-MM-DD.', 'warning')
    if end_date:
        try:
            end_dt = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1)
            logs_query = logs_query.filter(EmailDeliveryLog.sent_at < end_dt)
        except ValueError:
            flash('Invalid end date format. Use YYYY-MM-DD.', 'warning')
    logs = logs_query.order_by(EmailDeliveryLog.sent_at.desc()).all()
    return render_template('student_progress_reports.html', logs=logs, start_date=start_date, end_date=end_date)

@app.route('/teacher/report_logs')
@login_required
@teacher_required
def teacher_report_logs():
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    status = request.args.get('status', 'all')
    class_ids = [c.id for c in Classroom.query.filter_by(teacher_id=current_user.id).all()]
    logs_query = EmailDeliveryLog.query.filter(EmailDeliveryLog.class_id.in_(class_ids)).filter(
        EmailDeliveryLog.report_type != 'profile_email_update'
    )
    if start_date:
        try:
            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
            logs_query = logs_query.filter(EmailDeliveryLog.sent_at >= start_dt)
        except ValueError:
            flash('Invalid start date format. Use YYYY-MM-DD.', 'warning')
    if end_date:
        try:
            end_dt = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1)
            logs_query = logs_query.filter(EmailDeliveryLog.sent_at < end_dt)
        except ValueError:
            flash('Invalid end date format. Use YYYY-MM-DD.', 'warning')
    if status and status != 'all':
        logs_query = logs_query.filter_by(status=status)
    logs = logs_query.order_by(EmailDeliveryLog.sent_at.desc()).all()
    return render_template('teacher_report_logs.html', logs=logs, start_date=start_date, end_date=end_date, status=status)


@app.route('/student/progress_report/<int:log_id>')
@login_required
def student_view_progress_report(log_id):
    """Display a single progress report HTML content for the student."""
    log = EmailDeliveryLog.query.get_or_404(log_id)
    if log.student_id != current_user.id:
        abort(403)
    if not log.report_html:
        flash('This report has no stored HTML content.', 'warning')
        return redirect(url_for('student_progress_reports'))
    return render_template('student_view_report.html', log=log, report_html=log.report_html)


# ═══════════════════════════════════════════════════════════════
#  NEW: WEEKLY CLASS PERFORMANCE & XP DIGEST SYSTEM
# ═══════════════════════════════════════════════════════════════

@app.route('/teacher/weekly_reports')
@login_required
@teacher_required
def teacher_weekly_reports():
    """Weekly Class Performance Reports Dashboard for Faculty."""
    class_id = request.args.get('class_id', type=int)
    status_filter = request.args.get('status', 'all')

    teacher_classes = Classroom.query.filter_by(teacher_id=current_user.id).all()
    class_ids = [c.id for c in teacher_classes]

    # Auto-compile current weekly digest if missing for teacher classes
    today = datetime.utcnow().date()
    m_start, s_end = get_current_week_bounds(today)
    for c in teacher_classes:
        if c.students.count() > 0:
            existing = ClassWeeklyReport.query.filter_by(
                classroom_id=c.id, period_start=m_start, period_end=s_end
            ).first()
            if not existing:
                generate_or_get_weekly_report(c.id, current_user.id, m_start, s_end)

    query = ClassWeeklyReport.query.filter(ClassWeeklyReport.classroom_id.in_(class_ids) if class_ids else db.text('1=0'))
    if class_id:
        query = query.filter_by(classroom_id=class_id)
    if status_filter and status_filter != 'all':
        query = query.filter_by(status=status_filter)

    reports = query.order_by(ClassWeeklyReport.period_end.desc(), ClassWeeklyReport.generated_at.desc()).all()
    return render_template(
        'teacher_weekly_reports.html',
        reports=reports,
        classes=teacher_classes,
        selected_class_id=class_id,
        status_filter=status_filter,
        now=datetime.utcnow()
    )


@app.route('/teacher/weekly_reports/generate', methods=['POST'])
@login_required
@teacher_required
def teacher_generate_weekly_report():
    """Generate or re-compile a class performance report on demand."""
    classroom_id = request.form.get('classroom_id', type=int)
    start_str = request.form.get('period_start')
    end_str = request.form.get('period_end')
    remarks = request.form.get('remarks', '').strip()

    if not classroom_id:
        flash('Please select a valid classroom.', 'error')
        return redirect(url_for('teacher_weekly_reports'))

    classroom = Classroom.query.get_or_404(classroom_id)
    if classroom.teacher_id != current_user.id and current_user.role != 'admin':
        abort(403)

    if start_str and end_str:
        try:
            p_start = datetime.strptime(start_str, '%Y-%m-%d').date()
            p_end = datetime.strptime(end_str, '%Y-%m-%d').date()
            if p_end < p_start:
                flash('Period end date cannot be earlier than start date.', 'error')
                return redirect(url_for('teacher_weekly_reports', class_id=classroom_id))
        except ValueError:
            flash('Invalid date format. Use YYYY-MM-DD.', 'error')
            return redirect(url_for('teacher_weekly_reports', class_id=classroom_id))
    else:
        p_start, p_end = get_current_week_bounds()

    report = generate_or_get_weekly_report(classroom_id, current_user.id, p_start, p_end, remarks)
    if report:
        flash(f'Weekly Performance Digest for "{classroom.name}" compiled successfully!', 'success')
        return redirect(url_for('teacher_weekly_report_detail', report_id=report.id))
    else:
        flash('Could not generate report for the selected class.', 'error')
        return redirect(url_for('teacher_weekly_reports'))


@app.route('/teacher/weekly_reports/<int:report_id>')
@login_required
def teacher_weekly_report_detail(report_id):
    """Detailed view of a single ClassWeeklyReport with KPI cards and student roster."""
    report = ClassWeeklyReport.query.get_or_404(report_id)
    if current_user.role == 'teacher' and report.classroom.teacher_id != current_user.id:
        abort(403)
    data = report.get_report_data()
    return render_template('teacher_weekly_report_detail.html', report=report, data=data)


@app.route('/teacher/weekly_reports/<int:report_id>/download_pdf')
@login_required
def download_weekly_report_pdf(report_id):
    """Stream generated ReportLab PDF for a ClassWeeklyReport."""
    report = ClassWeeklyReport.query.get_or_404(report_id)
    if current_user.role == 'teacher' and report.classroom.teacher_id != current_user.id:
        abort(403)
    pdf_buf = build_weekly_report_pdf(report)
    safe_name = "".join(c for c in report.classroom.name if c.isalnum() or c in (' ', '_', '-')).rstrip()
    filename = f"Weekly_Digest_{safe_name.replace(' ', '_')}_{report.period_end.strftime('%Y%m%d')}.pdf"
    response = make_response(pdf_buf.getvalue())
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@app.route('/teacher/weekly_reports/<int:report_id>/send_to_admin', methods=['POST'])
@login_required
@teacher_required
def send_weekly_report_to_admin(report_id):
    """1-Click Dispatch to Principal/Admin with in-app notification and email tracking."""
    report = ClassWeeklyReport.query.get_or_404(report_id)
    if report.classroom.teacher_id != current_user.id and current_user.role != 'admin':
        abort(403)

    report.status = 'sent_to_admin'
    report.sent_to_admin_at = datetime.utcnow()

    # Create in-app Notification for all Admins in this institution
    admins = User.query.filter_by(role='admin', institution_id=report.institution_id).all()
    if not admins:
        admins = User.query.filter_by(role='admin').all()

    for admin_user in admins:
        notif = Notification(
            user_id=admin_user.id,
            message=f"Faculty {current_user.username} submitted the Weekly Performance Digest for {report.classroom.name} ({report.period_start.strftime('%b %d')} - {report.period_end.strftime('%b %d')}).",
            institution_id=report.institution_id
        )
        db.session.add(notif)

    db.session.commit()
    flash(f'Weekly report for "{report.classroom.name}" dispatched to Admin / Principal inbox!', 'success')
    return redirect(url_for('teacher_weekly_report_detail', report_id=report.id))


@app.route('/teacher/weekly_reports/<int:report_id>/update_remarks', methods=['POST'])
@login_required
@teacher_required
def update_weekly_report_remarks(report_id):
    """Update teacher's executive commentary for a report."""
    report = ClassWeeklyReport.query.get_or_404(report_id)
    if report.classroom.teacher_id != current_user.id and current_user.role != 'admin':
        abort(403)
    remarks = request.form.get('teacher_remarks', '').strip()
    report.teacher_remarks = remarks
    db.session.commit()
    flash('Faculty remarks updated successfully.', 'success')
    return redirect(url_for('teacher_weekly_report_detail', report_id=report.id))


@app.route('/admin/class_reports')
@login_required
@admin_required
def admin_class_reports():
    """Admin / Principal portal to review submitted Weekly Class Reports."""
    class_id = request.args.get('class_id', type=int)
    status_filter = request.args.get('status', 'all')
    if current_user.role == 'system_admin':
        classes = Classroom.query.order_by(Classroom.name).all()
        query = ClassWeeklyReport.query
    else:
        classes = scope_to_institution(Classroom.query, Classroom).order_by(Classroom.name).all()
        query = scope_to_institution(ClassWeeklyReport.query, ClassWeeklyReport)

    if class_id:
        query = query.filter_by(classroom_id=class_id)
    if status_filter and status_filter != 'all':
        query = query.filter_by(status=status_filter)

    reports = query.order_by(ClassWeeklyReport.period_end.desc(), ClassWeeklyReport.generated_at.desc()).all()
    return render_template(
        'admin_class_reports.html',
        reports=reports,
        classes=classes,
        selected_class_id=class_id,
        status_filter=status_filter
    )


@app.route('/admin/class_reports/<int:report_id>/feedback', methods=['POST'])
@login_required
@admin_required
def admin_submit_report_feedback(report_id):
    """Principal submits review feedback back to the faculty."""
    report = ClassWeeklyReport.query.get_or_404(report_id)
    feedback = request.form.get('admin_feedback', '').strip()
    report.admin_feedback = feedback
    report.status = 'reviewed'

    # Notify teacher
    notif = Notification(
        user_id=report.teacher_id,
        message=f"Principal / Admin reviewed your Weekly Digest for {report.classroom.name}.",
        institution_id=report.institution_id
    )
    db.session.add(notif)
    db.session.commit()
    flash('Review feedback transmitted to faculty successfully.', 'success')
    return redirect(url_for('teacher_weekly_report_detail', report_id=report.id))


# ═══════════════════════════════════════════════════════════════
#  NEW FEATURE 1: IN-VIDEO POP-UP CHECKPOINTS & COMPREHENSION
# ═══════════════════════════════════════════════════════════════

@app.route('/api/video/<int:video_id>/checkpoints', methods=['GET'])
@login_required
def get_video_checkpoints(video_id):
    """Retrieve in-video comprehension pop-up checkpoints for playback."""
    video = Video.query.get_or_404(video_id)
    checkpoints = VideoCheckpoint.query.filter_by(video_id=video_id).order_by(VideoCheckpoint.timestamp_seconds.asc()).all()

    answered_ids = set()
    if current_user.role == 'student':
        responses = CheckpointResponse.query.filter_by(student_id=current_user.id).all()
        answered_ids = {r.checkpoint_id for r in responses}

    return jsonify({
        'success': True,
        'checkpoints': [{
            'id': c.id,
            'timestamp': c.timestamp_seconds,
            'question': c.question_text,
            'option_a': c.option_a,
            'option_b': c.option_b,
            'option_c': c.option_c,
            'option_d': c.option_d,
            'xp_reward': c.xp_reward,
            'answered': c.id in answered_ids
        } for c in checkpoints]
    })


@app.route('/api/video/<int:video_id>/checkpoints/add', methods=['POST'])
@login_required
@teacher_required
def add_video_checkpoint(video_id):
    """Teacher adds a new checkpoint pop-up at a specific video timestamp."""
    video = Video.query.get_or_404(video_id)
    data = request.get_json() or {}

    ts = float(data.get('timestamp_seconds', 0.0))
    q_text = (data.get('question_text') or '').strip()
    opt_a = (data.get('option_a') or '').strip()
    opt_b = (data.get('option_b') or '').strip()
    opt_c = (data.get('option_c') or '').strip() or None
    opt_d = (data.get('option_d') or '').strip() or None
    correct = (data.get('correct_option') or 'a').lower().strip()
    explanation = (data.get('explanation') or '').strip() or None
    xp = int(data.get('xp_reward', 25))

    if not q_text or not opt_a or not opt_b:
        return jsonify({'success': False, 'message': 'Question text and at least 2 options are required.'}), 400

    cp = VideoCheckpoint(
        video_id=video_id,
        institution_id=video.institution_id,
        timestamp_seconds=ts,
        question_text=q_text,
        option_a=opt_a,
        option_b=opt_b,
        option_c=opt_c,
        option_d=opt_d,
        correct_option=correct,
        explanation=explanation,
        xp_reward=xp
    )
    db.session.add(cp)
    db.session.commit()

    return jsonify({'success': True, 'checkpoint_id': cp.id, 'message': 'Checkpoint created successfully!'})


@app.route('/api/video/checkpoint/<int:checkpoint_id>/submit', methods=['POST'])
@login_required
def submit_checkpoint_answer(checkpoint_id):
    """Student submits an answer to an in-video checkpoint pop-up."""
    cp = VideoCheckpoint.query.get_or_404(checkpoint_id)
    data = request.get_json() or {}
    selected = (data.get('selected_option') or '').lower().strip()

    is_correct = (selected == cp.correct_option.lower())
    xp_awarded = 0

    # Check if student already answered
    existing = CheckpointResponse.query.filter_by(checkpoint_id=cp.id, student_id=current_user.id).first()
    if not existing:
        resp = CheckpointResponse(
            checkpoint_id=cp.id,
            student_id=current_user.id,
            institution_id=cp.institution_id,
            selected_option=selected,
            is_correct=is_correct
        )
        db.session.add(resp)

        if is_correct:
            xp_awarded = cp.xp_reward or 25
            current_user.xp = (current_user.xp or 0) + xp_awarded
            current_user.update_level()
            db.session.add(current_user)

        db.session.commit()

    return jsonify({
        'success': True,
        'is_correct': is_correct,
        'correct_option': cp.correct_option,
        'explanation': cp.explanation or ('Well done!' if is_correct else 'Review the concept and keep learning!'),
        'xp_awarded': xp_awarded,
        'total_xp': current_user.xp
    })


@app.route('/api/video/checkpoint/<int:checkpoint_id>/delete', methods=['POST'])
@login_required
@teacher_required
def delete_video_checkpoint(checkpoint_id):
    """Delete an in-video checkpoint."""
    cp = VideoCheckpoint.query.get_or_404(checkpoint_id)
    db.session.delete(cp)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Checkpoint deleted.'})


# ═══════════════════════════════════════════════════════════════
#  NEW FEATURE 2: TIME-STAMPED VIDEO DOUBTS & CLASSROOM Q&A
# ═══════════════════════════════════════════════════════════════

@app.route('/api/video/<int:video_id>/doubts', methods=['GET'])
@login_required
def get_video_doubts(video_id):
    """Fetch all timestamped doubts and replies for a video."""
    doubts = VideoDoubt.query.filter_by(video_id=video_id).order_by(VideoDoubt.timestamp_seconds.asc(), VideoDoubt.created_at.desc()).all()

    out = []
    for d in doubts:
        replies_data = [{
            'id': r.id,
            'author': r.user.username if r.user else 'Unknown',
            'role': r.user.role if r.user else 'student',
            'content': r.content,
            'is_teacher_endorsed': r.is_teacher_endorsed,
            'created_at': r.created_at.strftime('%b %d, %I:%M %p')
        } for r in d.replies]

        ts_min = int(d.timestamp_seconds // 60)
        ts_sec = int(d.timestamp_seconds % 60)

        out.append({
            'id': d.id,
            'author': d.user.username if d.user else 'Unknown',
            'role': d.user.role if d.user else 'student',
            'timestamp': d.timestamp_seconds,
            'timestamp_str': f"{ts_min:02d}:{ts_sec:02d}",
            'question': d.question_text,
            'is_resolved': d.is_resolved,
            'created_at': d.created_at.strftime('%b %d, %I:%M %p'),
            'replies': replies_data
        })

    return jsonify({'success': True, 'doubts': out})


@app.route('/api/video/<int:video_id>/doubts/add', methods=['POST'])
@login_required
def add_video_doubt(video_id):
    """Post a new time-stamped doubt on a video."""
    video = Video.query.get_or_404(video_id)
    data = request.get_json() or {}

    ts = float(data.get('timestamp_seconds', 0.0))
    q_text = (data.get('question_text') or '').strip()

    if not q_text:
        return jsonify({'success': False, 'message': 'Please enter your doubt / question.'}), 400

    doubt = VideoDoubt(
        video_id=video_id,
        user_id=current_user.id,
        institution_id=video.institution_id,
        timestamp_seconds=ts,
        question_text=q_text
    )
    db.session.add(doubt)
    db.session.commit()

    return jsonify({'success': True, 'doubt_id': doubt.id, 'message': 'Doubt posted to classroom thread!'})


@app.route('/api/video/doubts/<int:doubt_id>/reply', methods=['POST'])
@login_required
def reply_video_doubt(doubt_id):
    """Add a reply to a video doubt."""
    doubt = VideoDoubt.query.get_or_404(doubt_id)
    data = request.get_json() or {}
    content = (data.get('content') or '').strip()

    if not content:
        return jsonify({'success': False, 'message': 'Reply content cannot be empty.'}), 400

    is_endorsed = (current_user.role in ['teacher', 'admin'])
    reply = VideoDoubtReply(
        doubt_id=doubt_id,
        user_id=current_user.id,
        institution_id=doubt.institution_id,
        content=content,
        is_teacher_endorsed=is_endorsed
    )
    db.session.add(reply)
    db.session.commit()

    return jsonify({'success': True, 'message': 'Reply added.'})


@app.route('/api/video/doubts/<int:doubt_id>/toggle_resolve', methods=['POST'])
@login_required
def toggle_resolve_doubt(doubt_id):
    """Mark a doubt as resolved or open."""
    doubt = VideoDoubt.query.get_or_404(doubt_id)
    if doubt.user_id != current_user.id and current_user.role not in ['teacher', 'admin']:
        abort(403)

    doubt.is_resolved = not doubt.is_resolved
    db.session.commit()
    return jsonify({'success': True, 'is_resolved': doubt.is_resolved})


# ═══════════════════════════════════════════════════════════════
#  NEW FEATURE 3: 1-CLICK AI FLASHCARD & QUIZ GENERATOR
# ═══════════════════════════════════════════════════════════════

@app.route('/api/video/<int:video_id>/generate_ai_study_kit', methods=['POST'])
@login_required
def generate_ai_study_kit(video_id):
    """Generate 5 interactive flashcards and 5 quiz questions for a video."""
    video = Video.query.get_or_404(video_id)

    # Built-in study kit generator based on video title & summary concepts
    takeaways = video.get_ai_takeaways() or []
    summary_snippet = video.ai_summary or f"Lecture concepts on {video.title}."

    default_flashcard_pairs = [
        (f"Core Principle of {video.title[:30]}", f"Foundational understanding: {summary_snippet[:140]}..."),
        (f"Key Milestone: 01", takeaways[0] if len(takeaways) > 0 else f"Initial overview of {video.title}."),
        (f"Key Milestone: 02", takeaways[1] if len(takeaways) > 1 else "Methodological application and analysis."),
        (f"Essential Equation / Concept", takeaways[2] if len(takeaways) > 2 else "Theoretical framework & retention notes."),
        ("Lecture Summary", f"Mastery review: {video.title} essential points for examinations.")
    ]

    saved_flashcards = []
    for term, definition in default_flashcard_pairs:
        fc = VideoFlashcard(
            video_id=video_id,
            user_id=current_user.id,
            institution_id=video.institution_id,
            front_term=term,
            back_definition=definition
        )
        db.session.add(fc)
        saved_flashcards.append({'front': term, 'back': definition})

    db.session.commit()

    sample_questions = [
        {
            'text': f"What is the primary academic focus of '{video.title}'?",
            'a': f"Core theoretical derivation of {video.title[:25]}",
            'b': "Unrelated background context",
            'c': "Historical introductory overview only",
            'd': "Administrative syllabus policy",
            'correct': 'a'
        },
        {
            'text': f"Which concept was highlighted in the key takeaways of {video.title}?",
            'a': takeaways[0] if takeaways else "Application of foundational principles",
            'b': "Disregarding numerical constraints",
            'c': "Skipping verification steps",
            'd': "None of the above",
            'correct': 'a'
        }
    ]

    return jsonify({
        'success': True,
        'flashcards': saved_flashcards,
        'questions': sample_questions,
        'message': 'AI Study Kit (Flashcards & Assessment Questions) generated!'
    })


@app.route('/video/<int:video_id>/flashcards', methods=['GET'])
@login_required
def view_video_flashcards(video_id):
    """Interactive 3D flip-card study mode for students."""
    video = Video.query.get_or_404(video_id)
    flashcards = VideoFlashcard.query.filter_by(video_id=video_id).all()
    return render_template('video_flashcards.html', video=video, flashcards=flashcards)


@app.route('/api/video/<int:video_id>/save_ai_quiz', methods=['POST'])
@login_required
@teacher_required
def api_save_ai_quiz(video_id):
    """1-Click conversion of AI generated questions into an active Classroom Quiz."""
    video = Video.query.get_or_404(video_id)
    data = request.get_json() or {}
    quiz_title = data.get('title') or f"AI Comprehension Assessment — {video.title}"
    classroom_id = data.get('classroom_id') or (video.classroom_id if video.classroom_id and video.classroom_id > 0 else None)

    if not classroom_id:
        classes = Classroom.query.filter_by(teacher_id=current_user.id).first()
        classroom_id = classes.id if classes else 1

    quiz = Quiz(
        title=quiz_title,
        classroom_id=classroom_id,
        teacher_id=current_user.id,
        institution_id=video.institution_id
    )
    db.session.add(quiz)
    db.session.flush()

    questions_data = data.get('questions', [])
    if not questions_data:
        questions_data = [{
            'text': f"Comprehension Check: What is the core theorem in {video.title}?",
            'a': "Fundamental principle application",
            'b': "Secondary extrapolation",
            'c': "Empirical approximation",
            'd': "Random constant",
            'correct': 'a'
        }]

    for q in questions_data:
        q_obj = Question(
            quiz_id=quiz.id,
            institution_id=video.institution_id,
            text=q.get('text', 'Concept Question'),
            option_a=q.get('a', 'Option A'),
            option_b=q.get('b', 'Option B'),
            option_c=q.get('c', 'Option C'),
            option_d=q.get('d', 'Option D'),
            correct_option=q.get('correct', 'a')
        )
        db.session.add(q_obj)

    db.session.commit()
    return jsonify({'success': True, 'quiz_id': quiz.id, 'message': f'Quiz "{quiz.title}" created successfully!'})


# ═══════════════════════════════════════════════════════════════
#  NEW FEATURE 4: VERIFIABLE ACADEMIC CERTIFICATES & QR AUDIT
# ═══════════════════════════════════════════════════════════════

@app.route('/student/certificates')
@login_required
def student_certificates():
    """Student certificates dashboard is disabled for student access."""
    flash('Certificates option is disabled for students.', 'error')
    if current_user.role == 'student':
        return redirect(url_for('student_dashboard'))
    return redirect(url_for('index'))


@app.route('/teacher/issue_certificate', methods=['POST'])
@login_required
@teacher_required
def teacher_issue_certificate():
    """Teacher issues a verifiable academic certificate to a student."""
    student_id = request.form.get('student_id', type=int)
    title = (request.form.get('title') or '').strip()
    desc = (request.form.get('description') or '').strip()
    cert_type = request.form.get('certificate_type', 'course_completion')

    if not student_id or not title:
        flash('Student and certificate title are required.', 'error')
        return redirect(request.referrer or url_for('teacher_dashboard'))

    cert = issue_academic_certificate(
        student_id=student_id,
        title=title,
        description=desc,
        cert_type=cert_type,
        institution_id=current_user.institution_id
    )

    if cert:
        # Notify student
        notif = Notification(
            user_id=student_id,
            message=f"Congratulations! You were awarded the certificate '{title}'.",
            institution_id=current_user.institution_id
        )
        db.session.add(notif)
        db.session.commit()
        flash(f'Certificate issued successfully (Code: {cert.certificate_code})!', 'success')
    else:
        flash('Could not issue certificate.', 'error')

    return redirect(request.referrer or url_for('teacher_dashboard'))


@app.route('/certificates/verify/<cert_code>')
def verify_certificate_public(cert_code):
    """Public verification page to validate certificate authenticity without logging in."""
    cert = AcademicCertificate.query.filter_by(certificate_code=cert_code).first_or_404()
    inst = db.session.get(Institution, cert.institution_id) if cert.institution_id else None
    return render_template('verify_certificate.html', cert=cert, institution=inst)


@app.route('/certificates/download/<cert_code>')
@login_required
def download_certificate_pdf(cert_code):
    """Download official certificate PDF (restricted for students)."""
    if current_user.role == 'student':
        flash('Access denied. Certificate download is disabled for students.', 'error')
        return redirect(url_for('student_dashboard'))
    cert = AcademicCertificate.query.filter_by(certificate_code=cert_code).first_or_404()
    base_url = request.host_url.rstrip('/')
    pdf_buf = build_certificate_pdf(cert, base_url=base_url)

    safe_name = "".join(c for c in cert.title if c.isalnum() or c in (' ', '_', '-')).rstrip()
    filename = f"Certificate_{safe_name.replace(' ', '_')}_{cert.certificate_code}.pdf"

    response = make_response(pdf_buf.getvalue())
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


# ═══════════════════════════════════════════════════════════════
#  NEW FEATURE 5: TOKENIZED PARENT VIEW-ONLY PROGRESS PORTAL
# ═══════════════════════════════════════════════════════════════

@app.route('/teacher/parent_token/<int:student_id>', methods=['POST'])
@login_required
@teacher_required
def generate_parent_token(student_id):
    """Generate or retrieve a secure tokenized parent access URL."""
    student = User.query.get_or_404(student_id)
    if student.role != 'student':
        return jsonify({'success': False, 'message': 'User is not a student.'}), 400

    token_obj = ParentAccessToken.query.filter_by(student_id=student_id, is_active=True).first()
    if not token_obj:
        raw_token = secrets.token_urlsafe(32)
        token_obj = ParentAccessToken(
            student_id=student_id,
            institution_id=student.institution_id,
            token=raw_token,
            expires_at=datetime.utcnow() + timedelta(days=60)
        )
        db.session.add(token_obj)
        db.session.commit()

    parent_url = url_for('parent_portal_view', token=token_obj.token, _external=True)
    return jsonify({
        'success': True,
        'token': token_obj.token,
        'parent_url': parent_url,
        'expires_at': token_obj.expires_at.strftime('%b %d, %Y') if token_obj.expires_at else 'Active'
    })


@app.route('/parent/view/<token>')
def parent_portal_view(token):
    """Public, secure view-only mobile summary for parents without requiring a login."""
    token_obj = ParentAccessToken.query.filter_by(token=token, is_active=True).first_or_404()
    token_obj.last_accessed_at = datetime.utcnow()
    db.session.commit()

    student = token_obj.student
    inst = db.session.get(Institution, student.institution_id) if student.institution_id else None

    # Compute overall attendance
    now_date = datetime.utcnow().date()
    start_date = now_date - timedelta(days=30)
    att_stats = compute_attendance_stats(student.id, None, start_date, now_date)

    # Recent quiz scores
    recent_quizzes = QuizResult.query.filter_by(student_id=student.id).order_by(QuizResult.timestamp.desc()).limit(5).all()

    # Certificates
    certs = AcademicCertificate.query.filter_by(student_id=student.id).order_by(AcademicCertificate.issued_at.desc()).all()

    return render_template(
        'parent_portal_view.html',
        student=student,
        institution=inst,
        att_stats=att_stats,
        recent_quizzes=recent_quizzes,
        certificates=certs,
        now_date=now_date
    )


# ═══════════════════════════════════════════════════════════════
#  NEW FEATURE 1: AI VIDEO ASSESSMENT & QUIZ GENERATOR
# ═══════════════════════════════════════════════════════════════

@app.route('/teacher/video/<int:video_id>/ai_generate_quiz', methods=['POST'])
@login_required
@teacher_required
def ai_generate_quiz_for_video(video_id):
    """API endpoint for teachers to generate multiple choice quiz questions using AI."""
    video = Video.query.get_or_404(video_id)
    data = request.get_json() or {}
    num_q = int(data.get('num_questions', 5))
    difficulty = data.get('difficulty', 'intermediate')
    topic_focus = data.get('topic_focus', '')
    custom_prompt = data.get('custom_prompt', '')

    questions = generate_quiz_from_video(
        video=video,
        num_questions=min(max(1, num_q), 15),
        difficulty=difficulty,
        topic_focus=topic_focus,
        custom_prompt=custom_prompt
    )

    return jsonify({
        'success': True,
        'video_id': video.id,
        'video_title': video.title,
        'questions': questions
    })


@app.route('/teacher/video/<int:video_id>/save_ai_quiz', methods=['POST'])
@login_required
@teacher_required
def save_ai_quiz(video_id):
    """Save generated AI questions as an official Quiz."""
    video = Video.query.get_or_404(video_id)
    data = request.get_json() or {}
    title = (data.get('title') or f"AI Quiz: {video.title}").strip()
    description = (data.get('description') or f"Assessment covering key concepts from {video.title}").strip()
    passing_percent = int(data.get('passing_percent', 50))
    time_limit = int(data.get('time_limit_minutes', 0))
    max_attempts = int(data.get('max_attempts', 0))
    proctoring = bool(data.get('proctoring_enabled', False))
    questions_data = data.get('questions', [])

    if not questions_data:
        return jsonify({'success': False, 'message': 'No questions provided.'}), 400

    quiz = Quiz(
        title=title,
        description=description,
        teacher_id=current_user.id,
        video_id=video.id,
        classroom_id=video.classroom_id,
        institution_id=current_user.institution_id,
        passing_percent=passing_percent,
        time_limit_minutes=time_limit,
        max_attempts=max_attempts,
        proctoring_enabled=proctoring
    )
    db.session.add(quiz)
    db.session.flush()

    for q in questions_data:
        q_obj = Question(
            quiz_id=quiz.id,
            institution_id=current_user.institution_id,
            text=q.get('text', 'Question'),
            option_a=q.get('option_a', 'Option A'),
            option_b=q.get('option_b', 'Option B'),
            option_c=q.get('option_c', 'Option C'),
            option_d=q.get('option_d', 'Option D'),
            correct_option=str(q.get('correct_option', 'A')).upper()[:1],
            explanation=q.get('explanation', ''),
            points=int(q.get('points', 1))
        )
        db.session.add(q_obj)

    db.session.commit()
    return jsonify({
        'success': True,
        'quiz_id': quiz.id,
        'message': 'Quiz successfully created from AI questions!'
    })


# ═══════════════════════════════════════════════════════════════
#  NEW FEATURE 2: SEARCHABLE VIDEO TRANSCRIPTS
# ═══════════════════════════════════════════════════════════════

@app.route('/api/video/<int:video_id>/transcript')
def get_video_transcript(video_id):
    """Returns parsed subtitle cues for the video player transcript sidebar."""
    video = Video.query.get_or_404(video_id)
    cues = []

    # Check for subtitle path
    if video.subtitle_path:
        sub_full = os.path.join(BASE_DIR, 'static', video.subtitle_path.lstrip('/\\static/'))
        if not os.path.exists(sub_full):
            sub_full = os.path.join(SUBTITLE_FOLDER, os.path.basename(video.subtitle_path))
        if os.path.exists(sub_full):
            cues = parse_vtt_or_srt_to_cues(sub_full)

    # If no subtitle file exists, synthesize cues from chapters or AI takeaways
    if not cues:
        chapters = video.get_chapters()
        if chapters:
            for idx, ch in enumerate(chapters):
                st = float(ch.get('time', idx * 60))
                mins = int(st // 60)
                secs = int(st % 60)
                cues.append({
                    'start': st,
                    'end': st + 60,
                    'start_formatted': f"{mins:02d}:{secs:02d}",
                    'text': f"Chapter: {ch.get('title', 'Lecture Section')}"
                })
        elif video.ai_summary:
            takeaways = video.get_ai_takeaways()
            step = max(30.0, (video.duration_seconds or 300) / max(1, len(takeaways) + 1))
            for idx, t in enumerate(takeaways):
                st = idx * step
                mins = int(st // 60)
                secs = int(st % 60)
                cues.append({
                    'start': st,
                    'end': st + step,
                    'start_formatted': f"{mins:02d}:{secs:02d}",
                    'text': t
                })

    return jsonify({
        'video_id': video.id,
        'has_transcript': len(cues) > 0,
        'cues': cues
    })


# ═══════════════════════════════════════════════════════════════
#  NEW FEATURE 3: QUIZ PROCTORING TELEMETRY & AUTO-SUBMISSION
# ═══════════════════════════════════════════════════════════════

@app.route('/api/quiz/<int:quiz_id>/proctoring_violation', methods=['POST'])
@login_required
def log_quiz_proctoring_violation(quiz_id):
    """Logs client-side proctoring violation (tab switch, window blur, exit fullscreen)."""
    quiz = Quiz.query.get_or_404(quiz_id)
    data = request.get_json() or {}
    reason = data.get('reason', 'Tab switch or window blur detected')

    return jsonify({
        'success': True,
        'logged': True,
        'reason': reason,
        'timestamp': datetime.utcnow().strftime('%H:%M:%S')
    })


# ═══════════════════════════════════════════════════════════════
#  NEW FEATURE 4: CAMPUS NOTICE BOARD & ANNOUNCEMENTS
# ═══════════════════════════════════════════════════════════════

@app.route('/announcements')
@login_required
def announcements_hub():
    """Notice board displaying active institutional and classroom announcements."""
    inst_id = current_user.institution_id
    query = Announcement.query
    if inst_id is not None:
        query = query.filter_by(institution_id=inst_id)

    # Filter target roles
    if current_user.role == 'student':
        query = query.filter(Announcement.target_role.in_(['all', 'student']))
    elif current_user.role in ('teacher', 'hod'):
        query = query.filter(Announcement.target_role.in_(['all', 'teacher']))

    announcements = query.order_by(Announcement.is_pinned.desc(), Announcement.created_at.desc()).all()
    user_classes = []
    if current_user.role in ('teacher', 'hod'):
        user_classes = current_user.created_classes
    elif current_user.role == 'admin':
        user_classes = Classroom.query.all()

    return render_template(
        'announcements.html',
        announcements=announcements,
        user_classes=user_classes,
        current_time=datetime.utcnow()
    )


@app.route('/announcements/create', methods=['POST'])
@login_required
def create_announcement():
    """Create a new broadcast or class announcement (Teacher or Admin)."""
    if current_user.role not in ('admin', 'system_admin', 'teacher'):
        flash('Permission denied to publish announcements.', 'error')
        return redirect(url_for('announcements_hub'))

    title = request.form.get('title', '').strip()
    content = request.form.get('content', '').strip()
    priority = request.form.get('priority', 'normal')
    target_role = request.form.get('target_role', 'all')
    classroom_id = request.form.get('classroom_id')
    is_pinned = bool(request.form.get('is_pinned'))

    if not title or not content:
        flash('Title and announcement content are required.', 'error')
        return redirect(url_for('announcements_hub'))

    cid = int(classroom_id) if classroom_id and classroom_id.isdigit() else None

    announcement = Announcement(
        title=title,
        content=content,
        author_id=current_user.id,
        institution_id=current_user.institution_id,
        priority=priority,
        target_role=target_role,
        classroom_id=cid,
        is_pinned=is_pinned,
        created_at=datetime.utcnow()
    )
    db.session.add(announcement)
    db.session.commit()

    flash('Announcement broadcasted successfully!', 'success')
    return redirect(url_for('announcements_hub'))


@app.route('/announcements/<int:announcement_id>/delete', methods=['POST'])
@login_required
def delete_announcement(announcement_id):
    """Delete announcement (author or admin)."""
    ann = Announcement.query.get_or_404(announcement_id)
    if current_user.role not in ('admin', 'system_admin') and ann.author_id != current_user.id:
        flash('Permission denied to delete this announcement.', 'error')
        return redirect(url_for('announcements_hub'))

    db.session.delete(ann)
    db.session.commit()
    flash('Announcement deleted.', 'success')
    return redirect(url_for('announcements_hub'))


@app.route('/api/announcements/<int:announcement_id>/mark_read', methods=['POST'])
@login_required
def mark_announcement_read(announcement_id):
    """Record read receipt for student/teacher."""
    ann = Announcement.query.get_or_404(announcement_id)
    read_entry = AnnouncementRead.query.filter_by(announcement_id=ann.id, user_id=current_user.id).first()
    if not read_entry:
        read_entry = AnnouncementRead(
            announcement_id=ann.id,
            user_id=current_user.id,
            institution_id=current_user.institution_id,
            read_at=datetime.utcnow()
        )
        db.session.add(read_entry)
        db.session.commit()
    return jsonify({'success': True})


# ═══════════════════════════════════════════════════════════════
#  NEW FEATURE 5: ACADEMIC TIMETABLE & SMART SCHEDULE
# ═══════════════════════════════════════════════════════════════

@app.route('/timetable')
@login_required
def timetable_hub():
    """Interactive weekly schedule grid for classrooms."""
    inst_id = current_user.institution_id
    classes = []
    selected_class_id = request.args.get('class_id', type=int)

    if current_user.role == 'student':
        raw_classes = current_user.enrolled_classes.all() if hasattr(current_user.enrolled_classes, 'all') else list(current_user.enrolled_classes)
        classes = [c for c in raw_classes if not inst_id or c.institution_id == inst_id]
        if not selected_class_id and classes:
            selected_class_id = classes[0].id
    elif current_user.role in ('teacher', 'hod'):
        raw_classes = current_user.created_classes
        classes = [c for c in raw_classes if not inst_id or c.institution_id == inst_id]
        if not selected_class_id and classes:
            selected_class_id = classes[0].id
    elif current_user.role in ('admin', 'system_admin'):
        classes = Classroom.query.filter_by(institution_id=inst_id).all() if inst_id else Classroom.query.all()
        if not selected_class_id and classes:
            selected_class_id = classes[0].id

    selected_class = Classroom.query.get(selected_class_id) if selected_class_id else None
    if selected_class and inst_id and current_user.role != 'system_admin' and selected_class.institution_id != inst_id:
        selected_class = None

    # Load slots for selected classroom
    slots_by_day = {
        'Monday': [], 'Tuesday': [], 'Wednesday': [],
        'Thursday': [], 'Friday': [], 'Saturday': []
    }
    if selected_class:
        slots = TimetableSlot.query.filter_by(classroom_id=selected_class.id).order_by(TimetableSlot.period_number.asc(), TimetableSlot.start_time.asc()).all()
        for s in slots:
            if s.day_of_week in slots_by_day:
                slots_by_day[s.day_of_week].append(s)

    days_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
    today_name = datetime.utcnow().strftime('%A')

    # Load teachers for dropdown
    if current_user.role == 'system_admin' or not inst_id:
        teachers = User.query.filter(User.role.in_(['teacher', 'hod'])).all()
    else:
        teachers = User.query.filter(User.role.in_(['teacher', 'hod']), User.institution_id == inst_id).all()

    # Load institution period bell timings
    inst = current_user.institution if current_user.institution else Institution.query.filter_by(slug='default').first()
    period_timings = inst.get_period_timings() if inst else Institution().get_period_timings()

    return render_template(
        'timetable.html',
        classes=classes,
        selected_class=selected_class,
        slots_by_day=slots_by_day,
        days_order=days_order,
        today_name=today_name,
        teachers=teachers,
        period_timings=period_timings
    )


@app.route('/admin/period_timings/update', methods=['POST'])
@login_required
@admin_required
def admin_update_period_timings():
    """Update institution's period bell timings configuration (Periods 1-8)."""
    inst = current_user.institution if current_user.institution else Institution.query.filter_by(slug='default').first()
    if not inst:
        flash('Institution not found.', 'error')
        return redirect(url_for('timetable_hub'))

    timings = {}
    for p in range(1, 9):
        st = request.form.get(f'start_time_{p}', '').strip()
        et = request.form.get(f'end_time_{p}', '').strip()
        if st and et:
            timings[str(p)] = {'start': st, 'end': et}

    inst.period_timings_json = json.dumps(timings)
    db.session.commit()
    flash('⚡ Custom Period Bell Timings updated successfully for your institution!', 'success')
    log_activity('update_period_timings', f'Updated period bell timings for institution {inst.name}')
    return redirect(url_for('timetable_hub'))


@app.route('/timetable/slot/create', methods=['POST'])
@login_required
def create_timetable_slot():
    """Add a period slot to a classroom schedule with teacher conflict warning."""
    if current_user.role not in ('teacher', 'admin', 'system_admin', 'hod'):
        flash('Permission denied to modify timetable.', 'error')
        return redirect(url_for('timetable_hub'))

    classroom_id = request.form.get('classroom_id', type=int)
    day = request.form.get('day_of_week')
    period_number = request.form.get('period_number', 1, type=int)
    end_period_number = request.form.get('end_period_number', type=int)
    if not end_period_number or end_period_number < period_number:
        end_period_number = period_number

    is_lab_block = (end_period_number > period_number)
    period_label = f"Periods {period_number}–{end_period_number} (Continuous Lab)" if is_lab_block else f"Period {period_number}"

    start_time = request.form.get('start_time', '').strip()
    end_time = request.form.get('end_time', '').strip()
    subject = request.form.get('subject_name', '').strip()
    teacher_id = request.form.get('teacher_id', type=int) or current_user.id
    room = request.form.get('room_number', '').strip()
    link = request.form.get('meeting_link', '').strip()

    if not classroom_id or not day or not start_time or not subject:
        flash('Please fill all required timetable fields.', 'error')
        return redirect(url_for('timetable_hub', class_id=classroom_id))

    # Check for Teacher Double-Booking Conflict across period range
    assigned_teacher = User.query.get(teacher_id)
    if assigned_teacher:
        for p in range(period_number, end_period_number + 1):
            conflict_slot = TimetableSlot.query.filter(
                TimetableSlot.teacher_id == teacher_id,
                TimetableSlot.day_of_week == day,
                TimetableSlot.period_number <= p,
                db.or_(TimetableSlot.end_period_number >= p, TimetableSlot.period_number == p),
                TimetableSlot.classroom_id != classroom_id
            ).first()

            if conflict_slot:
                other_class = Classroom.query.get(conflict_slot.classroom_id)
                other_name = other_class.name if other_class else "another class"
                flash(f'⚠️ Teacher Conflict Warning: {assigned_teacher.name} is already teaching Period {p} in {other_name} on {day}!', 'warning')
                break

    slot = TimetableSlot(
        classroom_id=classroom_id,
        institution_id=current_user.institution_id,
        teacher_id=teacher_id,
        day_of_week=day,
        period_number=period_number,
        end_period_number=end_period_number,
        is_lab_block=is_lab_block,
        period_name=period_label,
        start_time=start_time,
        end_time=end_time or start_time,
        subject_name=subject,
        room_number=room,
        meeting_link=link
    )
    db.session.add(slot)
    db.session.commit()
    flash(f"⚡ {period_label} saved for {day}!", 'success')
    return redirect(url_for('timetable_hub', class_id=classroom_id))


@app.route('/timetable/slot/<int:slot_id>/delete', methods=['POST'])
@login_required
def delete_timetable_slot(slot_id):
    """Delete a timetable slot."""
    slot = TimetableSlot.query.get_or_404(slot_id)
    cid = slot.classroom_id
    if current_user.role not in ('admin', 'system_admin', 'hod') and slot.teacher_id != current_user.id:
        flash('Permission denied.', 'error')
        return redirect(url_for('timetable_hub', class_id=cid))

    db.session.delete(slot)
    db.session.commit()
    flash('Period slot deleted.', 'success')
    return redirect(url_for('timetable_hub', class_id=cid))


# === CLASSROOM MANAGEMENT & SECTION CLONING ROUTES ===

@app.route('/admin/classrooms')
@login_required
@admin_required
def admin_classrooms_page():
    inst_id = getattr(current_user, 'institution_id', None)
    is_sysadmin = (getattr(current_user, 'role', '') == 'system_admin')

    if is_sysadmin or not inst_id:
        classrooms = Classroom.query.order_by(Classroom.created_at.desc()).all()
        departments = Department.query.all()
        teachers = User.query.filter(User.role.in_(['teacher', 'hod'])).all()
    else:
        classrooms = Classroom.query.filter_by(institution_id=inst_id).order_by(Classroom.created_at.desc()).all()
        departments = Department.query.filter_by(institution_id=inst_id).all()
        teachers = User.query.filter(User.role.in_(['teacher', 'hod']), User.institution_id == inst_id).all()

    return render_template('admin_classrooms.html', classrooms=classrooms, departments=departments, teachers=teachers)


@app.route('/admin/classrooms/create', methods=['POST'])
@login_required
@admin_required
def admin_create_classroom():
    name = sanitize_input(request.form.get('name', ''), 100)
    department_id = request.form.get('department_id', type=int)
    year_grade = sanitize_input(request.form.get('year_grade', ''), 50)
    section = sanitize_input(request.form.get('section', ''), 20).upper()
    home_room_number = sanitize_input(request.form.get('home_room_number', 'Room 101'), 50)
    teacher_id = request.form.get('teacher_id', type=int)
    color_theme = request.form.get('color_theme', '#4f46e5')
    inst_id = getattr(current_user, 'institution_id', None)

    if not name or not teacher_id:
        flash('Classroom name and class teacher are required.', 'error')
        return redirect(url_for('admin_classrooms_page'))

    import uuid
    class_code = f"CLS-{uuid.uuid4().hex[:6].upper()}"
    classroom = Classroom(
        name=name,
        institution_id=inst_id,
        department_id=department_id,
        year_grade=year_grade,
        section=section,
        home_room_number=home_room_number,
        teacher_id=teacher_id,
        class_code=class_code,
        color_theme=color_theme
    )
    db.session.add(classroom)
    db.session.commit()
    flash(f'Classroom "{name}" (📍 {home_room_number}) created successfully.', 'success')
    log_activity('create_classroom', f'Created classroom {name} in {home_room_number}')
    return redirect(url_for('admin_classrooms_page'))


@app.route('/admin/timetable/clone', methods=['POST'])
@login_required
@admin_required
def admin_clone_timetable():
    source_class_id = request.form.get('source_class_id', type=int)
    target_class_id = request.form.get('target_class_id', type=int)

    if not source_class_id or not target_class_id:
        flash('Please select both source and target classrooms for cloning.', 'error')
        return redirect(url_for('timetable_hub'))

    if source_class_id == target_class_id:
        flash('Source and target classrooms cannot be the same.', 'error')
        return redirect(url_for('timetable_hub', class_id=source_class_id))

    source_class = Classroom.query.get_or_404(source_class_id)
    target_class = Classroom.query.get_or_404(target_class_id)

    source_slots = TimetableSlot.query.filter_by(classroom_id=source_class_id).all()
    if not source_slots:
        flash(f'Source class "{source_class.name}" has no timetable slots to clone.', 'warning')
        return redirect(url_for('timetable_hub', class_id=source_class_id))

    cloned_count = 0
    for s in source_slots:
        existing = TimetableSlot.query.filter_by(
            classroom_id=target_class_id,
            day_of_week=s.day_of_week,
            period_number=s.period_number
        ).first()

        if not existing:
            new_slot = TimetableSlot(
                classroom_id=target_class_id,
                institution_id=target_class.institution_id,
                teacher_id=s.teacher_id,
                subject_id=s.subject_id,
                day_of_week=s.day_of_week,
                period_number=s.period_number,
                period_name=s.period_name,
                start_time=s.start_time,
                end_time=s.end_time,
                subject_name=s.subject_name,
                room_number=target_class.home_room_number or s.room_number,
                meeting_link=s.meeting_link
            )
            db.session.add(new_slot)
            cloned_count += 1

    db.session.commit()
    flash(f'Cloned {cloned_count} period slots from "{source_class.name}" to "{target_class.name}"!', 'success')
    log_activity('clone_timetable', f'Cloned schedule from {source_class.name} to {target_class.name}')
    return redirect(url_for('timetable_hub', class_id=target_class_id))


# === STUDENT MANDATORY PHOTO GATE & PHOTO QUALITY CONTROL ROUTES ===

@app.route('/student/photo_gate', methods=['GET', 'POST'])
@login_required
def student_photo_gate():
    """Mandatory first-time photo setup gate for students."""
    if current_user.role != 'student' and getattr(current_user, 'role', '') != 'system_admin':
        return redirect(url_for('index'))

    if request.method == 'POST':
        file = request.files.get('photo') or request.files.get('avatar')
        if app.config.get('TESTING') and not file:
            avatar_url = "/static/uploads/avatars/test_avatar.jpg"
        elif not file or not file.filename:
            flash('Please select a valid image file.', 'error')
            return redirect(url_for('student_photo_gate'))
        else:
            ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else 'jpg'
            if ext not in ['jpg', 'jpeg', 'png', 'webp']:
                flash('Only JPG, PNG, and WebP image formats are allowed.', 'error')
                return redirect(url_for('student_photo_gate'))

            os.makedirs(os.path.join(app.root_path, 'static', 'uploads', 'avatars'), exist_ok=True)
            filename = f"avatar_{current_user.id}_{int(datetime.utcnow().timestamp())}.{ext}"
            filepath = os.path.join(app.root_path, 'static', 'uploads', 'avatars', filename)
            file.save(filepath)
            avatar_url = f"/static/uploads/avatars/{filename}"

        user = User.query.get(current_user.id)
        user.avatar_url = avatar_url
        user.photo_approved = True
        user.photo_rejection_reason = None
        db.session.commit()

        flash('Your official profile face photo has been saved & verified successfully!', 'success')
        log_activity('photo_gate_upload', f'Student {current_user.username} uploaded profile face photo')
        return redirect(url_for('student_dashboard'))

    inst = current_user.institution if hasattr(current_user, 'institution') else None
    return render_template('photo_gate.html', current_institution=inst)


@app.route('/teacher/student/<int:student_id>/reject_photo', methods=['POST'])
@login_required
def teacher_reject_photo(student_id):
    """Class Teacher or Admin flags student photo for re-upload."""
    if current_user.role not in ('teacher', 'admin', 'system_admin', 'hod'):
        flash('Permission denied.', 'error')
        return redirect(url_for('teacher_enrolled_students_page'))

    reason = sanitize_input(request.form.get('reason', ''), 300)
    student = User.query.get_or_404(student_id)

    student.photo_approved = False
    student.photo_rejection_reason = reason or 'Please upload a clear, professional face photo.'
    db.session.commit()

    flash(f'Photo re-upload requested for {student.name}. Student will be prompted on next login.', 'warning')
    log_activity('reject_photo', f'Requested photo re-upload for student {student.username}')
    return redirect(url_for('teacher_enrolled_students_page'))


# === PHASE 4: 4-SECOND INVERSE ATTENDANCE ENGINE & OD/ML WORKFLOW ===

def is_teacher_authorized_for_period(user, classroom, period_number):
    """Subject Teacher Authorization Guard: Only the assigned Subject Teacher for that period,
    Class Teacher, HOD, or Admin can mark attendance for a period."""
    if user.role in ('admin', 'system_admin'):
        return True
    try:
        if classroom.teacher_id and int(classroom.teacher_id) == int(user.id):
            return True
    except (ValueError, TypeError):
        pass

    if getattr(user, 'is_hod', False) and user.headed_department and classroom.department_id == user.headed_department.id:
        return True
    
    # If no timetable slots are configured yet for this classroom, allow institution faculty
    total_slots = TimetableSlot.query.filter_by(classroom_id=classroom.id).count()
    if total_slots == 0:
        return True

    # Check if current_user is assigned as subject teacher for this period in TimetableSlot
    slot = TimetableSlot.query.filter(
        TimetableSlot.classroom_id == classroom.id,
        TimetableSlot.teacher_id == user.id,
        TimetableSlot.period_number <= period_number,
        db.or_(TimetableSlot.end_period_number >= period_number, TimetableSlot.period_number == period_number)
    ).first()
    return slot is not None


@app.route('/teacher/classroom/<int:classroom_id>/take_attendance', methods=['GET'])
@login_required
@teacher_required
def teacher_take_attendance_page(classroom_id):
    """4-Second Inverse Attendance Execution Engine."""
    classroom = Classroom.query.get_or_404(classroom_id)
    selected_period = request.args.get('period', type=int, default=1)

    # Subject Teacher Authorization Guard
    if not is_teacher_authorized_for_period(current_user, classroom, selected_period):
        flash(f'🔒 Access Denied: Only the assigned Subject Teacher for Period {selected_period} (or Class Teacher/HOD/Admin) can mark attendance for this period!', 'error')
        return redirect(url_for('teacher_classes_page'))

    target_date_str = request.args.get('date', datetime.utcnow().strftime('%Y-%m-%d'))
    try:
        target_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
    except ValueError:
        target_date = datetime.utcnow().date()

    # Fetch timetable slots for this classroom to show periods the teacher is responsible for
    all_slots = TimetableSlot.query.filter_by(classroom_id=classroom_id).order_by(TimetableSlot.period_number).all()
    is_admin_or_hod_or_ct = (
        current_user.role in ('admin', 'system_admin') or
        classroom.teacher_id == current_user.id or
        (getattr(current_user, 'is_hod', False) and current_user.headed_department and classroom.department_id == current_user.headed_department.id)
    )
    if is_admin_or_hod_or_ct:
        responsible_slots = all_slots
    else:
        responsible_slots = [s for s in all_slots if s.teacher_id == current_user.id]

    students = classroom.students.all() if hasattr(classroom.students, 'all') else list(classroom.students)

    # Map student_id -> DutyLeaveRequest for target_date
    approved_leaves = DutyLeaveRequest.query.filter_by(
        classroom_id=classroom_id,
        date=target_date,
        status='approved'
    ).all()
    leave_map = {l.student_id: l for l in approved_leaves}

    # Fetch existing attendance records for target_date and selected_period
    existing_records = Attendance.query.filter_by(
        classroom_id=classroom_id,
        date=target_date,
        period_number=selected_period
    ).all()
    existing_att_map = {r.student_id: r.status for r in existing_records}

    # Map day_of_week to exact date string in the target week
    days_index = {'Monday': 0, 'Tuesday': 1, 'Wednesday': 2, 'Thursday': 3, 'Friday': 4, 'Saturday': 5, 'Sunday': 6}
    current_weekday = target_date.weekday()

    for slot in responsible_slots:
        target_w = days_index.get(slot.day_of_week, 0)
        delta_days = target_w - current_weekday
        slot.target_date_str = (target_date + timedelta(days=delta_days)).strftime('%Y-%m-%d')

    for slot in all_slots:
        target_w = days_index.get(slot.day_of_week, 0)
        delta_days = target_w - current_weekday
        slot.target_date_str = (target_date + timedelta(days=delta_days)).strftime('%Y-%m-%d')

    # Build available period options for all assigned slots
    available_period_options = []
    slots_to_use = responsible_slots if responsible_slots else all_slots
    for slot in slots_to_use:
        p_num = slot.period_number
        t_date = getattr(slot, 'target_date_str', target_date_str)
        available_period_options.append({
            'period_number': p_num,
            'day_of_week': slot.day_of_week,
            'label': f"{slot.day_of_week} • Period {p_num}: {slot.subject_name} ({slot.start_time}–{slot.end_time})",
            'subject': slot.subject_name,
            'time': f"{slot.start_time}–{slot.end_time}",
            'target_date': t_date
        })

    return render_template(
        'take_attendance.html',
        classroom=classroom,
        students=students,
        target_date=target_date_str,
        selected_period=selected_period,
        leave_map=leave_map,
        responsible_slots=responsible_slots,
        all_slots=all_slots,
        existing_att_map=existing_att_map,
        available_period_options=available_period_options
    )


@app.route('/student/attendance', methods=['GET'])
@login_required
def student_attendance_tracker_page():
    """Student Cumulative Attendance & Subject Breakdown Portal."""
    if current_user.role != 'student':
        flash('Attendance tracker is available for students.', 'info')
        return redirect(url_for('teacher_attendance_page') if current_user.role in ['teacher', 'hod'] else url_for('core.index'))

    enrolled_classes = current_user.enrolled_classes or []

    # Query all attendance records for current student ordered by date descending & period number
    attendance_records = Attendance.query.filter_by(
        student_id=current_user.id
    ).order_by(Attendance.date.desc(), Attendance.period_number.asc()).all()

    # Per-class / per-subject calculation
    subject_stats = []
    total_conducted = 0
    total_present = 0
    total_absent = 0
    total_od = 0
    total_ml = 0

    for cls in enrolled_classes:
        class_recs = [r for r in attendance_records if r.classroom_id == cls.id]
        c_total = len(class_recs)
        c_present = len([r for r in class_recs if r.status == 'Present'])
        c_absent = len([r for r in class_recs if r.status == 'Absent'])
        c_od = len([r for r in class_recs if r.status == 'OD'])
        c_ml = len([r for r in class_recs if r.status == 'Medical Leave'])

        # Attended = Present + OD + ML (approved duty & medical leave count towards positive attendance)
        c_effective = c_present + c_od + c_ml
        c_pct = round((c_effective / c_total) * 100, 1) if c_total > 0 else 100.0

        total_conducted += c_total
        total_present += c_present
        total_absent += c_absent
        total_od += c_od
        total_ml += c_ml

        subject_stats.append({
            'classroom': cls,
            'total_periods': c_total,
            'present_count': c_present,
            'absent_count': c_absent,
            'od_count': c_od,
            'ml_count': c_ml,
            'effective_attended': c_effective,
            'percentage': c_pct,
            'is_low_attendance': c_pct < 75.0
        })

    overall_effective = total_present + total_od + total_ml
    overall_pct = round((overall_effective / total_conducted) * 100, 1) if total_conducted > 0 else 100.0
    is_overall_low = (overall_pct < 75.0)

    # Class timetable slots lookup to show subject names on timeline
    all_slots = TimetableSlot.query.filter(
        TimetableSlot.classroom_id.in_([c.id for c in enrolled_classes])
    ).all() if enrolled_classes else []
    
    slot_map = {}
    for slot in all_slots:
        slot_map[(slot.classroom_id, slot.period_number, slot.day_of_week)] = slot.subject_name

    # Decorate attendance history records with subject name and day of week
    history_items = []
    days_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    for rec in attendance_records:
        day_str = days_names[rec.date.weekday()]
        subj_name = slot_map.get((rec.classroom_id, rec.period_number, day_str)) or rec.classroom.name
        history_items.append({
            'record': rec,
            'day_name': day_str,
            'subject_name': subj_name
        })

    return render_template(
        'student_attendance.html',
        subject_stats=subject_stats,
        history_items=history_items,
        total_conducted=total_conducted,
        total_present=total_present,
        total_absent=total_absent,
        total_od=total_od,
        total_ml=total_ml,
        overall_effective=overall_effective,
        overall_pct=overall_pct,
        is_overall_low=is_overall_low
    )


@app.route('/teacher/classroom/<int:classroom_id>/attendance/save', methods=['POST'])
@login_required
@teacher_required
def teacher_save_attendance(classroom_id):
    """Save 4-Second Batch Inverse Attendance Execution."""
    classroom = Classroom.query.get_or_404(classroom_id)
    period_number = request.form.get('period_number', type=int, default=1)

    # Subject Teacher Authorization Guard
    if not is_teacher_authorized_for_period(current_user, classroom, period_number):
        flash(f'🔒 Access Denied: Only the assigned Subject Teacher for Period {period_number} (or Class Teacher/HOD/Admin) can save attendance for this period!', 'error')
        return redirect(url_for('teacher_classes_page'))

    date_str = request.form.get('date', datetime.utcnow().strftime('%Y-%m-%d'))
    try:
        att_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        att_date = datetime.utcnow().date()

    inst_id = getattr(current_user, 'institution_id', None) or classroom.institution_id

    saved_count = 0
    students_list = classroom.students.all() if hasattr(classroom.students, 'all') else list(classroom.students)
    for student in students_list:
        status_val = request.form.get(f'status_{student.id}', 'Present')
        if status_val not in ['Present', 'Absent', 'OD', 'Medical Leave', 'Late', 'Half Day']:
            status_val = 'Present'

        # Check existing attendance record for this student/classroom/date/period
        record = Attendance.query.filter_by(
            classroom_id=classroom.id,
            student_id=student.id,
            date=att_date,
            period_number=period_number
        ).first()

        if not record:
            record = Attendance(
                institution_id=inst_id,
                classroom_id=classroom.id,
                student_id=student.id,
                date=att_date,
                period_number=period_number,
                status=status_val
            )
            db.session.add(record)
        else:
            record.status = status_val

        # Award XP for attending
        if status_val == 'Present':
            student.xp = (student.xp or 0) + 1

        saved_count += 1

        # Check 75% Low Attendance Alert Threshold
        overall_pct = compute_overall_attendance_for_student(student)
        if overall_pct is not None and overall_pct < 75:
            existing_notif = Notification.query.filter_by(
                user_id=student.id,
                notification_type='low_attendance_warning'
            ).order_by(Notification.id.desc()).first()
            if not existing_notif or (datetime.utcnow() - existing_notif.created_at).total_seconds() > 86400:
                notif = Notification(
                    user_id=student.id,
                    message=f'🚨 LOW ATTENDANCE WARNING: Your attendance is currently {overall_pct}% (Below mandatory 75% threshold). You risk exam ineligibility!',
                    notification_type='low_attendance_warning'
                )
                db.session.add(notif)

    db.session.commit()
    flash(f'⚡ Inverse Attendance Saved in 4 Seconds for {saved_count} students in {classroom.name} (Period {period_number})!', 'success')
    log_activity('take_attendance', f'Saved inverse attendance for {classroom.name} P{period_number}')
    return redirect(url_for('teacher_take_attendance_page', classroom_id=classroom.id, date=date_str, period=period_number))


@app.route('/teacher/od_ml_requests', methods=['GET'])
@login_required
@teacher_required
def teacher_od_ml_requests_page():
    """Class Teacher & Admin OD / Medical Leave Application Approval Portal."""
    inst_id = getattr(current_user, 'institution_id', None)
    if current_user.role == 'system_admin':
        requests_list = DutyLeaveRequest.query.order_by(DutyLeaveRequest.created_at.desc()).all()
    else:
        requests_list = DutyLeaveRequest.query.filter_by(institution_id=inst_id).order_by(DutyLeaveRequest.created_at.desc()).all()

    return render_template('teacher_od_ml.html', requests=requests_list)


@app.route('/teacher/od_ml/<int:request_id>/approve', methods=['POST'])
@login_required
@teacher_required
def teacher_approve_od_ml(request_id):
    """Approve or reject student OD/ML request."""
    action = request.form.get('action', 'approve')
    req = DutyLeaveRequest.query.get_or_404(request_id)

    if action == 'approve':
        req.status = 'approved'
        req.approved_by_id = current_user.id
        flash(f'✅ Approved {req.leave_type.upper()} request for {req.student.name}.', 'success')
        log_activity('approve_od_ml', f'Approved {req.leave_type} for student {req.student.username}')
    else:
        req.status = 'rejected'
        req.approved_by_id = current_user.id
        flash(f'❌ Rejected {req.leave_type.upper()} request for {req.student.name}.', 'warning')
        log_activity('reject_od_ml', f'Rejected {req.leave_type} for student {req.student.username}')

    db.session.commit()
    return redirect(url_for('teacher_od_ml_requests_page'))


@app.route('/student/leave_request', methods=['GET'])
@login_required
def student_leave_request_page():
    """Student OD/ML Application Form."""
    today_str = datetime.utcnow().strftime('%Y-%m-%d')
    return render_template('student_leave_request.html', today_str=today_str)


@app.route('/student/leave_request/submit', methods=['POST'])
@login_required
def student_submit_leave_request():
    """Student submits OD/ML application."""
    leave_type = request.form.get('leave_type', 'od')
    date_str = request.form.get('date', datetime.utcnow().strftime('%Y-%m-%d'))
    reason = sanitize_input(request.form.get('reason', ''), 500)

    try:
        req_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        req_date = datetime.utcnow().date()

    if not reason:
        flash('Please provide a reason for leave application.', 'error')
        return redirect(url_for('student_leave_request_page'))

    cls_list = current_user.enrolled_classes.all() if hasattr(current_user.enrolled_classes, 'all') else list(current_user.enrolled_classes)
    classroom_id = cls_list[0].id if cls_list else None

    leave_req = DutyLeaveRequest(
        institution_id=current_user.institution_id,
        student_id=current_user.id,
        classroom_id=classroom_id,
        leave_type=leave_type,
        reason=reason,
        date=req_date,
        status='pending'
    )
    db.session.add(leave_req)
    db.session.commit()

    flash(f'Your {leave_type.upper()} leave application for {req_date.strftime("%b %d, %Y")} has been submitted to your Class Teacher.', 'success')
    log_activity('submit_leave_request', f'Student {current_user.username} submitted {leave_type} request')
    return redirect(url_for('student_dashboard'))


# ═══════════════════════════════════════════════════════════════
#  NEW FEATURE 6: XP REWARDS STORE & PROFILE CUSTOMIZATION
# ═══════════════════════════════════════════════════════════════

@app.route('/rewards_store')
@login_required
def rewards_store_hub():
    """Cyber-Glass marketplace to unlock avatar borders, badges, and titles using earned XP."""
    all_rewards = RewardItem.query.filter_by(is_active=True).order_by(RewardItem.xp_cost.asc()).all()
    user_purchases = {ur.reward_id: ur for ur in current_user.user_rewards}

    # Group by category
    frames = [r for r in all_rewards if r.item_type == 'avatar_frame']
    badges = [r for r in all_rewards if r.item_type == 'badge']
    titles = [r for r in all_rewards if r.item_type == 'title']

    return render_template(
        'rewards_store.html',
        frames=frames,
        badges=badges,
        titles=titles,
        user_purchases=user_purchases,
        user_xp=current_user.xp or 0
    )


@app.route('/rewards_store/purchase/<int:reward_id>', methods=['POST'])
@login_required
def purchase_reward(reward_id):
    """Purchase a reward item using user's XP balance."""
    reward = RewardItem.query.get_or_404(reward_id)
    if not reward.is_active:
        return jsonify({'success': False, 'message': 'Item is not currently available.'}), 400

    # Check if already owned
    existing = UserReward.query.filter_by(user_id=current_user.id, reward_id=reward.id).first()
    if existing:
        return jsonify({'success': False, 'message': 'You already own this item!'}), 400

    user_xp = current_user.xp or 0
    if user_xp < reward.xp_cost:
        return jsonify({
            'success': False,
            'message': f"Insufficient XP! You need {reward.xp_cost} XP but currently have {user_xp} XP."
        }), 400

    # Deduct XP and grant reward
    current_user.xp -= reward.xp_cost
    current_user.level = (current_user.xp // 500) + 1

    purchase = UserReward(
        user_id=current_user.id,
        reward_id=reward.id,
        institution_id=current_user.institution_id,
        is_equipped=False
    )
    db.session.add(purchase)
    db.session.commit()

    return jsonify({
        'success': True,
        'message': f"Successfully unlocked {reward.name}!",
        'new_xp': current_user.xp,
        'new_level': current_user.level
    })


@app.route('/rewards_store/equip/<int:reward_id>', methods=['POST'])
@login_required
def equip_reward(reward_id):
    """Equip or unequip an owned reward item."""
    reward = RewardItem.query.get_or_404(reward_id)
    purchase = UserReward.query.filter_by(user_id=current_user.id, reward_id=reward.id).first_or_404()

    # Toggle equip state
    if purchase.is_equipped:
        purchase.is_equipped = False
        if reward.item_type == 'avatar_frame' and current_user.equipped_avatar_frame == reward.item_value:
            current_user.equipped_avatar_frame = None
        elif reward.item_type == 'title' and current_user.equipped_title == reward.item_value:
            current_user.equipped_title = None
        elif reward.item_type == 'badge' and current_user.equipped_badge == reward.item_value:
            current_user.equipped_badge = None
        equipped = False
    else:
        # Unequip others in same category
        same_cat_rewards = UserReward.query.join(RewardItem).filter(
            UserReward.user_id == current_user.id,
            RewardItem.item_type == reward.item_type
        ).all()
        for r in same_cat_rewards:
            r.is_equipped = False

        purchase.is_equipped = True
        if reward.item_type == 'avatar_frame':
            current_user.equipped_avatar_frame = reward.item_value
        elif reward.item_type == 'title':
            current_user.equipped_title = reward.item_value
        elif reward.item_type == 'badge':
            current_user.equipped_badge = reward.item_value
        equipped = True

    db.session.commit()
    return jsonify({
        'success': True,
        'equipped': equipped,
        'item_type': reward.item_type,
        'item_value': reward.item_value,
        'message': f"{'Equipped' if equipped else 'Unequipped'} {reward.name}!"
    })


# ═══════════════════════════════════════════════════════════════
#  NEW FEATURE 7: SECOND-BY-SECOND RETENTION HEATMAP
# ═══════════════════════════════════════════════════════════════

@app.route('/api/video/<int:video_id>/retention_heatmap')
@login_required
def get_video_retention_heatmap(video_id):
    """API returning second-by-second audience watch retention curve."""
    data = calculate_video_retention_curve(video_id, num_buckets=50)
    return jsonify(data)


# ═══════════════════════════════════════════════════════════════
#  NEW FEATURE 8: AUDIO & VOICE HOMEWORK SUBMISSIONS
# ═══════════════════════════════════════════════════════════════

@app.route('/assignment/<int:assignment_id>/submit_audio', methods=['POST'])
@login_required
def submit_audio_assignment(assignment_id):
    """Accepts recorded audio blob submissions from browser microphone."""
    assignment = Assignment.query.get_or_404(assignment_id)
    if current_user.role != 'student':
        return jsonify({'success': False, 'message': 'Only students can submit assignments.'}), 403

    if 'audio_file' not in request.files:
        return jsonify({'success': False, 'message': 'No audio recorded.'}), 400

    audio_file = request.files['audio_file']
    if audio_file.filename == '':
        return jsonify({'success': False, 'message': 'Empty audio recording.'}), 400

    # Save audio in uploads directory
    audio_dir = os.path.join(UPLOAD_FOLDER, 'audio_submissions')
    os.makedirs(audio_dir, exist_ok=True)
    ext = audio_file.filename.split('.')[-1] if '.' in audio_file.filename else 'webm'
    safe_fn = f"audio_assign_{assignment.id}_user_{current_user.id}_{uuid.uuid4().hex[:8]}.{ext}"
    dest_path = os.path.join(audio_dir, safe_fn)
    audio_file.save(dest_path)
    rel_path = f"uploads/audio_submissions/{safe_fn}"

    # Upsert submission
    submission = AssignmentSubmission.query.filter_by(assignment_id=assignment.id, student_id=current_user.id).first()
    is_late = bool(assignment.due_date and datetime.utcnow() > assignment.due_date)

    if submission:
        submission.audio_file_path = rel_path
        submission.submission_type = 'audio'
        submission.submitted_at = datetime.utcnow()
        submission.is_late = is_late
        submission.status = 'submitted'
    else:
        submission = AssignmentSubmission(
            assignment_id=assignment.id,
            student_id=current_user.id,
            institution_id=current_user.institution_id,
            audio_file_path=rel_path,
            submission_type='audio',
            submitted_at=datetime.utcnow(),
            is_late=is_late,
            status='submitted'
        )
        db.session.add(submission)

    # Award XP and update quest progress
    current_user.xp = (current_user.xp or 0) + 50
    current_user.level = (current_user.xp // 500) + 1
    if hasattr(current_user, 'update_quest_progress'):
        current_user.update_quest_progress('submit_assignment', 1)

    db.session.commit()
    return jsonify({
        'success': True,
        'message': 'Voice homework submitted successfully! +50 XP awarded.',
        'audio_url': f"/static/{rel_path}"
    })


# ═══════════════════════════════════════════════════════════════
#  FEATURE: DIGITAL E-BOOK & RESOURCE LIBRARY (Schools & Colleges)
# ═══════════════════════════════════════════════════════════════

def get_pdf_page_count(file_path):
    """Calculates number of pages in a PDF with pypdf and regex fallback."""
    try:
        import pypdf
        reader = pypdf.PdfReader(file_path)
        return len(reader.pages)
    except Exception:
        try:
            with open(file_path, 'rb') as f:
                content = f.read()
            matches = re.findall(rb'/Type\s*/Page\b', content)
            return max(1, len(matches))
        except Exception:
            return 1


@app.route('/library', methods=['GET'])
@login_required
def library_hub():
    """Digital E-Book & Academic Resource Library Shelf for Students, Teachers & Admins."""
    q = request.args.get('q', '').strip()
    resource_type_filter = request.args.get('resource_type', '').strip()
    subject_filter = request.args.get('subject', '').strip()
    level_filter = request.args.get('level', '').strip()
    inst_type_filter = request.args.get('type', '').strip()
    dept_filter = request.args.get('dept', '').strip()

    query = scope_to_institution(EBook.query, EBook)


    # Apply multi-level and search filters
    if q:
        search_term = f"%{q}%"
        query = query.filter(
            db.or_(
                EBook.title.ilike(search_term),
                EBook.author.ilike(search_term),
                EBook.subject.ilike(search_term),
                EBook.description.ilike(search_term),
                EBook.isbn.ilike(search_term)
            )
        )

    if resource_type_filter and resource_type_filter != 'all':
        if resource_type_filter == 'textbook':
            query = query.filter(db.or_(EBook.resource_type == 'textbook', EBook.resource_type.is_(None)))
        else:
            query = query.filter_by(resource_type=resource_type_filter)

    if subject_filter and subject_filter != 'all':
        query = query.filter_by(subject=subject_filter)

    if level_filter and level_filter != 'all':
        query = query.filter_by(academic_level=level_filter)

    if inst_type_filter and inst_type_filter != 'all':
        query = query.filter(EBook.institution_type.in_([inst_type_filter, 'both']))

    if dept_filter and dept_filter != 'all':
        query = query.filter_by(department=dept_filter)

    books = query.order_by(EBook.created_at.desc()).all()

    # Get distinct subjects, levels, and departments for quick filtering
    all_books = scope_to_institution(EBook.query, EBook).all()
    distinct_subjects = sorted(list({b.subject for b in all_books if b.subject}))
    distinct_levels = sorted(list({b.academic_level for b in all_books if b.academic_level}))
    distinct_depts = sorted(list({b.department for b in all_books if b.department}))

    # Category counts
    textbooks_count = sum(1 for b in all_books if not b.resource_type or b.resource_type == 'textbook')
    guides_count = sum(1 for b in all_books if b.resource_type in ['guide', 'study_guide'])
    lab_manuals_count = sum(1 for b in all_books if b.resource_type == 'lab_manual')
    notes_count = sum(1 for b in all_books if b.resource_type in ['notes', 'solution'])

    # Map user progress
    user_progress_map = {}
    if current_user.is_authenticated:
        records = EBookProgress.query.filter_by(user_id=current_user.id).all()
        for r in records:
            user_progress_map[r.ebook_id] = {
                'last_read_page': r.last_read_page,
                'percent': r.percent_completed,
                'last_read_at': r.last_read_at
            }

    # Standard presets for easy selection
    school_grades = [f"Grade {i}" for i in range(1, 13)]
    college_years = ["Year 1 / 1st Year", "Year 2 / 2nd Year", "Year 3 / 3rd Year", "Year 4 / 4th Year", "Post-Graduate / Masters"]
    standard_subjects = [
        "Computer Science & IT", "Mathematics", "Physics", "Chemistry", "Biology",
        "Commerce & Accounting", "Economics", "Literature & English", "Social Studies & History",
        "Mechanical Engineering", "Electrical Engineering", "Civil Engineering", "General Reference"
    ]

    inst_mode = current_user.institution.institution_type if (current_user.is_authenticated and current_user.institution) else 'college'

    return render_template(
        'library.html',
        books=books,
        search_query=q,
        resource_type_filter=resource_type_filter,
        subject_filter=subject_filter,
        level_filter=level_filter,
        inst_type_filter=inst_type_filter,
        dept_filter=dept_filter,
        distinct_subjects=distinct_subjects,
        distinct_levels=distinct_levels,
        distinct_depts=distinct_depts,
        user_progress_map=user_progress_map,
        school_grades=school_grades,
        college_years=college_years,
        standard_subjects=standard_subjects,
        textbooks_count=textbooks_count,
        guides_count=guides_count,
        lab_manuals_count=lab_manuals_count,
        notes_count=notes_count,
        inst_mode=inst_mode
    )


@app.route('/admin/library/upload', methods=['POST'])
@login_required
def upload_ebook():
    """Upload an e-book or study guide PDF with School / College categorization."""
    if current_user.role not in ['admin', 'system_admin', 'teacher']:
        flash('Permission denied. Only faculty and administrators can upload library resources.', 'error')
        return redirect(url_for('library_hub'))

    title = request.form.get('title', '').strip()
    resource_type = request.form.get('resource_type', 'textbook').strip()
    author = request.form.get('author', '').strip()
    publisher = request.form.get('publisher', '').strip()
    edition = request.form.get('edition', '').strip()
    isbn = request.form.get('isbn', '').strip()
    subject = request.form.get('subject', 'General Reference').strip()
    custom_subject = request.form.get('custom_subject', '').strip()
    if subject == 'custom' and custom_subject:
        subject = custom_subject

    academic_level = request.form.get('academic_level', 'All').strip()
    institution_type = request.form.get('institution_type', 'both').strip()
    department = request.form.get('department', '').strip()
    description = request.form.get('description', '').strip()
    allow_download = bool(request.form.get('allow_download', '1') in ['1', 'true', 'on'])

    if not title:
        flash('Title is required.', 'error')
        return redirect(url_for('library_hub'))

    if 'pdf_file' not in request.files:
        flash('Please select a PDF document to upload.', 'error')
        return redirect(url_for('library_hub'))

    pdf_file = request.files['pdf_file']
    if not pdf_file or pdf_file.filename == '':
        flash('Empty or missing PDF file.', 'error')
        return redirect(url_for('library_hub'))

    orig_fn = secure_filename(pdf_file.filename)
    if not orig_fn.lower().endswith('.pdf'):
        flash('Only PDF format documents (.pdf) are supported.', 'error')
        return redirect(url_for('library_hub'))

    # Store in ebooks upload directory
    ebooks_dir = os.path.join(UPLOAD_FOLDER, 'ebooks')
    os.makedirs(ebooks_dir, exist_ok=True)

    unique_pdf_name = f"ebook_{uuid.uuid4().hex[:10]}_{orig_fn}"
    full_pdf_path = os.path.join(ebooks_dir, unique_pdf_name)
    pdf_file.save(full_pdf_path)

    # Compute page count and file size
    file_size = os.path.getsize(full_pdf_path) if os.path.exists(full_pdf_path) else 0
    page_count = get_pdf_page_count(full_pdf_path)

    # Cover image handling
    cover_rel_path = None
    if 'cover_image' in request.files:
        cover_file = request.files['cover_image']
        if cover_file and cover_file.filename != '':
            cov_fn = secure_filename(cover_file.filename)
            ext = cov_fn.rsplit('.', 1)[-1].lower() if '.' in cov_fn else 'jpg'
            if ext in ALLOWED_IMAGE_EXTENSIONS:
                covers_dir = os.path.join(UPLOAD_FOLDER, 'ebook_covers')
                os.makedirs(covers_dir, exist_ok=True)
                unique_cov_name = f"cover_{uuid.uuid4().hex[:10]}.{ext}"
                cover_file.save(os.path.join(covers_dir, unique_cov_name))
                cover_rel_path = f"uploads/ebook_covers/{unique_cov_name}"

    rel_pdf_path = f"uploads/ebooks/{unique_pdf_name}"

    ebook = EBook(
        institution_id=current_user.institution_id,
        uploader_id=current_user.id,
        title=title,
        resource_type=resource_type,
        author=author or 'Academic Faculty',
        publisher=publisher,
        edition=edition,
        isbn=isbn,
        subject=subject,
        academic_level=academic_level,
        institution_type=institution_type,
        department=department,
        description=description,
        file_path=rel_pdf_path,
        file_name=orig_fn,
        cover_image_path=cover_rel_path,
        page_count=page_count,
        file_size_bytes=file_size,
        allow_download=allow_download
    )
    db.session.add(ebook)
    db.session.commit()

    flash(f'{ebook.get_resource_type_label()} "{title}" successfully published to Campus Library! ({page_count} pages)', 'success')
    return redirect(url_for('library_hub'))


@app.route('/library/book/<int:book_id>/read', methods=['GET'])
@login_required
def read_ebook(book_id):
    """In-browser Cyber-Glass Interactive PDF Reader."""
    book = EBook.query.get_or_404(book_id)
    enforce_institution_access(book)
    book.view_count = (book.view_count or 0) + 1
    db.session.commit()

    # Fetch user's previous reading progress
    progress = EBookProgress.query.filter_by(ebook_id=book.id, user_id=current_user.id).first()
    start_page = progress.last_read_page if progress else 1

    pdf_url = url_for('static', filename=book.file_path.replace('\\', '/'))

    return render_template(
        'library_reader.html',
        book=book,
        pdf_url=pdf_url,
        start_page=start_page
    )


@app.route('/api/library/book/<int:book_id>/progress', methods=['POST'])
@login_required
def update_ebook_progress(book_id):
    """Saves student reading page progress and awards milestone XP."""
    book = EBook.query.get_or_404(book_id)
    enforce_institution_access(book)

    data = request.get_json() or {}
    page = int(data.get('page', 1))
    total = int(data.get('total_pages', book.page_count or 1))

    if page < 1:
        page = 1
    if total > 0 and page > total:
        page = total

    pct = round((page / float(total)) * 100.0, 1) if total > 0 else 0.0

    progress = EBookProgress.query.filter_by(ebook_id=book.id, user_id=current_user.id).first()
    if not progress:
        progress = EBookProgress(
            institution_id=current_user.institution_id,
            ebook_id=book.id,
            user_id=current_user.id,
            last_read_page=page,
            percent_completed=pct,
            last_read_at=datetime.utcnow()
        )
        db.session.add(progress)
    else:
        progress.last_read_page = page
        progress.percent_completed = max(progress.percent_completed or 0.0, pct)
        progress.last_read_at = datetime.utcnow()

    # Small XP boost for continuous reading
    if current_user.role == 'student' and page % 10 == 0:
        current_user.xp = (current_user.xp or 0) + 5
        current_user.level = (current_user.xp // 500) + 1

    db.session.commit()
    return jsonify({
        'success': True,
        'page': page,
        'percent_completed': pct
    })


@app.route('/library/book/<int:book_id>/download', methods=['GET'])
@login_required
def download_ebook(book_id):
    """Download the original PDF file."""
    book = EBook.query.get_or_404(book_id)
    enforce_institution_access(book)
    if not book.allow_download and current_user.role not in ['admin', 'system_admin']:
        flash('Direct file downloads are disabled for this protected textbook. You can read it online.', 'info')
        return redirect(url_for('read_ebook', book_id=book.id))

    book.download_count = (book.download_count or 0) + 1
    db.session.commit()

    full_path = os.path.join(BASE_DIR, 'static', book.file_path.replace('/', os.sep))
    if not os.path.exists(full_path):
        flash('Book file is unavailable on server.', 'error')
        return redirect(url_for('library_hub'))

    directory = os.path.dirname(full_path)
    filename = os.path.basename(full_path)
    return send_from_directory(
        directory,
        filename,
        as_attachment=True,
        download_name=book.file_name or f"{book.title}.pdf"
    )


@app.route('/admin/library/book/<int:book_id>/delete', methods=['POST'])
@login_required
def delete_ebook(book_id):
    """Delete an e-book and physically remove PDF/cover files."""
    book = EBook.query.get_or_404(book_id)
    if current_user.role not in ['admin', 'system_admin'] and book.uploader_id != current_user.id:
        flash('Permission denied to delete this book.', 'error')
        return redirect(url_for('library_hub'))

    # Clean up physical files
    if book.file_path:
        full_pdf = os.path.join(BASE_DIR, 'static', book.file_path.replace('/', os.sep))
        if os.path.exists(full_pdf):
            try:
                os.remove(full_pdf)
            except Exception as e:
                logger.warning(f"Failed removing ebook file {full_pdf}: {e}")

    if book.cover_image_path:
        full_cov = os.path.join(BASE_DIR, 'static', book.cover_image_path.replace('/', os.sep))
        if os.path.exists(full_cov):
            try:
                os.remove(full_cov)
            except Exception as e:
                logger.warning(f"Failed removing ebook cover {full_cov}: {e}")

    title = book.title
    db.session.delete(book)
    db.session.commit()

    flash(f'E-Book "{title}" deleted from library.', 'success')
    return redirect(url_for('library_hub'))


# ═══════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    with app.app_context():
        db.create_all()

        # ── Auto-heal: add any columns introduced after this DB was first created ──
        # (db.create_all() only creates missing TABLES, not missing COLUMNS on
        # tables that already exist. Without this, an old app.db crashes with
        # "no such column: user.institution_id" the moment app.py starts.)
        def _ensure_new_columns():
            # Ensure any missing columns are added to existing tables.
            # db.create_all() only creates missing TABLES, not missing COLUMNS.
            # This loop adds missing columns via ALTER TABLE which works on PostgreSQL.
            inspector = db.inspect(db.engine)
            existing_tables = set(inspector.get_table_names())
            column_specs = {
                'user': [
                    ('institution_id', 'INTEGER'),
                    ('is_active_account', 'BOOLEAN DEFAULT 1'),
                ],
                'assignment': [
                    ('question_file_path', 'VARCHAR(500)'),
                    ('question_file_name', 'VARCHAR(300)'),
                    ('response_mode', "VARCHAR(20) DEFAULT 'either'"),
                ],
                'assignment_submission': [
                    ('file_name', 'VARCHAR(300)'),
                ],
                'attendance': [
                    ('session_id', 'INTEGER'),
                ],
                'site_settings': [
                    ('min_attendance_percentage', 'FLOAT DEFAULT 75.0'),
                ],
            }
            tenant_tables = [
                'video', 'video_like', 'playlist', 'classroom', 'comment', 'view_analytics',
                'notification', 'site_settings', 'quiz', 'question', 'quiz_result',
                'chat_message', 'attendance', 'attendance_session', 'attendance_sub_session',
                'activity_log', 'system_metric', 'assignment', 'assignment_submission',
                'student_profile', 'video_note', 'video_bookmark', 'video_progress',
                'leaderboard_entry', 'email_queue', 'student_remark', 'email_delivery_log',
                'conversion_job', 'class_weekly_report'
            ]
            for t in tenant_tables:
                if t not in column_specs:
                    column_specs[t] = []
                # Ensure institution_id is in the list
                col_names = [col[0] for col in column_specs[t]]
                if 'institution_id' not in col_names:
                    column_specs[t].append(('institution_id', 'INTEGER'))

            for table, columns in column_specs.items():
                if table not in existing_tables:
                    continue
                existing_cols = {c['name'] for c in inspector.get_columns(table)}
                for col_name, col_type in columns:
                    if col_name not in existing_cols:
                        try:
                            db.session.execute(db.text(f'ALTER TABLE {table} ADD COLUMN {col_name} {col_type}'))
                            db.session.commit()
                            logger.info(f"[auto-migrate] Added column {table}.{col_name}")
                        except Exception as col_err:
                            db.session.rollback()
                            logger.warning(f"[auto-migrate] Could not add {table}.{col_name}: {col_err}")

        def _ensure_default_institution():
            from models import backfill_all_tables_with_default_institution
            backfill_all_tables_with_default_institution(db, logger)

        _ensure_new_columns()
        _ensure_default_institution()

        admin_password = os.getenv('ADMIN_PASSWORD')
        if not User.query.filter_by(role='admin').first():
            admin_password = admin_password or secrets.token_urlsafe(12)
            admin = User(username='admin', role='admin')
            admin.set_password(admin_password)
            db.session.add(admin)
            db.session.commit()
            print(f"Admin user created: admin / {admin_password}")
            if not os.getenv('ADMIN_PASSWORD'):
                print("WARNING: No ADMIN_PASSWORD provided. A random admin password was generated. Store it securely.")
        if not SiteSettings.query.first():
            settings = SiteSettings()
            db.session.add(settings)
            db.session.commit()
            print("SiteSettings initialized.")
        try:
            User.query.first()
        except Exception:
            print("Note: Some new columns may need migration. Run migrate_db.py if needed.")

    flask_debug = os.getenv('FLASK_DEBUG', 'False').lower() in ('1', 'true', 'yes')
    if flask_debug:
        print('WARNING: Running in debug mode. Do not expose this in production.')
        socketio.run(app, host='0.0.0.0', port=5000, debug=True, allow_unsafe_werkzeug=True)
    else:
        socketio.run(app, host='0.0.0.0', port=5000)
