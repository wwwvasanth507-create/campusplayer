from datetime import datetime, timedelta
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from extensions import db
from sqlalchemy import event
import secrets
import json

# ═══════════════════════════════════════════════════════════════
# NEW MODEL: Institution (multi-tenant anchor)
# ═══════════════════════════════════════════════════════════════
# Every Admin owns exactly one Institution. Every User, Classroom, Video,
# Assignment, Quiz, Attendance record etc. is scoped to an institution_id.
# A "Default Institution" is created during migration and all pre-existing
# data is backfilled into it, so nothing that already works stops working.

class Institution(db.Model):
    __tablename__ = 'institution'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(100), unique=True, nullable=False)  # url-safe identifier
    owner_admin_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20), default='active', index=True)  # active, suspended
    logo_url = db.Column(db.String(500), nullable=True)

    # Per-institution isolated storage root, e.g. uploads/institutions/<slug>/
    storage_root = db.Column(db.String(500), nullable=True)
    storage_used_bytes = db.Column(db.BigInteger, default=0)

    # === SYSADMIN VIDEO DELETION PERMISSIONS ===
    allow_manual_video_delete = db.Column(db.Boolean, default=True)  # Permission to delete videos manually
    allow_auto_video_delete = db.Column(db.Boolean, default=True)    # Permission for auto-deletion after fixed time
    max_video_retention_days = db.Column(db.Integer, default=365)    # Max allowable retention policy (days)

    owner_admin = db.relationship('User', foreign_keys=[owner_admin_id])
    users = db.relationship('User', backref='institution', lazy=True, foreign_keys='User.institution_id')


# Association table for Playlist-Video
playlist_videos = db.Table('playlist_videos',
    db.Column('playlist_id', db.Integer, db.ForeignKey('playlist.id'), primary_key=True),
    db.Column('video_id', db.Integer, db.ForeignKey('video.id'), primary_key=True)
)

# Association table for Student-Classroom
student_classes = db.Table('student_classes',
    db.Column('student_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('classroom_id', db.Integer, db.ForeignKey('classroom.id'), primary_key=True)
)

class User(UserMixin, db.Model):
    __table_args__ = (
        db.UniqueConstraint('username', 'institution_id', name='uq_user_username_institution'),
        db.Index('ix_user_institution_role', 'institution_id', 'role'),
    )
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, index=True)  # 'system_admin', 'admin', 'teacher', 'student'

    # NEW: multi-tenant scoping. Nullable so system_admin (who owns no single
    # institution) and pre-migration rows remain valid.
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)

    # NEW: whether this teacher is the "Class Teacher" (assigned per-classroom via
    # Classroom.teacher_id already; this flag is for institution-wide staff status)
    is_active_account = db.Column(db.Boolean, default=True, index=True)
    xp = db.Column(db.Integer, default=0, index=True)
    phone = db.Column(db.String(20), nullable=True)
    parent_email = db.Column(db.String(150))
    parent_name = db.Column(db.String(150))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    # === ADVANCED FIELDS ===
    email = db.Column(db.String(150), unique=True, nullable=True)

    # === TEACHER EMAIL CONFIGURATION FIELDS ===
    email_sender_address = db.Column(db.String(150), nullable=True)
    encrypted_app_password = db.Column(db.String(500), nullable=True)
    email_enabled = db.Column(db.Boolean, default=False)
    last_report_sent = db.Column(db.DateTime, nullable=True)

    # Profile & Preferences
    display_name = db.Column(db.String(150), nullable=True)
    avatar_url = db.Column(db.String(500), nullable=True)
    theme_preference = db.Column(db.String(10), default='dark')
    bio = db.Column(db.Text, nullable=True)

    def get_avatar_url(self):
        if self.avatar_url:
            return self.avatar_url
        return None

    @property
    def name(self):
        """Returns updated profile display name if set, otherwise formatted clean display name."""
        if self.display_name and self.display_name.strip():
            return self.display_name.strip()
        if self.username:
            return self.username.replace('_', ' ').replace('.', ' ').title()
        return "User"

    def get_display_name(self):
        return self.name

    # Session Tracking
    last_login = db.Column(db.DateTime, nullable=True)
    last_active = db.Column(db.DateTime, nullable=True, index=True)
    login_count = db.Column(db.Integer, default=0)

    # === GAMIFICATION FIELDS ===
    level = db.Column(db.Integer, default=1, index=True)
    streak_days = db.Column(db.Integer, default=0)
    last_streak_date = db.Column(db.Date, nullable=True)
    total_quiz_score = db.Column(db.Integer, default=0)
    total_quizzes_taken = db.Column(db.Integer, default=0)
    achievements_json = db.Column(db.Text, default='[]')  # JSON array of achievement IDs
    quests_json = db.Column(db.Text, default='{}')        # JSON object storing daily quests state and progress

    def get_achievements(self):
        return json.loads(self.achievements_json or '[]')

    def add_achievement(self, achievement_id):
        achievements = self.get_achievements()
        if achievement_id not in achievements:
            achievements.append(achievement_id)
            self.achievements_json = json.dumps(achievements)
            return True
        return False

    def get_daily_quests(self):
        """Return dict of active daily quests for today's UTC date based on DailyQuestTemplate definitions."""
        today_str = datetime.utcnow().strftime('%Y-%m-%d')
        raw = json.loads(self.quests_json or '{}')

        try:
            templates = DailyQuestTemplate.query.filter_by(is_active=True).all()
        except Exception:
            templates = []

        template_dict = {}
        if templates:
            for t in templates:
                template_dict[t.quest_key] = {
                    'id': t.quest_key,
                    'title': t.title,
                    'desc': t.desc,
                    'xp': t.xp,
                    'icon': t.icon,
                    'progress': 0,
                    'target': t.target,
                    'claimed': False
                }
        else:
            template_dict = {
                'daily_login': {
                    'id': 'daily_login',
                    'title': 'Daily Check-in',
                    'desc': 'Log in & remain active today',
                    'xp': 25,
                    'icon': 'event_available',
                    'progress': 1,
                    'target': 1,
                    'claimed': False
                },
                'watch_video': {
                    'id': 'watch_video',
                    'title': 'Course Explorer',
                    'desc': 'Watch or review an educational lecture',
                    'xp': 50,
                    'icon': 'play_circle_filled',
                    'progress': 0,
                    'target': 1,
                    'claimed': False
                },
                'take_quiz': {
                    'id': 'take_quiz',
                    'title': 'Quiz Challenger',
                    'desc': 'Complete an online assessment',
                    'xp': 75,
                    'icon': 'quiz',
                    'progress': 0,
                    'target': 1,
                    'claimed': False
                },
                'submit_assignment': {
                    'id': 'submit_assignment',
                    'title': 'Assignment Scholar',
                    'desc': 'Submit coursework or homework',
                    'xp': 100,
                    'icon': 'assignment_turned_in',
                    'progress': 0,
                    'target': 1,
                    'claimed': False
                }
            }

        if raw.get('date') != today_str:
            raw = {
                'date': today_str,
                'quests': {}
            }

        user_quests = raw.get('quests', {})
        merged_quests = {}

        for key, t_data in template_dict.items():
            if key in user_quests:
                q = user_quests[key]
                merged_quests[key] = {
                    'id': key,
                    'title': t_data['title'],
                    'desc': t_data['desc'],
                    'xp': t_data['xp'],
                    'icon': t_data['icon'],
                    'progress': q.get('progress', 1 if key == 'daily_login' else 0),
                    'target': t_data['target'],
                    'claimed': q.get('claimed', False)
                }
            else:
                q_progress = 1 if key == 'daily_login' else 0
                merged_quests[key] = {
                    'id': key,
                    'title': t_data['title'],
                    'desc': t_data['desc'],
                    'xp': t_data['xp'],
                    'icon': t_data['icon'],
                    'progress': q_progress,
                    'target': t_data['target'],
                    'claimed': False
                }

        raw['quests'] = merged_quests
        self.quests_json = json.dumps(raw)
        return merged_quests

    def update_quest_progress(self, quest_id, amount=1):
        """Increment progress for a specific daily quest."""
        today_str = datetime.utcnow().strftime('%Y-%m-%d')
        raw = json.loads(self.quests_json or '{}')
        if raw.get('date') != today_str:
            self.get_daily_quests()
            raw = json.loads(self.quests_json or '{}')
        quests = raw.get('quests', {})
        if quest_id in quests and not quests[quest_id].get('claimed', False):
            current = quests[quest_id].get('progress', 0)
            target = quests[quest_id].get('target', 1)
            quests[quest_id]['progress'] = min(target, current + amount)
            self.quests_json = json.dumps(raw)
            return True
        return False

    def claim_quest(self, quest_id):
        """Claim rewards for a completed quest and award XP."""
        today_str = datetime.utcnow().strftime('%Y-%m-%d')
        raw = json.loads(self.quests_json or '{}')
        if raw.get('date') != today_str:
            self.get_daily_quests()
            raw = json.loads(self.quests_json or '{}')
        quests = raw.get('quests', {})
        if quest_id in quests:
            quest = quests[quest_id]
            if quest.get('progress', 0) >= quest.get('target', 1) and not quest.get('claimed', False):
                reward_xp = quest.get('xp', 0)
                self.xp = (self.xp or 0) + reward_xp
                self.level = (self.xp // 500) + 1
                quest['claimed'] = True
                self.quests_json = json.dumps(raw)
                return True, reward_xp
        return False, 0

    # Relationships with cascades for clean deletions
    videos = db.relationship('Video', backref='uploader', lazy=True, cascade="all, delete-orphan")
    playlists = db.relationship('Playlist', backref='creator', lazy=True, cascade="all, delete-orphan")
    comments = db.relationship('Comment', backref='author', lazy=True, cascade="all, delete-orphan")
    views = db.relationship('ViewAnalytics', backref='viewer', lazy=True, cascade="all, delete-orphan")
    received_notifications = db.relationship('Notification', backref='recipient', lazy=True, cascade="all, delete-orphan")

    # Class relationships
    created_classes = db.relationship('Classroom', backref='teacher', lazy=True, cascade="all, delete-orphan")
    enrolled_classes = db.relationship('Classroom', secondary='student_classes', backref=db.backref('students', lazy='dynamic'))
    attendance_records = db.relationship('Attendance', backref='student', lazy=True, cascade="all, delete-orphan")

    # NEW: Assignment submissions
    assignment_submissions = db.relationship('AssignmentSubmission', backref='student', lazy=True, cascade="all, delete-orphan")

    # NEW: Video notes
    video_notes = db.relationship('VideoNote', backref='author', lazy=True, cascade="all, delete-orphan")

    # NEW: Video bookmarks
    bookmarks = db.relationship('VideoBookmark', backref='author', lazy=True, cascade="all, delete-orphan")

    # NEW: Video progress tracking
    video_progress = db.relationship('VideoProgress', backref='student_rel', lazy=True, cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def update_level(self):
        """Update user level based on XP."""
        new_level = (self.xp // 500) + 1
        if new_level != self.level:
            self.level = new_level
            return True
        return False

    def calculate_streak(self):
        """Update login streak."""
        today = datetime.utcnow().date()
        if self.last_streak_date:
            delta = (today - self.last_streak_date).days
            if delta == 1:
                self.streak_days += 1
            elif delta > 1:
                self.streak_days = 1
        else:
            self.streak_days = 1
        self.last_streak_date = today


class Video(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    title = db.Column(db.String(200), nullable=False)
    filename = db.Column(db.String(300), nullable=False)
    hls_playlist_path = db.Column(db.String(500))
    thumbnail_path = db.Column(db.String(500))
    upload_date = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    uploader_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey('classroom.id'), nullable=True, index=True)

    # YouTube & Source Type Fields
    video_type = db.Column(db.String(20), default='local', index=True)  # 'local' or 'youtube'
    youtube_id = db.Column(db.String(100), nullable=True, index=True)
    youtube_url = db.Column(db.String(500), nullable=True)

    # Progress tracking
    status = db.Column(db.String(20), default='pending', index=True)
    processing_progress = db.Column(db.Integer, default=0)

    # === ADVANCED FIELDS ===
    description = db.Column(db.Text, nullable=True)
    duration_seconds = db.Column(db.Integer, default=0)
    tags = db.Column(db.String(500), nullable=True)
    language = db.Column(db.String(10), default='en')

    # Captions/Subtitles
    subtitle_path = db.Column(db.String(500), nullable=True)
    subtitle_language = db.Column(db.String(10), default='en')

    # View counting
    view_count = db.Column(db.Integer, default=0)
    like_count = db.Column(db.Integer, default=0)

    # NEW: Chapter markers (JSON array of {time, title})
    chapters_json = db.Column(db.Text, nullable=True)

    # NEW: Difficulty level
    difficulty = db.Column(db.String(20), default='intermediate')  # beginner, intermediate, advanced

    # === FIXED-TIME AUTO-DELETION FIELDS ===
    auto_delete_at = db.Column(db.DateTime, nullable=True)  # Fixed date/time when video expires
    retention_days = db.Column(db.Integer, nullable=True)   # Retention duration in days (e.g. 7, 30, 90)

    # === ACADEMIC ARCHIVING FIELDS ===
    is_archived = db.Column(db.Boolean, default=False)
    archived_at = db.Column(db.DateTime, nullable=True)

    # === AI VIDEO SUMMARIZER CACHING FIELDS ===
    ai_summary = db.Column(db.Text, nullable=True)          # AI generated narrative summary
    ai_key_takeaways = db.Column(db.Text, nullable=True)    # JSON string array of bullet points
    ai_summary_generated_at = db.Column(db.DateTime, nullable=True)

    # ═══════════════════════════════════════════════════════════════
    # NEW: HLS Adaptive Streaming Fields
    # ═══════════════════════════════════════════════════════════════
    master_playlist_path = db.Column(db.String(500))  # Path to master.m3u8 with all renditions
    available_renditions = db.Column(db.Text, default='[]')  # JSON: [{"resolution":"1080p","bandwidth":5000000,"playlist":"1080p.m3u8"},...]
    source_width = db.Column(db.Integer, default=0)  # Original video width
    source_height = db.Column(db.Integer, default=0)  # Original video height
    source_bitrate = db.Column(db.Integer, default=0)  # Original video bitrate
    video_codec = db.Column(db.String(50), default='h264')  # Codec used
    audio_codec = db.Column(db.String(50), default='aac')
    fps = db.Column(db.Float, default=0.0)  # Frames per second
    has_adaptive_streams = db.Column(db.Boolean, default=False)  # Whether adaptive renditions exist

    # NEW: Seek preview / sprite sheet
    sprite_path = db.Column(db.String(500), nullable=True)  # Path to sprite sheet for seek preview
    sprite_tile_count = db.Column(db.Integer, default=0)  # Number of tiles in sprite

    # NEW: Thumbnails for timeline preview (VTT file)
    thumbnails_vtt_path = db.Column(db.String(500), nullable=True)  # Path to thumbnails.vtt

    @property
    def thumbnail_url(self):
        """Dynamic helper returning proper thumbnail URL for YouTube or local videos."""
        v_type = getattr(self, 'video_type', 'local') or 'local'
        yt_id = getattr(self, 'youtube_id', None)
        if not yt_id and self.filename and self.filename.startswith('youtube_'):
            yt_id = self.filename.replace('youtube_', '')

        if v_type == 'youtube' or yt_id:
            if yt_id:
                return f"https://img.youtube.com/vi/{yt_id}/hqdefault.jpg"

        if self.thumbnail_path:
            if self.thumbnail_path.startswith('http://') or self.thumbnail_path.startswith('https://'):
                return self.thumbnail_path
            return f"/static/{self.thumbnail_path.lstrip('/')}"
        return "/static/img/default_thumbnail.png"

    def get_chapters(self):
        return json.loads(self.chapters_json or '[]')

    def set_chapters(self, chapters_list):
        self.chapters_json = json.dumps(chapters_list)

    def get_renditions(self):
        return json.loads(self.available_renditions or '[]')

    def set_renditions(self, renditions_list):
        self.available_renditions = json.dumps(renditions_list)
        self.has_adaptive_streams = len(renditions_list) > 0

    def get_ai_takeaways(self):
        return json.loads(self.ai_key_takeaways or '[]')

    def is_expired(self):
        if self.auto_delete_at and datetime.utcnow() >= self.auto_delete_at:
            return True
        return False

    comments = db.relationship('Comment', backref='video', lazy=True, cascade="all, delete-orphan")
    analytics = db.relationship('ViewAnalytics', backref='video', lazy=True, cascade="all, delete-orphan")
    notifications = db.relationship('Notification', backref='video', lazy=True, cascade="all, delete-orphan")
    # NEW: Video progress
    progress_records = db.relationship('VideoProgress', backref='video', lazy=True, cascade="all, delete-orphan")
    # NEW: Notes on this video
    notes = db.relationship('VideoNote', backref='video', lazy=True, cascade="all, delete-orphan")
    # NEW: Bookmarks on this video
    video_bookmarks = db.relationship('VideoBookmark', backref='video', lazy=True, cascade="all, delete-orphan")


@event.listens_for(Video, 'before_delete')
def _on_video_before_delete(mapper, connection, target):
    """
    SQLAlchemy lifecycle hook: automatically and permanently deletes all physical files,
    HLS directories, TS segments, thumbnails, and subtitles on the server/PC whenever a
    Video model record is deleted from the database.
    """
    try:
        from services.video_cleanup import permanently_delete_video_assets
        permanently_delete_video_assets(target)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"[VideoModel] Error in before_delete file cleanup hook for #{getattr(target, 'id', None)}: {e}")


class VideoLike(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    video_id = db.Column(db.Integer, db.ForeignKey('video.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship('User', backref=db.backref('likes', lazy=True))
    __table_args__ = (db.UniqueConstraint('user_id', 'video_id', name='unique_like'),)


class Playlist(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    title = db.Column(db.String(150), nullable=False)
    thumbnail_path = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    creator_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    description = db.Column(db.Text, nullable=True)
    videos = db.relationship('Video', secondary=playlist_videos, lazy='subquery',
        backref=db.backref('playlists', lazy=True))


class Classroom(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    name = db.Column(db.String(100), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    start_time = db.Column(db.String(5), default="09:10")
    description = db.Column(db.Text, nullable=True)
    class_code = db.Column(db.String(10), unique=True, nullable=True)
    # NEW: Class color theme
    color_theme = db.Column(db.String(7), default='#4f46e5')

    videos = db.relationship('Video', backref='classroom', lazy=True)
    # NEW: Assignments for this class
    assignments = db.relationship('Assignment', backref='classroom', lazy=True, cascade="all, delete-orphan")


class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    video_id = db.Column(db.Integer, db.ForeignKey('video.id'), nullable=False, index=True)
    parent_id = db.Column(db.Integer, db.ForeignKey('comment.id'), nullable=True, index=True)
    edited = db.Column(db.Boolean, default=False)
    edited_at = db.Column(db.DateTime, nullable=True)
    replies = db.relationship('Comment', backref=db.backref('parent', remote_side=[id]), lazy=True)


class ViewAnalytics(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    video_id = db.Column(db.Integer, db.ForeignKey('video.id'), nullable=False, index=True)
    start_time = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    end_time = db.Column(db.DateTime)
    duration_seconds = db.Column(db.Integer, default=0)
    percent_watched = db.Column(db.Float, default=0.0)
    completed = db.Column(db.Boolean, default=False, index=True)
    ip_address = db.Column(db.String(50), nullable=True)
    user_agent = db.Column(db.String(300), nullable=True)
    # NEW: Quality selected during playback
    quality_selected = db.Column(db.String(20), nullable=True)


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    message = db.Column(db.Text, nullable=False)
    video_id = db.Column(db.Integer, db.ForeignKey('video.id'), nullable=True, index=True)
    comment_id = db.Column(db.Integer, db.ForeignKey('comment.id'), nullable=True)
    is_read = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    notification_type = db.Column(db.String(30), default='info')
    # NEW: Deep link URL
    action_url = db.Column(db.String(500), nullable=True)

    comment = db.relationship('Comment', backref=db.backref('notification', lazy=True))


class SiteSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    institution_name = db.Column(db.String(200), nullable=True, default='Campus Player')
    lock_video_speed = db.Column(db.Boolean, default=False)
    lock_video_skipping = db.Column(db.Boolean, default=False)
    global_playlist_thumbnail = db.Column(db.String(500), nullable=True)
    attendance_lock_time = db.Column(db.String(5), default="09:10")
    admin_sms_phone = db.Column(db.String(20), nullable=True)
    gemini_api_key = db.Column(db.String(200), nullable=True)

    # === ADVANCED SETTINGS ===
    allow_self_registration = db.Column(db.Boolean, default=False)
    max_video_size_mb = db.Column(db.Integer, default=20480)
    default_language = db.Column(db.String(10), default='en')
    enable_notifications = db.Column(db.Boolean, default=True)
    session_timeout_minutes = db.Column(db.Integer, default=120)

    # Appearance
    primary_color = db.Column(db.String(7), default='#d4af37')
    logo_url = db.Column(db.String(500), nullable=True)

    # NEW: Email settings
    smtp_server = db.Column(db.String(200), nullable=True)
    smtp_port = db.Column(db.Integer, default=587)
    smtp_username = db.Column(db.String(200), nullable=True)
    smtp_password = db.Column(db.String(200), nullable=True)
    email_from = db.Column(db.String(200), nullable=True)

    # NEW: Feature toggles
    enable_leaderboard = db.Column(db.Boolean, default=True)
    enable_achievements = db.Column(db.Boolean, default=True)
    enable_assignments = db.Column(db.Boolean, default=True)
    enable_email_alerts = db.Column(db.Boolean, default=False)

    # NEW: Auto-backup
    auto_backup_enabled = db.Column(db.Boolean, default=False)
    backup_interval_hours = db.Column(db.Integer, default=24)

    # NEW: Streaming settings
    enable_adaptive_streaming = db.Column(db.Boolean, default=True)
    max_rendition_height = db.Column(db.Integer, default=8640)  # Max quality to transcode to (up to 16K)
    hls_segment_duration = db.Column(db.Integer, default=6)  # HLS segment duration in seconds

    # NEW: Attendance settings — admin-configurable minimum attendance percentage.
    # Used to flag students/classes falling below the required threshold in
    # attendance session reports.
    min_attendance_percentage = db.Column(db.Float, default=75.0)

    # === ACADEMIC YEAR ROLLOVER & ARCHIVING SETTINGS ===
    scheduled_academic_year_end_date = db.Column(db.DateTime, nullable=True)
    academic_year_rollover_processed = db.Column(db.Boolean, default=False)
    # Global quest version for client polling & real-time sync
    quests_version = db.Column(db.Integer, default=1)


class DailyQuestTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    quest_key = db.Column(db.String(50), unique=True, nullable=False, index=True)
    title = db.Column(db.String(150), nullable=False)
    desc = db.Column(db.Text, nullable=False)
    xp = db.Column(db.Integer, default=50)
    icon = db.Column(db.String(50), default='event_available')
    target = db.Column(db.Integer, default=1)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)



class Quiz(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    video_id = db.Column(db.Integer, db.ForeignKey('video.id'), nullable=True, index=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey('classroom.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    time_limit_minutes = db.Column(db.Integer, default=0)
    shuffle_questions = db.Column(db.Boolean, default=False)
    # NEW: Passing percentage
    passing_percent = db.Column(db.Integer, default=50)
    # NEW: Max attempts
    max_attempts = db.Column(db.Integer, default=0)  # 0 = unlimited

    questions = db.relationship('Question', backref='quiz', lazy=True, cascade="all, delete-orphan")
    results = db.relationship('QuizResult', backref='quiz', lazy=True, cascade="all, delete-orphan")
    teacher = db.relationship('User', backref=db.backref('quizzes', lazy=True))


class Question(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    quiz_id = db.Column(db.Integer, db.ForeignKey('quiz.id'), nullable=False, index=True)
    text = db.Column(db.Text, nullable=False)
    option_a = db.Column(db.String(200), nullable=False)
    option_b = db.Column(db.String(200), nullable=False)
    option_c = db.Column(db.String(200), nullable=False)
    option_d = db.Column(db.String(200), nullable=False)
    correct_option = db.Column(db.String(1), nullable=False)
    explanation = db.Column(db.Text, nullable=True)
    # NEW: Points for this question
    points = db.Column(db.Integer, default=1)


class QuizResult(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    quiz_id = db.Column(db.Integer, db.ForeignKey('quiz.id'), nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    score = db.Column(db.Integer, nullable=False)
    total_questions = db.Column(db.Integer, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    answers_json = db.Column(db.Text, nullable=True)
    # NEW: Time taken
    time_taken_seconds = db.Column(db.Integer, default=0)
    # NEW: Pass/fail
    passed = db.Column(db.Boolean, default=False)

    # === ACADEMIC ARCHIVING FIELDS ===
    is_archived = db.Column(db.Boolean, default=False)
    archived_at = db.Column(db.DateTime, nullable=True)

    student = db.relationship('User', backref=db.backref('quiz_results', lazy=True))


class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey('classroom.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    message_type = db.Column(db.String(20), default='text')

    user = db.relationship('User', backref=db.backref('chat_messages', lazy=True))
    classroom = db.relationship('Classroom', backref=db.backref('messages', lazy='dynamic', cascade="all, delete-orphan"))


class Attendance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey('classroom.id'), nullable=False, index=True)
    date = db.Column(db.Date, default=lambda: datetime.utcnow().date(), index=True)
    status = db.Column(db.String(20), default='Absent', index=True)
    arrival_time = db.Column(db.DateTime)
    classroom_rel = db.relationship('Classroom', backref=db.backref('attendance_history', lazy=True))

    # NEW: link to an AttendanceSession (nullable so existing rows keep working)
    session_id = db.Column(db.Integer, db.ForeignKey('attendance_session.id'), nullable=True, index=True)

    __table_args__ = (
        db.Index('ix_attendance_class_date', 'classroom_id', 'date'),
        db.Index('ix_attendance_student_class', 'student_id', 'classroom_id'),
        db.Index('ix_attendance_session_student', 'session_id', 'student_id'),
    )

    # === ACADEMIC ARCHIVING FIELDS ===
    is_archived = db.Column(db.Boolean, default=False)
    archived_at = db.Column(db.DateTime, nullable=True)

    # Allowed status values (extends the old free-text 'Present'/'Absent'/'Late').
    # NOTE: 'Leave' has been removed — Present/Absent/Late/Half Day/Holiday/
    # Medical Leave/OD are the only recognised statuses now.
    STATUS_CHOICES = ['Present', 'Absent', 'Medical Leave', 'OD', 'Holiday', 'Late', 'Half Day']

    # Legacy status kept only so old rows marked 'Leave' before this change
    # still render correctly instead of crashing report pages.
    LEGACY_STATUS_CHOICES = STATUS_CHOICES + ['Leave']

    # ── Attendance percentage weighting ──
    # Present counts fully towards the percentage, Half Day/Late count as
    # half, Absent counts as zero. Holiday/Medical Leave/OD are "neutral" —
    # they are excluded entirely from the percentage calculation (no change
    # to the percentage either way).
    PERCENTAGE_WEIGHTS = {
        'Present': 1.0,
        'Late': 0.5,
        'Half Day': 0.5,
        'Absent': 0.0,
        'Leave': 0.0,  # legacy rows, treated like Absent if ever encountered
    }
    NEUTRAL_STATUSES = {'Holiday', 'Medical Leave', 'OD'}


class AttendanceSession(db.Model):
    """A teacher-defined attendance window: fixed start date, editable end date,
    with N sub-sessions (e.g. Period 1, Period 2, Morning, Evening, etc.)."""
    __tablename__ = 'attendance_session'
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey('classroom.id'), nullable=False, index=True)
    name = db.Column(db.String(150), nullable=False)
    start_date = db.Column(db.Date, nullable=False, index=True)   # fixed once created
    end_date = db.Column(db.Date, nullable=False, index=True)      # only the class teacher may edit this
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True, index=True)

    classroom = db.relationship('Classroom', backref=db.backref('attendance_sessions', lazy=True, cascade="all, delete-orphan"))
    creator = db.relationship('User', foreign_keys=[created_by])
    attendance_records = db.relationship('Attendance', backref='session', lazy=True)
    sub_sessions = db.relationship('AttendanceSubSession', backref='parent_session', lazy=True, cascade="all, delete-orphan")

    def can_edit_end_date(self, user):
        """Only the classroom's assigned Class Teacher (or an admin) may change end_date."""
        if user.role == 'admin':
            return True
        return self.classroom and self.classroom.teacher_id == user.id


class AttendanceSubSession(db.Model):
    """A single session/period within an AttendanceSession. Teachers can add unlimited
    sub-sessions (e.g. 'Period 1', 'Morning Roll Call', 'Lab Session')."""
    __tablename__ = 'attendance_sub_session'
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    attendance_session_id = db.Column(db.Integer, db.ForeignKey('attendance_session.id'), nullable=False, index=True)
    name = db.Column(db.String(150), nullable=False)
    session_date = db.Column(db.Date, default=lambda: datetime.utcnow().date(), index=True)
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ActivityLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    username = db.Column(db.String(150), nullable=True)
    action = db.Column(db.String(100), nullable=False)
    details = db.Column(db.Text, nullable=True)
    ip_address = db.Column(db.String(50), nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    user = db.relationship('User', backref=db.backref('activity_logs', lazy=True))


class SystemMetric(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    metric_name = db.Column(db.String(50), nullable=False, index=True)
    metric_value = db.Column(db.Float, nullable=False)
    recorded_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


# ═══════════════════════════════════════════════════════════════
# NEW MODELS: Assignments & Homework System
# ═══════════════════════════════════════════════════════════════

class Assignment(db.Model):
    """Teacher-created assignments with due dates."""
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey('classroom.id'), nullable=False, index=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    due_date = db.Column(db.DateTime, nullable=True, index=True)
    total_points = db.Column(db.Integer, default=100)
    assignment_type = db.Column(db.String(20), default='text')  # text, file, quiz
    allow_late_submission = db.Column(db.Boolean, default=False)
    late_penalty_percent = db.Column(db.Integer, default=10)

    # NEW: the question paper the teacher uploads (PDF or any document format)
    question_file_path = db.Column(db.String(500), nullable=True)
    question_file_name = db.Column(db.String(300), nullable=True)

    # NEW: teacher-controlled required response mode for students
    #   'either' = student may type OR upload (old default behavior)
    #   'type_only' = student must type an answer
    #   'file_only' = student must upload a document
    response_mode = db.Column(db.String(20), default='either')

    teacher = db.relationship('User', backref=db.backref('created_assignments', lazy=True))
    submissions = db.relationship('AssignmentSubmission', backref='assignment', lazy=True, cascade="all, delete-orphan")


class AssignmentSubmission(db.Model):
    """Student submission for an assignment."""
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey('assignment.id'), nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    content = db.Column(db.Text, nullable=True)  # Text response
    file_path = db.Column(db.String(500), nullable=True)  # File upload path
    file_name = db.Column(db.String(300), nullable=True)  # Original filename (any format)
    grade = db.Column(db.Float, nullable=True)  # NULL until graded
    feedback = db.Column(db.Text, nullable=True)
    graded_at = db.Column(db.DateTime, nullable=True)
    is_late = db.Column(db.Boolean, default=False)
    status = db.Column(db.String(20), default='submitted', index=True)  # submitted, graded, returned

    __table_args__ = (db.UniqueConstraint('assignment_id', 'student_id', name='unique_submission'),)


# ═══════════════════════════════════════════════════════════════
# NEW MODEL: Student Bio Data
# ═══════════════════════════════════════════════════════════════

class StudentProfile(db.Model):
    """Detailed bio-data profile for a student. One-to-one with User."""
    __tablename__ = 'student_profile'
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), unique=True, nullable=False)

    student_name = db.Column(db.String(200), nullable=True)
    student_id_number = db.Column(db.String(50), nullable=True)
    roll_number = db.Column(db.String(50), nullable=True)
    department = db.Column(db.String(150), nullable=True)
    year = db.Column(db.String(20), nullable=True)
    section = db.Column(db.String(20), nullable=True)
    requires_admin_review = db.Column(db.Boolean, default=False)  # Flagged on 4th year / final year rollover
    date_of_birth = db.Column(db.Date, nullable=True)
    gender = db.Column(db.String(20), nullable=True)
    blood_group = db.Column(db.String(10), nullable=True)
    phone_number = db.Column(db.String(20), nullable=True)
    email = db.Column(db.String(150), nullable=True)

    father_name = db.Column(db.String(150), nullable=True)
    mother_name = db.Column(db.String(150), nullable=True)
    father_phone = db.Column(db.String(20), nullable=True)
    mother_phone = db.Column(db.String(20), nullable=True)
    father_email = db.Column(db.String(150), nullable=True)
    mother_email = db.Column(db.String(150), nullable=True)
    guardian_name = db.Column(db.String(150), nullable=True)
    guardian_phone = db.Column(db.String(20), nullable=True)

    communication_address = db.Column(db.String(5000), nullable=True)
    permanent_address = db.Column(db.String(5000), nullable=True)

    nationality = db.Column(db.String(100), nullable=True)
    religion = db.Column(db.String(100), nullable=True)      # Optional
    category = db.Column(db.String(100), nullable=True)      # Optional

    emergency_contact = db.Column(db.String(150), nullable=True)
    emergency_phone = db.Column(db.String(20), nullable=True)

    photo_path = db.Column(db.String(500), nullable=True)
    signature_path = db.Column(db.String(500), nullable=True)

    # Document uploads
    aadhaar_path = db.Column(db.String(500), nullable=True)          # Optional
    transfer_certificate_path = db.Column(db.String(500), nullable=True)
    community_certificate_path = db.Column(db.String(500), nullable=True)
    other_certificates_json = db.Column(db.Text, default='[]')       # list of {name, path}

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    student = db.relationship('User', backref=db.backref('profile', uselist=False, cascade="all, delete-orphan"))

    def get_other_certificates(self):
        return json.loads(self.other_certificates_json or '[]')

    def add_other_certificate(self, name, path):
        certs = self.get_other_certificates()
        certs.append({'name': name, 'path': path})
        self.other_certificates_json = json.dumps(certs)


# ═══════════════════════════════════════════════════════════════
# NEW MODELS: Video Notes & Bookmarks
# ═══════════════════════════════════════════════════════════════

class VideoNote(db.Model):
    """Timestamped notes on videos."""
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    video_id = db.Column(db.Integer, db.ForeignKey('video.id'), nullable=False, index=True)
    timestamp_seconds = db.Column(db.Float, default=0)  # Where in the video
    content = db.Column(db.Text, nullable=False)
    color = db.Column(db.String(7), default='#fef08a')  # Highlight color
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class VideoBookmark(db.Model):
    """Bookmarked moments in videos."""
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    video_id = db.Column(db.Integer, db.ForeignKey('video.id'), nullable=False, index=True)
    timestamp_seconds = db.Column(db.Float, nullable=False)
    label = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class VideoProgress(db.Model):
    """Track video watch progress per student."""
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    video_id = db.Column(db.Integer, db.ForeignKey('video.id'), nullable=False, index=True)
    progress_seconds = db.Column(db.Float, default=0)  # Last watched position
    percent_complete = db.Column(db.Float, default=0.0)
    completed = db.Column(db.Boolean, default=False, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('user_id', 'video_id', name='unique_progress'),)


# ═══════════════════════════════════════════════════════════════
# NEW MODELS: Gamification - Achievements & Leaderboard
# ═══════════════════════════════════════════════════════════════

class Achievement(db.Model):
    """Achievement/badge definitions."""
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)  # e.g., 'first_login', 'quiz_master'
    title = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    icon_emoji = db.Column(db.String(10), default='🏆')
    xp_reward = db.Column(db.Integer, default=50)
    category = db.Column(db.String(30), default='general', index=True)  # general, quiz, video, attendance, social
    condition_type = db.Column(db.String(50), nullable=True)  # Type of condition to check
    condition_value = db.Column(db.Integer, default=1)  # Threshold value

    @staticmethod
    def seed_defaults():
        """Create default achievements."""
        defaults = [
            ('first_login', 'First Steps', 'Log in for the first time', '👋', 50, 'general', 'login_count', 1),
            ('streak_3', 'Hat Trick', 'Maintain a 3-day login streak', '🔥', 100, 'general', 'streak_days', 3),
            ('streak_7', 'Weekly Warrior', 'Maintain a 7-day login streak', '💪', 200, 'general', 'streak_days', 7),
            ('streak_30', 'Monthly Legend', 'Maintain a 30-day login streak', '🌟', 500, 'general', 'streak_days', 30),
            ('quiz_first', 'Quiz Novice', 'Complete your first quiz', '📝', 50, 'quiz', 'total_quizzes_taken', 1),
            ('quiz_10', 'Quiz Enthusiast', 'Complete 10 quizzes', '🎯', 200, 'quiz', 'total_quizzes_taken', 10),
            ('quiz_perfect', 'Perfect Score', 'Get 100% on any quiz', '💯', 300, 'quiz', 'perfect_quiz', 1),
            ('video_10', 'Video Watcher', 'Watch 10 videos', '🎬', 100, 'video', 'videos_watched', 10),
            ('video_50', 'Video Marathoner', 'Watch 50 videos', '📺', 300, 'video', 'videos_watched', 50),
            ('xp_1000', 'XP Hunter', 'Earn 1000 XP', '⚡', 100, 'general', 'xp', 1000),
            ('xp_5000', 'XP Champion', 'Earn 5000 XP', '🏅', 500, 'general', 'xp', 5000),
            ('attendance_perfect', 'Perfect Attendance', 'Get 100% attendance for a month', '📋', 300, 'attendance', 'monthly_perfect', 1),
            ('comment_first', 'First Comment', 'Post your first comment', '💬', 30, 'social', 'comments_count', 1),
            ('level_5', 'Level Up!', 'Reach Level 5', '⭐', 200, 'general', 'level', 5),
            ('level_10', 'Veteran', 'Reach Level 10', '👑', 500, 'general', 'level', 10),
            ('upload_first', 'Content Creator', 'Upload your first video', '🎥', 100, 'video', 'uploads_count', 1),
        ]
        for code, title, desc, icon, xp, cat, cond_type, cond_val in defaults:
            if not Achievement.query.filter_by(code=code).first():
                db.session.add(Achievement(
                    code=code, title=title, description=desc,
                    icon_emoji=icon, xp_reward=xp, category=cat,
                    condition_type=cond_type, condition_value=cond_val
                ))
        db.session.commit()


class LeaderboardEntry(db.Model):
    """Cached leaderboard entries for quick retrieval."""
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    username = db.Column(db.String(150), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    xp = db.Column(db.Integer, default=0, index=True)
    level = db.Column(db.Integer, default=1)
    streak_days = db.Column(db.Integer, default=0)
    quiz_count = db.Column(db.Integer, default=0)
    category = db.Column(db.String(30), default='global', index=True)  # global, class_{id}, school
    rank = db.Column(db.Integer, default=0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('leaderboard_entries', lazy=True))


# ═══════════════════════════════════════════════════════════════
# NEW MODEL: Email Queue for async email sending
# ═══════════════════════════════════════════════════════════════

class EmailQueue(db.Model):
    """Queue for sending emails asynchronously."""
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    recipient_email = db.Column(db.String(200), nullable=False)
    subject = db.Column(db.String(300), nullable=False)
    body_html = db.Column(db.Text, nullable=True)
    body_text = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default='pending', index=True)  # pending, sent, failed
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    sent_at = db.Column(db.DateTime, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    retry_count = db.Column(db.Integer, default=0)


# ═══════════════════════════════════════════════════════════════
# NEW MODELS: Advanced Teacher Email Automation System
# ═══════════════════════════════════════════════════════════════

class StudentRemark(db.Model):
    """Remarks given by a teacher to a student in a classroom."""
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey('classroom.id'), nullable=False, index=True)
    remark = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    student = db.relationship('User', backref=db.backref('student_remarks', lazy='dynamic', cascade='all, delete-orphan'))
    classroom = db.relationship('Classroom', backref=db.backref('student_remarks_rel', lazy='dynamic', cascade='all, delete-orphan'))
    
    __table_args__ = (db.UniqueConstraint('student_id', 'classroom_id', name='unique_student_classroom_remark'),)


class EmailDeliveryLog(db.Model):
    """Logs report email delivery status."""
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    execution_id = db.Column(db.String(50), nullable=False)
    class_id = db.Column(db.Integer, db.ForeignKey('classroom.id'), nullable=False, index=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    student_email = db.Column(db.String(150), nullable=False)
    subject = db.Column(db.String(256), nullable=False)
    status = db.Column(db.String(20), default='pending', index=True)  # sent, failed
    error_message = db.Column(db.Text, nullable=True)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    retry_count = db.Column(db.Integer, default=0)
    report_type = db.Column(db.String(30), default='daily_scheduled')  # daily_scheduled, admin_trigger
    report_html = db.Column(db.Text, nullable=True)

    classroom = db.relationship('Classroom', backref=db.backref('delivery_logs', lazy='dynamic', cascade='all, delete-orphan'))
    teacher = db.relationship('User', foreign_keys=[teacher_id], backref=db.backref('sent_reports', lazy='dynamic'))
    student = db.relationship('User', foreign_keys=[student_id], backref=db.backref('received_reports', lazy='dynamic'))

# ═══════════════════════════════════════════════════════════════
# NEW MODEL: Persistent Conversion Job System
# ═══════════════════════════════════════════════════════════════

class ConversionJob(db.Model):
    """Database-backed persistent job record for HLS video conversions."""
    __tablename__ = 'conversion_job'
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.String(64), unique=True, index=True, nullable=False)
    video_id = db.Column(db.Integer, db.ForeignKey('video.id', ondelete='CASCADE'), nullable=False, index=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)

    input_file = db.Column(db.String(500), nullable=False)
    output_directory = db.Column(db.String(500), nullable=False)

    # Statuses: queued, processing, paused, interrupted, completed, failed
    status = db.Column(db.String(20), default='queued', index=True)
    progress = db.Column(db.Integer, default=0)

    current_segment = db.Column(db.Integer, default=0)
    total_segments = db.Column(db.Integer, default=0)
    last_processed_position = db.Column(db.Float, default=0.0)

    worker_id = db.Column(db.String(50), nullable=True)
    retry_count = db.Column(db.Integer, default=0)
    max_retries = db.Column(db.Integer, default=3)
    error_message = db.Column(db.Text, nullable=True)

    renditions_state_json = db.Column(db.Text, default='{}')

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    started_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)

    video = db.relationship('Video', backref=db.backref('conversion_jobs', lazy=True, cascade='all, delete-orphan'))

    def get_renditions_state(self):
        return json.loads(self.renditions_state_json or '{}')

    def set_renditions_state(self, state_dict):
        self.renditions_state_json = json.dumps(state_dict)

    def to_dict(self):
        inst_name = 'Global'
        uploader_name = 'Unknown'
        uploader_role = None
        inst_id = self.institution_id
        if self.video:
            if not inst_id and self.video.institution_id:
                inst_id = self.video.institution_id
            if self.video.uploader:
                uploader_name = self.video.uploader.username
                uploader_role = self.video.uploader.role
                if self.video.uploader.institution:
                    inst_name = self.video.uploader.institution.name
                    if not inst_id:
                        inst_id = self.video.uploader.institution_id

        return {
            'id': self.id,
            'job_id': self.job_id,
            'video_id': self.video_id,
            'video_title': self.video.title if self.video else 'Unknown',
            'institution_id': inst_id,
            'institution_name': inst_name,
            'uploader_name': uploader_name,
            'uploader_role': uploader_role,
            'input_file': self.input_file,
            'output_directory': self.output_directory,
            'status': self.status,
            'progress': self.progress,
            'current_segment': self.current_segment,
            'total_segments': self.total_segments,
            'last_processed_position': self.last_processed_position,
            'worker_id': self.worker_id,
            'retry_count': self.retry_count,
            'max_retries': self.max_retries,
            'error_message': self.error_message,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M:%S') if self.created_at else None,
            'started_at': self.started_at.strftime('%Y-%m-%d %H:%M:%S') if self.started_at else None,
            'updated_at': self.updated_at.strftime('%Y-%m-%d %H:%M:%S') if self.updated_at else None,
            'completed_at': self.completed_at.strftime('%Y-%m-%d %H:%M:%S') if self.completed_at else None,
        }


# ═══════════════════════════════════════════════════════════════
# NEW MODEL: Weekly Class Performance & XP Digest
# ═══════════════════════════════════════════════════════════════

class ClassWeeklyReport(db.Model):
    """Weekly/Periodic Consolidated Class Performance, XP, Attendance & Assessment Report."""
    __tablename__ = 'class_weekly_report'
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey('classroom.id'), nullable=False, index=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)

    period_start = db.Column(db.Date, nullable=False, index=True)
    period_end = db.Column(db.Date, nullable=False, index=True)
    generated_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    # Consolidated class summary KPIs
    total_students = db.Column(db.Integer, default=0)
    avg_attendance_pct = db.Column(db.Float, default=0.0)
    total_xp_gained = db.Column(db.Integer, default=0)
    avg_quiz_score_pct = db.Column(db.Float, default=0.0)
    total_video_watch_seconds = db.Column(db.Integer, default=0)

    # Granular per-student telemetry and metrics (JSON encoded)
    report_data_json = db.Column(db.Text, default='{}')
    teacher_remarks = db.Column(db.Text, nullable=True)

    # Principal / Admin Dispatch Tracking
    status = db.Column(db.String(30), default='generated', index=True)  # 'generated', 'sent_to_admin', 'reviewed'
    sent_to_admin_at = db.Column(db.DateTime, nullable=True)
    admin_feedback = db.Column(db.Text, nullable=True)

    classroom = db.relationship('Classroom', backref=db.backref('weekly_reports', lazy=True, cascade='all, delete-orphan'))
    teacher = db.relationship('User', foreign_keys=[teacher_id])

    def get_report_data(self):
        return json.loads(self.report_data_json or '{}')

    def set_report_data(self, data_dict):
        self.report_data_json = json.dumps(data_dict)


# ═══════════════════════════════════════════════════════════════
# NEW MODELS: Video Checkpoints, Doubts, Flashcards, Certificates, Parent Portal
# ═══════════════════════════════════════════════════════════════

class VideoCheckpoint(db.Model):
    """In-stream pop-up comprehension checkpoints in videos."""
    __tablename__ = 'video_checkpoint'
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    video_id = db.Column(db.Integer, db.ForeignKey('video.id'), nullable=False, index=True)
    timestamp_seconds = db.Column(db.Float, nullable=False, index=True)
    question_text = db.Column(db.Text, nullable=False)
    option_a = db.Column(db.String(300), nullable=False)
    option_b = db.Column(db.String(300), nullable=False)
    option_c = db.Column(db.String(300), nullable=True)
    option_d = db.Column(db.String(300), nullable=True)
    correct_option = db.Column(db.String(1), nullable=False)  # 'a', 'b', 'c', 'd'
    explanation = db.Column(db.Text, nullable=True)
    xp_reward = db.Column(db.Integer, default=25)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    video = db.relationship('Video', backref=db.backref('checkpoints', lazy=True, cascade='all, delete-orphan'))
    responses = db.relationship('CheckpointResponse', backref='checkpoint', lazy=True, cascade='all, delete-orphan')


class CheckpointResponse(db.Model):
    """Student responses to in-video checkpoints."""
    __tablename__ = 'checkpoint_response'
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    checkpoint_id = db.Column(db.Integer, db.ForeignKey('video_checkpoint.id'), nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    selected_option = db.Column(db.String(1), nullable=False)
    is_correct = db.Column(db.Boolean, default=False)
    answered_at = db.Column(db.DateTime, default=datetime.utcnow)

    student = db.relationship('User', backref=db.backref('checkpoint_responses', lazy=True, cascade='all, delete-orphan'))


class VideoDoubt(db.Model):
    """Time-stamped student doubts / Q&A threads in videos."""
    __tablename__ = 'video_doubt'
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    video_id = db.Column(db.Integer, db.ForeignKey('video.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    timestamp_seconds = db.Column(db.Float, default=0.0, index=True)
    question_text = db.Column(db.Text, nullable=False)
    is_resolved = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    user = db.relationship('User', backref=db.backref('video_doubts', lazy=True, cascade='all, delete-orphan'))
    video = db.relationship('Video', backref=db.backref('doubts', lazy=True, cascade='all, delete-orphan'))
    replies = db.relationship('VideoDoubtReply', backref='doubt', lazy=True, cascade='all, delete-orphan')


class VideoDoubtReply(db.Model):
    """Replies to video doubts."""
    __tablename__ = 'video_doubt_reply'
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    doubt_id = db.Column(db.Integer, db.ForeignKey('video_doubt.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    content = db.Column(db.Text, nullable=False)
    is_teacher_endorsed = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', foreign_keys=[user_id])


class VideoFlashcard(db.Model):
    """AI-generated or manual revision flashcards."""
    __tablename__ = 'video_flashcard'
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    video_id = db.Column(db.Integer, db.ForeignKey('video.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    front_term = db.Column(db.String(300), nullable=False)
    back_definition = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    video = db.relationship('Video', backref=db.backref('flashcards', lazy=True, cascade='all, delete-orphan'))


class AcademicCertificate(db.Model):
    """Verifiable certificates of course completion & milestone excellence."""
    __tablename__ = 'academic_certificate'
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    certificate_code = db.Column(db.String(64), unique=True, nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    certificate_type = db.Column(db.String(50), default='course_completion', index=True)
    issued_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    criteria_met_json = db.Column(db.Text, default='{}')

    student = db.relationship('User', backref=db.backref('certificates', lazy=True, cascade='all, delete-orphan'))


class ParentAccessToken(db.Model):
    """Secure, tokenized read-only portal access for parents."""
    __tablename__ = 'parent_access_token'
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=True, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=True)
    is_active = db.Column(db.Boolean, default=True, index=True)
    last_accessed_at = db.Column(db.DateTime, nullable=True)

    student = db.relationship('User', backref=db.backref('parent_tokens', lazy=True, cascade='all, delete-orphan'))


# ═══════════════════════════════════════════════════════════════
# MULTI-TENANCY INTERCEPTORS (Automatic Query & Insert Isolation)
# ═══════════════════════════════════════════════════════════════
from flask import has_request_context, g
from flask_login import current_user
from sqlalchemy.event import listens_for
from sqlalchemy.orm import Query

def _get_request_tenant_context():
    """Cache and return (is_auth, role, inst_id) on flask.g to avoid redundant property lookups."""
    cached = getattr(g, '_cached_tenant_context', None)
    if cached is not None:
        return cached
    if getattr(g, 'loading_user', False):
        return False, None, None
    g.loading_user = True
    try:
        is_auth = current_user.is_authenticated
        role = current_user.role if is_auth else None
        inst_id = current_user.institution_id if is_auth else None
    except Exception:
        is_auth, role, inst_id = False, None, None
    finally:
        g.loading_user = False
    result = (is_auth, role, inst_id)
    g._cached_tenant_context = result
    return result

@listens_for(Query, "before_compile", retval=True)
def before_compile_listener(query):
    if has_request_context():
        # Avoid infinite recursion during user load or custom overrides
        if getattr(g, 'ignore_tenant_filter', False):
            return query
            
        is_auth, role, inst_id = _get_request_tenant_context()
            
        if is_auth and role != 'system_admin' and inst_id is not None:
            for desc in query.column_descriptions:
                entity = desc['entity']
                if entity and hasattr(entity, 'institution_id'):
                    orig_limit = query._limit_clause
                    orig_offset = query._offset_clause
                    query = query._clone()
                    query._limit_clause = None
                    query._offset_clause = None
                    query = query.filter(entity.institution_id == inst_id)
                    query._limit_clause = orig_limit
                    query._offset_clause = orig_offset
    return query

@listens_for(db.Model, 'before_insert', propagate=True)
def before_insert_listener(mapper, connection, target):
    if hasattr(target, 'institution_id') and getattr(target, 'institution_id') is None:
        if has_request_context():
            is_auth, role, inst_id = _get_request_tenant_context()
            if is_auth and role != 'system_admin' and inst_id is not None:
                target.institution_id = inst_id

@listens_for(db.Model, 'before_update', propagate=True)
def before_update_listener(mapper, connection, target):
    if hasattr(target, 'institution_id'):
        if has_request_context():
            is_auth, role, inst_id = _get_request_tenant_context()
            if is_auth and role != 'system_admin' and inst_id is not None:
                target.institution_id = inst_id


def backfill_all_tables_with_default_institution(db, logger=None):
    try:
        default_inst = Institution.query.filter_by(slug='default').first()
        if not default_inst:
            default_inst = Institution(name='Default Institution', slug='default', status='active')
            db.session.add(default_inst)
            db.session.commit()
            if logger:
                logger.info(f"Created Default Institution (id={default_inst.id})")
            else:
                print(f"Created Default Institution (id={default_inst.id})")
                
        tables_to_backfill = [
            User, Video, Playlist, Classroom, Comment, ViewAnalytics, Notification,
            SiteSettings, Quiz, Question, QuizResult, ChatMessage, Attendance,
            AttendanceSession, AttendanceSubSession, ActivityLog, SystemMetric,
            Assignment, AssignmentSubmission, StudentProfile, VideoNote,
            VideoBookmark, VideoProgress, LeaderboardEntry, EmailQueue,
            StudentRemark, EmailDeliveryLog, ConversionJob, ClassWeeklyReport,
            VideoCheckpoint, CheckpointResponse, VideoDoubt, VideoDoubtReply,
            VideoFlashcard, AcademicCertificate, ParentAccessToken
        ]
        
        # Bypass before_compile filter by setting ignore flag on g
        from flask import has_request_context, g
        orig_ignore = False
        if has_request_context():
            orig_ignore = getattr(g, 'ignore_tenant_filter', False)
            g.ignore_tenant_filter = True
            
        try:
            for model in tables_to_backfill:
                if hasattr(model, 'institution_id'):
                    if model == User:
                        User.query.filter(User.institution_id == None).filter(User.role != 'system_admin').update(
                            {User.institution_id: default_inst.id},
                            synchronize_session=False
                        )
                    elif model == SiteSettings:
                        if not SiteSettings.query.filter_by(institution_id=default_inst.id).first():
                            first_settings = SiteSettings.query.filter_by(institution_id=None).first()
                            if first_settings:
                                first_settings.institution_id = default_inst.id
                            else:
                                db.session.add(SiteSettings(institution_id=default_inst.id))
                    else:
                        model.query.filter(model.institution_id == None).update(
                            {model.institution_id: default_inst.id},
                            synchronize_session=False
                        )
            db.session.commit()
            if logger:
                logger.info("Backfilled existing records into Default Institution")
            else:
                print("Backfilled existing records into Default Institution")
        finally:
            if has_request_context():
                g.ignore_tenant_filter = orig_ignore
                
    except Exception as e:
        db.session.rollback()
        if logger:
            logger.warning(f"Error backfilling default institution: {e}")
        else:
            print(f"Error backfilling default institution: {e}")

