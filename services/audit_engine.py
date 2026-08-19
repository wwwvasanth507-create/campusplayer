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
            from models import (
                Institution, User, Video, Playlist, Classroom, Comment,
                ViewAnalytics, Notification, SiteSettings, Quiz, Question,
                QuizResult, ChatMessage, Attendance, Assignment, AssignmentSubmission,
                StudentProfile, VideoNote, VideoBookmark, VideoProgress,
                ConversionJob, Announcement, TimetableSlot, RewardItem, EBook,
                UserSession
            )

            # 1. Record counts
            counts = {
                'institutions': Institution.query.count(),
                'users_total': User.query.count(),
                'users_system_admin': User.query.filter_by(role='system_admin').count(),
                'users_admin': User.query.filter_by(role='admin').count(),
                'users_teacher': User.query.filter_by(role='teacher').count(),
                'users_student': User.query.filter_by(role='student').count(),
                'videos': Video.query.count(),
                'playlists': Playlist.query.count(),
                'classrooms': Classroom.query.count(),
                'comments': Comment.query.count(),
                'view_analytics': ViewAnalytics.query.count(),
                'notifications': Notification.query.count(),
                'site_settings': SiteSettings.query.count(),
                'quizzes': Quiz.query.count(),
                'questions': Question.query.count(),
                'quiz_results': QuizResult.query.count(),
                'chat_messages': ChatMessage.query.count(),
                'attendances': Attendance.query.count(),
                'assignments': Assignment.query.count(),
                'assignment_submissions': AssignmentSubmission.query.count(),
                'student_profiles': StudentProfile.query.count(),
                'video_notes': VideoNote.query.count(),
                'video_bookmarks': VideoBookmark.query.count(),
                'video_progresses': VideoProgress.query.count(),
                'conversion_jobs': ConversionJob.query.count(),
                'announcements': Announcement.query.count(),
                'timetable_slots': TimetableSlot.query.count(),
                'reward_items': RewardItem.query.count(),
                'ebooks': EBook.query.count(),
                'active_user_sessions': UserSession.query.filter_by(is_active=True).count()
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

            # 3. Check video file references on disk
            videos = Video.query.all()
            missing = []
            for v in videos:
                path_to_check = v.filename or v.hls_playlist_path
                if path_to_check and not path_to_check.startswith('http') and v.video_type != 'youtube':
                    abs_p = os.path.join(BASE_DIR, path_to_check.lstrip('/\\'))
                    if not os.path.exists(abs_p):
                        missing.append({'video_id': v.id, 'title': v.title, 'path': path_to_check})

            if missing:
                report['file_references'] = {'status': 'warning', 'missing_files': missing}


            # 4. Check for unattached / orphaned models (e.g. non-system_admin user with invalid institution)
            orphans = []
            inst_ids = {i.id for i in Institution.query.all()}
            unattached_users = User.query.filter(User.role != 'system_admin').all()
            for u in unattached_users:
                if u.institution_id is not None and u.institution_id not in inst_ids:
                    orphans.append(f"User {u.username} (id={u.id}) references missing institution_id={u.institution_id}")

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
