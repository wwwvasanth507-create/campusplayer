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
        
        # Rebuild user table to scope unique username to institution_id
        try:
            import sqlite3
            db_uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
            if 'sqlite:///' in db_uri:
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
                                avatar_url VARCHAR(200),
                                theme_preference VARCHAR(20) DEFAULT 'dark',
                                bio TEXT,
                                last_login DATETIME,
                                last_active DATETIME,
                                login_count INTEGER DEFAULT 0,
                                level INTEGER DEFAULT 1,
                                streak_days INTEGER DEFAULT 0,
                                last_streak_date DATE,
                                total_quiz_score INTEGER DEFAULT 0,
                                total_quizzes_taken INTEGER DEFAULT 0,
                                achievements_json TEXT DEFAULT '[]',
                                quests_json TEXT DEFAULT '{}',
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
            print(f"[Migration] [!] Failed to rebuild user table: {e}")

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
                    ('ix_institution_status', 'status'),
                    ('ix_institution_owner_admin_id', 'owner_admin_id'),
                ]
            },
            'user': {
                'columns': [
                    ('level', 'INTEGER DEFAULT 1'),
                    ('streak_days', 'INTEGER DEFAULT 0'),
                    ('last_streak_date', 'DATE'),
                    ('total_quiz_score', 'INTEGER DEFAULT 0'),
                    ('total_quizzes_taken', 'INTEGER DEFAULT 0'),
                    ('achievements_json', 'TEXT DEFAULT \'[]\''),
                    ('quests_json', 'TEXT DEFAULT \'{}\''),
                    ('bio', 'TEXT'),
                    ('display_name', 'VARCHAR(150)'),
                    ('avatar_url', 'VARCHAR(500)'),
                    ('theme_preference', 'VARCHAR(10) DEFAULT \'dark\''),
                    ('last_login', 'DATETIME'),
                    ('last_active', 'DATETIME'),
                    ('login_count', 'INTEGER DEFAULT 0'),
                    ('email', 'VARCHAR(150)'),
                    ('phone', 'VARCHAR(20)'),
                    ('parent_email', 'VARCHAR(150)'),
                    ('parent_name', 'VARCHAR(150)'),
                    ('email_sender_address', 'VARCHAR(150)'),
                    ('encrypted_app_password', 'VARCHAR(500)'),
                    ('email_enabled', 'BOOLEAN DEFAULT 0'),
                    ('last_report_sent', 'DATETIME'),
                    ('institution_id', 'INTEGER'),
                    ('is_active_account', 'BOOLEAN DEFAULT 1'),
                ],
                'indexes': [
                    ('ix_user_role', 'role'),
                    ('ix_user_xp', 'xp'),
                    ('ix_user_institution_id', 'institution_id'),
                    ('ix_user_is_active', 'is_active_account'),
                    ('ix_user_created_at', 'created_at'),
                    ('ix_user_last_active', 'last_active'),
                    ('ix_user_inst_role', 'institution_id, role'),
                ]
            },
            'assignment': {
                'columns': [
                    ('question_file_path', 'VARCHAR(500)'),
                    ('question_file_name', 'VARCHAR(300)'),
                    ('response_mode', 'VARCHAR(20) DEFAULT \'either\''),
                    ('max_score', 'INTEGER DEFAULT 100'),
                    ('is_archived', 'BOOLEAN DEFAULT 0'),
                    ('archived_at', 'DATETIME'),
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
                    ('file_name', 'VARCHAR(300)'),
                    ('score', 'INTEGER'),
                    ('feedback', 'TEXT'),
                    ('graded_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_assignment_submission_institution_id', 'institution_id'),
                    ('ix_assignment_submission_assignment_id', 'assignment_id'),
                    ('ix_assignment_submission_student_id', 'student_id'),
                    ('ix_assignment_submission_status', 'status'),
                    ('ix_assignment_submission_submitted_at', 'submitted_at'),
                ]
            },
            'attendance': {
                'columns': [
                    ('session_id', 'INTEGER'),
                    ('remarks', 'VARCHAR(255)'),
                    ('marked_by', 'INTEGER'),
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
            'video': {
                'columns': [
                    ('chapters_json', 'TEXT'),
                    ('difficulty', 'VARCHAR(20) DEFAULT \'intermediate\''),
                    ('views', 'INTEGER DEFAULT 0'),
                    ('likes_count', 'INTEGER DEFAULT 0'),
                    ('duration', 'INTEGER DEFAULT 0'),
                    ('thumbnail_path', 'VARCHAR(500)'),
                    ('tags', 'VARCHAR(500)'),
                    ('conversion_progress', 'INTEGER DEFAULT 0'),
                    ('hls_path', 'VARCHAR(500)'),
                    ('allow_comments', 'BOOLEAN DEFAULT 1'),
                    ('is_archived', 'BOOLEAN DEFAULT 0'),
                    ('archived_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_video_institution_id', 'institution_id'),
                    ('ix_video_uploader_id', 'uploader_id'),
                    ('ix_video_classroom_id', 'classroom_id'),
                    ('ix_video_status', 'status'),
                    ('ix_video_upload_date', 'upload_date'),
                ]
            },
            'video_like': {
                'columns': [],
                'indexes': [
                    ('ix_videolike_institution_id', 'institution_id'),
                    ('ix_videolike_user_id', 'user_id'),
                    ('ix_videolike_video_id', 'video_id'),
                ]
            },
            'playlist': {
                'columns': [
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
                    ('description', 'TEXT'),
                    ('color_theme', 'VARCHAR(7) DEFAULT \'#4f46e5\''),
                    ('is_archived', 'BOOLEAN DEFAULT 0'),
                    ('archived_at', 'DATETIME'),
                ],
                'indexes': [
                    ('ix_classroom_institution_id', 'institution_id'),
                    ('ix_classroom_teacher_id', 'teacher_id'),
                ]
            },
            'comment': {
                'columns': [
                    ('parent_id', 'INTEGER'),
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
                    ('watch_duration', 'INTEGER DEFAULT 0'),
                    ('completed', 'BOOLEAN DEFAULT 0'),
                    ('ip_address', 'VARCHAR(45)'),
                ],
                'indexes': [
                    ('ix_view_analytics_institution_id', 'institution_id'),
                    ('ix_view_analytics_user_id', 'user_id'),
                    ('ix_view_analytics_video_id', 'video_id'),
                    ('ix_view_analytics_start_time', 'start_time'),
                    ('ix_view_analytics_completed', 'completed'),
                ]
            },
            'question': {
                'columns': [
                    ('points', 'INTEGER DEFAULT 1'),
                    ('explanation', 'TEXT'),
                ],
                'indexes': [
                    ('ix_question_institution_id', 'institution_id'),
                    ('ix_question_quiz_id', 'quiz_id'),
                ]
            },
            'quiz_result': {
                'columns': [
                    ('time_taken_seconds', 'INTEGER DEFAULT 0'),
                    ('passed', 'BOOLEAN DEFAULT 0'),
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
            'notification': {
                'columns': [
                    ('action_url', 'VARCHAR(500)'),
                    ('is_read', 'BOOLEAN DEFAULT 0'),
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
                    ('min_attendance_percentage', 'FLOAT DEFAULT 75.0'),
                    ('scheduled_academic_year_end_date', 'DATETIME'),
                    ('academic_year_rollover_processed', 'BOOLEAN DEFAULT 0'),
                    ('allow_student_chat', 'BOOLEAN DEFAULT 1'),
                    ('allow_public_registration', 'BOOLEAN DEFAULT 0'),
                    ('quests_version', 'INTEGER DEFAULT 1'),
                ],
                'indexes': [
                    ('ix_site_settings_institution_id', 'institution_id'),
                ]
            },
            'quiz': {
                'columns': [
                    ('due_date', 'DATETIME'),
                    ('passing_percent', 'INTEGER DEFAULT 50'),
                    ('max_attempts', 'INTEGER DEFAULT 0'),
                    ('time_limit_minutes', 'INTEGER DEFAULT 0'),
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
            'chat_message': {
                'columns': [],
                'indexes': [
                    ('ix_chat_message_institution_id', 'institution_id'),
                    ('ix_chat_message_classroom_id', 'classroom_id'),
                    ('ix_chat_message_user_id', 'user_id'),
                    ('ix_chat_message_timestamp', 'timestamp'),
                ]
            },
            'attendance_session': {
                'columns': [],
                'indexes': [
                    ('ix_attendance_session_institution_id', 'institution_id'),
                    ('ix_attendance_session_classroom_id', 'classroom_id'),
                    ('ix_attendance_session_is_active', 'is_active'),
                    ('ix_attendance_session_dates', 'start_date, end_date'),
                ]
            },
            'attendance_sub_session': {
                'columns': [],
                'indexes': [
                    ('ix_attendance_sub_session_institution_id', 'institution_id'),
                    ('ix_attendance_sub_session_parent', 'attendance_session_id'),
                    ('ix_attendance_sub_session_date', 'session_date'),
                ]
            },
            'activity_log': {
                'columns': [],
                'indexes': [
                    ('ix_activity_log_institution_id', 'institution_id'),
                    ('ix_activity_log_user_id', 'user_id'),
                    ('ix_activity_log_timestamp', 'timestamp'),
                ]
            },
            'system_metric': {
                'columns': [],
                'indexes': [
                    ('ix_system_metric_institution_id', 'institution_id'),
                    ('ix_system_metric_name', 'metric_name'),
                    ('ix_system_metric_recorded_at', 'recorded_at'),
                ]
            },
            'student_profile': {
                'columns': [
                    ('requires_admin_review', 'BOOLEAN DEFAULT 0'),
                ],
                'indexes': [
                    ('ix_student_profile_institution_id', 'institution_id'),
                ]
            },
            'video_note': {
                'columns': [],
                'indexes': [
                    ('ix_video_note_institution_id', 'institution_id'),
                    ('ix_video_note_user_id', 'user_id'),
                    ('ix_video_note_video_id', 'video_id'),
                ]
            },
            'video_bookmark': {
                'columns': [],
                'indexes': [
                    ('ix_video_bookmark_institution_id', 'institution_id'),
                    ('ix_video_bookmark_user_id', 'user_id'),
                    ('ix_video_bookmark_video_id', 'video_id'),
                ]
            },
            'video_progress': {
                'columns': [],
                'indexes': [
                    ('ix_video_progress_institution_id', 'institution_id'),
                    ('ix_video_progress_user_id', 'user_id'),
                    ('ix_video_progress_video_id', 'video_id'),
                    ('ix_video_progress_completed', 'completed'),
                ]
            },
            'leaderboard_entry': {
                'columns': [],
                'indexes': [
                    ('ix_leaderboard_entry_institution_id', 'institution_id'),
                    ('ix_leaderboard_entry_user_id', 'user_id'),
                    ('ix_leaderboard_entry_category', 'category'),
                    ('ix_leaderboard_entry_xp', 'xp'),
                ]
            },
            'email_queue': {
                'columns': [],
                'indexes': [
                    ('ix_email_queue_institution_id', 'institution_id'),
                    ('ix_email_queue_status', 'status'),
                    ('ix_email_queue_created_at', 'created_at'),
                ]
            },
            'student_remark': {
                'columns': [],
                'indexes': [
                    ('ix_student_remark_institution_id', 'institution_id'),
                    ('ix_student_remark_student_id', 'student_id'),
                    ('ix_student_remark_classroom_id', 'classroom_id'),
                ]
            },
            'email_delivery_log': {
                'columns': [
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
                'columns': [],
                'indexes': [
                    ('ix_conversion_job_institution_id', 'institution_id'),
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
            'conversion_job'
        ]
        for table in tenant_tables:
            if table not in migrations:
                migrations[table] = {'columns': []}
            col_names = [col[0] for col in migrations[table]['columns']]
            if 'institution_id' not in col_names:
                migrations[table]['columns'].append(('institution_id', 'INTEGER'))

        for table, config in migrations.items():
            columns = inspector.get_columns(table)
            existing_cols = {c['name'] for c in columns}
            
            for col_name, col_type in config['columns']:
                if col_name not in existing_cols:
                    try:
                        db.session.execute(db.text(f'ALTER TABLE {table} ADD COLUMN {col_name} {col_type}'))
                        print(f"[OK] Added '{col_name}' to {table}")
                    except Exception as e:
                        print(f"[!] Could not add '{col_name}' to {table}: {str(e)[:60]}")
            
            # Create indexes
            for idx_name, idx_col in config.get('indexes', []):
                existing_indexes = [i['name'] for i in inspector.get_indexes(table)]
                if idx_name not in existing_indexes:
                    try:
                        db.session.execute(db.text(f'CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({idx_col})'))
                        print(f"[OK] Created index '{idx_name}' on {table}")
                    except Exception as e:
                        print(f"[!] Could not create index '{idx_name}': {str(e)[:60]}")
        
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
        
        # Update admin user level
        print("\n[Levels] Updating user levels...")
        users = User.query.all()
        for user in users:
            user.update_level()
        db.session.commit()
        print(f"[OK] Updated {len(users)} users")
        
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