import unittest
from app import app, db
from models import User, Institution, Video, Classroom, ChatMessage, Comment

class ProfileDisplayNameSecurityTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

        # Setup test data
        inst = Institution.query.filter_by(slug='sec_test_inst').first()
        if not inst:
            inst = Institution(name='Security Test Inst', slug='sec_test_inst')
            db.session.add(inst)
            db.session.commit()
        self.inst = inst

        teacher = User.query.filter_by(username='sec_teacher_user').first()
        if not teacher:
            teacher = User(
                username='sec_teacher_user',
                role='teacher',
                institution_id=inst.id,
                display_name='Dr. Sarah Connor',
                avatar_url='/static/uploads/avatars/sarah.png'
            )
            teacher.set_password('pass123')
            db.session.add(teacher)

        student = User.query.filter_by(username='sec_student_user').first()
        if not student:
            student = User(
                username='sec_student_user',
                role='student',
                institution_id=inst.id,
                display_name='Alexander Great',
                avatar_url='/static/uploads/avatars/alexander.png'
            )
            student.set_password('pass123')
            db.session.add(student)

        db.session.commit()
        self.teacher = User.query.filter_by(username='sec_teacher_user').first()
        self.student = User.query.filter_by(username='sec_student_user').first()

    def tearDown(self):
        self.ctx.pop()

    def test_user_name_property(self):
        """Test User.name property returns updated display_name when present, formatted fallback otherwise."""
        self.assertEqual(self.teacher.name, 'Dr. Sarah Connor')
        self.assertEqual(self.student.name, 'Alexander Great')

        # Test fallback when display_name is empty
        fallback_user = User(username='john_doe_99', role='student')
        self.assertEqual(fallback_user.name, 'John Doe 99')

    def test_video_uploader_display_name_rendering(self):
        """Test video uploader rendering uses profile display name and avatar photo."""
        video = Video.query.filter_by(title='Security Test Video').first()
        if not video:
            video = Video(
                title='Security Test Video',
                filename='sec_test.mp4',
                uploader_id=self.teacher.id,
                institution_id=self.inst.id,
                status='ready'
            )
            db.session.add(video)
            db.session.commit()

        # Login as student and access video page
        with self.client:
            self.client.post('/login', data={'username': 'sec_student_user', 'password': 'pass123', 'role': 'student'}, follow_redirects=True)
            res = self.client.get(f'/video/{video.id}')
            self.assertEqual(res.status_code, 200)
            html = res.get_data(as_text=True)
            # Verify teacher's display name and avatar photo appear in the HTML
            self.assertIn('Dr. Sarah Connor', html)
            self.assertIn('/static/uploads/avatars/sarah.png', html)
            self.assertNotIn('sec_teacher_user', html)

    def test_chat_room_display_name_api(self):
        """Test chat room API returns display_name and avatar_url."""
        cls = Classroom.query.filter_by(name='Security Physics').first()
        if not cls:
            cls = Classroom(name='Security Physics', teacher_id=self.teacher.id, institution_id=self.inst.id)
            db.session.add(cls)
            db.session.commit()
            cls.students.append(self.student)
            db.session.commit()

        with self.client:
            self.client.post('/login', data={'username': 'sec_student_user', 'password': 'pass123', 'role': 'student'}, follow_redirects=True)

            # Send chat message via API
            msg_res = self.client.post(f'/api/chatroom/{cls.id}/send', json={'content': 'Hello security world!'})
            self.assertEqual(msg_res.status_code, 200)
            msg_data = msg_res.get_json()
            self.assertEqual(msg_data['display_name'], 'Alexander Great')
            self.assertEqual(msg_data['name'], 'Alexander Great')
            self.assertEqual(msg_data['avatar_url'], '/static/uploads/avatars/alexander.png')

            # Fetch messages API
            get_res = self.client.get(f'/api/chatroom/{cls.id}/messages')
            self.assertEqual(get_res.status_code, 200)
            get_data = get_res.get_json()
            self.assertTrue(len(get_data['messages']) > 0)
            last_msg = get_data['messages'][-1]
            self.assertEqual(last_msg['display_name'], 'Alexander Great')
            self.assertEqual(last_msg['avatar_url'], '/static/uploads/avatars/alexander.png')

if __name__ == '__main__':
    unittest.main()
