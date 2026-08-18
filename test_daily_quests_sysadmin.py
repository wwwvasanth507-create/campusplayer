import unittest
from app import app
from extensions import db
from models import User, Institution, SiteSettings, DailyQuestTemplate

class TestSysadminDailyQuests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        cls.app_context = app.app_context()
        cls.app_context.push()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.app_context.pop()

    def setUp(self):
        # Create test institution if missing
        self.inst = Institution.query.filter_by(slug="test_quest_inst").first()
        if not self.inst:
            self.inst = Institution(name="Test Quest Inst", slug="test_quest_inst")
            db.session.add(self.inst)
            db.session.commit()

        # Site settings
        self.settings = SiteSettings.query.filter_by(institution_id=self.inst.id).first()
        if not self.settings:
            self.settings = SiteSettings(institution_id=self.inst.id, quests_version=1)
            db.session.add(self.settings)
            db.session.commit()

    def test_1_student_get_daily_quests(self):
        student = User.query.filter_by(username="quest_stud1").first()
        if not student:
            student = User(username="quest_stud1", email="quest_stud1@test.com", role="student", institution_id=self.inst.id, xp=100)
            student.set_password("pass123")
            db.session.add(student)
            db.session.commit()

        quests = student.get_daily_quests()
        self.assertIsInstance(quests, dict)

    def test_2_sysadmin_add_quest(self):
        sysadmin = User.query.filter_by(username="quest_sysadmin").first()
        if not sysadmin:
            sysadmin = User(username="quest_sysadmin", email="quest_sysadmin@test.com", role="system_admin")
            sysadmin.set_password("pass123")
            db.session.add(sysadmin)
            db.session.commit()

        # Cleanup target quest if exists
        old = DailyQuestTemplate.query.filter_by(quest_key='read_article_brand_new').first()
        if old:
            db.session.delete(old)
            db.session.commit()

        client = app.test_client()
        login_res = client.post('/login', data={'username': 'quest_sysadmin', 'password': 'pass123', 'role': 'system_admin'}, follow_redirects=True)
        self.assertEqual(login_res.status_code, 200)
        
        response = client.post('/sysadmin/quests/add', data={
            'quest_key': 'read_article_brand_new',
            'title': 'Read Article',
            'desc': 'Read an academic paper',
            'xp': '75',
            'icon': 'book',
            'target': '1'
        }, follow_redirects=True)

        self.assertEqual(response.status_code, 200)

        q = DailyQuestTemplate.query.filter_by(quest_key='read_article_brand_new').first()
        self.assertIsNotNone(q)
        self.assertEqual(q.title, 'Read Article')
        self.assertEqual(q.xp, 75)

    def test_3_sysadmin_edit_quest(self):
        client = app.test_client()
        client.post('/login', data={'username': 'quest_sysadmin', 'password': 'pass123', 'role': 'system_admin'}, follow_redirects=True)
        
        q = DailyQuestTemplate.query.filter_by(quest_key='read_article_brand_new').first()
        if not q:
            q = DailyQuestTemplate(quest_key="read_article_brand_new", title="Initial Title", desc="Desc", xp=50, icon="login", target=1, is_active=True)
            db.session.add(q)
            db.session.commit()

        response = client.post(f'/sysadmin/quests/edit/{q.id}', data={
            'title': 'Super Read Article',
            'desc': 'Updated desc',
            'xp': '100',
            'icon': 'stars',
            'target': '1',
            'is_active': 'true'
        }, follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        updated_q = db.session.get(DailyQuestTemplate, q.id)
        self.assertEqual(updated_q.title, 'Super Read Article')
        self.assertEqual(updated_q.xp, 100)

    def test_4_sysadmin_delete_quest(self):
        client = app.test_client()
        client.post('/login', data={'username': 'quest_sysadmin', 'password': 'pass123', 'role': 'system_admin'}, follow_redirects=True)
        
        q = DailyQuestTemplate.query.filter_by(quest_key='read_article_brand_new').first()
        if q:
            response = client.post(f'/sysadmin/quests/delete/{q.id}', follow_redirects=True)
            self.assertEqual(response.status_code, 200)
            deleted_q = db.session.get(DailyQuestTemplate, q.id)
            self.assertIsNone(deleted_q)

    def test_5_student_check_version_api(self):
        client = app.test_client()
        client.post('/login', data={'username': 'quest_stud1', 'password': 'pass123', 'role': 'student'}, follow_redirects=True)
        res = client.get('/api/student/quests/check_version')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data['status'], 'success')
        self.assertIn('quests_version', data)

    def test_6_student_get_quests_api(self):
        client = app.test_client()
        client.post('/login', data={'username': 'quest_stud1', 'password': 'pass123', 'role': 'student'}, follow_redirects=True)
        res = client.get('/api/student/quests')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data['status'], 'success')
        self.assertIn('quests', data)

if __name__ == '__main__':
    unittest.main()
