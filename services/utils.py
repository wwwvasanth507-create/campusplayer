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


def apply_media_cors_headers(response):
    """Attach CORS headers for media responses using an origin allow-list."""
    origin = request.headers.get('Origin')
    if origin and origin in MEDIA_ALLOWED_ORIGINS:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        response.headers['Vary'] = 'Origin'
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
    videos = Video.query.filter(Video.title.contains(query)).all()
    query_lower = query.lower()
    for video in Video.query.all():
        if video.id not in [x.id for x in videos] and query_lower in (video.title or '').lower():
            videos.append(video)
    unique = []
    seen_ids = set()
    for video in videos:
        if video.id not in seen_ids:
            seen_ids.add(video.id)
            unique.append(video)
    for video in unique:
        video._search_score = rank_results(video, query, 'title', extra_fields=['description'])
    unique.sort(key=lambda x: x._search_score, reverse=True)
    return unique


def search_playlists(query):
    if not query:
        return []
    playlists = Playlist.query.filter(Playlist.title.contains(query)).all()
    query_lower = query.lower()
    for item in Playlist.query.all():
        if item.id not in [x.id for x in playlists] and query_lower in (item.title or '').lower():
            playlists.append(item)
    unique = []
    seen_ids = set()
    for playlist in playlists:
        if playlist.id not in seen_ids:
            seen_ids.add(playlist.id)
            unique.append(playlist)
    for playlist in unique:
        playlist._search_score = rank_results(playlist, query, 'title')
    unique.sort(key=lambda x: x._search_score, reverse=True)
    return unique


def search_classes(query):
    if not query:
        return []
    classes = Classroom.query.filter(Classroom.name.contains(query)).all()
    query_lower = query.lower()
    for item in Classroom.query.all():
        if item.id not in [x.id for x in classes] and query_lower in (item.name or '').lower():
            classes.append(item)
    unique = []
    seen_ids = set()
    for cls in classes:
        if cls.id not in seen_ids:
            seen_ids.add(cls.id)
            unique.append(cls)
    for cls in unique:
        cls._search_score = rank_results(cls, query, 'name')
    unique.sort(key=lambda x: x._search_score, reverse=True)
    return unique


def search_quizzes(query):
    if not query:
        return []
    quizzes = Quiz.query.filter(Quiz.title.contains(query)).all()
    query_lower = query.lower()
    for q in Quiz.query.all():
        if q.id not in [x.id for x in quizzes] and query_lower in (q.title or '').lower():
            quizzes.append(q)
    unique = []
    seen_ids = set()
    for quiz in quizzes:
        if quiz.id not in seen_ids:
            seen_ids.add(quiz.id)
            unique.append(quiz)
    for quiz in unique:
        quiz._search_score = rank_results(quiz, query, 'title')
    unique.sort(key=lambda x: x._search_score, reverse=True)
    return unique


def search_users(query, role_filter=None):
    if not query:
        return []
    users_q = User.query
    if role_filter:
        users_q = users_q.filter(User.role == role_filter)
    users = users_q.filter(User.username.contains(query)).all()
    query_lower = query.lower()
    all_users_q = User.query
    if role_filter:
        all_users_q = all_users_q.filter(User.role == role_filter)
    for u in all_users_q.all():
        if u.id not in [x.id for x in users] and query_lower in (u.username or '').lower():
            users.append(u)
    unique = []
    seen_ids = set()
    for user in users:
        if user.id not in seen_ids:
            seen_ids.add(user.id)
            unique.append(user)
    for user in unique:
        user._search_score = rank_results(user, query, 'username')
    unique.sort(key=lambda x: x._search_score, reverse=True)
    return unique


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
