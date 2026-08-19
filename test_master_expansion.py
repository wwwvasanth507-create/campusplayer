import unittest
from datetime import datetime, date, timedelta
from app import app
from extensions import db
from models import (
    User, Institution, Classroom, Video, VideoCheckpoint,
    CheckpointResponse, VideoDoubt, VideoDoubtReply, VideoFlashcard,
    AcademicCertificate, ParentAccessToken
)
from services.certificate_engine import issue_academic_certificate, build_certificate_pdf


class TestMasterExpansionFeatures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        cls.client = app.test_client()

        with app.app_context():
            inst = Institution.query.filter_by(slug='default').first()
            if not inst:
                inst = Institution(name="Default Institution", slug="default")
                db.session.add(inst)
                db.session.commit()

            teacher = User.query.filter_by(username='test_teacher_exp').first()
            if not teacher:
                teacher = User(username='test_teacher_exp', role='teacher', institution_id=inst.id)
                teacher.set_password('teacher123')
                db.session.add(teacher)

            admin = User.query.filter_by(username='test_admin_exp').first()
            if not admin:
                admin = User(username='test_admin_exp', role='admin', institution_id=inst.id)
                admin.set_password('admin123')
                db.session.add(admin)

            student = User.query.filter_by(username='test_student_exp').first()
            if not student:
                student = User(username='test_student_exp', role='student', institution_id=inst.id, xp=200)
                student.set_password('student123')
                db.session.add(student)

            db.session.commit()

            # Ensure Classroom exists
            classroom = Classroom.query.filter_by(name='Expansion Test Class').first()
            if not classroom:
                classroom = Classroom(name='Expansion Test Class', teacher_id=teacher.id, institution_id=inst.id)
                db.session.add(classroom)
                db.session.commit()

            if student not in classroom.students:
                classroom.students.append(student)
                db.session.commit()

            # Ensure Video exists
            video = Video.query.filter_by(title='Expansion Lecture Video').first()
            if not video:
                video = Video(
                    title='Expansion Lecture Video',
                    filename='expansion_test_video.mp4',
                    description='Comprehensive lecture testing expansion features.',
                    uploader_id=teacher.id,
                    classroom_id=classroom.id,
                    institution_id=inst.id,
                    status='completed',
                    duration_seconds=300
                )
                db.session.add(video)
                db.session.commit()

            cls.inst_id = inst.id
            cls.teacher_id = teacher.id
            cls.admin_id = admin.id
            cls.student_id = student.id
            cls.classroom_id = classroom.id
            cls.video_id = video.id

    def login_as(self, username, password, role):
        self.client.get('/logout', follow_redirects=True)
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['csrf_token'] = 'test_token_exp_123'
        return self.client.post('/login', data={
            'username': username,
            'password': password,
            'role': role,
            'csrf_token': 'test_token_exp_123'
        }, follow_redirects=True)

    def test_01_in_video_checkpoints(self):
        # 1. Teacher adds checkpoint
        self.login_as('test_teacher_exp', 'teacher123', 'teacher')
        res = self.client.post(f'/api/video/{self.video_id}/checkpoints/add', json={
            'timestamp_seconds': 45.0,
            'question_text': 'What is the speed of light in vacuum?',
            'option_a': '3 x 10^8 m/s',
            'option_b': '3 x 10^6 m/s',
            'option_c': '1.5 x 10^8 m/s',
            'option_d': 'Zero',
            'correct_option': 'a',
            'xp_reward': 25
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data['success'])
        cp_id = data['checkpoint_id']

        # 2. Student retrieves checkpoints
        self.login_as('test_student_exp', 'student123', 'student')
        res = self.client.get(f'/api/video/{self.video_id}/checkpoints')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data['success'])
        self.assertGreaterEqual(len(data['checkpoints']), 1)

        # 3. Student submits correct answer
        res = self.client.post(f'/api/video/checkpoint/{cp_id}/submit', json={
            'selected_option': 'a'
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data['is_correct'])
        self.assertEqual(data['xp_awarded'], 25)

    def test_02_video_doubts_qa(self):
        # 1. Student posts doubt at 01:15
        self.login_as('test_student_exp', 'student123', 'student')
        res = self.client.post(f'/api/video/{self.video_id}/doubts/add', json={
            'timestamp_seconds': 75.0,
            'question_text': 'Why does the wavelength shift at 01:15?'
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data['success'])
        doubt_id = data['doubt_id']

        # 2. Teacher replies
        self.login_as('test_teacher_exp', 'teacher123', 'teacher')
        res = self.client.post(f'/api/video/doubts/{doubt_id}/reply', json={
            'content': 'Because of the Doppler effect relative to the observer.'
        })
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()['success'])

        # 3. Teacher marks resolved
        res = self.client.post(f'/api/video/doubts/{doubt_id}/toggle_resolve')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()['is_resolved'])

        # 4. Fetch doubts list
        res = self.client.get(f'/api/video/{self.video_id}/doubts')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data['success'])
        self.assertGreaterEqual(len(data['doubts']), 1)

    def test_03_ai_study_kit_and_flashcards(self):
        # 1. Generate study kit
        self.login_as('test_student_exp', 'student123', 'student')
        res = self.client.post(f'/api/video/{self.video_id}/generate_ai_study_kit')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data['success'])
        self.assertGreaterEqual(len(data['flashcards']), 1)

        # 2. View 3D interactive flashcards
        res = self.client.get(f'/video/{self.video_id}/flashcards')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'AI Study Flashcards', res.data)

        # 3. Teacher exports to Quiz
        self.login_as('test_teacher_exp', 'teacher123', 'teacher')
        res = self.client.post(f'/api/video/{self.video_id}/save_ai_quiz', json={
            'title': 'Expansion Lecture Assessment',
            'classroom_id': self.classroom_id
        })
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()['success'])

    def test_04_academic_certificates(self):
        # 1. Teacher issues certificate
        self.login_as('test_teacher_exp', 'teacher123', 'teacher')
        res = self.client.post('/teacher/issue_certificate', data={
            'student_id': self.student_id,
            'title': 'Mastery of Quantum Mechanics',
            'description': 'Exemplary completion and problem solving with honors.',
            'certificate_type': 'course_completion'
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        with app.app_context():
            cert = AcademicCertificate.query.filter_by(student_id=self.student_id).first()
            self.assertIsNotNone(cert)
            cert_code = cert.certificate_code

        # 2. Student attempts to view certificates hub (now disabled on backend)
        self.login_as('test_student_exp', 'student123', 'student')
        res = self.client.get('/student/certificates', follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Certificates option is disabled for students.', res.data)

        # 3. Public QR Verification endpoint (no auth needed)
        self.client.get('/logout', follow_redirects=True)
        res = self.client.get(f'/certificates/verify/{cert_code}')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Officially Verified Academic Credential', res.data)

        # 4. Download ReportLab PDF (blocked for students, available for teachers)
        self.login_as('test_student_exp', 'student123', 'student')
        res = self.client.get(f'/certificates/download/{cert_code}', follow_redirects=True)
        self.assertIn(b'Access denied. Certificate download is disabled for students.', res.data)

        self.login_as('test_teacher_exp', 'teacher123', 'teacher')
        res = self.client.get(f'/certificates/download/{cert_code}')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content_type, 'application/pdf')
        self.assertTrue(res.data.startswith(b'%PDF-'))

    def test_05_tokenized_parent_portal(self):
        # 1. Teacher generates tokenized link
        self.login_as('test_teacher_exp', 'teacher123', 'teacher')
        res = self.client.post(f'/teacher/parent_token/{self.student_id}')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data['success'])
        token = data['token']

        # 2. Parent opens public view without login
        self.client.get('/logout', follow_redirects=True)
        res = self.client.get(f'/parent/view/{token}')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Parent & Guardian Progress Digest', res.data)
        self.assertIn(b'test_student_exp', res.data)


if __name__ == '__main__':
    unittest.main()
