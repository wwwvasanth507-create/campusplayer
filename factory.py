import os
import secrets
from datetime import timedelta
from flask import Flask
from dotenv import load_dotenv
from extensions import db, login_manager, cache, limiter, socketio, mail, swagger, assets_env

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

    app.config.update(
        SECRET_KEY=secret_key,
        SQLALCHEMY_DATABASE_URI=os.getenv('DATABASE_URL', f'sqlite:///{os.path.join(BASE_DIR, "app.db").replace(chr(92), "/")}'),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={'connect_args': {'check_same_thread': False}},
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


from sqlalchemy import event
from sqlalchemy.engine import Engine
import sqlite3

def register_extensions(app):
    db.init_app(app)

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
    login_manager.login_view = 'login'
    login_manager.session_protection = 'basic'

    cache.init_app(app)
    limiter.init_app(app)
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
    from services.upload_engine import init_upload_engine

    app.register_blueprint(core_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(video_bp)

    # Initialize high-performance upload engine (10M req/min capable)
    init_upload_engine(upload_dir=app.config['UPLOAD_FOLDER'], hls_dir=app.config['HLS_FOLDER'])


def register_request_handlers(app):
    from services.security import enforce_https, csrf_protect_request, set_security_headers, update_last_active
    app.before_request(enforce_https)
    app.before_request(csrf_protect_request)
    app.before_request(update_last_active)
    app.after_request(set_security_headers)


def register_context_processors(app):
    from services.context import inject_settings
    app.context_processor(inject_settings)
