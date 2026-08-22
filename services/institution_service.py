"""
CampusPlayer - Institution Management & Safe Deletion Service Engine.

Provides production-safe, multi-tenant isolated deletion of institutions,
handling both database record cascades and filesystem asset cleanups with strict path security.
"""

import os
import shutil
import logging
import threading
from typing import Optional, Dict, Any, Tuple
from flask import has_request_context, g

from extensions import db
from models import (
    Institution, User, Video, VideoLike, Playlist, Classroom, Comment,
    ViewAnalytics, Notification, SiteSettings, Quiz, Question, QuizResult,
    ChatMessage, Attendance, AttendanceSession, AttendanceSubSession,
    ActivityLog, SystemMetric, Assignment, AssignmentSubmission, StudentProfile,
    VideoNote, VideoBookmark, VideoProgress, LeaderboardEntry, EmailQueue,
    StudentRemark, EmailDeliveryLog, ConversionJob, ClassWeeklyReport,
    VideoCheckpoint, CheckpointResponse, VideoDoubt, VideoDoubtReply,
    VideoFlashcard, AcademicCertificate, ParentAccessToken, Announcement,
    AnnouncementRead, TimetableSlot, UserReward, EBook, EBookProgress,
    AICopilotInteraction, UserSession, playlist_videos, student_classes
)

logger = logging.getLogger(__name__)

# Mutex to ensure thread-safe, non-concurrent institution deletions
_institution_deletion_lock = threading.Lock()


def ensure_institution_storage_directories(slug_or_id: str, base_dir: Optional[str] = None) -> str:
    """
    Creates and verifies the dedicated isolated storage directory structure for an institution.
    Subfolders: videos, pdfs, assignments, quizzes, thumbnails, other, temp, subtitles, global.
    """
    import re
    if not base_dir:
        base_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__))))
    clean_identifier = re.sub(r'[^a-zA-Z0-9_\-]', '', str(slug_or_id)) or 'default'
    target_dir = os.path.join(base_dir, 'static', 'uploads', 'institutions', clean_identifier)
    subdirectories = ['videos', 'pdfs', 'assignments', 'quizzes', 'thumbnails', 'other', 'temp', 'subtitles', 'global']
    for sub in subdirectories:
        os.makedirs(os.path.join(target_dir, sub), exist_ok=True)
    return target_dir


def get_tenant_upload_dir(institution_id_or_user: Any, subfolder: str = 'other') -> Tuple[str, str, str]:
    """
    Resolves isolated tenant upload directory and relative base path.
    Returns: (absolute_dir: str, rel_prefix: str, slug: str)
    """
    import re
    base_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__))))
    inst = None
    if isinstance(institution_id_or_user, User):
        inst_id = getattr(institution_id_or_user, 'institution_id', None)
    elif isinstance(institution_id_or_user, int):
        inst_id = institution_id_or_user
    else:
        inst_id = None

    if inst_id:
        inst = Institution.query.get(inst_id)

    if not inst:
        inst = Institution.query.filter_by(slug='default').first()

    slug = inst.slug if inst else 'default'
    clean_subfolder = re.sub(r'[^a-zA-Z0-9_\-]', '', subfolder) or 'other'

    abs_dir = os.path.join(base_dir, 'static', 'uploads', 'institutions', slug, clean_subfolder)
    os.makedirs(abs_dir, exist_ok=True)
    rel_prefix = f'uploads/institutions/{slug}/{clean_subfolder}'
    return abs_dir, rel_prefix, slug


def validate_storage_path_security(target_path: Optional[str], slug: str, base_dir: Optional[str] = None) -> Tuple[bool, str]:
    """
    Validates that a filesystem path is safe for deletion and strictly contained
    within the application's configured institution uploads directory.

    Returns:
        (is_safe: bool, reason_or_resolved_path: str)
    """
    if not target_path or not isinstance(target_path, str):
        return False, "Target path is empty or invalid"

    if not slug or not isinstance(slug, str) or '..' in slug or '/' in slug or '\\' in slug:
        return False, "Invalid or unsafe institution slug"

    if not base_dir:
        base_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__))))

    allowed_roots = [
        os.path.realpath(os.path.join(base_dir, 'static', 'uploads', 'institutions')),
        os.path.realpath(os.path.join(base_dir, 'uploads', 'institutions'))
    ]

    try:
        resolved_path = os.path.realpath(target_path)
    except Exception as e:
        return False, f"Could not resolve path: {e}"

    # 1. Reject system root, Windows drives, project root, static/uploads root
    forbidden_exact_paths = {
        os.path.realpath('/'),
        os.path.realpath(base_dir),
        os.path.realpath(os.path.join(base_dir, 'static')),
        os.path.realpath(os.path.join(base_dir, 'static', 'uploads')),
        os.path.realpath(os.path.join(base_dir, 'uploads')),
    }
    if os.name == 'nt':
        drive = os.path.splitdrive(resolved_path)[0]
        if drive:
            forbidden_exact_paths.add(os.path.realpath(drive + '\\'))
            forbidden_exact_paths.add(os.path.realpath(drive + '/'))

    if resolved_path in forbidden_exact_paths:
        return False, f"Refusing to operate on forbidden root directory: {resolved_path}"

    # 2. Check containment within one of the allowed institution roots
    is_contained = False
    for root in allowed_roots:
        if os.path.exists(root) or os.path.dirname(root):
            try:
                common = os.path.commonpath([root, resolved_path])
                if common == root and resolved_path != root:
                    is_contained = True
                    break
            except ValueError:
                continue

    if not is_contained:
        return False, f"Path {resolved_path} is outside allowed institution storage roots"

    # 3. Verify directory basename matches slug
    basename = os.path.basename(resolved_path)
    if basename != slug:
        return False, f"Path basename '{basename}' does not match target institution slug '{slug}'"

    return True, resolved_path


def permanently_delete_institution(
    institution_id: int,
    actor_user: Optional[User] = None,
    confirm_name: Optional[str] = None
) -> Dict[str, Any]:
    """
    Permanently and securely deletes an institution and all of its exclusive data
    from both the application database and server filesystem.

    Args:
        institution_id: The ID of the institution to delete.
        actor_user: The User performing the deletion (for authorization checks).
        confirm_name: The exact name typed by the user to confirm deletion.

    Returns:
        Dict with keys: success (bool), message (str), status_code (int)
    """
    with _institution_deletion_lock:
        # 1. Fetch institution without tenant filter restriction
        orig_ignore = False
        if has_request_context():
            orig_ignore = getattr(g, 'ignore_tenant_filter', False)
            g.ignore_tenant_filter = True

        try:
            institution = Institution.query.get(institution_id)
            if not institution:
                return {
                    'success': False,
                    'message': f'Institution #{institution_id} not found.',
                    'status_code': 404
                }

            # Hard protection for Default Institution
            if institution.slug == 'default':
                return {
                    'success': False,
                    'message': 'The Default System Institution cannot be deleted.',
                    'status_code': 400
                }

            # 2. Confirmation Name Check
            if confirm_name is not None:
                if confirm_name.strip() != institution.name.strip():
                    return {
                        'success': False,
                        'message': f'Validation Error: Confirmation name "{confirm_name}" does not match institution name "{institution.name}".',
                        'status_code': 400
                    }

            # 3. Authorization Check (System Admin Only per Requirement 7 & 10)
            if actor_user:
                try:
                    actor_role = getattr(actor_user, 'role', None)
                except Exception:
                    actor_role = None

                if actor_role != 'system_admin':
                    return {
                        'success': False,
                        'message': 'Forbidden: Only System Admin may permanently delete an institution.',
                        'status_code': 403
                    }

            inst_name = institution.name
            inst_slug = institution.slug

            # 4. Phase A — Inventory Collection & Asset Path Gathering
            users = User.query.filter(
                (User.institution_id == institution_id) | (User.id == institution.owner_admin_id)
            ).all()
            user_ids = [u.id for u in users if u.id]

            classrooms = Classroom.query.filter(
                (Classroom.institution_id == institution_id) |
                (Classroom.teacher_id.in_(user_ids) if user_ids else db.false())
            ).all()
            classroom_ids = [c.id for c in classrooms]

            videos = Video.query.filter(
                (Video.institution_id == institution_id) |
                (Video.uploader_id.in_(user_ids) if user_ids else db.false()) |
                (Video.classroom_id.in_(classroom_ids) if classroom_ids else db.false())
            ).all()
            video_ids = [v.id for v in videos]

            ebooks = EBook.query.filter(
                (EBook.institution_id == institution_id) |
                (EBook.uploader_id.in_(user_ids) if user_ids else db.false())
            ).all()
            ebook_ids = [e.id for e in ebooks]

            quizzes = Quiz.query.filter(
                (Quiz.institution_id == institution_id) |
                (Quiz.teacher_id.in_(user_ids) if user_ids else db.false()) |
                (Quiz.classroom_id.in_(classroom_ids) if classroom_ids else db.false()) |
                (Quiz.video_id.in_(video_ids) if video_ids else db.false())
            ).all()
            quiz_ids = [q.id for q in quizzes]

            questions = Question.query.filter(
                (Question.institution_id == institution_id) |
                (Question.quiz_id.in_(quiz_ids) if quiz_ids else db.false())
            ).all()
            question_ids = [q.id for q in questions]

            assignments = Assignment.query.filter(
                (Assignment.institution_id == institution_id) |
                (Assignment.teacher_id.in_(user_ids) if user_ids else db.false()) |
                (Assignment.classroom_id.in_(classroom_ids) if classroom_ids else db.false())
            ).all()
            assignment_ids = [a.id for a in assignments]

            attendance_sessions = AttendanceSession.query.filter(
                (AttendanceSession.institution_id == institution_id) |
                (AttendanceSession.classroom_id.in_(classroom_ids) if classroom_ids else db.false())
            ).all()
            session_ids = [s.id for s in attendance_sessions]

            announcements = Announcement.query.filter(
                (Announcement.institution_id == institution_id) |
                (Announcement.author_id.in_(user_ids) if user_ids else db.false()) |
                (Announcement.classroom_id.in_(classroom_ids) if classroom_ids else db.false())
            ).all()
            announcement_ids = [an.id for an in announcements]

            video_doubts = VideoDoubt.query.filter(
                (VideoDoubt.institution_id == institution_id) |
                (VideoDoubt.video_id.in_(video_ids) if video_ids else db.false()) |
                (VideoDoubt.user_id.in_(user_ids) if user_ids else db.false())
            ).all()
            doubt_ids = [d.id for d in video_doubts]

            checkpoints = VideoCheckpoint.query.filter(
                (VideoCheckpoint.institution_id == institution_id) |
                (VideoCheckpoint.video_id.in_(video_ids) if video_ids else db.false())
            ).all()
            checkpoint_ids = [chk.id for chk in checkpoints]

            playlists = Playlist.query.filter(
                (Playlist.institution_id == institution_id) |
                (Playlist.creator_id.in_(user_ids) if user_ids else db.false())
            ).all()
            playlist_ids = [p.id for p in playlists]

            # Gather standalone file paths from database records
            standalone_files_to_delete = set()
            base_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__))))

            def _add_local_path(raw_path):
                if raw_path and isinstance(raw_path, str) and not raw_path.startswith(('http://', 'https://')):
                    clean_rel = raw_path.lstrip('/\\')
                    for sub in ['static', 'uploads']:
                        if clean_rel.startswith(f"{sub}/"):
                            clean_rel = clean_rel[len(sub)+1:]
                    p1 = os.path.join(base_dir, 'static', 'uploads', clean_rel)
                    p2 = os.path.join(base_dir, 'uploads', clean_rel)
                    if os.path.exists(p1) and os.path.isfile(p1):
                        standalone_files_to_delete.add(p1)
                    elif os.path.exists(p2) and os.path.isfile(p2):
                        standalone_files_to_delete.add(p2)

            for eb in ebooks:
                _add_local_path(eb.file_path)
                _add_local_path(eb.cover_image_path)

            for sp in StudentProfile.query.filter(
                (StudentProfile.institution_id == institution_id) |
                (StudentProfile.user_id.in_(user_ids) if user_ids else db.false())
            ).all():
                _add_local_path(sp.photo_path)
                _add_local_path(sp.signature_path)
                _add_local_path(sp.aadhaar_path)
                _add_local_path(sp.transfer_certificate_path)
                _add_local_path(sp.community_certificate_path)
                for cert in sp.get_other_certificates():
                    if isinstance(cert, dict):
                        _add_local_path(cert.get('path'))

            for asg in assignments:
                _add_local_path(asg.question_file_path)

            for sub in AssignmentSubmission.query.filter(
                (AssignmentSubmission.institution_id == institution_id) |
                (AssignmentSubmission.student_id.in_(user_ids) if user_ids else db.false()) |
                (AssignmentSubmission.assignment_id.in_(assignment_ids) if assignment_ids else db.false())
            ).all():
                _add_local_path(sub.file_path)
                _add_local_path(sub.audio_file_path)

            for u in users:
                _add_local_path(u.avatar_url)

            st_settings = SiteSettings.query.filter(SiteSettings.institution_id == institution_id).all()
            for s in st_settings:
                _add_local_path(s.logo_url)
                _add_local_path(s.global_playlist_thumbnail)

            _add_local_path(institution.logo_url)

            # 5. Phase B — Atomic Database Transaction Deletion
            try:
                def _delete_q(q):
                    with db.session.no_autoflush:
                        for item in q.all():
                            db.session.delete(item)

                # Step B.1: Nullify circular FK on Institution owner_admin_id
                institution.owner_admin_id = None
                db.session.commit()

                # Step B.2: Child objects & interactions
                if checkpoint_ids:
                    _delete_q(CheckpointResponse.query.filter(CheckpointResponse.checkpoint_id.in_(checkpoint_ids)))
                if user_ids:
                    _delete_q(CheckpointResponse.query.filter(CheckpointResponse.student_id.in_(user_ids)))

                if video_ids:
                    _delete_q(VideoCheckpoint.query.filter(VideoCheckpoint.video_id.in_(video_ids)))

                if doubt_ids:
                    _delete_q(VideoDoubtReply.query.filter(VideoDoubtReply.doubt_id.in_(doubt_ids)))
                if user_ids:
                    _delete_q(VideoDoubtReply.query.filter(VideoDoubtReply.user_id.in_(user_ids)))
                if video_ids:
                    _delete_q(VideoDoubt.query.filter(VideoDoubt.video_id.in_(video_ids)))
                if user_ids:
                    _delete_q(VideoDoubt.query.filter(VideoDoubt.user_id.in_(user_ids)))

                if video_ids:
                    _delete_q(VideoFlashcard.query.filter(VideoFlashcard.video_id.in_(video_ids)))
                    _delete_q(VideoNote.query.filter(VideoNote.video_id.in_(video_ids)))
                    _delete_q(VideoBookmark.query.filter(VideoBookmark.video_id.in_(video_ids)))
                    _delete_q(VideoProgress.query.filter(VideoProgress.video_id.in_(video_ids)))
                    _delete_q(VideoLike.query.filter(VideoLike.video_id.in_(video_ids)))
                    _delete_q(ViewAnalytics.query.filter(ViewAnalytics.video_id.in_(video_ids)))
                    _delete_q(ConversionJob.query.filter(ConversionJob.video_id.in_(video_ids)))
                    _delete_q(Notification.query.filter(Notification.video_id.in_(video_ids)))

                if user_ids:
                    _delete_q(VideoFlashcard.query.filter(VideoFlashcard.user_id.in_(user_ids)))
                    _delete_q(VideoNote.query.filter(VideoNote.user_id.in_(user_ids)))
                    _delete_q(VideoBookmark.query.filter(VideoBookmark.user_id.in_(user_ids)))
                    _delete_q(VideoProgress.query.filter(VideoProgress.user_id.in_(user_ids)))
                    _delete_q(VideoLike.query.filter(VideoLike.user_id.in_(user_ids)))
                    _delete_q(ViewAnalytics.query.filter(ViewAnalytics.user_id.in_(user_ids)))
                    _delete_q(AICopilotInteraction.query.filter(AICopilotInteraction.user_id.in_(user_ids)))

                if video_ids:
                    _delete_q(AICopilotInteraction.query.filter(AICopilotInteraction.video_id.in_(video_ids)))

                if ebook_ids:
                    _delete_q(AICopilotInteraction.query.filter(AICopilotInteraction.cited_book_id.in_(ebook_ids)))
                    _delete_q(EBookProgress.query.filter(EBookProgress.ebook_id.in_(ebook_ids)))

                if user_ids:
                    _delete_q(EBookProgress.query.filter(EBookProgress.user_id.in_(user_ids)))

                _delete_q(EBook.query.filter((EBook.institution_id == institution_id) | (EBook.uploader_id.in_(user_ids) if user_ids else db.false())))

                if classroom_ids:
                    _delete_q(ChatMessage.query.filter(ChatMessage.classroom_id.in_(classroom_ids)))
                if user_ids:
                    _delete_q(ChatMessage.query.filter(ChatMessage.user_id.in_(user_ids)))

                if user_ids:
                    _delete_q(Comment.query.filter(Comment.user_id.in_(user_ids)))
                if video_ids:
                    _delete_q(Comment.query.filter(Comment.video_id.in_(video_ids)))

                if user_ids:
                    _delete_q(Notification.query.filter(Notification.user_id.in_(user_ids)))

                # Playlists & association table
                if video_ids:
                    db.session.execute(playlist_videos.delete().where(playlist_videos.c.video_id.in_(video_ids)))
                if playlist_ids:
                    db.session.execute(playlist_videos.delete().where(playlist_videos.c.playlist_id.in_(playlist_ids)))
                    _delete_q(Playlist.query.filter(Playlist.id.in_(playlist_ids)))
                if user_ids:
                    _delete_q(Playlist.query.filter(Playlist.creator_id.in_(user_ids)))

                # Quizzes & Questions & Results
                if user_ids:
                    _delete_q(QuizResult.query.filter(QuizResult.student_id.in_(user_ids)))
                if quiz_ids:
                    _delete_q(QuizResult.query.filter(QuizResult.quiz_id.in_(quiz_ids)))
                    _delete_q(Question.query.filter(Question.quiz_id.in_(quiz_ids)))
                    _delete_q(Quiz.query.filter(Quiz.id.in_(quiz_ids)))

                # Assignments & Submissions
                if user_ids:
                    _delete_q(AssignmentSubmission.query.filter(AssignmentSubmission.student_id.in_(user_ids)))
                if assignment_ids:
                    _delete_q(AssignmentSubmission.query.filter(AssignmentSubmission.assignment_id.in_(assignment_ids)))
                    _delete_q(Assignment.query.filter(Assignment.id.in_(assignment_ids)))

                # Attendance & Sessions
                if classroom_ids:
                    _delete_q(Attendance.query.filter(Attendance.classroom_id.in_(classroom_ids)))
                if user_ids:
                    _delete_q(Attendance.query.filter(Attendance.student_id.in_(user_ids)))

                if session_ids:
                    _delete_q(AttendanceSubSession.query.filter(AttendanceSubSession.attendance_session_id.in_(session_ids)))
                    _delete_q(AttendanceSession.query.filter(AttendanceSession.id.in_(session_ids)))

                # Remarks & Logs
                if user_ids:
                    _delete_q(StudentRemark.query.filter(StudentRemark.student_id.in_(user_ids)))
                if classroom_ids:
                    _delete_q(StudentRemark.query.filter(StudentRemark.classroom_id.in_(classroom_ids)))

                if user_ids:
                    _delete_q(EmailDeliveryLog.query.filter(
                        (EmailDeliveryLog.teacher_id.in_(user_ids)) |
                        (EmailDeliveryLog.student_id.in_(user_ids))
                    ))
                if classroom_ids:
                    _delete_q(EmailDeliveryLog.query.filter(EmailDeliveryLog.class_id.in_(classroom_ids)))

                if classroom_ids:
                    _delete_q(ClassWeeklyReport.query.filter(ClassWeeklyReport.classroom_id.in_(classroom_ids)))
                if user_ids:
                    _delete_q(ClassWeeklyReport.query.filter(ClassWeeklyReport.teacher_id.in_(user_ids)))

                if classroom_ids:
                    _delete_q(TimetableSlot.query.filter(TimetableSlot.classroom_id.in_(classroom_ids)))
                if user_ids:
                    _delete_q(TimetableSlot.query.filter(TimetableSlot.teacher_id.in_(user_ids)))

                # Announcements
                if announcement_ids:
                    _delete_q(AnnouncementRead.query.filter(AnnouncementRead.announcement_id.in_(announcement_ids)))
                if user_ids:
                    _delete_q(AnnouncementRead.query.filter(AnnouncementRead.user_id.in_(user_ids)))
                if announcement_ids:
                    _delete_q(Announcement.query.filter(Announcement.id.in_(announcement_ids)))

                # User Rewards
                if user_ids:
                    _delete_q(UserReward.query.filter(UserReward.user_id.in_(user_ids)))

                # Academic Certificates & Parent Tokens
                if user_ids:
                    _delete_q(AcademicCertificate.query.filter(AcademicCertificate.student_id.in_(user_ids)))
                    _delete_q(ParentAccessToken.query.filter(ParentAccessToken.student_id.in_(user_ids)))
                    _delete_q(StudentProfile.query.filter(StudentProfile.user_id.in_(user_ids)))

                if user_ids:
                    _delete_q(LeaderboardEntry.query.filter(LeaderboardEntry.user_id.in_(user_ids)))
                    _delete_q(ActivityLog.query.filter(ActivityLog.user_id.in_(user_ids)))

                # Videos
                if video_ids:
                    # Execute video cleanup hook per video ID
                    from services.video_cleanup import permanently_delete_video_assets
                    for vid in video_ids:
                        try:
                            permanently_delete_video_assets(vid)
                        except Exception as e:
                            logger.warning(f"[InstitutionDelete] Video cleanup error for video #{vid}: {e}")
                    _delete_q(Video.query.filter(Video.id.in_(video_ids)))

                # Classrooms & student_classes
                if classroom_ids:
                    Video.query.filter(Video.classroom_id.in_(classroom_ids)).update({Video.classroom_id: None}, synchronize_session=False)
                    db.session.execute(student_classes.delete().where(student_classes.c.classroom_id.in_(classroom_ids)))
                    _delete_q(Classroom.query.filter(Classroom.id.in_(classroom_ids)))
                if user_ids:
                    db.session.execute(student_classes.delete().where(student_classes.c.student_id.in_(user_ids)))

                # Sessions, Metrics, Queue, SiteSettings
                _delete_q(UserSession.query.filter(UserSession.institution_id == institution_id))
                if user_ids:
                    _delete_q(UserSession.query.filter(UserSession.user_id.in_(user_ids)))

                _delete_q(SystemMetric.query.filter(SystemMetric.institution_id == institution_id))
                _delete_q(EmailQueue.query.filter(EmailQueue.institution_id == institution_id))
                _delete_q(SiteSettings.query.filter(SiteSettings.institution_id == institution_id))

                # Delete Users & Institution
                for u in users:
                    db.session.delete(u)

                db.session.delete(institution)
                db.session.commit()
                logger.info(f"[InstitutionDelete] Database transaction committed successfully for institution '{inst_name}' (ID #{institution_id}).")

            except Exception as e:
                db.session.rollback()
                logger.error(f"[InstitutionDelete] Database transaction failed for institution #{institution_id}: {e}", exc_info=True)
                return {
                    'success': False,
                    'message': f'Database error during institution deletion: {str(e)}',
                    'status_code': 500
                }

            # 6. Phase C — Host Server Filesystem Cleanup
            storage_path_1 = os.path.join(base_dir, 'static', 'uploads', 'institutions', inst_slug)
            storage_path_2 = os.path.join(base_dir, 'uploads', 'institutions', inst_slug)

            cleaned_dirs = 0
            for sp in [storage_path_1, storage_path_2]:
                if os.path.exists(sp):
                    is_safe, res_or_reason = validate_storage_path_security(sp, inst_slug, base_dir)
                    if is_safe:
                        try:
                            shutil.rmtree(res_or_reason)
                            cleaned_dirs += 1
                            logger.info(f"[InstitutionDelete] Successfully deleted storage directory: {res_or_reason}")
                        except Exception as e:
                            logger.error(f"[InstitutionDelete] Failed to delete storage directory {res_or_reason}: {e}")
                    else:
                        logger.error(f"[InstitutionDelete] Path safety check rejected directory {sp}: {res_or_reason}")

            cleaned_files = 0
            for sf in standalone_files_to_delete:
                if sf and os.path.exists(sf) and os.path.isfile(sf):
                    try:
                        os.remove(sf)
                        cleaned_files += 1
                    except Exception as e:
                        logger.warning(f"[InstitutionDelete] Could not remove standalone file {sf}: {e}")

            logger.info(
                f"[InstitutionDelete] Completed full institution deletion for '{inst_name}' (ID #{institution_id}, slug '{inst_slug}'). "
                f"Directories cleaned: {cleaned_dirs}, Standalone files removed: {cleaned_files}."
            )

            return {
                'success': True,
                'message': f'Institution "{inst_name}" and all associated data permanently deleted.',
                'status_code': 200,
                'institution_id': institution_id,
                'institution_name': inst_name
            }

        finally:
            if has_request_context():
                g.ignore_tenant_filter = orig_ignore
