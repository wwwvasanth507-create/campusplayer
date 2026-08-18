"""
reset_systemadmin.py
=====================
Standalone utility to:
  1. Make sure the database schema is fully up to date (safe to run on an old DB —
     same auto-heal logic used by app.py, so this never crashes on a missing column).
  2. Reset (or create) the single System Admin account.
  3. Optionally seed sample Institutions, each with its own isolated Admin account,
     for testing the multi-institution hierarchy: System Admin -> Admins -> Teachers -> Students.

Usage:
    python reset_systemadmin.py
    python reset_systemadmin.py --username sysadmin --password "MyStrongPass123"
    python reset_systemadmin.py --seed-institutions 3
    python reset_systemadmin.py --wipe-institutions   # DANGEROUS: deletes every institution, admin,
                                                        # teacher, and student. Requires typing YES to confirm.

Environment variables (used if the matching flag isn't passed):
    SYSADMIN_USERNAME   (default: sysadmin)
    SYSADMIN_PASSWORD   (default: a random secure password, printed once)
    DATABASE_URL         (same variable app.py and migrate_db.py already use)
"""
import os
import sys
import argparse
import secrets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from factory import create_app
from extensions import db
from models import User, Institution


def ensure_schema_up_to_date(app):
    """Same additive, non-destructive column/table repair used by app.py's startup
    and migrate_db.py. Safe to run any number of times."""
    with app.app_context():
        db.create_all()
        inspector = db.inspect(db.engine)
        existing_tables = set(inspector.get_table_names())

        column_specs = {
            'user': [
                ('level', 'INTEGER DEFAULT 1'),
                ('streak_days', 'INTEGER DEFAULT 0'),
                ('last_streak_date', 'DATE'),
                ('total_quiz_score', 'INTEGER DEFAULT 0'),
                ('total_quizzes_taken', 'INTEGER DEFAULT 0'),
                ('achievements_json', "TEXT DEFAULT '[]'"),
                ('bio', 'TEXT'),
                ('email_sender_address', 'VARCHAR(150)'),
                ('encrypted_app_password', 'VARCHAR(500)'),
                ('email_enabled', 'BOOLEAN DEFAULT 0'),
                ('last_report_sent', 'DATETIME'),
                ('institution_id', 'INTEGER'),
                ('is_active_account', 'BOOLEAN DEFAULT 1'),
            ],
            'assignment': [
                ('question_file_path', 'VARCHAR(500)'),
                ('question_file_name', 'VARCHAR(300)'),
                ('response_mode', "VARCHAR(20) DEFAULT 'either'"),
            ],
            'assignment_submission': [
                ('file_name', 'VARCHAR(300)'),
            ],
            'attendance': [
                ('session_id', 'INTEGER'),
            ],
        }
        for table, columns in column_specs.items():
            if table not in existing_tables:
                continue
            existing_cols = {c['name'] for c in inspector.get_columns(table)}
            for col_name, col_type in columns:
                if col_name not in existing_cols:
                    try:
                        db.session.execute(db.text(f'ALTER TABLE {table} ADD COLUMN {col_name} {col_type}'))
                        db.session.commit()
                        print(f"[schema] Added column {table}.{col_name}")
                    except Exception as e:
                        db.session.rollback()
                        print(f"[schema] WARNING: could not add {table}.{col_name}: {e}")
        print("[schema] Database schema is up to date.")


from models import (
    User, Institution, Classroom, Video, Playlist, Comment, ViewAnalytics,
    Notification, SiteSettings, Quiz, Question, QuizResult, ChatMessage,
    Attendance, AttendanceSession, AttendanceSubSession, ActivityLog,
    SystemMetric, Assignment, AssignmentSubmission, StudentProfile,
    VideoNote, VideoBookmark, VideoProgress, VideoLike, LeaderboardEntry,
    EmailQueue, StudentRemark, EmailDeliveryLog, ConversionJob,
    playlist_videos, student_classes
)


def reset_system_admin(app, username, password, wipe_existing):
    with app.app_context():
        # First ensure target system admin exists / is updated
        existing = User.query.filter_by(username=username).first()
        if existing:
            if existing.role != 'system_admin':
                print(f"[system_admin] ERROR: username '{username}' is already taken by a "
                      f"'{existing.role}' account. Choose a different --username.")
                sys.exit(1)
            existing.set_password(password)
            db.session.commit()
            print(f"[system_admin] Reset password for existing system admin '{username}'.")
            target_user = existing
        else:
            sysadmin = User(username=username, role='system_admin')
            sysadmin.set_password(password)
            db.session.add(sysadmin)
            db.session.commit()
            print(f"[system_admin] Created new system admin '{username}'.")
            target_user = sysadmin

        if wipe_existing:
            # Safely remove any OTHER system_admin accounts without foreign key violations
            other_sysadmins = User.query.filter(User.role == 'system_admin', User.id != target_user.id).all()
            if other_sysadmins:
                other_ids = [u.id for u in other_sysadmins]
                # Clean up dependent records referencing these sysadmin users
                ActivityLog.query.filter(ActivityLog.user_id.in_(other_ids)).delete(synchronize_session=False)
                LeaderboardEntry.query.filter(LeaderboardEntry.user_id.in_(other_ids)).delete(synchronize_session=False)
                Notification.query.filter(Notification.user_id.in_(other_ids)).delete(synchronize_session=False)
                ChatMessage.query.filter(ChatMessage.user_id.in_(other_ids)).delete(synchronize_session=False)
                Comment.query.filter(Comment.user_id.in_(other_ids)).delete(synchronize_session=False)
                VideoNote.query.filter(VideoNote.user_id.in_(other_ids)).delete(synchronize_session=False)
                VideoBookmark.query.filter(VideoBookmark.user_id.in_(other_ids)).delete(synchronize_session=False)
                VideoProgress.query.filter(VideoProgress.user_id.in_(other_ids)).delete(synchronize_session=False)
                VideoLike.query.filter(VideoLike.user_id.in_(other_ids)).delete(synchronize_session=False)
                Institution.query.filter(Institution.owner_admin_id.in_(other_ids)).update({Institution.owner_admin_id: None}, synchronize_session=False)
                for u in other_sysadmins:
                    db.session.delete(u)
                db.session.commit()
                print(f"[system_admin] Removed {len(other_sysadmins)} other existing system_admin account(s).")


def wipe_all_institutions(app):
    """DANGEROUS: deletes every Institution, Admin, Teacher, and Student. The
    System Admin account itself is preserved."""
    with app.app_context():
        users = User.query.filter(User.role.in_(['admin', 'teacher', 'student'])).all()
        user_ids = [u.id for u in users]
        classrooms = Classroom.query.all()
        classroom_ids = [c.id for c in classrooms]

        if user_ids:
            ActivityLog.query.filter(ActivityLog.user_id.in_(user_ids)).delete(synchronize_session=False)
            StudentProfile.query.filter(StudentProfile.user_id.in_(user_ids)).delete(synchronize_session=False)
            EmailDeliveryLog.query.filter(
                (EmailDeliveryLog.teacher_id.in_(user_ids)) | 
                (EmailDeliveryLog.student_id.in_(user_ids))
            ).delete(synchronize_session=False)
            StudentRemark.query.filter(StudentRemark.student_id.in_(user_ids)).delete(synchronize_session=False)
            LeaderboardEntry.query.filter(LeaderboardEntry.user_id.in_(user_ids)).delete(synchronize_session=False)
            ChatMessage.query.filter(ChatMessage.user_id.in_(user_ids)).delete(synchronize_session=False)
            Attendance.query.filter(Attendance.student_id.in_(user_ids)).delete(synchronize_session=False)
            QuizResult.query.filter(QuizResult.student_id.in_(user_ids)).delete(synchronize_session=False)
            AssignmentSubmission.query.filter(AssignmentSubmission.student_id.in_(user_ids)).delete(synchronize_session=False)
            Comment.query.filter(Comment.user_id.in_(user_ids)).delete(synchronize_session=False)
            Notification.query.filter(Notification.user_id.in_(user_ids)).delete(synchronize_session=False)
            VideoNote.query.filter(VideoNote.user_id.in_(user_ids)).delete(synchronize_session=False)
            VideoBookmark.query.filter(VideoBookmark.user_id.in_(user_ids)).delete(synchronize_session=False)
            VideoProgress.query.filter(VideoProgress.user_id.in_(user_ids)).delete(synchronize_session=False)
            VideoLike.query.filter(VideoLike.user_id.in_(user_ids)).delete(synchronize_session=False)

        if classroom_ids:
            StudentRemark.query.filter(StudentRemark.classroom_id.in_(classroom_ids)).delete(synchronize_session=False)
            ChatMessage.query.filter(ChatMessage.classroom_id.in_(classroom_ids)).delete(synchronize_session=False)
            Attendance.query.filter(Attendance.classroom_id.in_(classroom_ids)).delete(synchronize_session=False)
            sessions = AttendanceSession.query.filter(AttendanceSession.classroom_id.in_(classroom_ids)).all()
            session_ids = [s.id for s in sessions]
            if session_ids:
                AttendanceSubSession.query.filter(AttendanceSubSession.attendance_session_id.in_(session_ids)).delete(synchronize_session=False)
                AttendanceSession.query.filter(AttendanceSession.id.in_(session_ids)).delete(synchronize_session=False)

        quizzes = Quiz.query.all()
        quiz_ids = [q.id for q in quizzes]
        if quiz_ids:
            QuizResult.query.filter(QuizResult.quiz_id.in_(quiz_ids)).delete(synchronize_session=False)
            Question.query.filter(Question.quiz_id.in_(quiz_ids)).delete(synchronize_session=False)
            Quiz.query.filter(Quiz.id.in_(quiz_ids)).delete(synchronize_session=False)

        assignments = Assignment.query.all()
        assignment_ids = [a.id for a in assignments]
        if assignment_ids:
            AssignmentSubmission.query.filter(AssignmentSubmission.assignment_id.in_(assignment_ids)).delete(synchronize_session=False)
            Assignment.query.filter(Assignment.id.in_(assignment_ids)).delete(synchronize_session=False)

        videos = Video.query.all()
        video_ids = [v.id for v in videos]
        if video_ids:
            Comment.query.filter(Comment.video_id.in_(video_ids)).delete(synchronize_session=False)
            VideoNote.query.filter(VideoNote.video_id.in_(video_ids)).delete(synchronize_session=False)
            VideoBookmark.query.filter(VideoBookmark.video_id.in_(video_ids)).delete(synchronize_session=False)
            VideoProgress.query.filter(VideoProgress.video_id.in_(video_ids)).delete(synchronize_session=False)
            VideoLike.query.filter(VideoLike.video_id.in_(video_ids)).delete(synchronize_session=False)
            Notification.query.filter(Notification.video_id.in_(video_ids)).delete(synchronize_session=False)
            db.session.execute(playlist_videos.delete().where(playlist_videos.c.video_id.in_(video_ids)))
            for v in videos:
                try:
                    from services.video_cleanup import permanently_delete_video_assets
                    permanently_delete_video_assets(v)
                except Exception:
                    pass
            Video.query.filter(Video.id.in_(video_ids)).delete(synchronize_session=False)

        playlists = Playlist.query.all()
        playlist_ids = [p.id for p in playlists]
        if playlist_ids:
            db.session.execute(playlist_videos.delete().where(playlist_videos.c.playlist_id.in_(playlist_ids)))
            Playlist.query.filter(Playlist.id.in_(playlist_ids)).delete(synchronize_session=False)

        if classroom_ids:
            db.session.execute(student_classes.delete().where(student_classes.c.classroom_id.in_(classroom_ids)))
            Classroom.query.filter(Classroom.id.in_(classroom_ids)).delete(synchronize_session=False)
        if user_ids:
            db.session.execute(student_classes.delete().where(student_classes.c.student_id.in_(user_ids)))

        SiteSettings.query.delete()
        Institution.query.update({Institution.owner_admin_id: None})
        db.session.commit()

        for u in users:
            db.session.delete(u)
        deleted_institutions = Institution.query.delete()
        db.session.commit()
        print(f"[wipe] Deleted {len(users)} admin/teacher/student account(s) "
              f"and {deleted_institutions} institution(s).")


def seed_sample_institutions(app, count):
    with app.app_context():
        created = []
        for i in range(1, count + 1):
            name = f"Sample Institution {i}"
            slug = f"sample-institution-{i}"
            username = f"admin_sample{i}"

            if Institution.query.filter_by(slug=slug).first():
                print(f"[seed] '{name}' already exists, skipping.")
                continue
            if User.query.filter_by(username=username).first():
                print(f"[seed] Admin username '{username}' already exists, skipping institution {i}.")
                continue

            password = secrets.token_urlsafe(9)
            institution = Institution(name=name, slug=slug, status='active',
                                       storage_root=f'uploads/institutions/{slug}/')
            db.session.add(institution)
            db.session.flush()

            admin_user = User(username=username, role='admin', institution_id=institution.id)
            admin_user.set_password(password)
            db.session.add(admin_user)
            db.session.flush()
            institution.owner_admin_id = admin_user.id
            db.session.commit()

            created.append((name, username, password))
            print(f"[seed] Created institution '{name}' with admin '{username}' (password: {password})")

        if created:
            print("\n[seed] IMPORTANT: save these admin credentials now — passwords are not stored in plain text and cannot be recovered:")
            for name, username, password in created:
                print(f"    {name:<28} username={username:<18} password={password}")
        else:
            print("[seed] No new sample institutions were created.")


def main():
    parser = argparse.ArgumentParser(description="Reset/create the System Admin account and optionally seed sample institutions.")
    parser.add_argument('--username', default=os.getenv('SYSADMIN_USERNAME', 'sysadmin'),
                         help="System admin username (default: sysadmin, or $SYSADMIN_USERNAME)")
    parser.add_argument('--password', default=os.getenv('SYSADMIN_PASSWORD'),
                         help="System admin password (default: $SYSADMIN_PASSWORD, or a random secure password)")
    parser.add_argument('--no-reset-existing', action='store_true',
                         help="Do not delete other existing system_admin accounts first (default: resets/removes them).")
    parser.add_argument('--seed-institutions', type=int, default=0, metavar='N',
                         help="Also create N sample institutions, each with its own admin, for testing.")
    parser.add_argument('--wipe-institutions', action='store_true',
                         help="DANGEROUS: delete ALL institutions, admins, teachers, and students. "
                              "Requires interactive confirmation. The system admin account is preserved.")
    args = parser.parse_args()

    password = args.password or secrets.token_urlsafe(12)
    password_was_generated = args.password is None and not os.getenv('SYSADMIN_PASSWORD')

    app = create_app()

    print("=" * 60)
    print("CampusPlayer - System Admin Reset")
    print("=" * 60)

    ensure_schema_up_to_date(app)

    if args.wipe_institutions:
        confirm = input(
            "\nThis will PERMANENTLY DELETE every institution, admin, teacher, and student.\n"
            "The system admin account will be kept. Type YES to continue: "
        )
        if confirm.strip() != 'YES':
            print("Aborted. No changes were made.")
            sys.exit(1)
        wipe_all_institutions(app)

    reset_system_admin(app, args.username, password, wipe_existing=not args.no_reset_existing)

    if args.seed_institutions > 0:
        seed_sample_institutions(app, args.seed_institutions)

    print("\n" + "=" * 60)
    print("Done.")
    print(f"System Admin login  ->  username: {args.username}")
    if password_was_generated:
        print(f"                         password: {password}   (generated — save this now, it will not be shown again)")
    else:
        print("                         password: (the one you provided)")
    print("Log in at /login and select role 'System Admin'.")
    print("=" * 60)


if __name__ == '__main__':
    main()
