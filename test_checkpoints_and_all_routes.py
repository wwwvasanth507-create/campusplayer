import unittest
import uuid
from app import app, db
from models import User, Institution, Video, VideoCheckpoint, CheckpointResponse, VideoDoubt, VideoDoubtReply, VideoFlashcard

class CheckpointsAndAllRoutesTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

        inst_slug = f'chk_inst_{uuid.uuid4().hex[:6]}'
        inst = Institution(name='Checkpoint Test Inst', slug=inst_slug)
        db.session.add(inst)
        db.session.commit()
        self.inst = inst

        teacher_name = f'chk_t_{uuid.uuid4().hex[:6]}'
        teacher = User(
            username=teacher_name,
            role='teacher',
            institution_id=inst.id,
            display_name='Professor Checkpoint',
            xp=100
        )
        teacher.set_password('pass123')
        db.session.add(teacher)

        student_name = f'chk_s_{uuid.uuid4().hex[:6]}'
        student = User(
            username=student_name,
            role='student',
            institution_id=inst.id,
            display_name='Student Tester',
            xp=480
        )
        student.set_password('pass123')
        db.session.add(student)

        db.session.commit()
        self.teacher = teacher
        self.student = student

        video = Video(
            title=f'Physics Checkpoint {uuid.uuid4().hex[:6]}',
            filename='chk_lecture.mp4',
            uploader_id=teacher.id,
            institution_id=inst.id,
            status='ready',
            duration_seconds=300
        )
        db.session.add(video)
        db.session.commit()
        self.video = video

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def test_checkpoint_lifecycle_and_xp(self):
        """Test full checkpoint lifecycle: add, get, student submission, XP award, level up, and deletion."""
        # 1. Teacher logs in and adds a video checkpoint
        with self.client:
            self.client.post('/login', data={'username': self.teacher.username, 'password': 'pass123', 'role': 'teacher'}, follow_redirects=True)
            add_res = self.client.post(f'/api/video/{self.video.id}/checkpoints/add', json={
                'timestamp_seconds': 45.5,
                'question_text': 'What is the unit of Force in SI system?',
                'option_a': 'Newton (N)',
                'option_b': 'Joule (J)',
                'option_c': 'Watt (W)',
                'option_d': 'Pascal (Pa)',
                'correct_option': 'a',
                'explanation': 'Force is measured in Newtons (kg*m/s^2).',
                'xp_reward': 30
            })
            self.assertEqual(add_res.status_code, 200)
            add_data = add_res.get_json()
            self.assertTrue(add_data['success'])
            checkpoint_id = add_data['checkpoint_id']

        # 2. Logout teacher and student logs in to fetch video checkpoints
        self.client.get('/logout', follow_redirects=True)
        self.client.post('/login', data={'username': self.student.username, 'password': 'pass123', 'role': 'student'}, follow_redirects=True)
        get_res = self.client.get(f'/api/video/{self.video.id}/checkpoints')
        self.assertEqual(get_res.status_code, 200)
        get_data = get_res.get_json()
        self.assertTrue(get_data['success'])
        self.assertEqual(len(get_data['checkpoints']), 1)
        cp_item = get_data['checkpoints'][0]
        self.assertEqual(cp_item['id'], checkpoint_id)
        self.assertEqual(cp_item['timestamp'], 45.5)
        self.assertFalse(cp_item['answered'])

        # 3. Student submits correct answer to checkpoint
        sub_res = self.client.post(f'/api/video/checkpoint/{checkpoint_id}/submit', json={
            'selected_option': 'a'
        })
        self.assertEqual(sub_res.status_code, 200)
        sub_data = sub_res.get_json()
        self.assertTrue(sub_data['success'])
        self.assertTrue(sub_data['is_correct'])
        self.assertEqual(sub_data['xp_awarded'], 30)

        # Verify Student XP increased (480 + 30 = 510) and Level increased (510 // 500 + 1 = 2)
        db.session.refresh(self.student)
        self.assertEqual(self.student.xp, 510)
        self.assertEqual(self.student.level, 2)

        # 4. Logout student and login teacher to delete checkpoint
        self.client.get('/logout', follow_redirects=True)
        self.client.post('/login', data={'username': self.teacher.username, 'password': 'pass123', 'role': 'teacher'}, follow_redirects=True)
        del_res = self.client.post(f'/api/video/checkpoint/{checkpoint_id}/delete')
        self.assertEqual(del_res.status_code, 200)
        del_data = del_res.get_json()
        self.assertTrue(del_data['success'])

        # Verify checkpoint deleted
        db.session.close()
        deleted_cp = db.session.get(VideoCheckpoint, checkpoint_id)
        self.assertIsNone(deleted_cp)

    def test_video_doubts_and_qa_api(self):
        """Test student submitting time-stamped doubt and teacher replying."""
        with self.client:
            self.client.post('/login', data={'username': self.student.username, 'password': 'pass123', 'role': 'student'}, follow_redirects=True)
            doubt_res = self.client.post(f'/api/video/{self.video.id}/doubts/add', json={
                'timestamp_seconds': 120.0,
                'question_text': 'Why does velocity change when acceleration is constant?'
            })
            self.assertEqual(doubt_res.status_code, 200)
            doubt_data = doubt_res.get_json()
            self.assertTrue(doubt_data['success'])
            doubt_id = doubt_data['doubt_id']

        with self.client:
            self.client.post('/login', data={'username': self.teacher.username, 'password': 'pass123', 'role': 'teacher'}, follow_redirects=True)
            reply_res = self.client.post(f'/api/video/doubts/{doubt_id}/reply', json={
                'content': 'Constant acceleration means velocity changes at a constant rate v = u + at.'
            })
            self.assertEqual(reply_res.status_code, 200)
            reply_data = reply_res.get_json()
            self.assertTrue(reply_data['success'])

    def test_flashcards_view(self):
        """Test flashcards view route."""
        with self.client:
            self.client.post('/login', data={'username': self.student.username, 'password': 'pass123', 'role': 'student'}, follow_redirects=True)
            res = self.client.get(f'/video/{self.video.id}/flashcards')
            self.assertEqual(res.status_code, 200)

if __name__ == '__main__':
    unittest.main()
