import unittest
from datetime import datetime, date, timedelta
from app import app
from extensions import db
from models import User, Institution, Classroom, Quiz, Question, QuizResult, Attendance, ViewAnalytics, ClassWeeklyReport, Notification
from services.report_engine import (
    aggregate_class_weekly_data, generate_or_get_weekly_report,
    build_weekly_report_pdf, get_current_week_bounds
)


class TestWeeklyClassReports(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        cls.client = app.test_client()

        with app.app_context():
            # Setup default institution
            inst = Institution.query.filter_by(slug='default').first()
            if not inst:
                inst = Institution(name="Default Institution", slug="default")
                db.session.add(inst)
                db.session.commit()

            # Ensure teacher and admin exist
            teacher = User.query.filter_by(username='test_teacher_reports').first()
            if not teacher:
                teacher = User(username='test_teacher_reports', role='teacher', institution_id=inst.id)
                teacher.set_password('teacher123')
                db.session.add(teacher)

            admin = User.query.filter_by(username='test_admin_reports').first()
            if not admin:
                admin = User(username='test_admin_reports', role='admin', institution_id=inst.id)
                admin.set_password('admin123')
                db.session.add(admin)

            # Ensure test student exists
            student = User.query.filter_by(username='test_student_reports').first()
            if not student:
                student = User(username='test_student_reports', role='student', institution_id=inst.id, xp=450)
                student.set_password('student123')
                db.session.add(student)

            db.session.commit()

            # Ensure test classroom exists
            cls_obj = Classroom.query.filter_by(name='Weekly Test Class 101').first()
            if not cls_obj:
                cls_obj = Classroom(name='Weekly Test Class 101', teacher_id=teacher.id, institution_id=inst.id)
                db.session.add(cls_obj)
                db.session.commit()

            if student not in cls_obj.students:
                cls_obj.students.append(student)
                db.session.commit()

            # Clean up prior test reports for idempotency
            ClassWeeklyReport.query.filter_by(classroom_id=cls_obj.id).delete()
            db.session.commit()

            cls.teacher_id = teacher.id
            cls.admin_id = admin.id
            cls.student_id = student.id
            cls.classroom_id = cls_obj.id

    def login_as(self, username, password, role):
        self.client.get('/logout', follow_redirects=True)
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['csrf_token'] = 'test_token_123'
        return self.client.post('/login', data={
            'username': username,
            'password': password,
            'role': role,
            'csrf_token': 'test_token_123'
        }, follow_redirects=True)

    def test_01_week_bounds(self):
        m, s = get_current_week_bounds()
        self.assertEqual(m.weekday(), 0)  # Monday
        self.assertEqual(s.weekday(), 6)  # Sunday
        self.assertEqual((s - m).days, 6)

    def test_02_data_aggregation(self):
        with app.app_context():
            m, s = get_current_week_bounds()
            data = aggregate_class_weekly_data(self.classroom_id, m, s)
            self.assertIsNotNone(data)
            self.assertEqual(data['classroom_name'], 'Weekly Test Class 101')
            self.assertGreaterEqual(data['total_students'], 1)
            self.assertIn('students', data)
            self.assertGreaterEqual(len(data['students']), 1)

    def test_03_report_compilation(self):
        with app.app_context():
            m, s = get_current_week_bounds()
            report = generate_or_get_weekly_report(
                self.classroom_id, self.teacher_id, m, s, remarks="Test remarks for weekly digest"
            )
            self.assertIsNotNone(report)
            self.assertEqual(report.classroom_id, self.classroom_id)
            self.assertEqual(report.teacher_remarks, "Test remarks for weekly digest")
            self.assertEqual(report.status, 'generated')

    def test_04_reportlab_pdf_generation(self):
        with app.app_context():
            m, s = get_current_week_bounds()
            report = generate_or_get_weekly_report(self.classroom_id, self.teacher_id, m, s)
            pdf_buf = build_weekly_report_pdf(report)
            self.assertIsNotNone(pdf_buf)
            content = pdf_buf.getvalue()
            self.assertTrue(content.startswith(b'%PDF-'), "Generated buffer must be a valid PDF document")

    def test_05_teacher_routes(self):
        login_res = self.login_as('test_teacher_reports', 'teacher123', 'teacher')
        self.assertEqual(login_res.status_code, 200)

        # 1. View weekly reports list
        res = self.client.get('/teacher/weekly_reports', follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Weekly Classroom Performance Digests', res.data)

        # 2. On-demand report compilation
        res = self.client.post('/teacher/weekly_reports/generate', data={
            'classroom_id': self.classroom_id,
            'remarks': 'On-demand compilation test'
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Weekly Test Class 101', res.data)

        # 3. View detail page
        with app.app_context():
            report = ClassWeeklyReport.query.filter_by(classroom_id=self.classroom_id).first()
            report_id = report.id

        res = self.client.get(f'/teacher/weekly_reports/{report_id}', follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Consolidated Scholar Roster', res.data)

        # 4. Download PDF
        res = self.client.get(f'/teacher/weekly_reports/{report_id}/download_pdf')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content_type, 'application/pdf')
        self.assertTrue(res.data.startswith(b'%PDF-'))

        # 5. Dispatch to Admin / Principal
        res = self.client.post(f'/teacher/weekly_reports/{report_id}/send_to_admin', follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        with app.app_context():
            updated_report = db.session.get(ClassWeeklyReport, report_id)
            self.assertEqual(updated_report.status, 'sent_to_admin')
            self.assertIsNotNone(updated_report.sent_to_admin_at)

    def test_06_admin_routes(self):
        login_res = self.login_as('test_admin_reports', 'admin123', 'admin')
        self.assertEqual(login_res.status_code, 200)

        # 1. Admin reviews class reports list
        res = self.client.get('/admin/class_reports', follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Principal / Admin Class Reports Portal', res.data)

        # 2. Admin submits review feedback
        with app.app_context():
            report = ClassWeeklyReport.query.filter_by(classroom_id=self.classroom_id).first()
            report_id = report.id

        res = self.client.post(f'/admin/class_reports/{report_id}/feedback', data={
            'admin_feedback': 'Commendable student XP and attendance retention.'
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        with app.app_context():
            updated_report = db.session.get(ClassWeeklyReport, report_id)
            self.assertEqual(updated_report.status, 'reviewed')
            self.assertEqual(updated_report.admin_feedback, 'Commendable student XP and attendance retention.')


if __name__ == '__main__':
    unittest.main()
