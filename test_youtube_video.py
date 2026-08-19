import unittest
from app import app, db, extract_youtube_id
from models import User, Institution, Video, SiteSettings

class YouTubeVideoTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

        inst = Institution.query.filter_by(slug='yt_test_inst').first()
        if not inst:
            inst = Institution(name='YouTube Test Inst', slug='yt_test_inst')
            db.session.add(inst)
            db.session.commit()
        self.inst = inst

        teacher = User.query.filter_by(username='yt_teacher_user').first()
        if not teacher:
            teacher = User(
                username='yt_teacher_user',
                role='teacher',
                institution_id=inst.id,
                display_name='Professor YouTube',
                xp=100
            )
            teacher.set_password('pass123')
            db.session.add(teacher)

        student = User.query.filter_by(username='yt_student_user').first()
        if not student:
            student = User(
                username='yt_student_user',
                role='student',
                institution_id=inst.id,
                display_name='Student Learner',
                xp=50
            )
            student.set_password('pass123')
            db.session.add(student)

        db.session.commit()
        self.teacher = User.query.filter_by(username='yt_teacher_user').first()
        self.student = User.query.filter_by(username='yt_student_user').first()

    def tearDown(self):
        self.ctx.pop()

    def test_youtube_url_extraction(self):
        """Test extract_youtube_id helper parses various YouTube URL formats including share links and live streams."""
        self.assertEqual(extract_youtube_id('https://www.youtube.com/watch?v=dQw4w9WgXcQ'), 'dQw4w9WgXcQ')
        self.assertEqual(extract_youtube_id('https://youtu.be/dQw4w9WgXcQ'), 'dQw4w9WgXcQ')
        self.assertEqual(extract_youtube_id('https://youtu.be/jFWsj_QT0G8?si=vOallyl-WvqdZPrl'), 'jFWsj_QT0G8')
        self.assertEqual(extract_youtube_id('https://www.youtube.com/embed/dQw4w9WgXcQ'), 'dQw4w9WgXcQ')
        self.assertEqual(extract_youtube_id('https://www.youtube.com/shorts/dQw4w9WgXcQ'), 'dQw4w9WgXcQ')
        self.assertEqual(extract_youtube_id('https://www.youtube.com/live/dQw4w9WgXcQ?feature=share'), 'dQw4w9WgXcQ')
        self.assertEqual(extract_youtube_id('dQw4w9WgXcQ'), 'dQw4w9WgXcQ')
        self.assertIsNone(extract_youtube_id('invalid_url_string'))

    def test_resolved_youtube_id_property(self):
        """Test Video.resolved_youtube_id property resolves YouTube ID from youtube_id, filename, or youtube_url."""
        v1 = Video(title='V1', filename='youtube_jFWsj_QT0G8', video_type='youtube', youtube_id='jFWsj_QT0G8', uploader_id=self.teacher.id)
        v2 = Video(title='V2', filename='youtube_dQw4w9WgXcQ', video_type='youtube', youtube_id=None, uploader_id=self.teacher.id)
        v3 = Video(title='V3', filename='local_file.mp4', video_type='youtube', youtube_id=None, youtube_url='https://youtu.be/jFWsj_QT0G8', uploader_id=self.teacher.id)

        self.assertEqual(v1.resolved_youtube_id, 'jFWsj_QT0G8')
        self.assertEqual(v2.resolved_youtube_id, 'dQw4w9WgXcQ')
        self.assertEqual(v3.resolved_youtube_id, 'jFWsj_QT0G8')

    def test_teacher_add_and_delete_youtube_video_route(self):
        """Test teacher adding a YouTube share video link and deleting it cleanly."""
        initial_xp = self.teacher.xp
        with self.client:
            self.client.post('/login', data={'username': 'yt_teacher_user', 'password': 'pass123', 'role': 'teacher'}, follow_redirects=True)
            res = self.client.post('/teacher/add_youtube_video', data={
                'title': 'Quantum Physics Share Link Lecture',
                'youtube_url': 'https://youtu.be/jFWsj_QT0G8?si=vOallyl-WvqdZPrl',
                'description': 'Introductory quantum physics overview',
                'tags': 'physics,quantum,lecture'
            }, follow_redirects=True)

            self.assertEqual(res.status_code, 200)

            # Query created video
            video = Video.query.filter_by(title='Quantum Physics Share Link Lecture').first()
            self.assertIsNotNone(video)
            self.assertEqual(video.video_type, 'youtube')
            self.assertEqual(video.youtube_id, 'jFWsj_QT0G8')
            self.assertEqual(video.status, 'completed')

            vid_id = video.id
            del_res = self.client.post(f'/teacher/delete_video/{vid_id}', follow_redirects=True)
            self.assertEqual(del_res.status_code, 200)

            # Assert video row is completely removed
            deleted_video = Video.query.get(vid_id)
            self.assertIsNone(deleted_video)
            self.assertEqual(video.thumbnail_path, 'https://img.youtube.com/vi/jFWsj_QT0G8/hqdefault.jpg')
            self.assertEqual(video.institution_id, self.inst.id)

            # Check teacher gained +50 XP
            updated_teacher = User.query.get(self.teacher.id)
            self.assertEqual(updated_teacher.xp, initial_xp + 50)

    def test_youtube_video_player_rendering(self):
        """Test watching a YouTube video renders the YouTube embed and lock parameters."""
        video = Video(
            title='Calculus Basics YT',
            filename='youtube_dQw4w9WgXcQ',
            uploader_id=self.teacher.id,
            institution_id=self.inst.id,
            video_type='youtube',
            youtube_id='dQw4w9WgXcQ',
            status='ready'
        )
        db.session.add(video)
        db.session.commit()

        with self.client:
            self.client.post('/login', data={'username': 'yt_student_user', 'password': 'pass123', 'role': 'student'}, follow_redirects=True)
            res = self.client.get(f'/video/{video.id}')
            self.assertEqual(res.status_code, 200)
            html = res.get_data(as_text=True)

            self.assertIn('youtube-player', html)
            self.assertIn('dQw4w9WgXcQ', html)
            self.assertIn('initYouTubeEngine', html)


if __name__ == '__main__':
    unittest.main()
