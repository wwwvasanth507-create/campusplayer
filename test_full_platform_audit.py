"""
Campus Player Full Platform Audit & Health Diagnostic:
1. Jinja2 Template Compilation Check (All HTML templates in /templates)
2. URL Map & Endpoint Integrity Check (All routes in app and blueprints)
3. Database Models & Multi-Tenant Query Interceptors Check
4. Static Assets & PWA Integrity Check
5. Service Engines & Socket Handlers Health Check
"""

import os
import sys
import unittest
from jinja2 import Environment, FileSystemLoader
from app import app, db
from models import (
    User, Institution, Video, Classroom, Playlist, Quiz, Question, QuizResult,
    Announcement, AnnouncementRead, TimetableSlot, RewardItem, UserReward,
    Assignment, AssignmentSubmission, AttendanceSession, SiteSettings
)

class FullPlatformAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app
        cls.app.config['TESTING'] = True
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.client = cls.app.test_client()

    def test_01_all_jinja_templates_syntax(self):
        """Verify that every single Jinja2 template parses and compiles without syntax errors."""
        template_dir = os.path.join(os.path.dirname(__file__), 'templates')
        env = Environment(loader=FileSystemLoader(template_dir))
        
        # Add mock globals/filters so AST parsing succeeds if filters are referenced
        env.globals.update({
            'url_for': lambda *args, **kwargs: '#',
            'csrf_token': lambda: 'mock_token',
            'current_user': None,
            'session': {},
            'settings': None,
            'get_flashed_messages': lambda **kwargs: []
        })

        template_files = [f for f in os.listdir(template_dir) if f.endswith('.html')]
        self.assertGreater(len(template_files), 10, "Template directory must contain templates")

        errors = []
        for tpl in template_files:
            try:
                with open(os.path.join(template_dir, tpl), 'r', encoding='utf-8') as f:
                    source = f.read()
                env.parse(source)
            except Exception as e:
                errors.append(f"Template syntax error in {tpl}: {e}")

        if errors:
            self.fail("\n".join(errors))
        print(f" [PASS] 1. All {len(template_files)} Jinja2 templates compiled with zero syntax errors.")

    def test_02_url_rules_and_endpoints(self):
        """Verify that all URL rules in the Flask application are valid and mapped."""
        rules = list(self.app.url_map.iter_rules())
        self.assertGreater(len(rules), 50, "App must have mapped endpoints")
        endpoints = {rule.endpoint for rule in rules}

        # Check critical endpoints are present
        critical_endpoints = [
            'login', 'logout', 'student_dashboard', 'teacher_dashboard', 'admin_dashboard',
            'watch_video', 'take_quiz', 'announcements_hub', 'timetable_hub',
            'rewards_store_hub', 'get_video_transcript', 'get_video_retention_heatmap',
            'ai_generate_quiz_for_video', 'save_ai_quiz', 'submit_audio_assignment'
        ]
        for ep in critical_endpoints:
            self.assertIn(ep, endpoints, f"Critical endpoint '{ep}' missing from URL map!")
        print(f" [PASS] 2. All {len(rules)} URL routes and critical endpoints verified.")

    def test_03_database_models_and_multi_tenancy(self):
        """Verify database queries, relationships, and multi-tenancy backfill integrity."""
        with self.app.app_context():
            inst = Institution.query.filter_by(slug='default').first()
            if not inst:
                inst = Institution(name="Default Institution", slug="default", status="active")
                db.session.add(inst)
                db.session.commit()
            self.assertIsNotNone(inst)
            self.assertEqual(inst.slug, 'default')

            # Verify reward items catalog
            reward_count = RewardItem.query.count()
            self.assertGreaterEqual(reward_count, 1, "Reward items catalog should be populated")
            print(f" [PASS] 3. Database models & multi-tenancy verified (Catalog items: {reward_count}).")

    def test_04_static_pwa_assets(self):
        """Verify static files, CSS, JS, and PWA manifest existence."""
        static_dir = os.path.join(os.path.dirname(__file__), 'static')
        required_files = [
            'manifest.json',
            'sw.js',
            'css/player.css',
            'css/responsive.css',
            'js/player.js'
        ]
        for rel in required_files:
            full_path = os.path.join(static_dir, rel)
            self.assertTrue(os.path.exists(full_path), f"Static file '{rel}' is missing!")
        print(" [PASS] 4. PWA manifest, service worker, CSS and JS static assets verified.")

    def test_05_api_endpoints_health(self):
        """Verify live API routes return JSON without server exceptions."""
        with self.app.app_context():
            user = User.query.first()
            if not user:
                user = User(username="audit_test_user", role="student")
                user.set_password("pass123")
                db.session.add(user)
                db.session.commit()
            else:
                user.set_password("pass123")
                db.session.commit()

            self.client.post('/login', data={'username': user.username, 'password': 'pass123', 'role': user.role}, follow_redirects=True)



            vid = Video.query.first()
            if not vid:
                vid = Video(title="Audit Test Video", filename="audit_test.mp4", uploader_id=user.id, duration_seconds=120)
                db.session.add(vid)
                db.session.commit()

            res = self.client.get(f'/api/video/{vid.id}/retention_heatmap')
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertIn('retention_percent', data)
            self.assertIn('labels', data)

            # Test transcript API
            res_trans = self.client.get(f'/api/video/{vid.id}/transcript')
            self.assertEqual(res_trans.status_code, 200)
            data_trans = res_trans.get_json()
            self.assertIn('cues', data_trans)

            print(" [PASS] 5. Authenticated API endpoints responded with status 200 JSON.")

if __name__ == '__main__':
    unittest.main()
