# CampusPlayer - Implementation Tasks

## ✅ Completed - All Backend Features

### Core Configuration & Limits
- [x] MAX_CONTENT_LENGTH updated to 20 GB (20480 MB) in app.py
- [x] .env MAX_VIDEO_SIZE_MB set to 20480
- [x] models.py SiteSettings.max_video_size_mb default changed to 20480
- [x] admin_dashboard.html max input updated to accept 20480

### 1. Chunked Video Uploads (Up to 20 GB)
- [x] `/teacher/upload_chunk` route in app.py - receives 20 MB chunks sequentially
- [x] `assemble_chunks()` function - buffered assembly with cleanup
- [x] teacher_videos.html - enhanced form with Classroom, Description, Tags
- [x] Chunked upload JavaScript with progress bar in teacher_videos.html

### 2. Class-Wise SMS Reports
- [x] `/teacher/send_class_sms_report/<class_id>` POST route in app.py
- [x] `/api/sms_status/<job_id>` GET route in app.py
- [x] "Send Class SMS" button in teacher_attendance.html
- [x] SMS progress modal with polling in teacher_attendance.html

### 3. Email Queue & Parent Alerts
- [x] `send_async_email()` spawns background thread for pending emails
- [x] `process_pending_emails()` - background email sender with retry logic
- [x] Parent email alerts triggered in `mark_attendance` for:
  - Late 3+ times per month → email sent to `parent_email`
  - Absent 3 consecutive days → critical email alert sent to `parent_email`

### 4. Creative Feature: Contextual AI Video Doubt-Solver
- [x] `/api/ai_video_chat` POST route in app.py
  - Receives `video_id`, `current_time`, `message`
  - Fetches video title, description from DB
  - Formats timestamp (e.g., "2:35 min")
  - Sends contextual prompt to Gemini with system instruction as video tutor
  - Falls back gracefully across gemini model versions

### 5. Creative Feature: Playlist Completion Certificate
- [x] `/student/playlist/<playlist_id>/certificate` GET route in app.py
  - Validates all videos in playlist have VideoProgress.completed=True
  - Generates premium landscape PDF with ReportLab/PyCanvas
  - Dark blue border, white inner area, gold decorative borders
  - Corner ornaments (circles), 🎓 emblem, certificate title
  - Student name, playlist title, institution name, date
  - Signature lines and Certificate ID
  - Served as PDF attachment download

## 🔲 Frontend Template Updates (when app is running)
- [ ] Add "🎓 Download Certificate" button to playlist_view.html
- [ ] Add AI Assistant (Doubt Solver) tab to video_player.html
---

© 2026 Vasanth V. — CampusPlayer. All Rights Reserved. See LICENSE.md.
