from .auth import auth_bp
from .core import core_bp
from .search import search_bp
from .video import video_bp

__all__ = [
    'auth_bp', 'core_bp', 'search_bp', 'video_bp'
]
