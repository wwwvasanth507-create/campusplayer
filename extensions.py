from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_caching import Cache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_socketio import SocketIO

db = SQLAlchemy()
login_manager = LoginManager()
cache = Cache()
limiter = Limiter(key_func=get_remote_address)
socketio = SocketIO(cors_allowed_origins="*")

# Optional: Email support (install flask-mail)
try:
    from flask_mail import Mail
    mail = Mail()
except ImportError:
    mail = None

# Optional: Swagger/OpenAPI docs (install flasgger)
try:
    from flasgger import Swagger
    swagger = Swagger()
except ImportError:
    swagger = None

# Optional: Asset management (install flask-assets)
try:
    from flask_assets import Environment, Bundle
    assets_env = Environment()
except ImportError:
    assets_env = None