"""
Comprehensive Automated Test Suite for AI Lecture Copilot & Instant Timestamp Citation Engine:
1. Transcript & Chapter Semantic Cue Indexing
2. Digital Library E-Book & Study Guide Cross-Referencing
3. Copilot Question Answering & Timestamp Citation Generation
4. 1-Click Micro-Quiz Verification & +20 XP Rewards
5. Live Exam Readiness Index Calculation
6. REST API Endpoints Integration (/copilot/ask, /copilot/history, /quiz_submit, /readiness)
"""

import os
import json
import unittest
from datetime import datetime
from app import app, db
from models import User, Institution, Video, EBook, AICopilotInteraction
from services.ai_lecture_copilot import (
    extract_video_cues,
    find_best_transcript_citation,
    find_relevant_library_guide,
    ask_lecture_copilot,
    evaluate_micro_quiz,
    calculate_video_exam_readiness
)

class AILectureCopilotTestSuite(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        self.app_context = self.app.app_context()
        self.app_context.push()

        self.inst = Institution.query.first()
        if not self.inst:
            self.inst = Institution(name="Default Institution", slug="default", status="active")
            db.session.add(self.inst)
            db.session.commit()

        self.student = User.query.filter_by(role='student').first()
        if not self.student:
            self.student = User(username="copilot_student", role="student", xp=100, institution_id=self.inst.id)
            self.student.set_password("pass123")
            db.session.add(self.student)
            db.session.commit()

        self.video = Video.query.first()
        if not self.video:
            self.video = Video(
                title="Introduction to Fluid Dynamics and Bernoulli Principle",
                description="Comprehensive lecture covering streamline flow, pressure differential, and Bernoulli equation derivation.",
                duration_seconds=900,
                institution_id=self.inst.id,
                ai_key_takeaways=json.dumps([
                    "Streamline and turbulent fluid flow definitions",
                    "Bernoulli equation derivation using energy conservation",
                    "Venturi meter applications and practice problems"
                ])
            )
            db.session.add(self.video)
            db.session.commit()

    def tearDown(self):
        db.session.rollback()
        self.app_context.pop()

    def test_01_transcript_cue_extraction_and_citations(self):
        """Test extraction of video cues and semantic citation finding."""
        cues = extract_video_cues(self.video)
        self.assertGreaterEqual(len(cues), 1)

        # Test query about Bernoulli derivation
        citation = find_best_transcript_citation(cues, "How do we derive Bernoulli equation using energy conservation?", 120.0)
        self.assertIsNotNone(citation)
        self.assertIn('start', citation)
        self.assertIn('start_formatted', citation)
        print(f" [PASS] 1. Transcript cue extraction and citation matched: [{citation['start_formatted']}] {citation['text'][:40]}...")

    def test_02_digital_library_cross_referencing(self):
        """Test cross-referencing related digital library study guides."""
        guide = EBook(
            title="Fluid Mechanics & Hydraulics Study Guide",
            subject="Physics",
            resource_type="guide",
            page_count=120,
            file_path="uploads/ebooks/fluid_guide.pdf",
            file_name="fluid_guide.pdf",
            institution_id=self.inst.id
        )
        db.session.add(guide)
        db.session.commit()

        book, page = find_relevant_library_guide(self.video, "fluid pressure equations")
        self.assertIsNotNone(book)
        self.assertEqual(book.resource_type, "guide")
        self.assertGreaterEqual(page, 1)
        print(f" [PASS] 2. Digital library study guide cross-referenced: '{book.title}' (Page {page})")

    def test_03_copilot_service_reasoning_and_micro_quiz(self):
        """Test full ask_lecture_copilot service execution."""
        res = ask_lecture_copilot(
            video=self.video,
            user=self.student,
            question="Why does velocity increase when pressure drops in a Venturi tube?",
            current_time=240.0
        )
        self.assertTrue(res['success'])
        self.assertIn('answer', res)
        self.assertIn('cited_timestamp', res)
        self.assertIn('cited_timestamp_formatted', res)
        self.assertIn('micro_quiz', res)
        self.assertEqual(len(res['micro_quiz']['options']), 4)
        print(f" [PASS] 3. Copilot reasoning generated answer with citation at {res['cited_timestamp_formatted']}")

    def test_04_micro_quiz_evaluation_and_xp(self):
        """Test micro-quiz answer submission and XP reward granting."""
        initial_xp = self.student.xp or 0
        res = ask_lecture_copilot(
            video=self.video,
            user=self.student,
            question="What is the conservation principle behind this lecture?",
            current_time=150.0
        )
        it_id = res['interaction_id']
        correct_idx = res['micro_quiz']['correct_index']

        eval_res = evaluate_micro_quiz(it_id, self.student, correct_idx)
        self.assertTrue(eval_res['success'])
        self.assertTrue(eval_res['is_correct'])
        self.assertEqual(eval_res['xp_awarded'], 20)
        self.assertEqual(self.student.xp, initial_xp + 20)
        print(f" [PASS] 4. Micro-quiz evaluated successfully, +20 XP awarded (New XP: {self.student.xp})")

    def test_05_exam_readiness_index(self):
        """Test live student exam readiness calculation."""
        readiness = calculate_video_exam_readiness(self.student, self.video)
        self.assertIn('score', readiness)
        self.assertIn('status', readiness)
        self.assertGreaterEqual(readiness['score'], 0)
        self.assertLessEqual(readiness['score'], 100)
        print(f" [PASS] 5. Live Exam Readiness score calculated: {readiness['score']}% ({readiness['status']})")

    def test_06_copilot_rest_api_endpoints(self):
        """Test /api/video/<id>/copilot/ask, /history, /quiz_submit, and /readiness."""
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.student.id)
            sess['_fresh'] = True

        # 1. Ask API
        ask_res = self.client.post(
            f'/api/video/{self.video.id}/copilot/ask',
            data=json.dumps({
                'question': 'Can you explain the main equation in this video?',
                'current_time': 180.0
            }),
            content_type='application/json'
        )
        self.assertEqual(ask_res.status_code, 200)
        ask_data = ask_res.get_json()
        self.assertTrue(ask_data['success'])
        it_id = ask_data['interaction_id']

        # 2. History API
        hist_res = self.client.get(f'/api/video/{self.video.id}/copilot/history')
        self.assertEqual(hist_res.status_code, 200)
        hist_data = hist_res.get_json()
        self.assertTrue(hist_data['success'])
        self.assertGreaterEqual(len(hist_data['history']), 1)

        # 3. Quiz Submit API
        quiz_res = self.client.post(
            f'/api/video/copilot/interaction/{it_id}/quiz_submit',
            data=json.dumps({'selected_index': 0}),
            content_type='application/json'
        )
        self.assertEqual(quiz_res.status_code, 200)
        quiz_data = quiz_res.get_json()
        self.assertTrue(quiz_data['success'])

        # 4. Readiness API
        read_res = self.client.get(f'/api/video/{self.video.id}/readiness')
        self.assertEqual(read_res.status_code, 200)
        read_data = read_res.get_json()
        self.assertTrue(read_data['success'])
        self.assertIn('readiness', read_data)
        print(" [PASS] 6. All Copilot REST APIs (/ask, /history, /quiz_submit, /readiness) verified 100%.")

if __name__ == '__main__':
    unittest.main()
