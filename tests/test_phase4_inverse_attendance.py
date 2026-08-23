import unittest
from datetime import datetime
from app import app as flask_app
from extensions import db, limiter
from models import User, Institution, Classroom, Attendance, DutyLeaveRequest

class TestPhase4InverseAttendanceSuite(unittest.TestCase):
    def setUp(self):
        flask_app.config.update({'TESTING': True, 'WTF_CSRF_ENABLED': False, 'RATELIMIT_ENABLED': False})
        limiter.enabled = False
        self.app_context = flask_app.app_context()
        self.app_context.push()
        self.client = flask_app.test_client()

        # Create Sysadmin & College Admin
        sysadmin = User.query.filter_by(username='sysadmin_p4_final').first()
        if not sysadmin:
            sysadmin = User(username='sysadmin_p4_final', role='system_admin')
            sysadmin.set_password('pass123')
            db.session.add(sysadmin)
            db.session.commit()

        self.client.post('/login', data={'username': 'sysadmin_p4_final', 'password': 'pass123'}, follow_redirects=True)
        self.client.post('/sysadmin/institutions/create', data={
            'institution_name': 'IIT Tech Institute Final',
            'admin_username': 'iit_admin_p4_final',
            'admin_password': 'pass123',
            'institution_type': 'college'
        }, follow_redirects=True)

    def tearDown(self):
        db.session.remove()
        self.app_context.pop()

    def test_01_inverse_attendance_and_od_ml_approval_flow(self):
        inst = Institution.query.filter_by(name='IIT Tech Institute Final').first()
        self.assertIsNotNone(inst)

        # 1. Login as Admin & Create Teacher, Classroom, Students
        self.client.post('/login', data={'username': 'iit_admin_p4_final', 'password': 'pass123'}, follow_redirects=True)
        self.client.post('/admin/add_teacher', data={
            'username': 'prof_david_p4',
            'password': 'pass123',
            'display_name': 'Prof. David'
        }, follow_redirects=True)

        teacher = User.query.filter_by(username='prof_david_p4').first()
        self.assertIsNotNone(teacher)

        # Create Classroom
        self.client.post('/admin/classrooms/create', data={
            'name': 'III - B.Tech CSE - Section A',
            'year_grade': '3rd Year',
            'section': 'A',
            'home_room_number': 'Room 501',
            'teacher_id': teacher.id
        }, follow_redirects=True)

        classroom = Classroom.query.filter_by(name='III - B.Tech CSE - Section A').first()
        self.assertIsNotNone(classroom)

        # Create 2 Students & Enroll in Classroom
        st1 = User.query.filter_by(username='st_alex_p4_final', institution_id=inst.id).first()
        if not st1:
            st1 = User(username='st_alex_p4_final', display_name='Alex', role='student', institution_id=inst.id)
            st1.set_password('pass123')
            db.session.add(st1)

        st2 = User.query.filter_by(username='st_bob_p4_final', institution_id=inst.id).first()
        if not st2:
            st2 = User(username='st_bob_p4_final', display_name='Bob', role='student', institution_id=inst.id)
            st2.set_password('pass123')
            db.session.add(st2)
        
        st1.photo_approved = True
        st1.avatar_url = '/static/uploads/avatars/alex.jpg'
        st2.photo_approved = True
        st2.avatar_url = '/static/uploads/avatars/bob.jpg'

        if st1 not in classroom.students:
            classroom.students.append(st1)
        if st2 not in classroom.students:
            classroom.students.append(st2)
        db.session.commit()

        # 2. Teacher takes 4-Second Inverse Attendance (Marks st1 Present, st2 Absent)
        self.client.get('/logout', follow_redirects=True)
        self.client.post('/login', data={'username': 'prof_david_p4', 'password': 'pass123'}, follow_redirects=True)

        today_str = datetime.utcnow().strftime('%Y-%m-%d')
        res_save = self.client.post(f'/teacher/classroom/{classroom.id}/attendance/save', data={
            'date': today_str,
            'period_number': 1,
            f'status_{st1.id}': 'Present',
            f'status_{st2.id}': 'Absent'
        }, follow_redirects=True)

        self.assertEqual(res_save.status_code, 200)

        # Verify Attendance Database Records
        att_st1 = Attendance.query.filter_by(classroom_id=classroom.id, student_id=st1.id, period_number=1).first()
        att_st2 = Attendance.query.filter_by(classroom_id=classroom.id, student_id=st2.id, period_number=1).first()

        self.assertIsNotNone(att_st1)
        self.assertEqual(att_st1.status, 'Present')
        self.assertIsNotNone(att_st2)
        self.assertEqual(att_st2.status, 'Absent')

        # 3. Student Bob submits OD Request
        self.client.get('/logout', follow_redirects=True)
        login_res = self.client.post('/login', data={'username': 'st_bob_p4_final', 'password': 'pass123'}, follow_redirects=True)
        
        res_leave = self.client.post('/student/leave_request/submit', data={
            'leave_type': 'od',
            'date': today_str,
            'reason': 'Representing college in Inter-State Robotics Competition'
        }, follow_redirects=True)

        leave_req = DutyLeaveRequest.query.filter_by(student_id=st2.id).order_by(DutyLeaveRequest.id.desc()).first()
        self.assertIsNotNone(leave_req)
        self.assertEqual(leave_req.status, 'pending')

        # 4. Teacher Approves OD Request
        self.client.get('/logout', follow_redirects=True)
        self.client.post('/login', data={'username': 'prof_david_p4', 'password': 'pass123'}, follow_redirects=True)

        res_appr = self.client.post(f'/teacher/od_ml/{leave_req.id}/approve', data={
            'action': 'approve'
        }, follow_redirects=True)
        self.assertEqual(res_appr.status_code, 200)

        db.session.refresh(leave_req)
        self.assertEqual(leave_req.status, 'approved')

        # 5. Teacher opens Attendance Page again — Bob should be pre-set to OD
        res_att_page = self.client.get(f'/teacher/classroom/{classroom.id}/take_attendance?date={today_str}&period=2')
        self.assertEqual(res_att_page.status_code, 200)
        self.assertIn('OD', res_att_page.get_data(as_text=True))

if __name__ == '__main__':
    unittest.main()
