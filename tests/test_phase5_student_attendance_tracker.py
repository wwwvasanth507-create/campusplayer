import unittest
from datetime import datetime
from app import app as flask_app
from extensions import db, limiter
from models import User, Institution, Classroom, Attendance

class TestPhase5StudentAttendanceTrackerSuite(unittest.TestCase):
    def setUp(self):
        flask_app.config.update({'TESTING': True, 'WTF_CSRF_ENABLED': False, 'RATELIMIT_ENABLED': False})
        limiter.enabled = False
        self.app_context = flask_app.app_context()
        self.app_context.push()
        self.client = flask_app.test_client()

        # Create Sysadmin & Setup Institution
        sysadmin = User.query.filter_by(username='sysadmin_p5').first()
        if not sysadmin:
            sysadmin = User(username='sysadmin_p5', role='system_admin')
            sysadmin.set_password('pass123')
            db.session.add(sysadmin)
            db.session.commit()

        self.client.post('/login', data={'username': 'sysadmin_p5', 'password': 'pass123'}, follow_redirects=True)
        self.client.post('/sysadmin/institutions/create', data={
            'institution_name': 'IIT Attendance Test Inst',
            'admin_username': 'admin_p5',
            'admin_password': 'pass123',
            'institution_type': 'college'
        }, follow_redirects=True)

    def tearDown(self):
        db.session.remove()
        self.app_context.pop()

    def test_01_student_attendance_tracker_page_render(self):
        inst = Institution.query.filter_by(name='IIT Attendance Test Inst').first()
        self.assertIsNotNone(inst)

        # Login as Admin & create student and attendance records
        self.client.post('/login', data={'username': 'admin_p5', 'password': 'pass123'}, follow_redirects=True)
        
        # Create student
        student = User.query.filter_by(username='student_att_test').first()
        if not student:
            student = User(username='student_att_test', display_name='Test Student', role='student', institution_id=inst.id)
            db.session.add(student)
        student.role = 'student'
        student.photo_approved = True
        student.avatar_url = 'http://example.com/avatar.jpg'
        student.set_password('pass123')
        db.session.commit()

        # Logout previous session, then login as student & access /student/attendance
        self.client.get('/logout', follow_redirects=True)
        self.client.post('/login', data={'username': 'student_att_test', 'password': 'pass123'}, follow_redirects=True)
        res = self.client.get('/student/attendance')
        self.assertEqual(res.status_code, 200, f"Got status {res.status_code}: {res.data.decode('utf-8')[:300]}")
        self.assertIn(b'Attendance Analytics', res.data)
        self.assertIn(b'Total Conducted', res.data)

if __name__ == '__main__':
    unittest.main()
