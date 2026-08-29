import os
import secrets
from datetime import timedelta
from flask import Flask
from dotenv import load_dotenv
from extensions import db, login_manager, cache, limiter, socketio, mail, swagger, assets_env, migrate

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
HLS_FOLDER = os.path.join(BASE_DIR, 'static', 'hls')
SUBTITLE_FOLDER = os.path.join(BASE_DIR, 'static', 'subtitles')
PDF_DIR = os.path.join(BASE_DIR, 'generated_pdfs')


from services.utils import get_or_create_persistent_secret_key
from services.session_store import SqlAlchemySessionInterface

def create_app(test_config=None):
    app = Flask(__name__, static_folder='static', template_folder='templates')

    secret_key = get_or_create_persistent_secret_key(BASE_DIR)

    # Detect test environment
    is_testing = (
        (test_config and test_config.get('TESTING')) or
        os.getenv('TESTING') or
        os.getenv('FLASK_TESTING')
    )

    raw_db_url = os.getenv('DATABASE_URL')
    if test_config and 'SQLALCHEMY_DATABASE_URI' in test_config:
        raw_db_url = test_config['SQLALCHEMY_DATABASE_URI']

    sqlite_default = f'sqlite:///{os.path.join(BASE_DIR, "app.db")}'

    if not raw_db_url:
        if is_testing:
            raw_db_url = os.getenv('TEST_DATABASE_URL', sqlite_default)
        else:
            raw_db_url = sqlite_default

    if raw_db_url.startswith('postgres://'):
        raw_db_url = raw_db_url.replace('postgres://', 'postgresql://', 1)

    if raw_db_url.startswith('postgresql'):
        try:
            from sqlalchemy import create_engine
            temp_engine = create_engine(raw_db_url, pool_pre_ping=True, connect_args={'connect_timeout': 2})
            conn = temp_engine.connect()
            conn.close()
            temp_engine.dispose()
        except Exception as e:
            raw_db_url = sqlite_default

    if raw_db_url.startswith('sqlite'):
        engine_options = {}
    else:
        engine_options = {
            'pool_size': int(os.getenv('DB_POOL_SIZE', 30)),
            'max_overflow': int(os.getenv('DB_MAX_OVERFLOW', 50)),
            'pool_timeout': int(os.getenv('DB_POOL_TIMEOUT', 30)),
            'pool_pre_ping': True,
            'pool_recycle': 1800
        }

    app.config.update(
        SECRET_KEY=secret_key,
        SQLALCHEMY_DATABASE_URI=raw_db_url,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS=engine_options,
        UPLOAD_FOLDER=UPLOAD_FOLDER,
        HLS_FOLDER=HLS_FOLDER,
        SUBTITLE_FOLDER=SUBTITLE_FOLDER,
        MAX_CONTENT_LENGTH=1024 * 1024 * 1024 * 1024 * 10,  # 10TB effective unlimited
        CACHE_TYPE='SimpleCache',
        CACHE_DEFAULT_TIMEOUT=300,
        SESSION_COOKIE_HTTPONLY=True,
        FORCE_HTTPS=os.getenv('FORCE_HTTPS', 'False').lower() in ('1', 'true', 'yes'),
        SESSION_COOKIE_SECURE=os.getenv('SESSION_COOKIE_SECURE', 'False').lower() in ('1', 'true', 'yes'),
        REMEMBER_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_REFRESH_EACH_REQUEST=True,
        PREFERRED_URL_SCHEME='https',
        JSON_AS_ASCII=False,
        PERMANENT_SESSION_LIFETIME=timedelta(days=30)
    )

    app.session_interface = SqlAlchemySessionInterface()


    if test_config:
        app.config.update(test_config)

    os.makedirs(PDF_DIR, exist_ok=True)
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(HLS_FOLDER, exist_ok=True)
    os.makedirs(SUBTITLE_FOLDER, exist_ok=True)

    register_extensions(app)
    register_blueprints(app)
    register_request_handlers(app)
    register_context_processors(app)

    return app


def register_extensions(app):
    db.init_app(app)
    migrate.init_app(app, db)

    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    login_manager.session_protection = 'basic'

    @login_manager.user_loader
    def load_user(user_id):
        try:
            from flask import session
            from models import User
            user = User.query.get(int(user_id))
            if user and getattr(user, 'is_active_account', True):
                sess_ver = session.get('session_version')
                if sess_ver is not None and sess_ver != getattr(user, 'session_version', 1):
                    return None
                return user
            return None
        except Exception:
            return None

    cache.init_app(app)
    limiter.init_app(app)
    if app.config.get('TESTING') or os.getenv('TESTING') or os.getenv('FLASK_TESTING'):
        limiter.enabled = False
    socketio.init_app(app)
    if mail:
        mail.init_app(app)
    if swagger:
        swagger.init_app(app)
    if assets_env:
        assets_env.init_app(app)


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
    from flask import request, jsonify, render_template
    import traceback

    app.before_request(enforce_https)
    app.before_request(csrf_protect_request)
    app.before_request(update_last_active)
    app.after_request(set_security_headers)

    @app.errorhandler(404)
    def page_not_found(e):
        if request.path.startswith('/api/') or request.is_json or request.headers.get('Accept') == 'application/json':
            return jsonify({'success': False, 'error': 'Resource not found'}), 404
        try:
            return render_template('404.html'), 404
        except Exception:
            return "<h1>404 Not Found</h1><p>The requested page was not found on Campus Player.</p>", 404

    @app.errorhandler(500)
    @app.errorhandler(Exception)
    def handle_internal_server_error(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            try:
                db.session.rollback()
            except Exception:
                pass
            return e

        try:
            db.session.rollback()
        except Exception:
            pass

        app.logger.error(f"Internal Server Error on {request.path}: {e}\n{traceback.format_exc()}")

        if request.path.startswith('/api/') or request.is_json or request.headers.get('Accept') == 'application/json':
            return jsonify({'success': False, 'error': 'Internal server error', 'message': str(e)}), 500
        try:
            return render_template('500.html', error=str(e)), 500
        except Exception:
            return "<h1>500 Internal Server Error</h1><p>An internal server error occurred.</p>", 500


def register_context_processors(app):
    from services.context import inject_settings
    app.context_processor(inject_settings)

    @app.context_processor
    def inject_smart_url_for():
        from flask import url_for as flask_url_for
        def url_for(endpoint, **values):
            try:
                return flask_url_for(endpoint, **values)
            except Exception:
                if f'auth.{endpoint}' in app.view_functions:
                    return flask_url_for(f'auth.{endpoint}', **values)
                if f'core.{endpoint}' in app.view_functions:
                    return flask_url_for(f'core.{endpoint}', **values)
                if f'video.{endpoint}' in app.view_functions:
                    return flask_url_for(f'video.{endpoint}', **values)
                return f"/{endpoint.lstrip('/')}"
        return dict(url_for=url_for)
