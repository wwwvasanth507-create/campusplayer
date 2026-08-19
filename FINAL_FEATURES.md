# Campus Player — Final Feature Architecture & User Guide

This document details all implemented features, user capabilities, and system workflows across the Campus Player platform.

---

## 🚀 Primary Feature Modules

### 1. Universal Profile Identity & Photo Security
- **Privacy Protection**: Raw usernames are completely shielded from public UI touchpoints.
- **Custom Profile Photo**: Displays `user.avatar_url` (or static avatar image) on video cards, video player pages, comment sections, real-time chat rooms, dashboards, leaderboards, and PDF reports.
- **Display Name Fallback**: When custom profile photos are absent, renders an avatar badge with the first letter of the user's updated **Profile Display Name** (`user.name[0].upper()`).

### 2. Multi-Tenant Institution Architecture
- **Tenant Segregation**: All data models, uploads, queries, classrooms, and reports are strictly scoped with `institution_id`.
- **System Admin Portal**: Manage institutions, create institution owner admins, configure domain routing, and inspect global telemetry.

### 3. Adaptive HLS Video Engine
- **Resumable Chunk Upload**: Supports large video files up to 20 GB with order-independent chunk verification and concurrency locks.
- **Multi-Bitrate HLS Transcoding**: Converts uploaded videos into adaptive HLS streams (144p, 360p, 720p, 1080p, 4K, 16K) with FFmpeg.
- **Hover Seek Preview**: Generates sprite sheets and WebVTT cue files for interactive timeline scrubbing.

### 4. Interactive Live Chat Hub
- **Socket.IO Real-Time Chat**: Classroom chat rooms support instant messaging with profile photos, display names, and message history.

### 5. Gamification (Levels, XP & Daily Quests)
- **Student XP**: Earns 1 XP per watch tick and 100 XP per passed quiz.
- **Teacher XP**: Earns 50 XP per video uploaded.
- **Dynamic Levels**: Calculated via formula `(xp // 500) + 1`.
- **Daily Quests & Streaks**: Login streak tracking and daily quest milestones.

### 6. Classroom Workflows, Quizzes & Homework Assignments
- **Quiz Maker**: Create quizzes with customizable question types, attempt limits, and passing score thresholds.
- **Homework Assignments**: Teacher paper upload, student file submissions, and grading.

### 7. Attendance Engine & Lock Sessions
- **Session Attendance**: Time-bound attendance sessions with lock status enforcement before configured start times.
- **Parent Notifications**: Email and SMS alerts for absent/late students.

### 8. Native PDF Report Generation
- **ReportLab PDF Engine**: Generates downloadable PDFs for Attendance, Class Summaries, Leaderboard Levels, Weekly Performance, and Certificates of Achievement.

### 9. AI Assistant & Study Kit
- **Gemini Integration**: AI doubt solver, video summarizer, and flashcard study kit generator.

### 10. Admin Controls & System Settings
- **Video Controls**: Admin locks for video playback speed and skipping.
- **Global Branding**: Institution logos and global playlist thumbnails.

---

## 👥 Usage Instructions by Role

### System Admin
1. Log in at `/system_admin/login`.
2. Provision new institutions, assign owner admins, and inspect global platform telemetry.

### Institution Admin
1. Log in at `/admin`.
2. Manage teachers, view institution analytics, configure global playback rules, and generate level reports.

### Teacher
1. Log in at `/teacher`.
2. Upload videos, build playlists, create classrooms, assign quizzes/homework, track attendance, and inspect student watch analytics.

### Student
1. Log in at `/student`.
2. Watch adaptive HLS videos, participate in live chat rooms, submit homework, attempt quizzes, earn XP/Levels, and access the AI Assistant.

---

© 2026 Vasanth V. — Campus Player. All Rights Reserved. See [`LICENSE.md`](LICENSE.md).
