import os
import sys
os.environ['FLASK_TESTING'] = '1'
import unittest
from flask import json, g

sys.path.insert(0, os.path.abspath('.'))

from app import app
from extensions import db
from models import (
    Institution, User, Classroom, Video, Playlist, EBook, Quiz, Question,
    QuizResult, Attendance, ViewAnalytics, SiteSettings, ActivityLog
)
from services.institution_service import permanently_delete_institution

class MultiTenantIsolationTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        app.config['SERVER_NAME'] = 'localhost'
        cls.client = app.test_client()

        with app.app_context():
            g.ignore_tenant_filter = True
            db.session.rollback()
            db.session.remove()
            db.engine.dispose()
            db.create_all()

            import time
            ts = str(int(time.time()))
            cls.slug_a = f'alpha_u_{ts}'
            cls.slug_b = f'beta_i_{ts}'

            # Seed test institutions
            cls.inst_a = Institution(name=f'Alpha University {ts}', slug=cls.slug_a, status='active')
            db.session.add(cls.inst_a)
            cls.inst_b = Institution(name=f'Beta Institute {ts}', slug=cls.slug_b, status='active')
            db.session.add(cls.inst_b)
            db.session.commit()

            cls.inst_a_id = cls.inst_a.id
            cls.inst_b_id = cls.inst_b.id

            # Seed Inst A Users & Data
            cls.admin_a = User(username='alpha_admin', role='admin', institution_id=cls.inst_a_id)
            cls.admin_a.set_password('Pass123!')
            db.session.add(cls.admin_a)

            cls.teacher_a_list = []
            for i in range(1, 4):
                t = User(username=f'alpha_teacher_{i}', role='teacher', institution_id=cls.inst_a_id)
                t.set_password('Pass123!')
                db.session.add(t)
                cls.teacher_a_list.append(t)

            cls.student_a_list = []
            for i in range(1, 6):
                s = User(username=f'alpha_student_{i}', role='student', institution_id=cls.inst_a_id, xp=100*i)
                s.set_password('Pass123!')
                db.session.add(s)
                cls.student_a_list.append(s)

            db.session.commit()

            # Seed Inst A Classes, Videos, EBooks, Quizzes
            cls.class_a_list = []
            for i in range(1, 3):
                c = Classroom(name=f'Alpha Class {i}', teacher_id=cls.teacher_a_list[0].id, institution_id=cls.inst_a_id)
                db.session.add(c)
                cls.class_a_list.append(c)

            cls.video_a_list = []
            for i in range(1, 6):
                v = Video(
                    title=f'Alpha Video {i}',
                    filename=f'alpha_video_{i}.mp4',
                    uploader_id=cls.teacher_a_list[0].id,
                    institution_id=cls.inst_a_id,
                    status='completed',
                    hls_playlist_path=f'uploads/institutions/alpha_u/hls/{i}/master.m3u8'
                )
                db.session.add(v)
                cls.video_a_list.append(v)

            cls.ebook_a_list = []
            for i in range(1, 4):
                eb = EBook(
                    title=f'Alpha PDF {i}',
                    institution_id=cls.inst_a_id,
                    uploader_id=cls.teacher_a_list[0].id,
                    subject='Computer Science',
                    file_path=f'uploads/institutions/alpha_u/pdfs/alpha_{i}.pdf',
                    file_name=f'alpha_{i}.pdf',
                    page_count=50
                )
                db.session.add(eb)
                cls.ebook_a_list.append(eb)

            cls.quiz_a_list = []
            for i in range(1, 3):
                q = Quiz(
                    title=f'Alpha Quiz {i}',
                    institution_id=cls.inst_a_id,
                    teacher_id=cls.teacher_a_list[0].id
                )
                db.session.add(q)
                cls.quiz_a_list.append(q)

            db.session.commit()

            # Seed Inst B Users & Data
            cls.admin_b = User(username='beta_admin', role='admin', institution_id=cls.inst_b_id)
            cls.admin_b.set_password('Pass123!')
            db.session.add(cls.admin_b)

            cls.teacher_b_list = []
            for i in range(1, 3):
                t = User(username=f'beta_teacher_{i}', role='teacher', institution_id=cls.inst_b_id)
                t.set_password('Pass123!')
                db.session.add(t)
                cls.teacher_b_list.append(t)

            cls.student_b_list = []
            for i in range(1, 4):
                s = User(username=f'beta_student_{i}', role='student', institution_id=cls.inst_b_id, xp=50*i)
                s.set_password('Pass123!')
                db.session.add(s)
                cls.student_b_list.append(s)

            db.session.commit()

            cls.class_b_list = []
            for i in range(1, 2):
                c = Classroom(name=f'Beta Class {i}', teacher_id=cls.teacher_b_list[0].id, institution_id=cls.inst_b_id)
                db.session.add(c)
                cls.class_b_list.append(c)

            cls.video_b_list = []
            for i in range(1, 3):
                v = Video(
                    title=f'Beta Video {i}',
                    filename=f'beta_video_{i}.mp4',
                    uploader_id=cls.teacher_b_list[0].id,
                    institution_id=cls.inst_b_id,
                    status='completed',
                    hls_playlist_path=f'uploads/institutions/beta_i/hls/{i}/master.m3u8'
                )
                db.session.add(v)
                cls.video_b_list.append(v)

            cls.ebook_b_list = []
            for i in range(1, 2):
                eb = EBook(
                    title=f'Beta PDF {i}',
                    institution_id=cls.inst_b_id,
                    uploader_id=cls.teacher_b_list[0].id,
                    subject='Physics',
                    file_path=f'uploads/institutions/beta_i/pdfs/beta_{i}.pdf',
                    file_name=f'beta_{i}.pdf',
                    page_count=30
                )
                db.session.add(eb)
                cls.ebook_b_list.append(eb)

            cls.quiz_b_list = []
            for i in range(1, 2):
                q = Quiz(
                    title=f'Beta Quiz {i}',
                    institution_id=cls.inst_b_id,
                    teacher_id=cls.teacher_b_list[0].id
                )
                db.session.add(q)
                cls.quiz_b_list.append(q)

            db.session.commit()

            # Store key entity IDs for cross-tenant IDOR tests
            cls.video_b_id = cls.video_b_list[0].id
            cls.ebook_b_id = cls.ebook_b_list[0].id
            cls.quiz_b_id = cls.quiz_b_list[0].id
            cls.teacher_b_id = cls.teacher_b_list[0].id

            db.session.remove()

    def setUp(self):
        self.client = app.test_client()
        with app.app_context():
            db.session.remove()

    def tearDown(self):
        with app.app_context():
            db.session.remove()

    def login(self, username, password='Pass123!'):
        self.client.get('/logout')
        return self.client.post('/login', data={'username': username, 'password': password}, follow_redirects=True)

    def test_01_institution_a_admin_dashboard_isolation(self):
        """Institution A admin must see ONLY Inst A counts."""
        res_login = self.login('alpha_admin')
        self.assertEqual(res_login.status_code, 200)
        res = self.client.get('/admin')
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn('alpha_teacher_1', html)
        self.assertNotIn('beta_teacher_1', html)

        # Test /admin/api/stats for Inst A
        api_res = self.client.get('/admin/api/stats')
        self.assertEqual(api_res.status_code, 200)
        data = api_res.get_json()
        self.assertIn('views_today', data)

    def test_02_institution_b_admin_dashboard_isolation(self):
        """Institution B admin must see ONLY Inst B counts."""
        res_login = self.login('beta_admin')
        self.assertEqual(res_login.status_code, 200)
        res = self.client.get('/admin')
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn('beta_teacher_1', html)
        self.assertNotIn('alpha_teacher_1', html)

    def test_03_teacher_dashboard_isolation(self):
        """Alpha teacher sees only Alpha classes; Beta teacher sees only Beta classes."""
        res_login = self.login('alpha_teacher_1')
        self.assertEqual(res_login.status_code, 200)
        res = self.client.get('/teacher/classes')
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn('Alpha Class 1', html)
        self.assertNotIn('Beta Class 1', html)

        res_login = self.login('beta_teacher_1')
        self.assertEqual(res_login.status_code, 200)
        res = self.client.get('/teacher/classes')
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn('Beta Class 1', html)
        self.assertNotIn('Alpha Class 1', html)

    def test_04_search_isolation(self):
        """Search in Inst A must not return Inst B videos, users, or classes."""
        res_login = self.login('alpha_student_1')
        self.assertEqual(res_login.status_code, 200)
        res = self.client.get('/api/search/suggest?q=Beta')
        data = res.get_json()
        self.assertEqual(len(data.get('suggestions', [])), 0)

        res = self.client.get('/api/search/suggest?q=Alpha')
        data = res.get_json()
        self.assertGreater(len(data.get('suggestions', [])), 0)

    def test_05_cross_tenant_idor_video_access(self):
        """Inst A student trying to access Inst B video or HLS stream must be rejected with 403 or 404."""
        res_login = self.login('alpha_student_1')
        self.assertEqual(res_login.status_code, 200)
        res = self.client.get(f'/video/{self.video_b_id}')
        self.assertIn(res.status_code, (403, 404))

        res = self.client.get(f'/hls/{self.video_b_id}/master.m3u8')
        self.assertIn(res.status_code, (403, 404))

    def test_06_cross_tenant_idor_ebook_access(self):
        """Inst A student trying to read or download Inst B ebook must be rejected with 403 or 404."""
        res_login = self.login('alpha_student_1')
        self.assertEqual(res_login.status_code, 200)
        res = self.client.get(f'/library/book/{self.ebook_b_id}/read')
        self.assertIn(res.status_code, (403, 404))

        res = self.client.get(f'/library/book/{self.ebook_b_id}/download')
        self.assertIn(res.status_code, (403, 404))

    def test_07_cross_tenant_idor_quiz_access(self):
        """Inst A student trying to take Inst B quiz must be rejected with 403 or 404."""
        res_login = self.login('alpha_student_1')
        self.assertEqual(res_login.status_code, 200)
        res = self.client.get(f'/student/quiz/{self.quiz_b_id}')
        self.assertIn(res.status_code, (403, 404))

    def test_08_cross_tenant_password_change_attack(self):
        """Inst A admin attempting to change Inst B teacher password must fail and leave password unchanged."""
        res_login = self.login('alpha_admin')
        self.assertEqual(res_login.status_code, 200)
        res = self.client.post('/admin/change_teacher_password', data={
            'user_id': self.teacher_b_id,
            'new_password': 'Hacked123!'
        })
        # Password change attempt for cross-tenant user should fail (not allowed)
        with app.app_context():
            g.ignore_tenant_filter = True
            tb = User.query.get(self.teacher_b_id)
            self.assertTrue(tb.check_password('Pass123!'))
            self.assertFalse(tb.check_password('Hacked123!'))

    def test_09_institution_deletion_isolation(self):
        """Deleting Institution A must leave Institution B 100% intact."""
        with app.app_context():
            db.session.remove()
            res = permanently_delete_institution(self.inst_a_id)
            self.assertTrue(res.get('success'))

            # Verify Inst B data is 100% intact
            self.assertEqual(User.query.filter_by(role='teacher', institution_id=self.inst_b_id).count(), 2)
            self.assertEqual(User.query.filter_by(role='student', institution_id=self.inst_b_id).count(), 3)
            self.assertEqual(Classroom.query.filter_by(institution_id=self.inst_b_id).count(), 1)
            self.assertEqual(Video.query.filter_by(institution_id=self.inst_b_id).count(), 2)
            self.assertEqual(EBook.query.filter_by(institution_id=self.inst_b_id).count(), 1)
            self.assertEqual(Quiz.query.filter_by(institution_id=self.inst_b_id).count(), 1)

    @classmethod
    def tearDownClass(cls):
        with app.app_context():
            db.session.remove()

if __name__ == '__main__':
    unittest.main()
