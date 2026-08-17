from .context import inject_settings
from .security import enforce_https, csrf_protect_request, set_security_headers, update_last_active
from .utils import (
    generate_csrf_token, validate_csrf_token, sanitize_input,
    is_safe_uuid, allowed_file, allowed_image_file, allowed_subtitle_file,
    rank_results, search_videos, search_playlists, search_classes,
    search_quizzes, search_users, global_search
)
from .auth import admin_required, teacher_required, log_activity
