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
            'user': {
                'columns': [
                    ('level', 'INTEGER DEFAULT 1'),
                    ('streak_days', 'INTEGER DEFAULT 0'),
                    ('last_streak_date', 'DATE'),
                    ('total_quiz_score', 'INTEGER DEFAULT 0'),
                    ('total_quizzes_taken', 'INTEGER DEFAULT 0'),
                    ('achievements_json', 'TEXT DEFAULT \'[]\''),
                    ('bio', 'TEXT'),
                    ('email_sender_address', 'VARCHAR(150)'),
                    ('encrypted_app_password', 'VARCHAR(500)'),
                    ('email_enabled', 'BOOLEAN DEFAULT 0'),
                    ('last_report_sent', 'DATETIME'),
                    # NEW: multi-tenancy + account status
                    ('institution_id', 'INTEGER'),
                    ('is_active_account', 'BOOLEAN DEFAULT 1'),
                ],
                'indexes': [
                    ('ix_user_role', 'role'),
                    ('ix_user_xp', 'xp'),
                    ('ix_user_institution_id', 'institution_id'),
                ]
            },
            'assignment': {
                'columns': [
                    ('question_file_path', 'VARCHAR(500)'),
                    ('question_file_name', 'VARCHAR(300)'),
                    ('response_mode', 'VARCHAR(20) DEFAULT \'either\''),
                ]
            },
            'assignment_submission': {
                'columns': [
                    ('file_name', 'VARCHAR(300)'),
                ]
            },
            'attendance': {
                'columns': [
                    ('session_id', 'INTEGER'),
                ],
                'indexes': [
                    ('ix_attendance_session_id', 'session_id'),
                ]
            },
            'video': {
                'columns': [
                    ('chapters_json', 'TEXT'),
                    ('difficulty', 'VARCHAR(20) DEFAULT \'intermediate\''),
                ]
            },
            'classroom': {
                'columns': [
                    ('color_theme', 'VARCHAR(7) DEFAULT \'#4f46e5\''),
                ]
            },
            'quiz': {
                'columns': [
                    ('passing_percent', 'INTEGER DEFAULT 50'),
                    ('max_attempts', 'INTEGER DEFAULT 0'),
                ]
            },
            'question': {
                'columns': [
                    ('points', 'INTEGER DEFAULT 1'),
                ]
            },
            'quiz_result': {
                'columns': [
                    ('time_taken_seconds', 'INTEGER DEFAULT 0'),
                    ('passed', 'BOOLEAN DEFAULT 0'),
                ]
            },
            'notification': {
                'columns': [
                    ('action_url', 'VARCHAR(500)'),
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
                ]
            },
            'email_delivery_log': {
                'columns': [
                    ('report_html', 'TEXT'),
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
            'leaderboard_entry', 'email_queue', 'student_remark', 'email_delivery_log'
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