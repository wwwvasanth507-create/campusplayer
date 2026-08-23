import unittest
import json
from datetime import datetime, timedelta
from app import app as flask_app
from extensions import db
from models import User, Institution

class TestQuestExpirationSuite(unittest.TestCase):
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

        self.student = User.query.filter_by(username='careless_student').first()
        if not self.student:
            self.student = User(username='careless_student', role='student', institution_id=inst.id, xp=100)
            self.student.set_password('pass123')
            db.session.add(self.student)
            db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.app_context.pop()

    def test_01_active_quest_can_be_claimed(self):
        s = User.query.filter_by(username='careless_student').first()
        quests = s.get_daily_quests()
        self.assertIn('daily_login', quests)

        # Claim daily check-in quest
        success, xp_gained = s.claim_quest('daily_login')
        self.assertTrue(success)
        self.assertEqual(xp_gained, 25)

    def test_02_expired_quest_cannot_be_claimed_after_24h(self):
        s = User.query.filter_by(username='careless_student').first()
        s.quests_json = json.dumps({
            'date': datetime.utcnow().strftime('%Y-%m-%d'),
            'quests': {
                'take_quiz': {
                    'id': 'take_quiz',
                    'title': 'Quiz Challenger',
                    'desc': 'Complete an online assessment',
                    'xp': 75,
                    'icon': 'quiz',
                    'progress': 1,
                    'target': 1,
                    'claimed': False,
                    'expired': False,
                    'completed_at': (datetime.utcnow() - timedelta(hours=25)).isoformat()
                }
            }
        })
        db.session.commit()

        # Check daily quests (triggers expiration calculation)
        quests = s.get_daily_quests()
        self.assertTrue(quests['take_quiz']['expired'])

        # Attempt to claim expired quest
        success, xp_gained = s.claim_quest('take_quiz')
        self.assertFalse(success)
        self.assertEqual(xp_gained, 0)

    def test_03_date_rollover_expires_unclaimed_quests(self):
        s = User.query.filter_by(username='careless_student').first()
        yesterday_str = (datetime.utcnow() - timedelta(days=2)).strftime('%Y-%m-%d')
        s.quests_json = json.dumps({
            'date': yesterday_str,
            'quests': {
                'submit_assignment': {
                    'id': 'submit_assignment',
                    'title': 'Assignment Scholar',
                    'desc': 'Submit coursework',
                    'xp': 100,
                    'progress': 1,
                    'target': 1,
                    'claimed': False
                }
            }
        })
        db.session.commit()

        # Fetch daily quests for today -> rollover expires yesterday's quest
        quests = s.get_daily_quests()
        expired_entry = [q for k, q in quests.items() if q.get('expired')]
        self.assertTrue(len(expired_entry) > 0)
        self.assertEqual(expired_entry[0]['xp'], 100)

if __name__ == '__main__':
    unittest.main()
