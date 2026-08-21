import unittest
import sys
import os
from datetime import datetime

os.environ['TESTING'] = '1'

from app import app, db
from models import User, Institution, Classroom, TimetableSlot

class TestTimetableFix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        cls.app = app
        cls.ctx = app.app_context()
        cls.ctx.push()

        ts = datetime.utcnow().strftime('%Y%m%d%H%M%S')
        cls.inst = Institution(name=f'TT Inst {ts}', slug=f'tt-inst-{ts}')
        db.session.add(cls.inst)
        db.session.commit()

        cls.student = User(username=f'tt_student_{ts}', role='student', institution_id=cls.inst.id, is_active_account=True)
        cls.student.set_password('Pass123!')
        cls.teacher = User(username=f'tt_teacher_{ts}', role='teacher', institution_id=cls.inst.id, is_active_account=True)
        cls.teacher.set_password('Pass123!')
        cls.admin = User(username=f'tt_admin_{ts}', role='system_admin', institution_id=cls.inst.id, is_active_account=True)
        cls.admin.set_password('Pass123!')

        db.session.add_all([cls.student, cls.teacher, cls.admin])
        db.session.commit()

        cls.classroom = Classroom(name=f'Math 101 {ts}', class_code=f'M{ts[2:]}', institution_id=cls.inst.id, teacher_id=cls.teacher.id)

        db.session.add(cls.classroom)
        db.session.commit()

        # Enroll student
        cls.student.enrolled_classes.append(cls.classroom)
        db.session.commit()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.pop()

    def test_01_student_timetable_hub_access(self):
        """Verify student accessing /timetable receives 200 OK without 500 error."""
        client = self.app.test_client()
        client.post('/login', data={'username': self.student.username, 'password': 'Pass123!'}, follow_redirects=True)

        res = client.get('/timetable')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Math 101', res.data)

    def test_02_teacher_timetable_hub_access(self):
        """Verify teacher accessing /timetable receives 200 OK."""
        client = self.app.test_client()
        client.post('/login', data={'username': self.teacher.username, 'password': 'Pass123!'}, follow_redirects=True)

        res = client.get('/timetable')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Math 101', res.data)

    def test_03_admin_timetable_hub_access(self):
        """Verify admin accessing /timetable receives 200 OK."""
        client = self.app.test_client()
        client.post('/login', data={'username': self.admin.username, 'password': 'Pass123!'}, follow_redirects=True)

        res = client.get('/timetable')
        self.assertEqual(res.status_code, 200)

if __name__ == '__main__':
    unittest.main()
