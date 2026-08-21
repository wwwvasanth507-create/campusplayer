"""
CampusPlayer - Secure Institution Deletion & Multi-Tenant Isolation Verification Suite.

Tests end-to-end institution deletion flow, path security validation, database cascades,
filesystem asset removal, authorization controls, and multi-tenant isolation guarantees.
"""

import os
import sys
import shutil
import unittest
from datetime import datetime

os.environ['FLASK_TESTING'] = '1'
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, BASE_DIR)

from app import app
from extensions import db
from models import (
    Institution, User, Video, Classroom, Quiz, Question, QuizResult, EBook,
    Playlist, Assignment, AssignmentSubmission, Attendance, AttendanceSession,
    Comment, VideoNote, VideoBookmark, VideoProgress, VideoLike, ViewAnalytics,
    Notification, SiteSettings, StudentProfile, AICopilotInteraction
)
from services.institution_service import permanently_delete_institution, validate_storage_path_security


class TestInstitutionDeletionSuite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = app
        cls.app.config['TESTING'] = True
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.client = cls.app.test_client()

        with cls.app.app_context():
            db.session.remove()
            db.create_all()

            ts = datetime.utcnow().strftime('%Y%m%d%H%M%S')
            cls.ts = ts

            # 1. System Admin
            sysadmin = User.query.filter_by(username='sysadmin_del_test').first()
            if not sysadmin:
                sysadmin = User(username='sysadmin_del_test', role='system_admin', is_active_account=True)
                sysadmin.set_password('SysAdminPass123!')
                db.session.add(sysadmin)
                db.session.commit()
            cls.sysadmin_id = sysadmin.id

            # 2. Institution A (Target to delete)
            cls.inst_a_name = f"Alpha College {ts}"
            cls.inst_a_slug = f"alpha-college-{ts}"
            inst_a = Institution(name=cls.inst_a_name, slug=cls.inst_a_slug, status='active')
            db.session.add(inst_a)
            db.session.commit()
            cls.inst_a_id = inst_a.id

            admin_a = User(username=f"admin_a_{ts}", role="admin", institution_id=cls.inst_a_id, is_active_account=True)
            admin_a.set_password("Pass123!")
            db.session.add(admin_a)
            db.session.commit()
            cls.admin_a_id = admin_a.id
            inst_a.owner_admin_id = admin_a.id
            db.session.commit()

            teacher_a = User(username=f"teacher_a_{ts}", role="teacher", institution_id=cls.inst_a_id, is_active_account=True)
            teacher_a.set_password("Pass123!")
            student_a = User(username=f"student_a_{ts}", role="student", institution_id=cls.inst_a_id, is_active_account=True)
            student_a.set_password("Pass123!")
            db.session.add_all([teacher_a, student_a])
            db.session.commit()
            cls.teacher_a_id = teacher_a.id
            cls.student_a_id = student_a.id

            classroom_a = Classroom(name="Computer Science A", teacher_id=teacher_a.id, institution_id=cls.inst_a_id)
            db.session.add(classroom_a)
            db.session.commit()
            cls.classroom_a_id = classroom_a.id

            video_a = Video(
                title=f"Lecture A {ts}", filename=f"video_a_{ts}.mp4", status="completed",
                uploader_id=teacher_a.id, classroom_id=classroom_a.id, institution_id=cls.inst_a_id
            )
            db.session.add(video_a)
            db.session.commit()
            cls.video_a_id = video_a.id

            quiz_a = Quiz(title=f"Quiz A {ts}", teacher_id=teacher_a.id, classroom_id=classroom_a.id, institution_id=cls.inst_a_id)
            db.session.add(quiz_a)
            db.session.commit()
            cls.quiz_a_id = quiz_a.id

            question_a = Question(quiz_id=quiz_a.id, text="Q1?", option_a="A", option_b="B", option_c="C", option_d="D", correct_option="A", institution_id=cls.inst_a_id)
            db.session.add(question_a)
            db.session.commit()

            ebook_a = EBook(title=f"Algorithms A {ts}", subject="CS", file_path=f"uploads/institutions/{cls.inst_a_slug}/ebooks/algo.pdf", file_name="algo.pdf", uploader_id=teacher_a.id, institution_id=cls.inst_a_id)
            db.session.add(ebook_a)
            db.session.commit()
            cls.ebook_a_id = ebook_a.id

            # Create physical directory & dummy asset files for Institution A
            cls.storage_dir_a = os.path.join(BASE_DIR, 'static', 'uploads', 'institutions', cls.inst_a_slug)
            os.makedirs(os.path.join(cls.storage_dir_a, 'hls', str(video_a.id)), exist_ok=True)
            os.makedirs(os.path.join(cls.storage_dir_a, 'ebooks'), exist_ok=True)

            with open(os.path.join(cls.storage_dir_a, 'hls', str(video_a.id), 'master.m3u8'), 'w') as f:
                f.write('#EXTM3U\n')
            with open(os.path.join(cls.storage_dir_a, 'ebooks', 'algo.pdf'), 'w') as f:
                f.write('%PDF-1.4 dummy ebook content')

            # 3. Institution B (Must remain untouched)
            cls.inst_b_name = f"Beta University {ts}"
            cls.inst_b_slug = f"beta-university-{ts}"
            inst_b = Institution(name=cls.inst_b_name, slug=cls.inst_b_slug, status='active')
            db.session.add(inst_b)
            db.session.commit()
            cls.inst_b_id = inst_b.id

            admin_b = User(username=f"admin_b_{ts}", role="admin", institution_id=cls.inst_b_id, is_active_account=True)
            admin_b.set_password("Pass123!")
            db.session.add(admin_b)
            db.session.commit()
            cls.admin_b_id = admin_b.id

            student_b = User(username=f"student_b_{ts}", role="student", institution_id=cls.inst_b_id, is_active_account=True)
            student_b.set_password("Pass123!")
            db.session.add(student_b)
            db.session.commit()
            cls.student_b_id = student_b.id

            video_b = Video(
                title=f"Lecture B {ts}", filename=f"video_b_{ts}.mp4", status="completed",
                uploader_id=admin_b.id, institution_id=cls.inst_b_id
            )
            db.session.add(video_b)
            db.session.commit()
            cls.video_b_id = video_b.id

            cls.storage_dir_b = os.path.join(BASE_DIR, 'static', 'uploads', 'institutions', cls.inst_b_slug)
            os.makedirs(os.path.join(cls.storage_dir_b, 'hls', str(video_b.id)), exist_ok=True)
            with open(os.path.join(cls.storage_dir_b, 'hls', str(video_b.id), 'master.m3u8'), 'w') as f:
                f.write('#EXTM3U\n')

    def test_01_path_security_validation(self):
        """Test path traversal and forbidden path rejection."""
        base_dir = os.path.abspath(BASE_DIR)
        
        # Safe path
        safe_path = os.path.join(base_dir, 'static', 'uploads', 'institutions', 'my-school')
        is_safe, _ = validate_storage_path_security(safe_path, 'my-school', base_dir)
        self.assertTrue(is_safe)

        # Unsafe traversal path
        unsafe_traversal = os.path.join(base_dir, 'static', 'uploads', 'institutions', '..', '..')
        is_safe, _ = validate_storage_path_security(unsafe_traversal, 'my-school', base_dir)
        self.assertFalse(is_safe)

        # System root path
        is_safe, _ = validate_storage_path_security(os.path.abspath('/'), 'my-school', base_dir)
        self.assertFalse(is_safe)

        # Project root path
        is_safe, _ = validate_storage_path_security(base_dir, 'my-school', base_dir)
        self.assertFalse(is_safe)

    def test_02_unauthenticated_deletion_denied(self):
        """Unauthenticated delete API request must be rejected."""
        res = self.client.delete(f'/api/institutions/{self.inst_a_id}')
        self.assertIn(res.status_code, (401, 302, 403))

    def test_03_unauthorized_user_deletion_denied(self):
        """Student or teacher attempting deletion must be denied (403)."""
        with self.app.app_context():
            student = User.query.get(self.student_a_id)
            res = permanently_delete_institution(self.inst_a_id, actor_user=student, confirm_name=self.inst_a_name)
            self.assertFalse(res['success'])
            self.assertEqual(res['status_code'], 403)

    def test_04_admin_cannot_delete_other_institution(self):
        """Institution Admin B cannot delete Institution A."""
        with self.app.app_context():
            admin_b = User.query.get(self.admin_b_id)
            res = permanently_delete_institution(self.inst_a_id, actor_user=admin_b, confirm_name=self.inst_a_name)
            self.assertFalse(res['success'])
            self.assertEqual(res['status_code'], 403)

    def test_05_default_institution_deletion_blocked(self):
        """System Default Institution (slug='default') cannot be deleted."""
        with self.app.app_context():
            sysadmin = User.query.get(self.sysadmin_id)
            def_inst = Institution.query.filter_by(slug='default').first()
            if not def_inst:
                def_inst = Institution(name='Default Institution', slug='default', status='active')
                db.session.add(def_inst)
                db.session.commit()

            res = permanently_delete_institution(def_inst.id, actor_user=sysadmin, confirm_name='Default Institution')
            self.assertFalse(res['success'])
            self.assertEqual(res['status_code'], 400)
            self.assertIn('Default System Institution cannot be deleted', res['message'])

    def test_06_invalid_confirmation_name_rejected(self):
        """Mismatching confirmation name must return 400 Bad Request."""
        with self.app.app_context():
            sysadmin = User.query.get(self.sysadmin_id)
            res = permanently_delete_institution(self.inst_a_id, actor_user=sysadmin, confirm_name='Wrong Name Inc')
            self.assertFalse(res['success'])
            self.assertEqual(res['status_code'], 400)

    def test_07_successful_institution_a_deletion(self):
        """Authorized deletion of Institution A permanently removes DB records and filesystem files."""
        # Confirm asset file & directory existed before deletion
        self.assertTrue(os.path.exists(self.storage_dir_a))

        with self.app.app_context():
            sysadmin = User.query.get(self.sysadmin_id)
            res = permanently_delete_institution(self.inst_a_id, actor_user=sysadmin, confirm_name=self.inst_a_name)
            self.assertTrue(res['success'])
            self.assertEqual(res['status_code'], 200)

            # DB Verification — Institution A records deleted
            self.assertIsNone(Institution.query.get(self.inst_a_id))
            self.assertEqual(User.query.filter_by(institution_id=self.inst_a_id).count(), 0)
            self.assertEqual(Video.query.filter_by(institution_id=self.inst_a_id).count(), 0)
            self.assertEqual(Classroom.query.filter_by(institution_id=self.inst_a_id).count(), 0)
            self.assertEqual(Quiz.query.filter_by(institution_id=self.inst_a_id).count(), 0)
            self.assertEqual(EBook.query.filter_by(institution_id=self.inst_a_id).count(), 0)

        # Filesystem Verification — Directory deleted
        self.assertFalse(os.path.exists(self.storage_dir_a))

    def test_08_multi_tenant_isolation_institution_b_unaffected(self):
        """Institution B must remain 100% intact after Institution A deletion."""
        with self.app.app_context():
            inst_b = Institution.query.get(self.inst_b_id)
            self.assertIsNotNone(inst_b)
            self.assertEqual(inst_b.name, self.inst_b_name)

            users_b = User.query.filter_by(institution_id=self.inst_b_id).all()
            self.assertGreaterEqual(len(users_b), 2)

            videos_b = Video.query.filter_by(institution_id=self.inst_b_id).all()
            self.assertEqual(len(videos_b), 1)
            self.assertEqual(videos_b[0].title, f"Lecture B {self.ts}")

        # Storage directory B untouched
        self.assertTrue(os.path.exists(self.storage_dir_b))

    def test_09_idempotency_repeated_deletion(self):
        """Repeated delete request for already-deleted institution returns 404."""
        with self.app.app_context():
            sysadmin = User.query.get(self.sysadmin_id)
            res = permanently_delete_institution(self.inst_a_id, actor_user=sysadmin, confirm_name=self.inst_a_name)
            self.assertFalse(res['success'])
            self.assertEqual(res['status_code'], 404)

    @classmethod
    def tearDownClass(cls):
        """Clean up test directory B."""
        if hasattr(cls, 'storage_dir_b') and os.path.exists(cls.storage_dir_b):
            try:
                shutil.rmtree(cls.storage_dir_b)
            except Exception:
                pass


if __name__ == '__main__':
    unittest.main()
