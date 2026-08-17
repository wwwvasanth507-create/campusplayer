"""
Database migration script to add video expiration, institution deletion permissions,
and AI video summarizer fields to existing SQLite/PostgreSQL database.
"""
from factory import create_app
from extensions import db
from sqlalchemy import text

app = create_app()

def run_migration():
    with app.app_context():
        engine = db.engine
        with engine.connect() as conn:
            # Video table updates
            try:
                conn.execute(text("ALTER TABLE video ADD COLUMN auto_delete_at DATETIME"))
                print("Added auto_delete_at to video table.")
            except Exception as e:
                print(f"auto_delete_at column exists or error: {e}")

            try:
                conn.execute(text("ALTER TABLE video ADD COLUMN retention_days INTEGER"))
                print("Added retention_days to video table.")
            except Exception as e:
                print(f"retention_days column exists or error: {e}")

            try:
                conn.execute(text("ALTER TABLE video ADD COLUMN ai_summary TEXT"))
                print("Added ai_summary to video table.")
            except Exception as e:
                print(f"ai_summary column exists or error: {e}")

            try:
                conn.execute(text("ALTER TABLE video ADD COLUMN ai_key_takeaways TEXT"))
                print("Added ai_key_takeaways to video table.")
            except Exception as e:
                print(f"ai_key_takeaways column exists or error: {e}")

            try:
                conn.execute(text("ALTER TABLE video ADD COLUMN ai_summary_generated_at DATETIME"))
                print("Added ai_summary_generated_at to video table.")
            except Exception as e:
                print(f"ai_summary_generated_at column exists or error: {e}")

            # Institution table updates
            try:
                conn.execute(text("ALTER TABLE institution ADD COLUMN allow_manual_video_delete BOOLEAN DEFAULT 1"))
                print("Added allow_manual_video_delete to institution table.")
            except Exception as e:
                print(f"allow_manual_video_delete column exists or error: {e}")

            try:
                conn.execute(text("ALTER TABLE institution ADD COLUMN allow_auto_video_delete BOOLEAN DEFAULT 1"))
                print("Added allow_auto_video_delete to institution table.")
            except Exception as e:
                print(f"allow_auto_video_delete column exists or error: {e}")

            try:
                conn.execute(text("ALTER TABLE institution ADD COLUMN max_video_retention_days INTEGER DEFAULT 365"))
                print("Added max_video_retention_days to institution table.")
            except Exception as e:
                print(f"max_video_retention_days column exists or error: {e}")

            conn.commit()
            print("Migration completed successfully!")

if __name__ == '__main__':
    run_migration()