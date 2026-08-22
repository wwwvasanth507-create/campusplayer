"""
Data Migration Utility: SQLite (app.db) -> PostgreSQL Canonical Storage.

Usage:
    python scripts/migrate_sqlite_to_postgres.py [--sqlite-db app.db]
"""
import os
import sys
import json
import sqlite3
import logging
from datetime import datetime
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger('sqlite_to_postgres_migration')

def run_migration(sqlite_path='app.db'):
    from factory import create_app
    from extensions import db
    from models import (
        User, Institution, Video, Playlist, Comment, ViewAnalytics, Notification,
        Quiz, Question, QuizResult, SiteSettings, Classroom, ChatMessage, Attendance,
        VideoLike, ActivityLog, SystemMetric, Assignment, AssignmentSubmission,
        StudentProfile, VideoNote, VideoBookmark, VideoProgress, Achievement,
        LeaderboardEntry, EmailQueue, StudentRemark, EmailDeliveryLog, ConversionJob,
        ClassWeeklyReport, VideoCheckpoint, CheckpointResponse, VideoDoubt,
        VideoDoubtReply, VideoFlashcard, AcademicCertificate, ParentAccessToken,
        Announcement, AnnouncementRead, TimetableSlot, RewardItem, UserReward,
        EBook, EBookProgress, AICopilotInteraction, UserSession, DailyQuestTemplate,
        backfill_all_tables_with_default_institution
    )

    if not os.path.exists(sqlite_path):
        logger.error(f"SQLite database file not found at: {sqlite_path}")
        return False

    app = create_app()

    report = {
        'started_at': datetime.utcnow().isoformat(),
        'source_db': sqlite_path,
        'tables': {},
        'errors': []
    }

    models_order = [
        Institution, User, Classroom, Video, Playlist, Comment, ViewAnalytics,
        Notification, Quiz, Question, QuizResult, SiteSettings, ChatMessage,
        Attendance, VideoLike, ActivityLog, SystemMetric, Assignment,
        AssignmentSubmission, StudentProfile, VideoNote, VideoBookmark,
        VideoProgress, Achievement, LeaderboardEntry, EmailQueue, StudentRemark,
        EmailDeliveryLog, ConversionJob, ClassWeeklyReport, VideoCheckpoint,
        CheckpointResponse, VideoDoubt, VideoDoubtReply, VideoFlashcard,
        AcademicCertificate, ParentAccessToken, Announcement, AnnouncementRead,
        TimetableSlot, RewardItem, UserReward, EBook, EBookProgress,
        AICopilotInteraction, UserSession, DailyQuestTemplate
    ]

    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row
    sqlite_cursor = sqlite_conn.cursor()

    with app.app_context():
        # Ensure destination tables exist
        db.create_all()

        for model in models_order:
            table_name = model.__tablename__
            report['tables'][table_name] = {'source_rows': 0, 'inserted_rows': 0, 'errors': 0}

            try:
                sqlite_cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'")
                if not sqlite_cursor.fetchone():
                    logger.info(f"Table '{table_name}' not found in SQLite DB. Skipping.")
                    continue

                sqlite_cursor.execute(f"SELECT * FROM {table_name}")
                rows = sqlite_cursor.fetchall()
                report['tables'][table_name]['source_rows'] = len(rows)

                if not rows:
                    continue

                columns = [column[0] for column in sqlite_cursor.description]
                model_columns = {c.name for c in model.__table__.columns}

                for row in rows:
                    row_dict = {}
                    for col in columns:
                        if col in model_columns:
                            val = row[col]
                            row_dict[col] = val

                    try:
                        # Inspect duplicate key or insert
                        pk_col = model.__table__.primary_key.columns.keys()[0]
                        pk_val = row_dict.get(pk_col)

                        existing = None
                        if pk_val is not None:
                            existing = model.query.get(pk_val)

                        if not existing:
                            obj = model(**row_dict)
                            db.session.add(obj)
                            report['tables'][table_name]['inserted_rows'] += 1
                    except Exception as row_err:
                        report['tables'][table_name]['errors'] += 1
                        report['errors'].append({'table': table_name, 'error': str(row_err)})

                db.session.commit()
                logger.info(f"Migrated table '{table_name}': {report['tables'][table_name]['inserted_rows']} rows inserted.")
            except Exception as table_err:
                db.session.rollback()
                logger.error(f"Error migrating table '{table_name}': {table_err}")
                report['errors'].append({'table': table_name, 'error': str(table_err)})

        # Perform tenant backfill
        backfill_all_tables_with_default_institution(db, logger=logger)

    sqlite_conn.close()

    report['completed_at'] = datetime.utcnow().isoformat()
    report['status'] = 'success' if len(report['errors']) == 0 else 'completed_with_errors'

    with open('migration_report.json', 'w') as f:
        json.dump(report, f, indent=2)

    summary = {
        'status': report['status'],
        'total_tables_processed': len(report['tables']),
        'total_errors': len(report['errors'])
    }
    with open('migration_summary.json', 'w') as f:
        json.dump(summary, f, indent=2)

    with open('migration_errors.json', 'w') as f:
        json.dump(report['errors'], f, indent=2)

    logger.info("Migration completed. Reports generated: migration_report.json, migration_summary.json, migration_errors.json")
    return True

if __name__ == '__main__':
    src = sys.argv[1] if len(sys.argv) > 1 else 'app.db'
    run_migration(src)
