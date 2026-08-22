"""
Database migration script for new columns/models.
Run this after updating models.py to add new columns.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from factory import create_app
from extensions import db
from models import *

app = create_app()

def migrate():
    """Add new columns and tables to existing database."""
    with app.app_context():
        sys.stdout.reconfigure(encoding='utf-8')  # Force UTF-8
        
        print("=" * 60)
        print("CampusPlayer Database Migration")
        print("=" * 60)

        # Automated Pre-Migration Backup
        from services.backup_engine import create_backup
        ok, backup_res = create_backup(app)
        if not ok:
            print(f"❌ [Migration Error] Pre-migration database backup failed: {backup_res}")
            print("Aborting migration to preserve database integrity.")
            sys.exit(1)
        print(f"[Backup] Pre-migration database backup verified: {backup_res}\n")

        
        # Rebuild user table to scope unique username to institution_id
        try:
            import sqlite3
            if db.engine.dialect.name == 'sqlite':
                db_uri = str(app.config.get('SQLALCHEMY_DATABASE_URI', ''))
                db_path = db_uri.replace('sqlite:///', '')
                if not os.path.isabs(db_path):
                    db_path = os.path.abspath(db_path)
                if os.path.exists(db_path):
                    conn = sqlite3.connect(db_path)
                    cursor = conn.cursor()
                    cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='user'")
                    row = cursor.fetchone()
                    if row:
                        sql = row[0]
                        if 'unique' in sql.lower() and 'uq_user_username_institution' not in sql.lower() and 'unique(username)' not in sql.lower().replace(' ', ''):
                            print("[Migration] Rebuilding 'user' table to remove single UNIQUE constraint on username...")
                            cursor.execute("PRAGMA table_info(user)")
                            columns = [r[1] for r in cursor.fetchall()]
                            cols_str = ", ".join(columns)
                            cursor.execute("ALTER TABLE user RENAME TO old_user")
                            new_table_sql = """
                            CREATE TABLE user (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                username VARCHAR(150) NOT NULL,
                                password_hash VARCHAR(256) NOT NULL,
                                role VARCHAR(20) NOT NULL,
                                institution_id INTEGER,
                                is_active_account BOOLEAN DEFAULT 1,
                                xp INTEGER DEFAULT 0,
                                phone VARCHAR(20),
                                parent_email VARCHAR(150),
                                parent_name VARCHAR(150),
                                created_at DATETIME,
                                email VARCHAR(150),
                                email_sender_address VARCHAR(150),
                                encrypted_app_password VARCHAR(500),
                                email_enabled BOOLEAN DEFAULT 0,
                                last_report_sent DATETIME,
                                display_name VARCHAR(150),
                                avatar_url VARCHAR(500),
                                theme_preference VARCHAR(10) DEFAULT 'dark',
                                bio TEXT,
                                last_login DATETIME,
                                last_active DATETIME,
                                login_count INTEGER DEFAULT 0,
                                session_version INTEGER DEFAULT 1,
                                level INTEGER DEFAULT 1,
                                streak_days INTEGER DEFAULT 0,
                                last_streak_date DATE,
                                total_quiz_score INTEGER DEFAULT 0,
                                total_quizzes_taken INTEGER DEFAULT 0,
                                achievements_json TEXT DEFAULT '[]',
                                quests_json TEXT DEFAULT '{}',
                                equipped_avatar_frame VARCHAR(100),
                                equipped_title VARCHAR(100),
                                equipped_badge VARCHAR(100),
                                UNIQUE (username, institution_id),
                                FOREIGN KEY(institution_id) REFERENCES institution(id)
                            )
                            """
                            cursor.execute(new_table_sql)
                            cursor.execute(f"INSERT INTO user ({cols_str}) SELECT {cols_str} FROM old_user")
                            cursor.execute("DROP TABLE old_user")
                            conn.commit()
                            print("[Migration] Rebuilt 'user' table successfully!")
                    conn.close()
        except Exception as e:
            print(f"[Migration] [!] Note rebuilding user table: {e}")

        # Create all new tables
        print("\n[Tables] Creating new tables...")
        db.create_all()
        print("[OK] Tables created successfully")
        
        # Try to add new columns to existing tables
        print("\n[Columns] Checking for new columns...")
        
        # Connect and add columns
        inspector = db.inspect(db.engine)
        
        migrations = {
            'institution': {
                'columns': [
                    ('slug', 'VARCHAR(100)'),
                    ('owner_admin_id', 'INTEGER'),
                    ('status', 'VARCHAR(20) DEFAULT \'active\''),
                    ('logo_url', 'VARCHAR(500)'),
                    ('storage_root', 'VARCHAR(500)'),
                    ('storage_used_bytes', 'BIGINT DEFAULT 0'),
                    ('allow_manual_video_delete', 'BOOLEAN DEFAULT 1'),
                    ('allow_auto_video_delete', 'BOOLEAN DEFAULT 1'),
                    ('max_video_retention_days', 'INTEGER DEFAULT 365'),
                ],
                'indexes': [
                    ('ix_institution_slug', 'slug'),
                    ('ix_institution_status', 'status'),
                    ('ix_institution_owner_admin_id', 'owner_admin_id'),
                ]
            },
            'user': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('is_active_account', 'BOOLEAN DEFAULT 1'),
                    ('xp', 'INTEGER DEFAULT 0'),
                    ('phone', 'VARCHAR(20)'),
                    ('parent_email', 'VARCHAR(150)'),
                    ('parent_name', 'VARCHAR(150)'),
                    ('created_at', 'DATETIME'),
                    ('email', 'VARCHAR(150)'),
                    ('email_sender_address', 'VARCHAR(150)'),
                    ('encrypted_app_password', 'VARCHAR(500)'),
                    ('email_enabled', 'BOOLEAN DEFAULT 0'),
                    ('last_report_sent', 'DATETIME'),
                    ('display_name', 'VARCHAR(150)'),
                    ('avatar_url', 'VARCHAR(500)'),
                    ('theme_preference', 'VARCHAR(10) DEFAULT \'dark\''),
                    ('bio', 'TEXT'),
                    ('last_login', 'DATETIME'),
                    ('last_active', 'DATETIME'),
                    ('login_count', 'INTEGER DEFAULT 0'),
                    ('session_version', 'INTEGER DEFAULT 1'),
                    ('level', 'INTEGER DEFAULT 1'),
                    ('streak_days', 'INTEGER DEFAULT 0'),
                    ('last_streak_date', 'DATE'),
                    ('total_quiz_score', 'INTEGER DEFAULT 0'),
                    ('total_quizzes_taken', 'INTEGER DEFAULT 0'),
                    ('achievements_json', 'TEXT DEFAULT \'[]\''),
                    ('quests_json', 'TEXT DEFAULT \'{}\''),
                    ('equipped_avatar_frame', 'VARCHAR(100)'),
                    ('equipped_title', 'VARCHAR(100)'),
                    ('equipped_badge', 'VARCHAR(100)'),
                ],
                'indexes': [
                    ('ix_user_role', 'role'),
                    ('ix_user_xp', 'xp'),
                    ('ix_user_institution_id', 'institution_id'),
                    ('ix_user_is_active', 'is_active_account'),
                    ('ix_user_created_at', 'created_at'),
                    ('ix_user_last_active', 'last_active'),
                    ('ix_user_level', 'level'),
                    ('ix_user_inst_role', 'institution_id, role'),
                    ('ix_user_institution_role', 'institution_id, role'),
                ]
            },
            'video': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('title', 'VARCHAR(200)'),
                    ('filename', 'VARCHAR(300)'),
                    ('hls_playlist_path', 'VARCHAR(500)'),
                    ('thumbnail_path', 'VARCHAR(500)'),
                    ('upload_date', 'DATETIME'),
                    ('uploader_id', 'INTEGER'),
                    ('classroom_id', 'INTEGER'),
                    ('video_type', 'VARCHAR(20) DEFAULT \'local\''),
                    ('youtube_id', 'VARCHAR(100)'),
                    ('youtube_url', 'VARCHAR(500)'),
                    ('status', 'VARCHAR(20) DEFAULT \'pending\''),
                    ('processing_progress', 'INTEGER DEFAULT 0'),
                    ('processing_error', 'TEXT'),
                    ('description', 'TEXT'),
                    ('duration_seconds', 'INTEGER DEFAULT 0'),
                    ('tags', 'VARCHAR(500)'),
                    ('language', 'VARCHAR(10) DEFAULT \'en\''),
                    ('subtitle_path', 'VARCHAR(500)'),
                    ('subtitle_language', 'VARCHAR(10) DEFAULT \'en\''),
                    ('view_count', 'INTEGER DEFAULT 0'),
                    ('like_count', 'INTEGER DEFAULT 0'),
                    ('chapters_json', 'TEXT'),
                    ('difficulty', 'VARCHAR(20) DEFAULT \'intermediate\''),
                    ('auto_delete_at', 'DATETIME'),
                    ('retention_days', 'INTEGER'),
                    ('is_archived', 'BOOLEAN DEFAULT 0'),
                    ('archived_at', 'DATETIME'),
                    ('ai_summary', 'TEXT'),
                    ('ai_key_takeaways', 'TEXT'),
                    ('ai_summary_generated_at', 'DATETIME'),
                    ('master_playlist_path', 'VARCHAR(500)'),
                    ('available_renditions', 'TEXT DEFAULT \'[]\''),
                    ('source_width', 'INTEGER DEFAULT 0'),
                    ('source_height', 'INTEGER DEFAULT 0'),
                    ('source_bitrate', 'INTEGER DEFAULT 0'),
                    ('video_codec', 'VARCHAR(50) DEFAULT \'h264\''),
                    ('audio_codec', 'VARCHAR(50) DEFAULT \'aac\''),
                    ('fps', 'FLOAT DEFAULT 0.0'),
                    ('has_adaptive_streams', 'BOOLEAN DEFAULT 0'),
                    ('sprite_path', 'VARCHAR(500)'),
                    ('sprite_tile_count', 'INTEGER DEFAULT 0'),
                    ('thumbnails_vtt_path', 'VARCHAR(500)'),
                ],
                'indexes': [
                    ('ix_video_institution_id', 'institution_id'),
                    ('ix_video_uploader_id', 'uploader_id'),
                    ('ix_video_classroom_id', 'classroom_id'),
                    ('ix_video_status', 'status'),
                    ('ix_video_upload_date', 'upload_date'),
                    ('ix_video_video_type', 'video_type'),
                    ('ix_video_youtube_id', 'youtube_id'),
                    ('ix_video_auto_delete_at', 'auto_delete_at'),
                ]
            },
            'video_like': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('user_id', 'INTEGER'),
                    ('video_id', 'INTEGER'),
                    ('created_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_video_like_institution_id', 'institution_id'),
                    ('ix_video_like_user_id', 'user_id'),
                    ('ix_video_like_video_id', 'video_id'),
                ]
            },
            'playlist': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('title', 'VARCHAR(150)'),
                    ('thumbnail_path', 'VARCHAR(500)'),
                    ('created_at', 'DATETIME'),
                    ('creator_id', 'INTEGER'),
                    ('description', 'TEXT'),
                ],
                'indexes': [
                    ('ix_playlist_institution_id', 'institution_id'),
                    ('ix_playlist_creator_id', 'creator_id'),
                    ('ix_playlist_created_at', 'created_at'),
                ]
            },
            'classroom': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('name', 'VARCHAR(100)'),
                    ('teacher_id', 'INTEGER'),
                    ('created_at', 'DATETIME'),
                    ('start_time', 'VARCHAR(5) DEFAULT \'09:10\''),
                    ('description', 'TEXT'),
                    ('class_code', 'VARCHAR(10)'),
                    ('color_theme', 'VARCHAR(7) DEFAULT \'#4f46e5\''),
                ],
                'indexes': [
                    ('ix_classroom_institution_id', 'institution_id'),
                    ('ix_classroom_teacher_id', 'teacher_id'),
                    ('ix_classroom_class_code', 'class_code'),
                ]
            },
            'comment': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('content', 'TEXT'),
                    ('timestamp', 'DATETIME'),
                    ('user_id', 'INTEGER'),
                    ('video_id', 'INTEGER'),
                    ('parent_id', 'INTEGER'),
                    ('edited', 'BOOLEAN DEFAULT 0'),
                    ('edited_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_comment_institution_id', 'institution_id'),
                    ('ix_comment_video_id', 'video_id'),
                    ('ix_comment_user_id', 'user_id'),
                    ('ix_comment_parent_id', 'parent_id'),
                    ('ix_comment_timestamp', 'timestamp'),
                ]
            },
            'view_analytics': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('user_id', 'INTEGER'),
                    ('video_id', 'INTEGER'),
                    ('start_time', 'DATETIME'),
                    ('end_time', 'DATETIME'),
                    ('duration_seconds', 'INTEGER DEFAULT 0'),
                    ('percent_watched', 'FLOAT DEFAULT 0.0'),
                    ('completed', 'BOOLEAN DEFAULT 0'),
                    ('ip_address', 'VARCHAR(50)'),
                    ('user_agent', 'VARCHAR(300)'),
                    ('quality_selected', 'VARCHAR(20)'),
                ],
                'indexes': [
                    ('ix_view_analytics_institution_id', 'institution_id'),
                    ('ix_view_analytics_user_id', 'user_id'),
                    ('ix_view_analytics_video_id', 'video_id'),
                    ('ix_view_analytics_start_time', 'start_time'),
                    ('ix_view_analytics_completed', 'completed'),
                ]
            },
            'notification': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('user_id', 'INTEGER'),
                    ('message', 'TEXT'),
                    ('video_id', 'INTEGER'),
                    ('comment_id', 'INTEGER'),
                    ('is_read', 'BOOLEAN DEFAULT 0'),
                    ('created_at', 'DATETIME'),
                    ('notification_type', 'VARCHAR(30) DEFAULT \'info\''),
                    ('action_url', 'VARCHAR(500)'),
                ],
                'indexes': [
                    ('ix_notification_institution_id', 'institution_id'),
                    ('ix_notification_user_id', 'user_id'),
                    ('ix_notification_video_id', 'video_id'),
                    ('ix_notification_is_read', 'is_read'),
                    ('ix_notification_created_at', 'created_at'),
                ]
            },
            'site_settings': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('institution_name', 'VARCHAR(200) DEFAULT \'Campus Player\''),
                    ('lock_video_speed', 'BOOLEAN DEFAULT 0'),
                    ('lock_video_skipping', 'BOOLEAN DEFAULT 0'),
                    ('global_playlist_thumbnail', 'VARCHAR(500)'),
                    ('attendance_lock_time', 'VARCHAR(5) DEFAULT \'09:10\''),
                    ('admin_sms_phone', 'VARCHAR(20)'),
                    ('gemini_api_key', 'VARCHAR(200)'),
                    ('allow_self_registration', 'BOOLEAN DEFAULT 0'),
                    ('max_video_size_mb', 'INTEGER DEFAULT 20480'),
                    ('default_language', 'VARCHAR(10) DEFAULT \'en\''),
                    ('enable_notifications', 'BOOLEAN DEFAULT 1'),
                    ('session_timeout_minutes', 'INTEGER DEFAULT 120'),
                    ('primary_color', 'VARCHAR(7) DEFAULT \'#d4af37\''),
                    ('logo_url', 'VARCHAR(500)'),
                    ('smtp_server', 'VARCHAR(200)'),
                    ('smtp_port', 'INTEGER DEFAULT 587'),
                    ('smtp_username', 'VARCHAR(200)'),
                    ('smtp_password', 'VARCHAR(200)'),
                    ('email_from', 'VARCHAR(200)'),
                    ('enable_leaderboard', 'BOOLEAN DEFAULT 1'),
                    ('enable_achievements', 'BOOLEAN DEFAULT 1'),
                    ('enable_assignments', 'BOOLEAN DEFAULT 1'),
                    ('enable_email_alerts', 'BOOLEAN DEFAULT 0'),
                    ('auto_backup_enabled', 'BOOLEAN DEFAULT 0'),
                    ('backup_interval_hours', 'INTEGER DEFAULT 24'),
                    ('enable_adaptive_streaming', 'BOOLEAN DEFAULT 1'),
                    ('max_rendition_height', 'INTEGER DEFAULT 8640'),
                    ('hls_segment_duration', 'INTEGER DEFAULT 6'),
                    ('min_attendance_percentage', 'FLOAT DEFAULT 75.0'),
                    ('scheduled_academic_year_end_date', 'DATETIME'),
                    ('academic_year_rollover_processed', 'BOOLEAN DEFAULT 0'),
                    ('quests_version', 'INTEGER DEFAULT 1'),
                ],
                'indexes': [
                    ('ix_site_settings_institution_id', 'institution_id'),
                ]
            },
            'daily_quest_template': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('quest_key', 'VARCHAR(50)'),
                    ('title', 'VARCHAR(150)'),
                    ('desc', 'TEXT'),
                    ('xp', 'INTEGER DEFAULT 50'),
                    ('icon', 'VARCHAR(50) DEFAULT \'event_available\''),
                    ('target', 'INTEGER DEFAULT 1'),
                    ('is_active', 'BOOLEAN DEFAULT 1'),
                    ('created_at', 'DATETIME'),
                    ('updated_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_daily_quest_template_institution_id', 'institution_id'),
                    ('ix_daily_quest_template_quest_key', 'quest_key'),
                ]
            },
            'quiz': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('title', 'VARCHAR(200)'),
                    ('description', 'TEXT'),
                    ('teacher_id', 'INTEGER'),
                    ('video_id', 'INTEGER'),
                    ('classroom_id', 'INTEGER'),
                    ('created_at', 'DATETIME'),
                    ('time_limit_minutes', 'INTEGER DEFAULT 0'),
                    ('shuffle_questions', 'BOOLEAN DEFAULT 0'),
                    ('passing_percent', 'INTEGER DEFAULT 50'),
                    ('max_attempts', 'INTEGER DEFAULT 0'),
                    ('proctoring_enabled', 'BOOLEAN DEFAULT 0'),
                    ('max_tab_switches', 'INTEGER DEFAULT 3'),
                    ('block_copy_paste', 'BOOLEAN DEFAULT 1'),
                    ('due_date', 'DATETIME'),
                    ('is_archived', 'BOOLEAN DEFAULT 0'),
                    ('archived_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_quiz_institution_id', 'institution_id'),
                    ('ix_quiz_teacher_id', 'teacher_id'),
                    ('ix_quiz_video_id', 'video_id'),
                    ('ix_quiz_classroom_id', 'classroom_id'),
                    ('ix_quiz_created_at', 'created_at'),
                ]
            },
            'question': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('quiz_id', 'INTEGER'),
                    ('text', 'TEXT'),
                    ('option_a', 'VARCHAR(200)'),
                    ('option_b', 'VARCHAR(200)'),
                    ('option_c', 'VARCHAR(200)'),
                    ('option_d', 'VARCHAR(200)'),
                    ('correct_option', 'VARCHAR(1)'),
                    ('explanation', 'TEXT'),
                    ('points', 'INTEGER DEFAULT 1'),
                ],
                'indexes': [
                    ('ix_question_institution_id', 'institution_id'),
                    ('ix_question_quiz_id', 'quiz_id'),
                ]
            },
            'quiz_result': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('quiz_id', 'INTEGER'),
                    ('student_id', 'INTEGER'),
                    ('score', 'INTEGER'),
                    ('total_questions', 'INTEGER'),
                    ('timestamp', 'DATETIME'),
                    ('answers_json', 'TEXT'),
                    ('time_taken_seconds', 'INTEGER DEFAULT 0'),
                    ('passed', 'BOOLEAN DEFAULT 0'),
                    ('proctoring_violations_count', 'INTEGER DEFAULT 0'),
                    ('proctoring_log_json', 'TEXT DEFAULT \'[]\''),
                    ('auto_submitted_due_to_cheating', 'BOOLEAN DEFAULT 0'),
                    ('is_archived', 'BOOLEAN DEFAULT 0'),
                    ('archived_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_quiz_result_institution_id', 'institution_id'),
                    ('ix_quiz_result_quiz_id', 'quiz_id'),
                    ('ix_quiz_result_student_id', 'student_id'),
                    ('ix_quiz_result_timestamp', 'timestamp'),
                ]
            },
            'chat_message': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('classroom_id', 'INTEGER'),
                    ('user_id', 'INTEGER'),
                    ('content', 'TEXT'),
                    ('timestamp', 'DATETIME'),
                    ('message_type', 'VARCHAR(20) DEFAULT \'text\''),
                ],
                'indexes': [
                    ('ix_chat_message_institution_id', 'institution_id'),
                    ('ix_chat_message_classroom_id', 'classroom_id'),
                    ('ix_chat_message_user_id', 'user_id'),
                    ('ix_chat_message_timestamp', 'timestamp'),
                ]
            },
            'attendance': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('student_id', 'INTEGER'),
                    ('classroom_id', 'INTEGER'),
                    ('date', 'DATE'),
                    ('status', 'VARCHAR(20) DEFAULT \'Absent\''),
                    ('arrival_time', 'DATETIME'),
                    ('session_id', 'INTEGER'),
                    ('is_archived', 'BOOLEAN DEFAULT 0'),
                    ('archived_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_attendance_institution_id', 'institution_id'),
                    ('ix_attendance_student_id', 'student_id'),
                    ('ix_attendance_classroom_id', 'classroom_id'),
                    ('ix_attendance_date', 'date'),
                    ('ix_attendance_status', 'status'),
                    ('ix_attendance_session_id', 'session_id'),
                    ('ix_attendance_class_date', 'classroom_id, date'),
                    ('ix_attendance_student_class', 'student_id, classroom_id'),
                    ('ix_attendance_session_student', 'session_id, student_id'),
                ]
            },
            'attendance_session': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('classroom_id', 'INTEGER'),
                    ('name', 'VARCHAR(150)'),
                    ('start_date', 'DATE'),
                    ('end_date', 'DATE'),
                    ('created_by', 'INTEGER'),
                    ('created_at', 'DATETIME'),
                    ('is_active', 'BOOLEAN DEFAULT 1'),
                ],
                'indexes': [
                    ('ix_attendance_session_institution_id', 'institution_id'),
                    ('ix_attendance_session_classroom_id', 'classroom_id'),
                    ('ix_attendance_session_is_active', 'is_active'),
                    ('ix_attendance_session_dates', 'start_date, end_date'),
                ]
            },
            'attendance_sub_session': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('attendance_session_id', 'INTEGER'),
                    ('name', 'VARCHAR(150)'),
                    ('session_date', 'DATE'),
                    ('created_by', 'INTEGER'),
                    ('created_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_attendance_sub_session_institution_id', 'institution_id'),
                    ('ix_attendance_sub_session_parent', 'attendance_session_id'),
                    ('ix_attendance_sub_session_date', 'session_date'),
                ]
            },
            'activity_log': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('user_id', 'INTEGER'),
                    ('username', 'VARCHAR(150)'),
                    ('action', 'VARCHAR(100)'),
                    ('details', 'TEXT'),
                    ('ip_address', 'VARCHAR(50)'),
                    ('timestamp', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_activity_log_institution_id', 'institution_id'),
                    ('ix_activity_log_user_id', 'user_id'),
                    ('ix_activity_log_timestamp', 'timestamp'),
                ]
            },
            'system_metric': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('metric_name', 'VARCHAR(50)'),
                    ('metric_value', 'FLOAT'),
                    ('recorded_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_system_metric_institution_id', 'institution_id'),
                    ('ix_system_metric_name', 'metric_name'),
                    ('ix_system_metric_recorded_at', 'recorded_at'),
                ]
            },
            'assignment': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('title', 'VARCHAR(200)'),
                    ('description', 'TEXT'),
                    ('classroom_id', 'INTEGER'),
                    ('teacher_id', 'INTEGER'),
                    ('created_at', 'DATETIME'),
                    ('due_date', 'DATETIME'),
                    ('total_points', 'INTEGER DEFAULT 100'),
                    ('assignment_type', 'VARCHAR(20) DEFAULT \'text\''),
                    ('allow_late_submission', 'BOOLEAN DEFAULT 0'),
                    ('late_penalty_percent', 'INTEGER DEFAULT 10'),
                    ('question_file_path', 'VARCHAR(500)'),
                    ('question_file_name', 'VARCHAR(300)'),
                    ('response_mode', 'VARCHAR(20) DEFAULT \'either\''),
                ],
                'indexes': [
                    ('ix_assignment_institution_id', 'institution_id'),
                    ('ix_assignment_classroom_id', 'classroom_id'),
                    ('ix_assignment_teacher_id', 'teacher_id'),
                    ('ix_assignment_created_at', 'created_at'),
                    ('ix_assignment_due_date', 'due_date'),
                ]
            },
            'assignment_submission': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('assignment_id', 'INTEGER'),
                    ('student_id', 'INTEGER'),
                    ('submitted_at', 'DATETIME'),
                    ('content', 'TEXT'),
                    ('file_path', 'VARCHAR(500)'),
                    ('file_name', 'VARCHAR(300)'),
                    ('grade', 'FLOAT'),
                    ('feedback', 'TEXT'),
                    ('graded_at', 'DATETIME'),
                    ('is_late', 'BOOLEAN DEFAULT 0'),
                    ('status', 'VARCHAR(20) DEFAULT \'submitted\''),
                    ('audio_file_path', 'VARCHAR(500)'),
                    ('submission_type', 'VARCHAR(20) DEFAULT \'standard\''),
                ],
                'indexes': [
                    ('ix_assignment_submission_institution_id', 'institution_id'),
                    ('ix_assignment_submission_assignment_id', 'assignment_id'),
                    ('ix_assignment_submission_student_id', 'student_id'),
                    ('ix_assignment_submission_status', 'status'),
                    ('ix_assignment_submission_submitted_at', 'submitted_at'),
                ]
            },
            'student_profile': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('user_id', 'INTEGER'),
                    ('student_name', 'VARCHAR(200)'),
                    ('student_id_number', 'VARCHAR(50)'),
                    ('roll_number', 'VARCHAR(50)'),
                    ('department', 'VARCHAR(150)'),
                    ('year', 'VARCHAR(20)'),
                    ('section', 'VARCHAR(20)'),
                    ('requires_admin_review', 'BOOLEAN DEFAULT 0'),
                    ('date_of_birth', 'DATE'),
                    ('gender', 'VARCHAR(20)'),
                    ('blood_group', 'VARCHAR(10)'),
                    ('phone_number', 'VARCHAR(20)'),
                    ('email', 'VARCHAR(150)'),
                    ('father_name', 'VARCHAR(150)'),
                    ('mother_name', 'VARCHAR(150)'),
                    ('father_phone', 'VARCHAR(20)'),
                    ('mother_phone', 'VARCHAR(20)'),
                    ('father_email', 'VARCHAR(150)'),
                    ('mother_email', 'VARCHAR(150)'),
                    ('guardian_name', 'VARCHAR(150)'),
                    ('guardian_phone', 'VARCHAR(20)'),
                    ('communication_address', 'TEXT'),
                    ('permanent_address', 'TEXT'),
                    ('nationality', 'VARCHAR(100)'),
                    ('religion', 'VARCHAR(100)'),
                    ('category', 'VARCHAR(100)'),
                    ('emergency_contact', 'VARCHAR(150)'),
                    ('emergency_phone', 'VARCHAR(20)'),
                    ('photo_path', 'VARCHAR(500)'),
                    ('signature_path', 'VARCHAR(500)'),
                    ('aadhaar_path', 'VARCHAR(500)'),
                    ('transfer_certificate_path', 'VARCHAR(500)'),
                    ('community_certificate_path', 'VARCHAR(500)'),
                    ('other_certificates_json', 'TEXT DEFAULT \'[]\''),
                    ('created_at', 'DATETIME'),
                    ('updated_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_student_profile_institution_id', 'institution_id'),
                    ('ix_student_profile_user_id', 'user_id'),
                ]
            },
            'video_note': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('user_id', 'INTEGER'),
                    ('video_id', 'INTEGER'),
                    ('timestamp_seconds', 'FLOAT DEFAULT 0.0'),
                    ('content', 'TEXT'),
                    ('color', 'VARCHAR(7) DEFAULT \'#fef08a\''),
                    ('created_at', 'DATETIME'),
                    ('updated_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_video_note_institution_id', 'institution_id'),
                    ('ix_video_note_user_id', 'user_id'),
                    ('ix_video_note_video_id', 'video_id'),
                ]
            },
            'video_bookmark': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('user_id', 'INTEGER'),
                    ('video_id', 'INTEGER'),
                    ('timestamp_seconds', 'FLOAT'),
                    ('label', 'VARCHAR(200)'),
                    ('created_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_video_bookmark_institution_id', 'institution_id'),
                    ('ix_video_bookmark_user_id', 'user_id'),
                    ('ix_video_bookmark_video_id', 'video_id'),
                ]
            },
            'video_progress': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('user_id', 'INTEGER'),
                    ('video_id', 'INTEGER'),
                    ('progress_seconds', 'FLOAT DEFAULT 0.0'),
                    ('percent_complete', 'FLOAT DEFAULT 0.0'),
                    ('completed', 'BOOLEAN DEFAULT 0'),
                    ('updated_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_video_progress_institution_id', 'institution_id'),
                    ('ix_video_progress_user_id', 'user_id'),
                    ('ix_video_progress_video_id', 'video_id'),
                    ('ix_video_progress_completed', 'completed'),
                ]
            },
            'achievement': {
                'columns': [
                    ('code', 'VARCHAR(50)'),
                    ('title', 'VARCHAR(100)'),
                    ('description', 'TEXT'),
                    ('icon_emoji', 'VARCHAR(10) DEFAULT \'🏆\''),
                    ('xp_reward', 'INTEGER DEFAULT 50'),
                    ('category', 'VARCHAR(30) DEFAULT \'general\''),
                    ('condition_type', 'VARCHAR(50)'),
                    ('condition_value', 'INTEGER DEFAULT 1'),
                ],
                'indexes': [
                    ('ix_achievement_code', 'code'),
                    ('ix_achievement_category', 'category'),
                ]
            },
            'leaderboard_entry': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('user_id', 'INTEGER'),
                    ('username', 'VARCHAR(150)'),
                    ('role', 'VARCHAR(20)'),
                    ('xp', 'INTEGER DEFAULT 0'),
                    ('level', 'INTEGER DEFAULT 1'),
                    ('streak_days', 'INTEGER DEFAULT 0'),
                    ('quiz_count', 'INTEGER DEFAULT 0'),
                    ('category', 'VARCHAR(30) DEFAULT \'global\''),
                    ('rank', 'INTEGER DEFAULT 0'),
                    ('updated_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_leaderboard_entry_institution_id', 'institution_id'),
                    ('ix_leaderboard_entry_user_id', 'user_id'),
                    ('ix_leaderboard_entry_category', 'category'),
                    ('ix_leaderboard_entry_xp', 'xp'),
                ]
            },
            'email_queue': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('recipient_email', 'VARCHAR(200)'),
                    ('subject', 'VARCHAR(300)'),
                    ('body_html', 'TEXT'),
                    ('body_text', 'TEXT'),
                    ('status', 'VARCHAR(20) DEFAULT \'pending\''),
                    ('created_at', 'DATETIME'),
                    ('sent_at', 'DATETIME'),
                    ('error_message', 'TEXT'),
                    ('retry_count', 'INTEGER DEFAULT 0'),
                ],
                'indexes': [
                    ('ix_email_queue_institution_id', 'institution_id'),
                    ('ix_email_queue_status', 'status'),
                    ('ix_email_queue_created_at', 'created_at'),
                ]
            },
            'student_remark': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('student_id', 'INTEGER'),
                    ('classroom_id', 'INTEGER'),
                    ('remark', 'TEXT'),
                    ('updated_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_student_remark_institution_id', 'institution_id'),
                    ('ix_student_remark_student_id', 'student_id'),
                    ('ix_student_remark_classroom_id', 'classroom_id'),
                ]
            },
            'email_delivery_log': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('execution_id', 'VARCHAR(50)'),
                    ('class_id', 'INTEGER'),
                    ('teacher_id', 'INTEGER'),
                    ('student_id', 'INTEGER'),
                    ('student_email', 'VARCHAR(150)'),
                    ('subject', 'VARCHAR(256)'),
                    ('status', 'VARCHAR(20) DEFAULT \'pending\''),
                    ('error_message', 'TEXT'),
                    ('sent_at', 'DATETIME'),
                    ('retry_count', 'INTEGER DEFAULT 0'),
                    ('report_type', 'VARCHAR(30) DEFAULT \'daily_scheduled\''),
                    ('report_html', 'TEXT'),
                ],
                'indexes': [
                    ('ix_email_delivery_log_institution_id', 'institution_id'),
                    ('ix_email_delivery_log_class_id', 'class_id'),
                    ('ix_email_delivery_log_teacher_id', 'teacher_id'),
                    ('ix_email_delivery_log_student_id', 'student_id'),
                    ('ix_email_delivery_log_status', 'status'),
                    ('ix_email_delivery_log_sent_at', 'sent_at'),
                ]
            },
            'conversion_job': {
                'columns': [
                    ('job_id', 'VARCHAR(64)'),
                    ('video_id', 'INTEGER'),
                    ('institution_id', 'INTEGER'),
                    ('input_file', 'VARCHAR(500)'),
                    ('output_directory', 'VARCHAR(500)'),
                    ('status', 'VARCHAR(20) DEFAULT \'queued\''),
                    ('progress', 'INTEGER DEFAULT 0'),
                    ('current_segment', 'INTEGER DEFAULT 0'),
                    ('total_segments', 'INTEGER DEFAULT 0'),
                    ('last_processed_position', 'FLOAT DEFAULT 0.0'),
                    ('worker_id', 'VARCHAR(50)'),
                    ('retry_count', 'INTEGER DEFAULT 0'),
                    ('max_retries', 'INTEGER DEFAULT 3'),
                    ('error_message', 'TEXT'),
                    ('renditions_state_json', 'TEXT DEFAULT \'{}\''),
                    ('created_at', 'DATETIME'),
                    ('started_at', 'DATETIME'),
                    ('updated_at', 'DATETIME'),
                    ('completed_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_conversion_job_job_id', 'job_id'),
                    ('ix_conversion_job_video_id', 'video_id'),
                    ('ix_conversion_job_status', 'status'),
                    ('ix_conversion_job_institution_id', 'institution_id'),
                ]
            },
            'class_weekly_report': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('classroom_id', 'INTEGER'),
                    ('teacher_id', 'INTEGER'),
                    ('period_start', 'DATE'),
                    ('period_end', 'DATE'),
                    ('generated_at', 'DATETIME'),
                    ('total_students', 'INTEGER DEFAULT 0'),
                    ('avg_attendance_pct', 'FLOAT DEFAULT 0.0'),
                    ('total_xp_gained', 'INTEGER DEFAULT 0'),
                    ('avg_quiz_score_pct', 'FLOAT DEFAULT 0.0'),
                    ('total_video_watch_seconds', 'INTEGER DEFAULT 0'),
                    ('report_data_json', "TEXT DEFAULT '{}'"),
                    ('teacher_remarks', 'TEXT'),
                    ('status', "VARCHAR(30) DEFAULT 'generated'"),
                    ('sent_to_admin_at', 'DATETIME'),
                    ('admin_feedback', 'TEXT'),
                ],
                'indexes': [
                    ('ix_class_weekly_report_institution_id', 'institution_id'),
                    ('ix_class_weekly_report_classroom_id', 'classroom_id'),
                    ('ix_class_weekly_report_teacher_id', 'teacher_id'),
                    ('ix_class_weekly_report_period_start', 'period_start'),
                    ('ix_class_weekly_report_period_end', 'period_end'),
                    ('ix_class_weekly_report_generated_at', 'generated_at'),
                    ('ix_class_weekly_report_status', 'status'),
                ]
            },
            'video_checkpoint': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('video_id', 'INTEGER'),
                    ('timestamp_seconds', 'FLOAT'),
                    ('question_text', 'TEXT'),
                    ('option_a', 'VARCHAR(300)'),
                    ('option_b', 'VARCHAR(300)'),
                    ('option_c', 'VARCHAR(300)'),
                    ('option_d', 'VARCHAR(300)'),
                    ('correct_option', 'VARCHAR(1)'),
                    ('explanation', 'TEXT'),
                    ('xp_reward', 'INTEGER DEFAULT 25'),
                    ('created_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_video_checkpoint_institution_id', 'institution_id'),
                    ('ix_video_checkpoint_video_id', 'video_id'),
                    ('ix_video_checkpoint_timestamp_seconds', 'timestamp_seconds'),
                ]
            },
            'checkpoint_response': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('checkpoint_id', 'INTEGER'),
                    ('student_id', 'INTEGER'),
                    ('selected_option', 'VARCHAR(1)'),
                    ('is_correct', 'BOOLEAN DEFAULT 0'),
                    ('answered_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_checkpoint_response_institution_id', 'institution_id'),
                    ('ix_checkpoint_response_checkpoint_id', 'checkpoint_id'),
                    ('ix_checkpoint_response_student_id', 'student_id'),
                ]
            },
            'video_doubt': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('video_id', 'INTEGER'),
                    ('user_id', 'INTEGER'),
                    ('timestamp_seconds', 'FLOAT DEFAULT 0.0'),
                    ('question_text', 'TEXT'),
                    ('is_resolved', 'BOOLEAN DEFAULT 0'),
                    ('created_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_video_doubt_institution_id', 'institution_id'),
                    ('ix_video_doubt_video_id', 'video_id'),
                    ('ix_video_doubt_user_id', 'user_id'),
                    ('ix_video_doubt_timestamp_seconds', 'timestamp_seconds'),
                    ('ix_video_doubt_is_resolved', 'is_resolved'),
                ]
            },
            'video_doubt_reply': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('doubt_id', 'INTEGER'),
                    ('user_id', 'INTEGER'),
                    ('content', 'TEXT'),
                    ('is_teacher_endorsed', 'BOOLEAN DEFAULT 0'),
                    ('created_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_video_doubt_reply_institution_id', 'institution_id'),
                    ('ix_video_doubt_reply_doubt_id', 'doubt_id'),
                    ('ix_video_doubt_reply_user_id', 'user_id'),
                ]
            },
            'video_flashcard': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('video_id', 'INTEGER'),
                    ('user_id', 'INTEGER'),
                    ('front_term', 'VARCHAR(300)'),
                    ('back_definition', 'TEXT'),
                    ('created_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_video_flashcard_institution_id', 'institution_id'),
                    ('ix_video_flashcard_video_id', 'video_id'),
                    ('ix_video_flashcard_user_id', 'user_id'),
                ]
            },
            'academic_certificate': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('certificate_code', 'VARCHAR(64)'),
                    ('student_id', 'INTEGER'),
                    ('title', 'VARCHAR(200)'),
                    ('description', 'TEXT'),
                    ('certificate_type', "VARCHAR(50) DEFAULT 'course_completion'"),
                    ('issued_at', 'DATETIME'),
                    ('criteria_met_json', "TEXT DEFAULT '{}'"),
                ],
                'indexes': [
                    ('ix_academic_certificate_institution_id', 'institution_id'),
                    ('ix_academic_certificate_certificate_code', 'certificate_code'),
                    ('ix_academic_certificate_student_id', 'student_id'),
                    ('ix_academic_certificate_certificate_type', 'certificate_type'),
                ]
            },
            'parent_access_token': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('student_id', 'INTEGER'),
                    ('token', 'VARCHAR(64)'),
                    ('created_at', 'DATETIME'),
                    ('expires_at', 'DATETIME'),
                    ('is_active', 'BOOLEAN DEFAULT 1'),
                    ('last_accessed_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_parent_access_token_institution_id', 'institution_id'),
                    ('ix_parent_access_token_student_id', 'student_id'),
                    ('ix_parent_access_token_token', 'token'),
                    ('ix_parent_access_token_is_active', 'is_active'),
                ]
            },
            'announcement': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('title', 'VARCHAR(200)'),
                    ('content', 'TEXT'),
                    ('author_id', 'INTEGER'),
                    ('priority', "VARCHAR(20) DEFAULT 'normal'"),
                    ('target_role', "VARCHAR(20) DEFAULT 'all'"),
                    ('classroom_id', 'INTEGER'),
                    ('is_pinned', 'BOOLEAN DEFAULT 0'),
                    ('created_at', 'DATETIME'),
                    ('expires_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_announcement_institution_id', 'institution_id'),
                    ('ix_announcement_author_id', 'author_id'),
                    ('ix_announcement_priority', 'priority'),
                    ('ix_announcement_is_pinned', 'is_pinned'),
                    ('ix_announcement_created_at', 'created_at'),
                ]
            },
            'announcement_read': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('announcement_id', 'INTEGER'),
                    ('user_id', 'INTEGER'),
                    ('read_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_announcement_read_institution_id', 'institution_id'),
                    ('ix_announcement_read_announcement_id', 'announcement_id'),
                    ('ix_announcement_read_user_id', 'user_id'),
                ]
            },
            'timetable_slot': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('classroom_id', 'INTEGER'),
                    ('teacher_id', 'INTEGER'),
                    ('day_of_week', 'VARCHAR(15)'),
                    ('period_name', 'VARCHAR(50)'),
                    ('start_time', 'VARCHAR(10)'),
                    ('end_time', 'VARCHAR(10)'),
                    ('subject_name', 'VARCHAR(150)'),
                    ('room_number', 'VARCHAR(50)'),
                    ('meeting_link', 'VARCHAR(500)'),
                ],
                'indexes': [
                    ('ix_timetable_slot_institution_id', 'institution_id'),
                    ('ix_timetable_slot_classroom_id', 'classroom_id'),
                    ('ix_timetable_slot_day_of_week', 'day_of_week'),
                ]
            },
            'reward_item': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('code', 'VARCHAR(50)'),
                    ('name', 'VARCHAR(150)'),
                    ('description', 'TEXT'),
                    ('item_type', 'VARCHAR(30)'),
                    ('item_value', 'VARCHAR(200)'),
                    ('xp_cost', 'INTEGER DEFAULT 500'),
                    ('icon', "VARCHAR(50) DEFAULT 'military_tech'"),
                    ('is_active', 'BOOLEAN DEFAULT 1'),
                    ('created_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_reward_item_institution_id', 'institution_id'),
                    ('ix_reward_item_code', 'code'),
                    ('ix_reward_item_item_type', 'item_type'),
                ]
            },
            'user_reward': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('user_id', 'INTEGER'),
                    ('reward_id', 'INTEGER'),
                    ('purchased_at', 'DATETIME'),
                    ('is_equipped', 'BOOLEAN DEFAULT 0'),
                ],
                'indexes': [
                    ('ix_user_reward_institution_id', 'institution_id'),
                    ('ix_user_reward_user_id', 'user_id'),
                    ('ix_user_reward_reward_id', 'reward_id'),
                    ('ix_user_reward_is_equipped', 'is_equipped'),
                ]
            },
            'ebook': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('uploader_id', 'INTEGER'),
                    ('title', 'VARCHAR(250)'),
                    ('author', 'VARCHAR(150)'),
                    ('publisher', 'VARCHAR(150)'),
                    ('edition', 'VARCHAR(50)'),
                    ('isbn', 'VARCHAR(50)'),
                    ('subject', 'VARCHAR(100)'),
                    ('academic_level', "VARCHAR(50) DEFAULT 'All'"),
                    ('institution_type', "VARCHAR(20) DEFAULT 'both'"),
                    ('resource_type', "VARCHAR(50) DEFAULT 'textbook'"),
                    ('department', 'VARCHAR(100)'),
                    ('description', 'TEXT'),
                    ('file_path', 'VARCHAR(500)'),
                    ('file_name', 'VARCHAR(255)'),
                    ('cover_image_path', 'VARCHAR(500)'),
                    ('page_count', 'INTEGER DEFAULT 0'),
                    ('file_size_bytes', 'BIGINT DEFAULT 0'),
                    ('allow_download', 'BOOLEAN DEFAULT 1'),
                    ('view_count', 'INTEGER DEFAULT 0'),
                    ('download_count', 'INTEGER DEFAULT 0'),
                    ('created_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_ebook_institution_id', 'institution_id'),
                    ('ix_ebook_uploader_id', 'uploader_id'),
                    ('ix_ebook_title', 'title'),
                    ('ix_ebook_author', 'author'),
                    ('ix_ebook_subject', 'subject'),
                    ('ix_ebook_academic_level', 'academic_level'),
                    ('ix_ebook_institution_type', 'institution_type'),
                    ('ix_ebook_resource_type', 'resource_type'),
                    ('ix_ebook_department', 'department'),
                    ('ix_ebook_allow_download', 'allow_download'),
                    ('ix_ebook_created_at', 'created_at'),
                ]
            },
            'ebook_progress': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('ebook_id', 'INTEGER'),
                    ('user_id', 'INTEGER'),
                    ('last_read_page', 'INTEGER DEFAULT 1'),
                    ('percent_completed', 'FLOAT DEFAULT 0.0'),
                    ('last_read_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_ebook_progress_institution_id', 'institution_id'),
                    ('ix_ebook_progress_ebook_id', 'ebook_id'),
                    ('ix_ebook_progress_user_id', 'user_id'),
                    ('ix_ebook_progress_last_read_at', 'last_read_at'),
                ]
            },
            'ai_copilot_interaction': {
                'columns': [
                    ('institution_id', 'INTEGER'),
                    ('user_id', 'INTEGER'),
                    ('video_id', 'INTEGER'),
                    ('question', 'TEXT'),
                    ('answer', 'TEXT'),
                    ('playback_timestamp', 'FLOAT DEFAULT 0.0'),
                    ('cited_timestamp', 'FLOAT'),
                    ('cited_timestamp_formatted', 'VARCHAR(20)'),
                    ('cited_book_id', 'INTEGER'),
                    ('cited_page', 'INTEGER'),
                    ('micro_quiz_json', 'TEXT'),
                    ('quiz_answered', 'BOOLEAN DEFAULT 0'),
                    ('quiz_correct', 'BOOLEAN DEFAULT 0'),
                    ('created_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_ai_copilot_institution_id', 'institution_id'),
                    ('ix_ai_copilot_user_id', 'user_id'),
                    ('ix_ai_copilot_video_id', 'video_id'),
                    ('ix_ai_copilot_created_at', 'created_at'),
                ]
            },
            'user_session': {
                'columns': [
                    ('sid', 'VARCHAR(128)'),
                    ('user_id', 'INTEGER'),
                    ('institution_id', 'INTEGER'),
                    ('data', 'TEXT'),
                    ('expiry', 'DATETIME'),
                    ('created_at', 'DATETIME'),
                    ('last_accessed', 'DATETIME'),
                    ('user_agent', 'VARCHAR(500)'),
                    ('ip_address', 'VARCHAR(100)'),
                    ('is_active', 'BOOLEAN DEFAULT 1'),
                ],
                'indexes': [
                    ('ix_user_session_user_id', 'user_id'),
                    ('ix_user_session_institution_id', 'institution_id'),
                    ('ix_user_session_expiry', 'expiry'),
                    ('ix_user_session_last_accessed', 'last_accessed'),
                    ('ix_user_session_is_active', 'is_active'),
                ]
            },
        }
        
        # Dynamically add institution_id to all tenant tables
        tenant_tables = [
            'video', 'video_like', 'playlist', 'classroom', 'comment', 'view_analytics',
            'notification', 'site_settings', 'quiz', 'question', 'quiz_result',
            'chat_message', 'attendance', 'attendance_session', 'attendance_sub_session',
            'activity_log', 'system_metric', 'assignment', 'assignment_submission',
            'student_profile', 'video_note', 'video_bookmark', 'video_progress',
            'leaderboard_entry', 'email_queue', 'student_remark', 'email_delivery_log',
            'conversion_job', 'class_weekly_report', 'video_checkpoint',
            'checkpoint_response', 'video_doubt', 'video_doubt_reply',
            'video_flashcard', 'academic_certificate', 'parent_access_token',
            'announcement', 'announcement_read', 'timetable_slot', 'reward_item', 'user_reward',
            'ebook', 'ebook_progress', 'ai_copilot_interaction', 'daily_quest_template', 'user_session'
        ]
        for table in tenant_tables:
            if table not in migrations:
                migrations[table] = {'columns': []}
            col_names = [col[0] for col in migrations[table]['columns']]
            if 'institution_id' not in col_names:
                migrations[table]['columns'].append(('institution_id', 'INTEGER'))

        is_postgres = (db.engine.dialect.name == 'postgresql')

        for table, config in migrations.items():
            try:
                columns = inspector.get_columns(table)
                existing_cols = {c['name'] for c in columns}
                
                for col_name, col_type in config['columns']:
                    if col_name not in existing_cols:
                        exec_type = col_type
                        if is_postgres:
                            exec_type = exec_type.replace('DATETIME', 'TIMESTAMP')
                            exec_type = exec_type.replace('BOOLEAN DEFAULT 1', 'BOOLEAN DEFAULT TRUE')
                            exec_type = exec_type.replace('BOOLEAN DEFAULT 0', 'BOOLEAN DEFAULT FALSE')
                        try:
                            db.session.execute(db.text(f'ALTER TABLE "{table}" ADD COLUMN {col_name} {exec_type}'))
                            db.session.commit()
                            print(f"[OK] Added '{col_name}' to {table}")
                        except Exception as e:
                            db.session.rollback()
                            print(f"[!] Could not add '{col_name}' to {table}: {str(e)[:100]}")
                
                # Create indexes
                for idx_name, idx_col in config.get('indexes', []):
                    try:
                        existing_indexes = [i['name'] for i in inspector.get_indexes(table)]
                        if idx_name not in existing_indexes:
                            try:
                                db.session.execute(db.text(f'CREATE INDEX IF NOT EXISTS {idx_name} ON "{table}" ({idx_col})'))
                                db.session.commit()
                                print(f"[OK] Created index '{idx_name}' on {table}")
                            except Exception as e:
                                db.session.rollback()
                                print(f"[!] Could not create index '{idx_name}': {str(e)[:100]}")
                    except Exception as e:
                        db.session.rollback()
                        print(f"[!] Could not inspect indexes for {table}: {str(e)[:100]}")
            except Exception as e:
                db.session.rollback()
                print(f"[!] Table {table} does not exist yet or inspection error: {str(e)[:100]}")
        
        # Backfill default institution for all tables
        print("\n[Institution] Backfilling all tables with Default Institution...")
        backfill_all_tables_with_default_institution(db)

        # Seed default achievements
        print("\n[Achievements] Seeding default achievements...")
        try:
            Achievement.seed_defaults()
            print("[OK] Achievements seeded successfully")
        except Exception as e:
            print(f"[!] Could not seed achievements: {e}")

        # Seed default rewards
        print("\n[Rewards] Seeding default XP reward items...")
        try:
            RewardItem.seed_defaults()
            print("[OK] Rewards seeded successfully")
        except Exception as e:
            print(f"[!] Could not seed rewards: {e}")

        # Seed default daily quest templates
        print("\n[Quests] Seeding default daily quest templates...")
        try:
            default_quests = [
                {'quest_key': 'daily_login', 'title': 'Daily Check-in', 'desc': 'Log in & remain active today', 'xp': 25, 'icon': 'event_available', 'target': 1},
                {'quest_key': 'watch_video', 'title': 'Course Explorer', 'desc': 'Watch or review an educational lecture', 'xp': 50, 'icon': 'play_circle_filled', 'target': 1},
                {'quest_key': 'take_quiz', 'title': 'Quiz Challenger', 'desc': 'Complete an online assessment', 'xp': 75, 'icon': 'quiz', 'target': 1},
                {'quest_key': 'submit_assignment', 'title': 'Assignment Scholar', 'desc': 'Submit coursework or homework', 'xp': 100, 'icon': 'assignment_turned_in', 'target': 1}
            ]
            for q_def in default_quests:
                q_obj = DailyQuestTemplate.query.filter_by(quest_key=q_def['quest_key']).first()
                if not q_obj:
                    q_obj = DailyQuestTemplate(**q_def)
                    db.session.add(q_obj)
            db.session.commit()
            print("[OK] Daily quest templates seeded successfully")
        except Exception as e:
            print(f"[!] Could not seed daily quest templates: {e}")
        
        db.session.commit()
        
        # Auto-sync YouTube thumbnails
        print("\n[YouTube] Syncing YouTube thumbnails...")
        yt_videos = Video.query.filter(
            (Video.video_type == 'youtube') | (Video.filename.like('youtube_%'))
        ).all()
        yt_updated = 0
        for v in yt_videos:
            yt_id = v.youtube_id or (v.filename.replace('youtube_', '') if v.filename and v.filename.startswith('youtube_') else None)
            if yt_id:
                expected_thumb = f"https://img.youtube.com/vi/{yt_id}/hqdefault.jpg"
                if v.thumbnail_path != expected_thumb or v.video_type != 'youtube' or v.youtube_id != yt_id:
                    v.thumbnail_path = expected_thumb
                    v.video_type = 'youtube'
                    v.youtube_id = yt_id
                    v.youtube_url = f"https://www.youtube.com/watch?v={yt_id}"
                    yt_updated += 1
        if yt_updated > 0:
            db.session.commit()
            print(f"[OK] Synced {yt_updated} YouTube video thumbnails")
        else:
            print("[OK] YouTube thumbnails already synced")

        # Post-Migration Integrity Validation
        from services.audit_engine import run_platform_audit
        healthy, audit_rep = run_platform_audit(app)
        if not healthy:
            print("❌ [Migration Error] Post-migration validation failed!")
            if audit_rep['integrity']['message']:
                print(f"  Integrity Error: {audit_rep['integrity']['message']}")
            if audit_rep['foreign_keys']['violations']:
                print("  FK Violations:")
                for v in audit_rep['foreign_keys']['violations']:
                    print(f"    - {v}")
            sys.exit(1)
        print("[OK] Post-migration validation PASSED cleanly!")

        print("\n" + "=" * 60)
        print("Migration Complete!")
        print("=" * 60)



def reset_db():
    """WARNING: Drops and recreates all tables (data loss)."""
    with app.app_context():
        print("\n⚠️  WARNING: This will DROP ALL TABLES and data!")
        confirm = input("Type 'RESET' to confirm: ")
        if confirm == 'RESET':
            db.drop_all()
            db.create_all()
            print("   ✅ Database reset completed")
            
            # Create admin
            admin = User(username='admin', role='admin')
            admin.set_password('admin123')
            db.session.add(admin)
            
            # Create settings
            db.session.add(SiteSettings())
            
            # Seed achievements
            Achievement.seed_defaults()
            
            db.session.commit()
            print("   ✅ Admin user created: admin / admin123")
            print("   ✅ SiteSettings initialized")
            print("   ✅ Achievements seeded")
        else:
            print("   ❌ Reset cancelled")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='CampusPlayer DB Migration')
    parser.add_argument('--reset', action='store_true', help='Reset database (DANGER: deletes all data)')
    args = parser.parse_args()
    
    if args.reset:
        reset_db()
    else:
        migrate()