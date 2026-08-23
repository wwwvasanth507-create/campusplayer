import io
import unittest
from app import app as flask_app
from extensions import db
from models import User, Institution, Classroom

class TestPhase3StudentOnboardingSuite(unittest.TestCase):
    def setUp(self):
        flask_app.config.update({'TESTING': True, 'WTF_CSRF_ENABLED': False})
        self.app_context = flask_app.app_context()
        self.app_context.push()
        self.client = flask_app.test_client()

        # Create Sysadmin & College Admin
        sysadmin = User.query.filter_by(username='sysadmin_p3').first()
        if not sysadmin:
            sysadmin = User(username='sysadmin_p3', role='system_admin')
            sysadmin.set_password('pass123')
            db.session.add(sysadmin)
            db.session.commit()

        self.client.post('/login', data={'username': 'sysadmin_p3', 'password': 'pass123'}, follow_redirects=True)
        self.client.post('/sysadmin/institutions/create', data={
            'institution_name': 'Imperial College of Tech',
            'admin_username': 'imperial_admin_p3',
            'admin_password': 'pass123',
            'institution_type': 'college'
        }, follow_redirects=True)

    def tearDown(self):
        db.session.remove()
        self.app_context.pop()

    def test_01_student_photo_gate_and_verification_flow(self):
        inst = Institution.query.filter_by(name='Imperial College of Tech').first()
        
        # 1. Create Teacher & Student Accounts
        self.client.post('/login', data={'username': 'imperial_admin_p3', 'password': 'pass123'}, follow_redirects=True)
        self.client.post('/admin/add_teacher', data={
            'username': 'prof_dr_john_p3',
            'password': 'pass123',
            'display_name': 'Prof. John'
        }, follow_redirects=True)

        teacher = User.query.filter_by(username='prof_dr_john_p3').first()
        self.assertIsNotNone(teacher)

        # Provision Student Account
        student = User.query.filter_by(username='student_sanjay_p3', institution_id=inst.id).first()
        if not student:
            student = User(
                username='student_sanjay_p3',
                role='student',
                display_name='Sanjay Hariharan',
                institution_id=inst.id,
                photo_approved=False
            )
            student.set_password('pass123')
            db.session.add(student)
            db.session.commit()

        # 2. Logout admin and student logs in for the first time
        self.client.get('/logout', follow_redirects=True)
        res_login = self.client.post('/login', data={'username': 'student_sanjay_p3', 'password': 'pass123'}, follow_redirects=True)

        # 3. Student uploads photo at Photo Gate
        from werkzeug.datastructures import FileStorage
        dummy_img = FileStorage(stream=io.BytesIO(b"fake image bytes"), filename="headshot.jpg", content_type="image/jpeg")
        res_upload = self.client.post('/student/photo_gate', data={
            'photo': dummy_img
        }, follow_redirects=True)

        student_id = student.id
        db.session.remove()
        student = User.query.get(student_id)
        self.assertTrue(student.is_photo_verified)
        self.assertTrue(student.photo_approved)
        self.assertIsNotNone(student.avatar_url)

        # 4. Teacher views enrolled student directory and checks verified badge
        self.client.get('/logout', follow_redirects=True)
        self.client.post('/login', data={'username': 'prof_dr_john_p3', 'password': 'pass123'}, follow_redirects=True)
        res_dir = self.client.get('/teacher/enrolled_students')
        self.assertEqual(res_dir.status_code, 200)
        self.assertIn('Logged In', res_dir.get_data(as_text=True))

        # 5. Teacher requests photo re-upload due to quality issue
        res_reject = self.client.post(f'/teacher/student/{student_id}/reject_photo', data={
            'reason': 'Blurry photo, please upload a clear headshot.'
        }, follow_redirects=True)
        self.assertEqual(res_reject.status_code, 200)

        db.session.remove()
        student = User.query.get(student_id)
        self.assertFalse(student.photo_approved)
        self.assertFalse(student.is_photo_verified)
        self.assertEqual(student.photo_rejection_reason, 'Blurry photo, please upload a clear headshot.')

if __name__ == '__main__':
    unittest.main()
