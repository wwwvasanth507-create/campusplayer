import io
import unittest
import os
from app import app
from extensions import db
from models import User, Institution

class ProfilePhotoTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        cls.client = app.test_client()

        with app.app_context():
            inst = Institution.query.filter_by(slug='default').first()
            if not inst:
                inst = Institution(name="Default Inst", slug="default")
                db.session.add(inst)
                db.session.commit()
            cls.inst_id = inst.id

            cls.student = User.query.filter_by(username='pic_student').first()
            if not cls.student:
                cls.student = User(username='pic_student', role='student', institution_id=cls.inst_id)
                cls.student.set_password('pass123')
                db.session.add(cls.student)
            
            cls.teacher = User.query.filter_by(username='pic_teacher').first()
            if not cls.teacher:
                cls.teacher = User(username='pic_teacher', role='teacher', institution_id=cls.inst_id)
                cls.teacher.set_password('pass123')
                db.session.add(cls.teacher)

            cls.admin = User.query.filter_by(username='pic_admin').first()
            if not cls.admin:
                cls.admin = User(username='pic_admin', role='admin', institution_id=cls.inst_id)
                cls.admin.set_password('pass123')
                db.session.add(cls.admin)

            db.session.commit()

    def _login(self, username, password, role):
        self.client.get('/logout', follow_redirects=True)
        return self.client.post('/login', data={
            'username': username,
            'password': password,
            'role': role
        }, follow_redirects=True)

    def test_1_get_avatar_url_helper(self):
        with app.app_context():
            user = User.query.filter_by(username='pic_student').first()
            self.assertIsNone(user.get_avatar_url())
            
            user.avatar_url = '/static/uploads/avatars/avatar_test.png'
            db.session.commit()
            self.assertEqual(user.get_avatar_url(), '/static/uploads/avatars/avatar_test.png')

            user.avatar_url = None
            db.session.commit()

    def test_2_student_profile_photo_upload(self):
        self._login('pic_student', 'pass123', 'student')
        
        data = {
            'avatar_file': (io.BytesIO(b'fake_image_bytes'), 'profile.png')
        }
        resp = self.client.post('/profile', data=data, content_type='multipart/form-data', follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

        with app.app_context():
            db.session.expire_all()
            user = User.query.filter_by(username='pic_student').first()
            self.assertIsNotNone(user.avatar_url)
            self.assertTrue(user.avatar_url.startswith('/static/uploads/avatars/avatar_'))

    def test_3_teacher_profile_photo_removal(self):
        with app.app_context():
            db.session.expunge_all()
            t = User.query.filter_by(username='pic_teacher').first()
            t.avatar_url = '/static/uploads/avatars/avatar_teacher.png'
            db.session.commit()
            teacher_id = t.id

        self._login('pic_teacher', 'pass123', 'teacher')

        data = {
            'display_name': 'Pic Teacher',
            'remove_avatar': 'true'
        }
        resp = self.client.post('/profile', data=data, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

        with app.app_context():
            db.session.remove()
            t = User.query.get(teacher_id)
            self.assertIsNone(t.avatar_url)

    def test_4_invalid_image_extension_rejection(self):
        self._login('pic_admin', 'pass123', 'admin')
        data = {
            'avatar_file': (io.BytesIO(b'malicious_script'), 'script.sh')
        }
        resp = self.client.post('/profile', data=data, content_type='multipart/form-data', follow_redirects=True)
        self.assertIn(b'Invalid image format', resp.data)

        with app.app_context():
            db.session.expire_all()
            admin = User.query.filter_by(username='pic_admin').first()
            self.assertIsNone(admin.avatar_url)

if __name__ == '__main__':
    unittest.main()

if __name__ == '__main__':
    unittest.main()
