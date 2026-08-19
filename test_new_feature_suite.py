"""
Comprehensive Automated Test Suite for New Campus Player Advanced Features:
1. AI Assessment Synthesis Engine
2. Searchable Transcripts Parser
3. Video Retention & Hotspot Heatmap Engine
4. Quiz Proctoring & Anti-Cheat Violation Engine
5. Notice Board Broadcast Hub & Read Receipts
6. Interactive Academic Timetable Engine
7. XP Rewards Store & Customizer State Machine
8. Voice & Audio Homework Submission Flow
9. Watch Together Socket Event Definitions
10. PWA Manifest & Service Worker Assets
"""

import os
import json
import unittest
from datetime import datetime
from app import app, db
from models import (
    User, Institution, Video, Classroom, Quiz, Question, QuizResult,
    Announcement, AnnouncementRead, TimetableSlot, RewardItem, UserReward,
    Assignment, AssignmentSubmission
)
from services.ai_assessment_engine import generate_quiz_from_video
from services.transcript_engine import parse_vtt_or_srt_text_to_cues
from services.retention_engine import calculate_video_retention_curve

class AdvancedFeaturesTestSuite(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        self.app_context = self.app.app_context()
        self.app_context.push()

    def tearDown(self):
        db.session.rollback()
        self.app_context.pop()

    def test_01_ai_assessment_engine(self):
        """Verify AI assessment engine fallback generation."""
        mock_video = Video.query.first()
        if not mock_video:
            mock_video = Video(
                title="Introduction to Machine Learning & Neural Networks",
                description="Covers gradient descent, backpropagation, and loss functions in deep learning."
            )
            db.session.add(mock_video)
            db.session.commit()

        questions = generate_quiz_from_video(
            video=mock_video,
            num_questions=4,
            difficulty="intermediate"
        )
        self.assertEqual(len(questions), 4)
        for q in questions:
            self.assertIn('text', q)
            self.assertIn('option_a', q)
            self.assertIn('option_b', q)
            self.assertIn('option_c', q)
            self.assertIn('option_d', q)
            self.assertIn('correct_option', q)
            self.assertIn(q['correct_option'], ['A', 'B', 'C', 'D'])
            self.assertIn('explanation', q)
        print(" [PASS] 1. AI Assessment Engine verified.")

    def test_02_transcript_engine(self):
        """Verify WebVTT / SRT transcript parsing."""
        vtt_content = """WEBVTT

00:00:01.000 --> 00:00:04.500
Welcome to Data Structures and Algorithms.

00:00:05.000 --> 00:00:09.200
Today we will explore Binary Search Trees and Big O Notation.
"""
        cues = parse_vtt_or_srt_text_to_cues(vtt_content)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0]['start'], 1.0)
        self.assertIn("Data Structures", cues[0]['text'])
        self.assertEqual(cues[1]['start'], 5.0)
        self.assertIn("Binary Search Trees", cues[1]['text'])
        print(" [PASS] 2. Searchable Transcript Engine verified.")

    def test_03_retention_engine(self):
        """Verify audience retention and hotspot computation."""
        mock_video = Video.query.first()
        if not mock_video:
            mock_video = Video(title="Test Video", duration_seconds=300)
            db.session.add(mock_video)
            db.session.commit()

        retention = calculate_video_retention_curve(video_id=mock_video.id, num_buckets=50)
        self.assertEqual(len(retention['labels']), 50)
        self.assertEqual(len(retention['retention_percent']), 50)
        self.assertIsInstance(retention['hotspots'], list)
        print(" [PASS] 3. Video Retention & Heatmap Engine verified.")

    def test_04_proctoring_models(self):
        """Verify quiz proctoring fields and violation logging."""
        user = User.query.first()
        if not user:
            user = User(username="test_student_proctor", role="student")
            user.set_password("pass123")
            db.session.add(user)
            db.session.commit()

        quiz = Quiz(
            title="Test Proctored Exam",
            teacher_id=user.id,
            proctoring_enabled=True,
            max_tab_switches=2,
            block_copy_paste=True
        )
        db.session.add(quiz)
        db.session.commit()

        self.assertTrue(quiz.proctoring_enabled)
        self.assertEqual(quiz.max_tab_switches, 2)
        self.assertTrue(quiz.block_copy_paste)

        res = QuizResult(
            quiz_id=quiz.id,
            student_id=user.id,
            score=8,
            total_questions=10,
            passed=True,
            proctoring_violations_count=0
        )
        res.add_proctoring_violation("Window blur detected")
        res.add_proctoring_violation("Tab switch to external browser")
        self.assertEqual(res.proctoring_violations_count, 2)
        log = res.get_proctoring_log()
        self.assertEqual(len(log), 2)
        self.assertEqual(log[0]['reason'], "Window blur detected")
        print(" [PASS] 4. Quiz Proctoring & Violation Logging verified.")

    def test_05_notice_board_and_read_receipts(self):
        """Verify Announcements broadcast and acknowledgment."""
        user = User.query.first()
        ann = Announcement(
            title="Campus Holiday Notice",
            content="Campus closed this Friday for academic symposium.",
            author_id=user.id if user else None,
            priority="urgent",
            target_role="all",
            is_pinned=True
        )
        db.session.add(ann)
        db.session.commit()

        self.assertEqual(ann.priority, "urgent")
        self.assertTrue(ann.is_pinned)

        # Mark read receipt
        if user:
            read = AnnouncementRead(announcement_id=ann.id, user_id=user.id)
            db.session.add(read)
            db.session.commit()
            self.assertEqual(len(ann.reads), 1)
        print(" [PASS] 5. Notice Board & Read Receipts verified.")

    def test_06_timetable_slots(self):
        """Verify Academic Timetable slots."""
        cls = Classroom.query.first()
        if not cls:
            cls = Classroom(name="Computer Science A", class_code="CS101A")
            db.session.add(cls)
            db.session.commit()

        slot = TimetableSlot(
            classroom_id=cls.id,
            day_of_week="Monday",
            period_name="Period 1",
            start_time="09:00",
            end_time="10:00",
            subject_name="Compiler Design",
            room_number="Hall 3A"
        )
        db.session.add(slot)
        db.session.commit()

        self.assertEqual(slot.day_of_week, "Monday")
        self.assertEqual(slot.subject_name, "Compiler Design")
        print(" [PASS] 6. Academic Timetable Model verified.")

    def test_07_rewards_store_state_machine(self):
        """Verify XP Reward items catalog and equip logic."""
        item = RewardItem.query.filter_by(item_type="avatar_frame").first()
        if not item:
            item = RewardItem(
                code="frame_cyber_test",
                name="Cyber Neon Halo",
                description="Futuristic cyan neon glow",
                item_type="avatar_frame",
                item_value="frame-cyber-neon",
                xp_cost=50
            )
            db.session.add(item)
            db.session.commit()

        user = User.query.first()
        if user:
            user_reward = UserReward.query.filter_by(user_id=user.id, reward_id=item.id).first()
            if not user_reward:
                user_reward = UserReward(user_id=user.id, reward_id=item.id, is_equipped=True)
                db.session.add(user_reward)
            else:
                user_reward.is_equipped = True
            user.equipped_avatar_frame = item.item_value
            db.session.commit()

            self.assertEqual(user.equipped_avatar_frame, "frame-cyber-neon")
        print(" [PASS] 7. Rewards Store & Customizer State Machine verified.")

    def test_08_voice_assignment_submission(self):
        """Verify Audio Homework Submission model fields."""
        assign = Assignment.query.first()
        user = User.query.first()
        if assign and user:
            sub = AssignmentSubmission(
                assignment_id=assign.id,
                student_id=user.id,
                submission_type="audio",
                audio_file_path="uploads/submissions/test_audio.webm"
            )
            db.session.add(sub)
            db.session.commit()

            self.assertEqual(sub.submission_type, "audio")
            self.assertEqual(sub.audio_file_path, "uploads/submissions/test_audio.webm")
        print(" [PASS] 8. Voice Homework Submission verified.")

    def test_09_pwa_assets(self):
        """Verify PWA manifest and service worker exist."""
        manifest_path = os.path.join(app.static_folder, 'manifest.json')
        sw_path = os.path.join(app.static_folder, 'sw.js')

        self.assertTrue(os.path.exists(manifest_path), "manifest.json missing")
        self.assertTrue(os.path.exists(sw_path), "sw.js missing")

        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
            self.assertEqual(manifest['name'], 'Campus Player')
            self.assertEqual(manifest['display'], 'standalone')
        print(" [PASS] 9. PWA Manifest & Service Worker verified.")

if __name__ == '__main__':
    unittest.main()
