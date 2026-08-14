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


def csrf_protect_request():
    """CSRF protection for state-changing requests.

    Validates the submitted token against the value stored in the
    server-side session using a constant-time comparison. Previously this
    only checked that a token was *present* without verifying it matched
    the session value, which meant CSRF protection was effectively
    bypassable (any non-empty token would pass). Fixed to reuse the same
    validation used elsewhere in the app.
    """
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        from flask import abort
        from services.utils import validate_csrf_token
        token = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token')
        if not validate_csrf_token(token):
            abort(400, description='Invalid CSRF token')


def set_security_headers(response):
    """Add security headers to all responses."""
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "img-src 'self' data: blob:; "
        "font-src 'self' https://fonts.gstatic.com; "
        "media-src 'self' blob: data:; "
        "connect-src 'self' blob: data:; "
        "frame-ancestors 'none'; base-uri 'self';"
    )
    from app import app
    if app.config.get('FORCE_HTTPS') or request.is_secure:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains; preload'
    return response


def update_last_active():
    """Update user's last_active timestamp on each request and check suspension status."""
    from flask_login import current_user, logout_user
    from flask import request, redirect, url_for, current_app
    from app import db
    from models import User, Institution
    import datetime
    
    if current_user.is_authenticated:
        if request.endpoint in ('static', 'logout'):
            return
            
        if not getattr(current_user, 'is_active_account', True):
            logout_user()
            login_url = url_for('auth.login') if 'auth.login' in current_app.view_functions else url_for('login')
            return redirect(login_url)
            
        if getattr(current_user, 'institution_id', None):
            inst = Institution.query.get(current_user.institution_id)
            if inst and inst.status == 'suspended':
                logout_user()
                login_url = url_for('auth.login') if 'auth.login' in current_app.view_functions else url_for('login')
                return redirect(login_url)
                
        try:
            current_user.last_active = datetime.datetime.utcnow()
            db.session.commit()
        except Exception:
            db.session.rollback()