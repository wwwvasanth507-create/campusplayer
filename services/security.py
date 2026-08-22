"""
Security utilities for CampusPlayer.
"""

from flask import request


def enforce_https():
    """Redirect HTTP to HTTPS if FORCE_HTTPS is enabled."""
    from app import app
    if app.config.get('FORCE_HTTPS'):
        proto = request.headers.get('X-Forwarded-Proto', 'http')
        if proto != 'https' and not request.is_secure:
            url = request.url.replace('http://', 'https://', 1)
            from flask import redirect
            return redirect(url, code=301)


def enforce_institution_access(resource):
    """
    Validates that current_user has access to resource based on institution_id.
    Sysadmin bypasses tenant restrictions. Other roles must match current_user.institution_id.
    """
    from flask_login import current_user
    from flask import abort
    if not current_user or not current_user.is_authenticated:
        abort(401)
    if getattr(current_user, 'role', None) == 'system_admin':
        return
    res_inst_id = getattr(resource, 'institution_id', None)
    if res_inst_id is not None and res_inst_id != getattr(current_user, 'institution_id', None):
        abort(403, description="Access denied. Resource belongs to another institution.")


def csrf_protect_request():
    """CSRF protection for state-changing requests."""
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        from flask import abort, current_app
        if current_app.config.get('TESTING') or not current_app.config.get('WTF_CSRF_ENABLED', True):
            return
        if request.path in ('/login', '/auth/login', '/teacher/upload_chunk', '/api/upload/chunk'):
            return
        from services.utils import validate_csrf_token
        token = (
            request.form.get('csrf_token') or
            request.headers.get('X-CSRF-Token') or
            request.headers.get('X-CSRFToken') or
            request.headers.get('X-Csrf-Token') or
            request.headers.get('X-Csrftoken') or
            request.headers.get('X-CSRF_TOKEN') or
            request.args.get('csrf_token')
        )
        if not validate_csrf_token(token):
            abort(400, description='Invalid CSRF token')



import gzip

def set_security_headers(response):
    """Add security headers, caching headers, and response compression."""
    if request.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'public, max-age=86400'
    else:
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
        from flask import current_app
        if current_app.config.get('FORCE_HTTPS') or request.is_secure:
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


def update_last_active():
    """Update user's last_active timestamp with throttling and check suspension status."""
    from flask_login import current_user, logout_user
    from flask import request, redirect, url_for, current_app
    from app import db
    from models import Institution
    import datetime
    
    if current_user.is_authenticated:
        endpoint = request.endpoint or ''
        path = request.path or ''
        # Skip static assets, media streaming, and frequent background polling endpoints
        if endpoint in ('static', 'logout', 'serve_hls', 'video.serve_hls') or \
           path.startswith('/static/') or path.startswith('/hls/') or \
           path.startswith('/api/video/progress') or path.startswith('/api/chatroom/') or \
           path.startswith('/api/video_status') or path.startswith('/api/notifications') or \
           path.startswith('/api/teacher/processing_videos'):
            return
            
        if not getattr(current_user, 'is_active_account', True):
            logout_user()
            login_url = url_for('auth.login') if 'auth.login' in current_app.view_functions else url_for('login')
            return redirect(login_url)
            
        inst_id = getattr(current_user, 'institution_id', None)
        if inst_id:
            from extensions import cache
            cache_key = f'inst_status_{inst_id}'
            inst_status = cache.get(cache_key) if cache else None
            if inst_status is None:
                inst = Institution.query.get(inst_id)
                inst_status = inst.status if inst else 'active'
                if cache:
                    cache.set(cache_key, inst_status, timeout=60)
            if inst_status == 'suspended':
                from flask import flash
                logout_user()
                flash("Your institution has been suspended.", "danger")
                login_url = url_for('auth.login') if 'auth.login' in current_app.view_functions else url_for('login')
                return redirect(login_url)
                
        now = datetime.datetime.utcnow()
        last = getattr(current_user, 'last_active', None)
        # Throttle DB writes: only commit timestamp at most once every 120 seconds
        if last is None or (now - last).total_seconds() > 120:
            try:
                current_user.last_active = now
                db.session.commit()
            except Exception:
                db.session.rollback()