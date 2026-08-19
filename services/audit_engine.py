"""
CampusPlayer - Data Integrity & Platform Audit Engine.

Calculates database record counts, verifies foreign key relationships, detects orphaned records,
and verifies file reference integrity on disk.
"""

import os
import sys
import json
import sqlite3
from datetime import datetime

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def run_platform_audit(app=None):
    """
    Run full data integrity audit against current app database.
    Returns (healthy: bool, audit_report: dict).
    """
    report = {
        'timestamp': datetime.utcnow().isoformat(),
        'counts': {},
        'foreign_keys': {'status': 'ok', 'violations': []},
        'integrity': {'status': 'ok', 'message': ''},
        'file_references': {'status': 'ok', 'missing_files': []},
        'orphaned_records': {'status': 'ok', 'orphans': []}
    }

    try:
        from factory import create_app
        if not app:
            app = create_app()

        with app.app_context():
            from extensions import db

            def safe_scalar(sql, default=0):
                try:
                    res = db.session.execute(db.text(sql)).scalar()
                    return res if res is not None else default
                except Exception:
                    return default

            def safe_rows(sql):
                try:
                    return db.session.execute(db.text(sql)).fetchall()
                except Exception:
                    return []

            # 1. Record counts (raw SQL - immune to missing ORM columns before migration)
            counts = {
                'institutions': safe_scalar('SELECT count(*) FROM institution'),
                'users_total': safe_scalar('SELECT count(*) FROM "user"'),
                'users_system_admin': safe_scalar('SELECT count(*) FROM "user" WHERE role = \'system_admin\''),
                'users_admin': safe_scalar('SELECT count(*) FROM "user" WHERE role = \'admin\''),
                'users_teacher': safe_scalar('SELECT count(*) FROM "user" WHERE role = \'teacher\''),
                'users_student': safe_scalar('SELECT count(*) FROM "user" WHERE role = \'student\''),
                'videos': safe_scalar('SELECT count(*) FROM video'),
                'playlists': safe_scalar('SELECT count(*) FROM playlist'),
                'classrooms': safe_scalar('SELECT count(*) FROM classroom'),
                'comments': safe_scalar('SELECT count(*) FROM comment'),
                'view_analytics': safe_scalar('SELECT count(*) FROM view_analytics'),
                'notifications': safe_scalar('SELECT count(*) FROM notification'),
                'site_settings': safe_scalar('SELECT count(*) FROM site_settings'),
                'quizzes': safe_scalar('SELECT count(*) FROM quiz'),
                'questions': safe_scalar('SELECT count(*) FROM question'),
                'quiz_results': safe_scalar('SELECT count(*) FROM quiz_result'),
                'chat_messages': safe_scalar('SELECT count(*) FROM chat_message'),
                'attendances': safe_scalar('SELECT count(*) FROM attendance'),
                'assignments': safe_scalar('SELECT count(*) FROM assignment'),
                'assignment_submissions': safe_scalar('SELECT count(*) FROM assignment_submission'),
                'student_profiles': safe_scalar('SELECT count(*) FROM student_profile'),
                'video_notes': safe_scalar('SELECT count(*) FROM video_note'),
                'video_bookmarks': safe_scalar('SELECT count(*) FROM video_bookmark'),
                'video_progresses': safe_scalar('SELECT count(*) FROM video_progress'),
                'conversion_jobs': safe_scalar('SELECT count(*) FROM conversion_job'),
                'announcements': safe_scalar('SELECT count(*) FROM announcement'),
                'timetable_slots': safe_scalar('SELECT count(*) FROM timetable_slot'),
                'reward_items': safe_scalar('SELECT count(*) FROM reward_item'),
                'ebooks': safe_scalar('SELECT count(*) FROM ebook'),
                'active_user_sessions': safe_scalar('SELECT count(*) FROM user_session WHERE is_active = 1')
            }
            report['counts'] = counts

            # 2. SQLite Foreign Key & Quick Integrity Check
            db_uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
            if 'sqlite:///' in db_uri:
                db_path = db_uri.replace('sqlite:///', '')
                if not os.path.isabs(db_path):
                    db_path = os.path.abspath(os.path.join(BASE_DIR, db_path))

                if os.path.exists(db_path):
                    conn = sqlite3.connect(db_path)
                    cursor = conn.cursor()

                    # Quick check
                    cursor.execute("PRAGMA quick_check;")
                    row = cursor.fetchone()
                    if row and row[0] != 'ok':
                        report['integrity'] = {'status': 'error', 'message': str(row[0])}

                    # FK check
                    cursor.execute("PRAGMA foreign_key_check;")
                    fk_rows = cursor.fetchall()
                    if fk_rows:
                        report['foreign_keys'] = {
                            'status': 'error',
                            'violations': [f"Table {r[0]} rowid {r[1]} references {r[2]}" for r in fk_rows[:20]]
                        }

                    conn.close()

            # 3. Check video file references on disk (raw SQL columns)
            video_rows = safe_rows('SELECT id, title, filename, hls_playlist_path, video_type FROM video')
            missing = []
            for r in video_rows:
                vid_id, title, filename, hls_path, vtype = r[0], r[1], r[2], r[3], r[4]
                path_to_check = filename or hls_path
                if path_to_check and not path_to_check.startswith('http') and vtype != 'youtube':
                    abs_p = os.path.join(BASE_DIR, path_to_check.lstrip('/\\'))
                    if not os.path.exists(abs_p):
                        missing.append({'video_id': vid_id, 'title': title, 'path': path_to_check})

            if missing:
                report['file_references'] = {'status': 'warning', 'missing_files': missing}

            # 4. Check for unattached / orphaned models (raw SQL)
            inst_rows = safe_rows('SELECT id FROM institution')
            inst_ids = {r[0] for r in inst_rows}
            user_rows = safe_rows('SELECT id, username, institution_id FROM "user" WHERE role != \'system_admin\'')
            orphans = []
            for r in user_rows:
                uid, uname, inst_id = r[0], r[1], r[2]
                if inst_id is not None and inst_id not in inst_ids:
                    orphans.append(f"User {uname} (id={uid}) references missing institution_id={inst_id}")

            if orphans:
                report['orphaned_records'] = {'status': 'error', 'orphans': orphans}

            is_healthy = (
                report['integrity']['status'] == 'ok' and
                report['foreign_keys']['status'] == 'ok' and
                report['orphaned_records']['status'] == 'ok'
            )

            return is_healthy, report

    except Exception as e:
        report['integrity'] = {'status': 'error', 'message': str(e)}
        return False, report

