"""
CampusPlayer - Multi-Tenant Institution Isolation Verification Suite.

Tests strict data boundaries ensuring users of Institution A cannot access, view, search,
or interact with videos, classrooms, quizzes, e-books, or user records of Institution B.
"""

import os
import sys
import unittest
from datetime import datetime

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, BASE_DIR)

from app import app
from extensions import db
from models import Institution, User, Video, Classroom, Quiz, EBook, Playlist
from services.utils import scope_to_institution, enforce_institution_access


class TestMultiTenantIsolation(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = app
        cls.app.config['TESTING'] = True
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.client = cls.app.test_client()

        with cls.app.app_context():
            db.create_all()

            # Clean up stale test data from previous runs
            Video.query.filter(Video.title.like('IsoVid%')).delete(synchronize_session=False)
            Quiz.query.filter(Quiz.title.like('Secret Quiz%')).delete(synchronize_session=False)
            EBook.query.filter(EBook.title.like('Restricted EBook%')).delete(synchronize_session=False)
            db.session.commit()


            ts = datetime.utcnow().strftime('%Y%m%d%H%M%S')
            cls.username_a = f'iso_usr_a_{ts}'
            cls.username_b = f'iso_usr_b_{ts}'
            cls.username_admin = f'iso_sysadmin_{ts}'
            cls.vtitle_a = f'IsoVidA_{ts}'
            cls.vtitle_b = f'IsoVidB_{ts}'
            cls.qtitle_a = f'Secret Quiz A {ts}'
            cls.qtitle_b = f'Secret Quiz B {ts}'
            cls.etitle_a = f'Restricted EBook A {ts}'
            cls.etitle_b = f'Restricted EBook B {ts}'


            # Unique Institutions
            cls.inst_a = Institution(name=f'Inst A {ts}', slug=f'inst-a-{ts}')
            cls.inst_b = Institution(name=f'Inst B {ts}', slug=f'inst-b-{ts}')
            db.session.add(cls.inst_a)
            db.session.add(cls.inst_b)
            db.session.commit()

            cls.inst_a_id = cls.inst_a.id
            cls.inst_b_id = cls.inst_b.id

            # Users
            cls.user_a = User(username=cls.username_a, role='student', institution_id=cls.inst_a_id, is_active_account=True)
            cls.user_a.set_password('StudentPass123!')
            db.session.add(cls.user_a)

            cls.user_b = User(username=cls.username_b, role='student', institution_id=cls.inst_b_id, is_active_account=True)
            cls.user_b.set_password('StudentPass123!')
            db.session.add(cls.user_b)

            cls.sysadmin = User(username=cls.username_admin, role='system_admin', is_active_account=True)
            cls.sysadmin.set_password('AdminPass123!')
            db.session.add(cls.sysadmin)

            db.session.commit()

            cls.user_a_id = cls.user_a.id
            cls.user_b_id = cls.user_b.id
            cls.sysadmin_id = cls.sysadmin.id

            # Isolated Video A & Video B
            cls.video_a = Video(title=cls.vtitle_a, filename=f'test_a_{ts}.mp4', status='completed', uploader_id=cls.user_a_id, institution_id=cls.inst_a_id)
            cls.video_b = Video(title=cls.vtitle_b, filename=f'test_b_{ts}.mp4', status='completed', uploader_id=cls.user_b_id, institution_id=cls.inst_b_id)
            db.session.add(cls.video_a)
            db.session.add(cls.video_b)

            # Isolated Quiz A & Quiz B
            cls.quiz_a = Quiz(title=cls.qtitle_a, teacher_id=cls.user_a_id, institution_id=cls.inst_a_id)
            cls.quiz_b = Quiz(title=cls.qtitle_b, teacher_id=cls.user_b_id, institution_id=cls.inst_b_id)
            db.session.add(cls.quiz_a)
            db.session.add(cls.quiz_b)

            # Isolated EBook A & EBook B
            cls.ebook_a = EBook(title=cls.etitle_a, subject='General', file_path=f'static/uploads/a_{ts}.pdf', file_name=f'a_{ts}.pdf', uploader_id=cls.user_a_id, institution_id=cls.inst_a_id)
            cls.ebook_b = EBook(title=cls.etitle_b, subject='General', file_path=f'static/uploads/b_{ts}.pdf', file_name=f'b_{ts}.pdf', uploader_id=cls.user_b_id, institution_id=cls.inst_b_id)
            db.session.add(cls.ebook_a)
            db.session.add(cls.ebook_b)

            db.session.commit()

            cls.video_a_id = cls.video_a.id
            cls.video_b_id = cls.video_b.id
            cls.quiz_a_id = cls.quiz_a.id
            cls.quiz_b_id = cls.quiz_b.id
            cls.ebook_a_id = cls.ebook_a.id
            cls.ebook_b_id = cls.ebook_b.id

    def test_01_cross_institution_video_access_denied(self):
        """TEST 1: User A cannot watch Video B (Institution B). Expect 403."""
        client_a = self.app.test_client()
        client_a.post('/login', data={'username': self.username_a, 'password': 'StudentPass123!'}, follow_redirects=True)

        res = client_a.get(f'/watch/{self.video_b_id}')
        self.assertEqual(res.status_code, 403)

    def test_02_same_institution_video_access_allowed(self):
        """TEST 2: User A can watch Video A (Institution A). Expect 200."""
        client_a = self.app.test_client()
        client_a.post('/login', data={'username': self.username_a, 'password': 'StudentPass123!'}, follow_redirects=True)

        res = client_a.get(f'/watch/{self.video_a_id}')
        self.assertEqual(res.status_code, 200)

    def test_03_search_query_isolation(self):
        """TEST 3: User A searching for 'IsoVid' gets 0 items from Inst B."""
        client_a = self.app.test_client()
        client_a.post('/login', data={'username': self.username_a, 'password': 'StudentPass123!'}, follow_redirects=True)

        res = client_a.get('/api/search/suggest?q=IsoVid')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        titles = [s['text'] for s in data.get('suggestions', [])]
        self.assertIn(self.vtitle_a, titles)
        self.assertNotIn(self.vtitle_b, titles)




    def test_04_cross_institution_quiz_access_denied(self):
        """TEST 4: Student A attempting Quiz B (Institution B) gets 403 Forbidden."""
        client_a = self.app.test_client()
        client_a.post('/login', data={'username': self.username_a, 'password': 'StudentPass123!'}, follow_redirects=True)

        res = client_a.get(f'/student/quiz/{self.quiz_b_id}')
        self.assertEqual(res.status_code, 403)

    def test_05_cross_institution_ebook_access_denied(self):
        """TEST 5: User A opening EBook B (Institution B) gets 403 Forbidden."""
        client_a = self.app.test_client()
        client_a.post('/login', data={'username': self.username_a, 'password': 'StudentPass123!'}, follow_redirects=True)

        res = client_a.get(f'/library/book/{self.ebook_b_id}/read')
        self.assertEqual(res.status_code, 403)

    def test_06_system_admin_cross_institution_bypass(self):
        """TEST 6: System Admin can view resources across all institutions."""
        client_admin = self.app.test_client()
        client_admin.post('/login', data={'username': self.username_admin, 'password': 'AdminPass123!', 'role': 'system_admin'}, follow_redirects=True)

        res_a = client_admin.get(f'/watch/{self.video_a_id}')
        self.assertEqual(res_a.status_code, 200)

        res_b = client_admin.get(f'/watch/{self.video_b_id}')
        self.assertEqual(res_b.status_code, 200)


if __name__ == '__main__':
    unittest.main()
