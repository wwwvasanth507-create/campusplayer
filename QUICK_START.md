# 🎓 CampusPlayer - Quick Start Guide

## 🚀 Getting Started

### Step 1: Start the Server
```bash
cd campusplayer
python app.py
```

The server will start at: **http://127.0.0.1:5000**

---

## 👥 User Workflows

### 🔐 Initial Login (Admin)

1. Open browser and go to: http://127.0.0.1:5000
2. You'll see the ADS page → Click **"Visit"** or **"Login"**
3. Login as admin:
   - **Username**: `admin`
   - **Password**: `admin123`
   - **Role**: Select "Admin"

---

### 👨‍💼 Admin Workflow

**As Admin, you can:**

1. **Add a Teacher**:
   - On Admin Dashboard, find "Add Teacher" section
   - Enter username (e.g., `john_doe`)
   - Enter password (e.g., `teacher123`)
   - Click "Add Teacher"

2. **Change Teacher Password**:
   - Find teacher in the list
   - Click "Change Password"
   - Enter new password
   - Submit

3. **Delete Teacher**:
   - Find teacher in the list
   - Click "Delete"
   - Confirm deletion

4. **Logout**: Click logout button

---

### 👨‍🏫 Teacher Workflow

**Login as Teacher** (use credentials you created as admin):
- Username: `john_doe`
- Password: `teacher123`
- Role: Select "Teacher"

**As Teacher, you can:**

1. **Add Students**:
   - On Teacher Dashboard, find "Add Student" section
   - Enter student username (e.g., `alice_student`)
   - Enter password (e.g., `student123`)
   - Click "Add Student"

2. **Upload Videos** (requires FFmpeg):
   - Find "Upload Video" section
   - Enter video title (e.g., "Introduction to Python")
   - Choose video file (.mp4, .mov, .avi, .mkv)
   - Click "Upload"
   - Video will automatically convert to HLS

3. **Create Playlists**:
   - Find "Create Playlist" section
   - Enter playlist title (e.g., "Python Basics")
   - Click "Create Playlist"

4. **Add Videos to Playlist**:
   - Find "Add to Playlist" section
   - Select playlist from dropdown
   - Select video from dropdown
   - Click "Add to Playlist"

5. **View Analytics**:
   - Click "View Analytics" or navigate to /teacher/analytics
   - See which students watched your videos
   - See when they started and how long they watched

6. **Change Student Password**:
   - Find student in the list
   - Click "Change Password"
   - Enter new password
   - Submit

---

### 👨‍🎓 Student Workflow

**Login as Student** (use credentials created by teacher):
- Username: `alice_student`
- Password: `student123`
- Role: Select "Student"

**As Student, you can:**

1. **Browse Videos**:
   - Student Dashboard shows all available videos
   - Videos are listed by newest first

2. **Browse Playlists**:
   - See all playlists created by teachers
   - Click on a playlist to view videos in it

3. **Search**:
   - Use search bar at top
   - Search for video titles or playlist names
   - Press Enter to search

4. **Watch Videos**:
   - Click on any video to watch
   - Video player will load with HLS streaming
   - Your watch time is automatically tracked

5. **View Related Videos**:
   - While watching a video, see "Related Videos" sidebar
   - Click to jump to another video

6. **Comment on Videos**:
   - Scroll down below the video player
   - Type your comment in the text box
   - Click "Post Comment"
   - Teacher will receive your comment

7. **Reply to Comments**:
   - Click "Reply" on any comment
   - Type your reply
   - Submit

---

## 📊 Analytics Explained

### For Teachers:

When you view Analytics, you'll see a table with:
- **Video Title**: Which video was watched
- **Student Name**: Which student watched it
- **Start Time**: When they started watching
- **Duration**: How long they watched (in seconds)

**Example:**
| Video Title | Student Name | Start Time | Duration |
|------------|--------------|------------|----------|
| Intro to Python | alice_student | 2026-02-10 14:30:22 | 320 |
| Python Lists | bob_student | 2026-02-10 15:45:10 | 180 |

This helps you track student engagement!

---

## 🎬 Video Upload Requirements

### FFmpeg Installation (Required for video upload):

**Windows:**
1. Download FFmpeg from: https://ffmpeg.org/download.html
2. Extract to `C:\ffmpeg`
3. Add `C:\ffmpeg\bin` to your PATH environment variable
4. Restart computer
5. Test by running: `ffmpeg -version`

**Supported Video Formats:**
- MP4 (.mp4)
- MOV (.mov)
- AVI (.avi)
- MKV (.mkv)

**What Happens When You Upload:**
1. Original video saved to `static/uploads/`
2. FFmpeg converts video to HLS format
3. HLS files saved to `static/hls/<video_id>/`
4. Creates `master.m3u8` playlist file
5. Video segments (.ts files) for streaming

---

## 🔍 Testing the Application

### Run Automated Tests:
```bash
python test_app.py
```

This will test:
- ✅ ADS page loading
- ✅ Admin login and features
- ✅ Teacher login and features
- ✅ Student login and features
- ✅ All CRUD operations
- ✅ CSRF-protected form submissions

Note: The application generates an admin account on first run. Set `ADMIN_PASSWORD` in `.env` if you want a fixed password like `admin123`.

---

## 📂 Important Files & Folders

```
campus_video_player/
├── app.py                 → Main application (Flask routes)
├── models.py              → Database models
├── app.db                 → SQLite database (auto-created)
├── templates/             → HTML pages
├── static/
│   ├── uploads/           → Original uploaded videos
│   └── hls/               → HLS converted videos
└── test_app.py           → Automated test suite
```

---

## 🐛 Troubleshooting

### Problem: "SQLite error"
**Solution**: Already fixed! The database URI is now Windows-compatible.

### Problem: "FFmpeg not found" when uploading videos
**Solution**: 
1. Install FFmpeg (see Video Upload Requirements above)
2. Make sure it's in your PATH
3. Restart the Flask server

### Problem: "Login failed"
**Solution**:
1. Check username/password are correct
2. Make sure you selected the correct role
3. Admin default: `admin` / `admin123`

### Problem: "Page not found (404)"
**Solution**:
1. Make sure Flask server is running
2. Check the URL is correct
3. Restart the server: Stop (Ctrl+C) and run `python app.py` again

### Problem: Videos not playing
**Solution**:
1. Ensure video was uploaded with FFmpeg installed
2. Check `static/hls/<video_id>/master.m3u8` exists
3. Try a different video format
4. Check browser console for errors

---

## 🎯 Quick Test Scenario

**Complete User Journey:**

1. **As Admin**:
   - Login: `admin` / `admin123`
   - Create teacher: `prof_smith` / `teacher123`
   - Logout

2. **As Teacher** (`prof_smith`):
   - Login with new credentials
   - Create student: `student_john` / `student123`
   - Create playlist: "Web Development Basics"
   - (Upload video if FFmpeg is installed)
   - Logout

3. **As Student** (`student_john`):
   - Login with new credentials
   - Browse playlists
   - Search for videos
   - Watch a video
   - Post a comment

4. **Back to Teacher** (`prof_smith`):
   - Login
   - Click "View Analytics"
   - See that `student_john` watched your video!

---

## ✅ Success Criteria

**You know it's working when:**
- ✅ You can access the ADS page
- ✅ You can login as admin, teacher, and student
- ✅ Admin can create/manage teachers
- ✅ Teachers can create/manage students
- ✅ Teachers can create playlists
- ✅ Students can browse and search content
- ✅ Comments work
- ✅ Analytics show view tracking
- ✅ All tests pass (run `python test_app.py`)

---

## 🎉 You're All Set!

The CampusPlayer is now fully functional. Start by logging in as admin and create your first teacher!

**Server URL**: http://127.0.0.1:5000

**Need help?** Check the TEST_RESULTS.md for detailed feature documentation.

---

© 2026 Vasanth V. — CampusPlayer. All Rights Reserved. See LICENSE.md.
