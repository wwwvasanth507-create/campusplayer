import unittest
import json
from app import app as flask_app
from extensions import db
from models import User, Institution, Department, Subject

class TestPhase1InstitutionSetupSuite(unittest.TestCase):
    def setUp(self):
        flask_app.config.update({'TESTING': True, 'WTF_CSRF_ENABLED': False})
        self.app_context = flask_app.app_context()
        self.app_context.push()
        self.client = flask_app.test_client()

        # Clean up testing artifacts
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.app_context.pop()

    def test_01_institution_mode_config(self):
        # Create Sysadmin user
        sysadmin = User.query.filter_by(username='sysadmin_phase1').first()
        if not sysadmin:
            sysadmin = User(username='sysadmin_phase1', role='system_admin')
            sysadmin.set_password('pass123')
            db.session.add(sysadmin)
            db.session.commit()

        self.client.post('/login', data={'username': 'sysadmin_phase1', 'password': 'pass123'}, follow_redirects=True)

        # Create School Mode Institution
        res_school = self.client.post('/sysadmin/institutions/create', data={
            'institution_name': 'Test High School',
            'admin_username': 'school_admin_p1',
            'admin_password': 'pass123',
            'institution_type': 'school'
        }, follow_redirects=True)
        self.assertEqual(res_school.status_code, 200)

        inst_school = Institution.query.filter_by(name='Test High School').first()
        self.assertIsNotNone(inst_school)
        self.assertEqual(inst_school.institution_type, 'school')

        # Create College Mode Institution
        res_college = self.client.post('/sysadmin/institutions/create', data={
            'institution_name': 'Test Engineering College',
            'admin_username': 'college_admin_p1',
            'admin_password': 'pass123',
            'institution_type': 'college'
        }, follow_redirects=True)
        self.assertEqual(res_college.status_code, 200)

        inst_college = Institution.query.filter_by(name='Test Engineering College').first()
        self.assertIsNotNone(inst_college)
        self.assertEqual(inst_college.institution_type, 'college')

    def test_02_department_hod_and_subject_registry(self):
        # Create College Admin
        college_admin = User.query.filter_by(username='college_admin_p1').first()
        inst = Institution.query.filter_by(name='Test Engineering College').first()

        self.client.post('/login', data={'username': 'college_admin_p1', 'password': 'pass123'}, follow_redirects=True)

        # 1. Create Department
        res_dept = self.client.post('/admin/departments/create', data={
            'name': 'Department of Artificial Intelligence & Machine Learning',
            'code': 'AI & ML'
        }, follow_redirects=True)
        self.assertEqual(res_dept.status_code, 200)

        dept = Department.query.filter_by(code='AI & ML', institution_id=inst.id).first()
        self.assertIsNotNone(dept)
        self.assertEqual(dept.name, 'Department of Artificial Intelligence & Machine Learning')

        # 2. Add Master Subject under Department
        res_sub = self.client.post('/admin/subjects/create', data={
            'name': 'Deep Learning',
            'code': 'CS301',
            'department_id': dept.id
        }, follow_redirects=True)
        self.assertEqual(res_sub.status_code, 200)

        sub = Subject.query.filter_by(code='CS301', department_id=dept.id).first()
        self.assertIsNotNone(sub)

        # 3. Add Teacher with Department & Subject Specialization
        res_teacher = self.client.post('/admin/add_teacher', data={
            'username': 'dr_smith_p1',
            'password': 'pass123',
            'display_name': 'Dr. Smith',
            'department_id': dept.id,
            'subject_specializations': ['Deep Learning', 'Machine Learning']
        }, follow_redirects=True)
        self.assertEqual(res_teacher.status_code, 200)

        teacher = User.query.filter_by(username='dr_smith_p1').first()
        self.assertIsNotNone(teacher)
        self.assertEqual(teacher.department_id, dept.id)
        self.assertIn('Deep Learning', teacher.subject_specializations)

        # 4. Appoint Teacher as HOD of Department
        res_hod = self.client.post(f'/admin/departments/assign_hod/{dept.id}', data={
            'hod_id': teacher.id
        }, follow_redirects=True)
        self.assertEqual(res_hod.status_code, 200)

        db.session.refresh(dept)
        db.session.refresh(teacher)
        self.assertEqual(dept.hod_id, teacher.id)
        self.assertEqual(teacher.role, 'hod')

if __name__ == '__main__':
    unittest.main()
