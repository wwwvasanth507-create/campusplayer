import os
import re
import secrets
from flask import session, request
from models import Video, Playlist, Classroom, Quiz, User

# Allowed cross-origin hosts for media (HLS/subtitle) responses that also set
# Access-Control-Allow-Credentials. `Access-Control-Allow-Origin: *` combined
# with credentials=true is an invalid/insecure combination, so the origin is
# reflected only if it is explicitly allow-listed via the
# MEDIA_ALLOWED_ORIGINS env var (comma-separated). With nothing configured,
# no cross-origin access is granted (same-origin only).
MEDIA_ALLOWED_ORIGINS = {o.strip() for o in os.getenv('MEDIA_ALLOWED_ORIGINS', '').split(',') if o.strip()}


def get_or_create_persistent_secret_key(base_dir):
    """
    Retrieve SECRET_KEY from environment or persist a dedicated key to file/env
    so it survives process, Gunicorn, systemd, and server restarts.
    """
    key = os.getenv('SECRET_KEY') or os.getenv('CAMPUSPLAYER_SECRET_KEY')
    if key and key.strip():
        return key.strip()

    key_file = os.path.join(base_dir, '.secret_key')
    if os.path.exists(key_file):
        try:
            with open(key_file, 'r', encoding='utf-8') as f:
                k = f.read().strip()
                if k:
                    return k
        except Exception:
            pass

    new_key = secrets.token_urlsafe(48)
    try:
        with open(key_file, 'w', encoding='utf-8') as f:
            f.write(new_key + '\n')
    except Exception:
        pass

    env_file = os.path.join(base_dir, '.env')
    if os.path.exists(env_file):
        try:
            with open(env_file, 'r', encoding='utf-8') as f:
                content = f.read()
            if 'SECRET_KEY=' in content:
                lines = content.splitlines()
                updated_lines = []
                for line in lines:
                    if line.strip().startswith('SECRET_KEY='):
                        val = line.split('=', 1)[1].strip()
                        if not val:
                            updated_lines.append(f'SECRET_KEY={new_key}')
                        else:
                            updated_lines.append(line)
                    else:
                        updated_lines.append(line)
                with open(env_file, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(updated_lines) + '\n')
        except Exception:
            pass

    return new_key


def get_current_institution_id():
    """Get active institution_id for current authenticated user."""
    from flask_login import current_user
    if not current_user or not current_user.is_authenticated:
        return None
    if getattr(current_user, 'role', '') == 'system_admin':
        return None
    return getattr(current_user, 'institution_id', None)


def scope_to_institution(query, model_cls=None):
    """
    Filter SQLAlchemy query by current_user's institution_id.
    Bypasses filtering for system_admin users.
    """
    inst_id = get_current_institution_id()
    if inst_id is not None:
        if model_cls is not None and hasattr(model_cls, 'institution_id'):
            return query.filter(model_cls.institution_id == inst_id)
        for desc in getattr(query, 'column_descriptions', []):
            entity = desc.get('entity')
            if entity and hasattr(entity, 'institution_id'):
                return query.filter(entity.institution_id == inst_id)
    return query


def enforce_institution_access(resource, custom_inst_id=None):
    """
    Verify that current user has permission to access a resource.
    Aborts with 403 Forbidden if user belongs to a different institution.
    """
    from flask_login import current_user
    from flask import abort
    if not current_user or not current_user.is_authenticated:
        return
    if getattr(current_user, 'role', '') == 'system_admin':
        return
    user_inst_id = getattr(current_user, 'institution_id', None)
    res_inst_id = custom_inst_id if custom_inst_id is not None else getattr(resource, 'institution_id', None)
    if res_inst_id is not None and user_inst_id is not None and res_inst_id != user_inst_id:
        abort(403, description="Access denied: Resource belongs to another institution.")


def make_tenant_cache_key(*args, **kwargs):
    """
    Generate a tenant-aware cache key for Flask-Caching.
    Format: "<endpoint_or_path>:inst_<institution_id>"
    """
    from flask_login import current_user
    inst_id = 'global'
    if current_user and current_user.is_authenticated:
        inst_id = getattr(current_user, 'institution_id', 'global') or 'global'
    return f"{request.endpoint or request.path}:inst_{inst_id}"


def apply_media_cors_headers(response):

    """Attach CORS headers for media responses using an origin allow-list or request origin."""
    origin = request.headers.get('Origin')
    if origin:
        if not MEDIA_ALLOWED_ORIGINS or origin in MEDIA_ALLOWED_ORIGINS or '*' in MEDIA_ALLOWED_ORIGINS:
            response.headers['Access-Control-Allow-Origin'] = origin
            response.headers['Access-Control-Allow-Credentials'] = 'true'
            response.headers['Vary'] = 'Origin'
    else:
        response.headers['Access-Control-Allow-Origin'] = '*'
    return response


def generate_csrf_token():
    token = session.get('csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['csrf_token'] = token
    return token


def validate_csrf_token(token):
    expected = session.get('csrf_token', '')
    return bool(token and expected and secrets.compare_digest(token, expected))


def get_institution_slug(user=None, uploader_id=None):
    """Resolve institution slug for directory scoping. Defaults to 'default'."""
    from models import User, Institution
    if not user and uploader_id:
        try:
            user = User.query.get(uploader_id)
        except Exception:
            pass
    if user and getattr(user, 'institution_id', None):
        try:
            inst = Institution.query.get(user.institution_id)
            if inst and inst.slug:
                return inst.slug
        except Exception:
            pass
    return 'default'


def get_video_storage_dir(video_id, user=None, uploader_id=None, app=None):
    """
    Returns absolute folder path:
    /opt/campusplayer/static/uploads/institutions/<institution_slug>/<video_id>
    """
    from flask import current_app
    slug = get_institution_slug(user=user, uploader_id=uploader_id)
    target_app = app or (current_app._get_current_object() if current_app else None)
    base_upload = target_app.config['UPLOAD_FOLDER'] if target_app else os.path.join(BASE_DIR, 'static', 'uploads')
    video_dir = os.path.abspath(os.path.join(base_upload, 'institutions', slug, str(video_id)))
    os.makedirs(video_dir, exist_ok=True)
    return video_dir, slug


def sanitize_input(value, max_length=200):
    if value is None:
        return ''
    value = str(value).strip()
    if len(value) > max_length:
        value = value[:max_length]
    return value


def is_safe_uuid(value):
    return bool(re.fullmatch(r'[A-Za-z0-9_-]{8,64}', str(value or '')))


def allowed_file(filename):
    if not filename or '..' in filename or filename.startswith('/') or filename.startswith('\\'):
        return False
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in {'mp4', 'mov', 'avi', 'mkv'}


def allowed_image_file(filename):
    if not filename or '..' in filename or filename.startswith('/') or filename.startswith('\\'):
        return False
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in {'png', 'jpg', 'jpeg', 'gif'}


def allowed_subtitle_file(filename):
    if not filename or '..' in filename or filename.startswith('/') or filename.startswith('\\'):
        return False
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in {'vtt', 'srt'}


def get_video_duration(input_path):
    import subprocess
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            input_path
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return float(result.stdout.strip())
    except Exception:
        return 0


def rank_results(item, query, name_field, extra_fields=None):
    name_value = getattr(item, name_field, '') or ''
    query_lower = query.lower()
    score = 0
    name_lower = name_value.lower()

    if query_lower in name_lower:
        score += 10
    if name_lower.startswith(query_lower):
        score += 20
    if query_lower == name_lower:
        score += 40

    if extra_fields:
        for field in extra_fields:
            value = getattr(item, field, '') or ''
            if query_lower in value.lower():
                score += 5

    return score


def search_videos(query):
    if not query:
        return []
    term = f"%{query}%"
    q = Video.query.filter(
        (Video.title.ilike(term)) | (Video.description.ilike(term)) | (Video.filename.ilike(term))
    )
    videos = scope_to_institution(q, Video).limit(50).all()
    for video in videos:
        video._search_score = rank_results(video, query, 'title', extra_fields=['description'])
    videos.sort(key=lambda x: getattr(x, '_search_score', 0), reverse=True)
    return videos


def search_playlists(query):
    if not query:
        return []
    term = f"%{query}%"
    q = Playlist.query.filter(
        (Playlist.title.ilike(term)) | (Playlist.description.ilike(term))
    )
    playlists = scope_to_institution(q, Playlist).limit(50).all()
    for playlist in playlists:
        playlist._search_score = rank_results(playlist, query, 'title', extra_fields=['description'])
    playlists.sort(key=lambda x: getattr(x, '_search_score', 0), reverse=True)
    return playlists


def search_classes(query):
    if not query:
        return []
    term = f"%{query}%"
    q = Classroom.query.filter(
        (Classroom.name.ilike(term)) | (Classroom.description.ilike(term))
    )
    classes = scope_to_institution(q, Classroom).limit(50).all()
    for cls in classes:
        cls._search_score = rank_results(cls, query, 'name', extra_fields=['description'])
    classes.sort(key=lambda x: getattr(x, '_search_score', 0), reverse=True)
    return classes


def search_quizzes(query):
    if not query:
        return []
    term = f"%{query}%"
    q = Quiz.query.filter(
        (Quiz.title.ilike(term)) | (Quiz.description.ilike(term))
    )
    quizzes = scope_to_institution(q, Quiz).limit(50).all()
    for quiz in quizzes:
        quiz._search_score = rank_results(quiz, query, 'title', extra_fields=['description'])
    quizzes.sort(key=lambda x: getattr(x, '_search_score', 0), reverse=True)
    return quizzes


def search_users(query, role_filter=None):
    if not query:
        return []
    term = f"%{query}%"
    users_q = User.query
    if role_filter:
        users_q = users_q.filter(User.role == role_filter)
    users_q = users_q.filter(
        (User.username.ilike(term)) | (User.email.ilike(term))
    )
    users = scope_to_institution(users_q, User).limit(50).all()
    for user in users:
        user._search_score = rank_results(user, query, 'username')
    users.sort(key=lambda x: getattr(x, '_search_score', 0), reverse=True)
    return users



def global_search(query):
    result = {
        'videos': [], 'playlists': [], 'classes': [],
        'quizzes': [], 'teachers': [], 'students': [],
        'total_count': 0
    }
    if not query or len(query.strip()) < 1:
        return result
    query = query.strip()
    result['videos'] = search_videos(query)[:10]
    result['playlists'] = search_playlists(query)[:10]
    result['classes'] = search_classes(query)[:10]
    result['quizzes'] = search_quizzes(query)[:10]
    result['teachers'] = search_users(query, 'teacher')[:10]
    result['students'] = search_users(query, 'student')[:10]
    for key in result:
        if key != 'total_count':
            result['total_count'] += len(result[key])
    return result
