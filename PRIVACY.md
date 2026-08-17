# CampusPlayer — Privacy Policy

_Last updated: 2026_

This Privacy Policy describes what personal data CampusPlayer collects,
why, how it is stored, and the rights users have over their data. It
applies to any institution's deployment of the CampusPlayer platform.

## 1. What Data We Collect

Depending on your role (student, parent/guardian, teacher, or
administrator), CampusPlayer may collect:

- **Account data**: username, email address, password (stored as a
  salted hash, never in plain text)
- **Profile / bio data**: name, phone number, communication address
- **Parent/guardian data** (for student accounts): parent or guardian
  name, phone number, and email address, used for attendance and
  progress notifications
- **Academic data**: class enrollments, attendance records, quiz
  results, assignment submissions, video watch progress
- **Uploaded content**: videos, thumbnails, subtitles, and assignment
  files submitted by teachers or students
- **Technical data**: IP address and session metadata, used for
  security (e.g. rate limiting, audit logs) and to keep you signed in
- **Communication data**: email/SMS delivery logs when notifications
  (e.g. attendance or progress reports) are sent

## 2. Why We Collect It

- To authenticate users and maintain secure sessions
- To operate core features: video streaming, attendance tracking,
  quizzes, assignments, and progress reporting
- To send notifications to parents/guardians and teachers (email/SMS)
  where enabled by the institution
- To detect and prevent abuse (e.g. rate limiting, suspicious login
  activity)
- To generate reports (PDF/analytics) requested by teachers or
  administrators

## 3. How We Store It

- Data is stored in the institution's configured database (SQLite by
  default, PostgreSQL recommended for production)
- Passwords are hashed using Werkzeug's `generate_password_hash`
  (never stored in plain text)
- Sensitive stored values may be encrypted using a server-side
  encryption key (see `crypto_helper.py`); this key must be configured
  by the deploying institution and never committed to source control
- Uploaded videos are stored on the server's filesystem (or configured
  storage) and transcoded to HLS for streaming

## 4. Whether We Share It

CampusPlayer does not sell personal data. Data is shared only:

- Within the operating institution (e.g. a teacher can see their
  students' attendance and progress)
- With parents/guardians, for their own child's records
- With third-party services the institution explicitly configures
  (e.g. an SMTP provider for email, or the Gemini API for the optional
  AI assistant feature) — solely to provide that feature
- When required by law

## 5. User Rights

Users (or, for minors, their parent/guardian) may request:

- Access to the personal data held about them
- Correction of inaccurate data
- Deletion of their account and associated personal data, subject to
  academic record-keeping requirements the institution may have

Requests should be directed to the administrator of your institution's
CampusPlayer deployment.

## 6. Cookies & Sessions

CampusPlayer uses a session cookie to keep you signed in and a CSRF
token to protect against cross-site request forgery. These are
functionally required and are not used for advertising or third-party
tracking.

## 7. Children's Data

Because CampusPlayer is an educational platform, it may process data
belonging to minors (students) under the supervision of their school and
parents/guardians. Institutions deploying CampusPlayer are responsible
for obtaining any consent required under applicable law (e.g. parental
consent) before enrolling minors.

## 8. Changes to This Policy

This policy may be updated from time to time. Material changes will be
reflected in the "Last updated" date above.

## 9. Contact

For privacy questions or data requests, contact the administrator of
your CampusPlayer deployment, or the copyright owner directly.

See also: `COPYRIGHT.md`, `LICENSE.md`, `TERMS.md`, `NOTICE.md`.
