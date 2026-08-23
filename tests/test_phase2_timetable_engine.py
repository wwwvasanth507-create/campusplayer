import unittest
from app import app as flask_app
from extensions import db
from models import User, Institution, Department, Subject, Classroom, TimetableSlot

class TestPhase2TimetableEngineSuite(unittest.TestCase):
    def setUp(self):
        flask_app.config.update({'TESTING': True, 'WTF_CSRF_ENABLED': False})
        self.app_context = flask_app.app_context()
        self.app_context.push()
        self.client = flask_app.test_client()

        # Create Sysadmin & College Admin
        sysadmin = User.query.filter_by(username='sysadmin_p2').first()
        if not sysadmin:
            sysadmin = User(username='sysadmin_p2', role='system_admin')
            sysadmin.set_password('pass123')
            db.session.add(sysadmin)
            db.session.commit()

        self.client.post('/login', data={'username': 'sysadmin_p2', 'password': 'pass123'}, follow_redirects=True)
        self.client.post('/sysadmin/institutions/create', data={
            'institution_name': 'MIT Engineering Institute',
            'admin_username': 'mit_admin_p2',
            'admin_password': 'pass123',
            'institution_type': 'college'
        }, follow_redirects=True)

    def tearDown(self):
        db.session.remove()
        self.app_context.pop()

    def test_01_classroom_creation_and_timetable_cloning(self):
        self.client.post('/login', data={'username': 'mit_admin_p2', 'password': 'pass123'}, follow_redirects=True)
        inst = Institution.query.filter_by(name='MIT Engineering Institute').first()
        self.assertIsNotNone(inst)

        # 1. Create Department
        res_dept = self.client.post('/admin/departments/create', data={
            'name': 'Department of Computer Science',
            'code': 'CSE'
        }, follow_redirects=True)
        self.assertEqual(res_dept.status_code, 200)

        dept = Department.query.filter_by(code='CSE').first()
        self.assertIsNotNone(dept)

        # 2. Add Faculty
        self.client.post('/admin/add_teacher', data={
            'username': 'prof_sarah_p2',
            'password': 'pass123',
            'display_name': 'Prof. Sarah',
            'department_id': dept.id
        }, follow_redirects=True)

        teacher = User.query.filter_by(username='prof_sarah_p2').first()
        self.assertIsNotNone(teacher)

        # 3. Create Classroom Section A
        res_class_a = self.client.post('/admin/classrooms/create', data={
            'name': 'II - B.Tech CSE - Section A',
            'department_id': dept.id,
            'year_grade': '2nd Year',
            'section': 'A',
            'home_room_number': 'Room 301',
            'teacher_id': teacher.id
        }, follow_redirects=True)
        self.assertEqual(res_class_a.status_code, 200)

        class_a = Classroom.query.filter_by(name='II - B.Tech CSE - Section A').first()
        self.assertIsNotNone(class_a)
        self.assertEqual(class_a.home_room_number, 'Room 301')

        # 4. Create Classroom Section B
        res_class_b = self.client.post('/admin/classrooms/create', data={
            'name': 'II - B.Tech CSE - Section B',
            'department_id': dept.id,
            'year_grade': '2nd Year',
            'section': 'B',
            'home_room_number': 'Room 302',
            'teacher_id': teacher.id
        }, follow_redirects=True)
        self.assertEqual(res_class_b.status_code, 200)

        class_b = Classroom.query.filter_by(name='II - B.Tech CSE - Section B').first()
        self.assertIsNotNone(class_b)

        # 5. Add Period Slots to Section A
        res_slot = self.client.post('/timetable/slot/create', data={
            'classroom_id': class_a.id,
            'day_of_week': 'Monday',
            'period_number': 1,
            'start_time': '09:00',
            'end_time': '09:45',
            'subject_name': 'Operating Systems',
            'teacher_id': teacher.id,
            'room_number': 'Room 301'
        }, follow_redirects=True)
        self.assertEqual(res_slot.status_code, 200)

        slot_a = TimetableSlot.query.filter_by(classroom_id=class_a.id, period_number=1).first()
        self.assertIsNotNone(slot_a)
        self.assertEqual(slot_a.subject_name, 'Operating Systems')

        # 6. Execute 1-Click Section Timetable Clone from Section A to Section B
        res_clone = self.client.post('/admin/timetable/clone', data={
            'source_class_id': class_a.id,
            'target_class_id': class_b.id
        }, follow_redirects=True)
        self.assertEqual(res_clone.status_code, 200)

        slot_b = TimetableSlot.query.filter_by(classroom_id=class_b.id, period_number=1).first()
        self.assertIsNotNone(slot_b)
        self.assertEqual(slot_b.subject_name, 'Operating Systems')
        self.assertEqual(slot_b.room_number, 'Room 302') # Auto-adapted home room!

if __name__ == '__main__':
    unittest.main()
