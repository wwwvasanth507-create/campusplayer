import unittest
from datetime import datetime
from app import app
from extensions import db
from models import User, Institution, Classroom, Quiz, Question, QuizResult


class TestQuizAttemptLimit(unittest.TestCase):
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

            teacher = User.query.filter_by(username='quiz_test_teacher').first()
            if not teacher:
                teacher = User(username='quiz_test_teacher', role='teacher', institution_id=inst.id)
                teacher.set_password('teacher123')
                db.session.add(teacher)

            student = User.query.filter_by(username='quiz_test_student').first()
            if not student:
                student = User(username='quiz_test_student', role='student', institution_id=inst.id, xp=100)
                student.set_password('student123')
                db.session.add(student)

            db.session.commit()

            classroom = Classroom.query.filter_by(name='Quiz Attempt Class 101').first()
            if not classroom:
                classroom = Classroom(name='Quiz Attempt Class 101', teacher_id=teacher.id, institution_id=inst.id)
                db.session.add(classroom)
                db.session.commit()

            if student not in classroom.students:
                classroom.students.append(student)
                db.session.commit()

            quiz = Quiz.query.filter_by(title='Single Attempt Exam').first()
            if not quiz:
                quiz = Quiz(
                    title='Single Attempt Exam',
                    classroom_id=classroom.id,
                    teacher_id=teacher.id,
                    passing_percent=50,
                    institution_id=inst.id
                )
                db.session.add(quiz)
                db.session.commit()

                q1 = Question(quiz_id=quiz.id, text='What is 2+2?', option_a='4', option_b='3', option_c='2', option_d='5', correct_option='A')
                db.session.add(q1)
                db.session.commit()

            # Clean up prior test results
            QuizResult.query.filter_by(quiz_id=quiz.id, student_id=student.id).delete()
            db.session.commit()

            cls.teacher_id = teacher.id
            cls.student_id = student.id
            cls.quiz_id = quiz.id
            cls.q1_id = quiz.questions[0].id

    def login_as_student(self):
        self.client.get('/logout', follow_redirects=True)
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['csrf_token'] = 'test_csrf_token'
        return self.client.post('/login', data={
            'username': 'quiz_test_student',
            'password': 'student123',
            'role': 'student',
            'csrf_token': 'test_csrf_token'
        }, follow_redirects=True)

    def test_single_quiz_attempt_flow(self):
        self.login_as_student()

        # 1. Available quiz list before taking
        res = self.client.get('/student/quizzes')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Begin Quiz', res.data)

        # 2. First GET to enter quiz page
        res = self.client.get(f'/student/quiz/{self.quiz_id}')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Taking Quiz: Single Attempt Exam', res.data)

        # 3. First POST submit answers
        res = self.client.post(f'/student/quiz/{self.quiz_id}', data={
            f'q_{self.q1_id}': 'A',
            'csrf_token': 'test_csrf_token'
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Quiz submitted', res.data)

        # Verify QuizResult created
        with app.app_context():
            q_res = QuizResult.query.filter_by(quiz_id=self.quiz_id, student_id=self.student_id).first()
            self.assertIsNotNone(q_res)
            self.assertEqual(q_res.score, 1)

        # 4. Check available quizzes listing now shows Completed button, NOT Retake Quiz
        res = self.client.get('/student/quizzes')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Completed (1/1 Attempt)', res.data)
        self.assertNotIn(b'Retake Quiz', res.data)

        # 5. Subsequent GET to take_quiz should be blocked and redirected
        res = self.client.get(f'/student/quiz/{self.quiz_id}', follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'You have already completed this quiz. Only one attempt is permitted.', res.data)

        # 6. Subsequent POST to take_quiz should also be blocked and redirected
        res = self.client.post(f'/student/quiz/{self.quiz_id}', data={
            f'q_{self.q1_id}': 'A',
            'csrf_token': 'test_csrf_token'
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'You have already completed this quiz. Only one attempt is permitted.', res.data)


if __name__ == '__main__':
    unittest.main()
