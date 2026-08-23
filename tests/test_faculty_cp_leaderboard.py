import unittest
from app import app as flask_app
from extensions import db
from models import User, Institution

class TestFacultyCPLeaderboardSuite(unittest.TestCase):
    def setUp(self):
        flask_app.config.update({'TESTING': True, 'WTF_CSRF_ENABLED': False})
        self.app_context = flask_app.app_context()
        self.app_context.push()
        self.client = flask_app.test_client()

        inst = Institution.query.filter_by(slug='default').first()
        if not inst:
            inst = Institution(name='Default Test Institution', slug='default')
            db.session.add(inst)
            db.session.commit()
        self.inst_id = inst.id

        self.teacher_1 = User.query.filter_by(username='cp_teacher_1').first()
        if not self.teacher_1:
            self.teacher_1 = User(username='cp_teacher_1', role='teacher', institution_id=self.inst_id, xp=1200)
            self.teacher_1.set_password('pass123')
            db.session.add(self.teacher_1)

        self.teacher_2 = User.query.filter_by(username='cp_teacher_2').first()
        if not self.teacher_2:
            self.teacher_2 = User(username='cp_teacher_2', role='teacher', institution_id=self.inst_id, xp=2200)
            self.teacher_2.set_password('pass123')
            db.session.add(self.teacher_2)

        self.student_1 = User.query.filter_by(username='cp_student_1').first()
        if not self.student_1:
            self.student_1 = User(username='cp_student_1', role='student', institution_id=self.inst_id, xp=400)
            self.student_1.set_password('pass123')
            db.session.add(self.student_1)

        db.session.commit()

        self.teacher_1_id = self.teacher_1.id
        self.teacher_2_id = self.teacher_2.id
        self.student_1_id = self.student_1.id

    def tearDown(self):
        db.session.remove()
        self.app_context.pop()

    def test_01_faculty_badge_milestones(self):
        t1 = User.query.get(self.teacher_1_id)
        t2 = User.query.get(self.teacher_2_id)
        s1 = User.query.get(self.student_1_id)

        # Check CP Faculty badges
        self.assertEqual(t1.faculty_badge['title'], 'Gold Mentor')
        self.assertIn('Master Instructor', t1.faculty_badge['badge'])

        self.assertEqual(t2.faculty_badge['title'], 'Diamond Educator')
        self.assertIn('Distinguished Faculty', t2.faculty_badge['badge'])

        # Students return None for faculty_badge
        self.assertIsNone(s1.faculty_badge)

    def test_02_student_access_denied_to_faculty_arena(self):
        # Login as student
        self.client.post('/login', data={'username': 'cp_student_1', 'password': 'pass123'}, follow_redirects=True)

        # Attempt to access dedicated faculty leaderboard
        res = self.client.get('/teacher/leaderboard', follow_redirects=True)
        self.assertIn(b'Faculty CP Leaderboard is strictly reserved for Teachers and Admins', res.data)

    def test_03_teacher_access_to_faculty_cp_arena(self):
        # Login as teacher
        self.client.post('/login', data={'username': 'cp_teacher_1', 'password': 'pass123'}, follow_redirects=True)

        # Visit /teacher/leaderboard
        res = self.client.get('/teacher/leaderboard', follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Faculty CP Competition Arena', res.data)
        self.assertIn(b'2200 CP', res.data)
        self.assertIn(b'1200 CP', res.data)
        self.assertIn(b'Distinguished Faculty', res.data)

    def test_04_student_leaderboard_contains_only_students(self):
        # Login as student
        self.client.post('/login', data={'username': 'cp_student_1', 'password': 'pass123'}, follow_redirects=True)

        res = self.client.get('/leaderboard', follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'Scholar Academic Leaderboard', res.data)
        self.assertIn(b'Cp Student 1', res.data)

if __name__ == '__main__':
    unittest.main()
