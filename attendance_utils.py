"""
Attendance calculation helpers.

Centralises the attendance-percentage rules so every report (teacher session
report, admin session report, student dashboard) uses the exact same logic:

  - Present          -> counts 100% towards the percentage
  - Late / Half Day  -> counts 50% towards the percentage
  - Absent           -> counts 0% towards the percentage
  - Holiday / Medical Leave / OD -> "neutral": excluded from the percentage
    calculation entirely (they don't move the percentage up or down)
  - Any day within range where NO attendance was taken for the whole class
    at all is treated as a Holiday (teacher never opened attendance that day)
  - Any day where the class *did* take attendance but a particular student
    was never marked is treated as Absent for that student.

"Current percentage" for a session means the percentage calculated from the
session's fixed starting date up to today (or the session's ending date,
whichever is earlier) — i.e. the percentage as it stands right now, even
before the session has formally ended.
"""
from datetime import datetime, timedelta

from extensions import db
from models import Attendance


def _daterange(start_date, end_date):
    """Yield every date from start_date to end_date inclusive."""
    if not start_date or not end_date or end_date < start_date:
        return
    days = (end_date - start_date).days
    for i in range(days + 1):
        yield start_date + timedelta(days=i)


def get_class_marked_dates(classroom_id, start_date, end_date):
    """Return the set of dates within [start_date, end_date] on which at
    least one Attendance record exists for this classroom — i.e. dates the
    teacher actually took attendance for the class."""
    if not start_date or not end_date:
        return set()
    rows = (
        db.session.query(Attendance.date)
        .filter(
            Attendance.classroom_id == classroom_id,
            Attendance.date >= start_date,
            Attendance.date <= end_date,
        )
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


def compute_attendance_stats(student_id, classroom_id, start_date, end_date,
                              marked_dates=None, records_by_date=None):
    """Compute attendance stats for one student in one classroom over a date
    range (inclusive). Days where the teacher never took attendance for the
    class are treated as Holiday. Days where the class took attendance but
    this particular student wasn't marked are treated as Absent.

    Returns a dict with per-status counts, countable_days, weighted_present,
    and percentage (None if there were no countable days at all).
    """
    today = datetime.utcnow().date()
    end_date = min(end_date, today) if end_date else today

    if marked_dates is None:
        marked_dates = get_class_marked_dates(classroom_id, start_date, end_date)

    if records_by_date is None:
        rows = Attendance.query.filter(
            Attendance.student_id == student_id,
            Attendance.classroom_id == classroom_id,
            Attendance.date >= start_date,
            Attendance.date <= end_date,
        ).all()
        records_by_date = {r.date: r.status for r in rows}

    counts = {
        'Present': 0, 'Absent': 0, 'Late': 0, 'Half Day': 0,
        'Holiday': 0, 'Medical Leave': 0, 'OD': 0, 'Leave': 0,
    }
    total_days = 0
    for d in _daterange(start_date, end_date):
        total_days += 1
        status = records_by_date.get(d)
        if not status:
            status = 'Absent' if d in marked_dates else 'Holiday'
        if status not in counts:
            status = 'Absent'
        counts[status] += 1

    countable_days = counts['Present'] + counts['Absent'] + counts['Late'] + counts['Half Day'] + counts['Leave']
    weighted_present = (
        counts['Present'] * Attendance.PERCENTAGE_WEIGHTS['Present']
        + counts['Late'] * Attendance.PERCENTAGE_WEIGHTS['Late']
        + counts['Half Day'] * Attendance.PERCENTAGE_WEIGHTS['Half Day']
        + counts['Absent'] * Attendance.PERCENTAGE_WEIGHTS['Absent']
        + counts['Leave'] * Attendance.PERCENTAGE_WEIGHTS['Leave']
    )
    percentage = (weighted_present / countable_days * 100) if countable_days > 0 else None

    return {
        'counts': counts,
        'total_days': total_days,
        'countable_days': countable_days,
        'neutral_days': counts['Holiday'] + counts['Medical Leave'] + counts['OD'],
        'weighted_present': weighted_present,
        'percentage': round(percentage, 1) if percentage is not None else None,
    }


def compute_session_report(session, settings=None):
    """Build a full attendance report for an AttendanceSession: every
    enrolled student's overall percentage (start_date -> end_date) and their
    "current" percentage (start_date -> today, capped at end_date).
    """
    classroom = session.classroom
    students = list(classroom.students) if classroom else []
    today = datetime.utcnow().date()
    current_end = min(session.end_date, today)

    full_marked = get_class_marked_dates(classroom.id, session.start_date, session.end_date)
    current_marked = full_marked if current_end == session.end_date else \
        {d for d in full_marked if d <= current_end}

    min_pct = getattr(settings, 'min_attendance_percentage', 75.0) if settings else 75.0
    if min_pct is None:
        min_pct = 75.0

    # Batch query: fetch all attendance records for this classroom in the session date range in 1 single query
    all_attendance_rows = Attendance.query.filter(
        Attendance.classroom_id == classroom.id,
        Attendance.date >= session.start_date,
        Attendance.date <= session.end_date,
    ).all()
    records_by_student = {}
    for r in all_attendance_rows:
        records_by_student.setdefault(r.student_id, {})[r.date] = r.status

    rows = []
    for student in students:
        student_records = records_by_student.get(student.id, {})
        full_stats = compute_attendance_stats(
            student.id, classroom.id, session.start_date, session.end_date,
            marked_dates=full_marked, records_by_date=student_records)
        current_stats = compute_attendance_stats(
            student.id, classroom.id, session.start_date, current_end,
            marked_dates=current_marked, records_by_date=student_records)
        pct = full_stats['percentage']
        cur_pct = current_stats['percentage']
        rows.append({
            'student': student,
            'full': full_stats,
            'current': current_stats,
            'percentage': pct,
            'current_percentage': cur_pct,
            'below_minimum': (cur_pct is not None and cur_pct < min_pct),
        })

    rows.sort(key=lambda r: (r['current_percentage'] if r['current_percentage'] is not None else -1), reverse=True)
    return {
        'session': session,
        'classroom': classroom,
        'rows': rows,
        'min_attendance_percentage': min_pct,
        'current_end_date': current_end,
    }


def compute_overall_attendance_for_student(user):
    """Aggregate 'current' attendance percentage across every class a
    student is enrolled in, using each class's earliest attendance session
    (falling back to the earliest attendance record) as the starting point.
    Used for the simple attendance summary shown on the student dashboard.
    """
    classes = list(getattr(user, 'enrolled_classes', []) or [])
    if not classes:
        return None

    today = datetime.utcnow().date()
    total_weighted = 0.0
    total_countable = 0
    for cls in classes:
        sessions = sorted(getattr(cls, 'attendance_sessions', []) or [], key=lambda s: s.start_date)
        if sessions:
            start_date = sessions[0].start_date
            end_date = min(max(s.end_date for s in sessions), today)
        else:
            first_record = Attendance.query.filter_by(
                student_id=user.id, classroom_id=cls.id
            ).order_by(Attendance.date.asc()).first()
            if not first_record:
                continue
            start_date = first_record.date
            end_date = today
        stats = compute_attendance_stats(user.id, cls.id, start_date, end_date)
        total_weighted += stats['weighted_present']
        total_countable += stats['countable_days']

    if total_countable == 0:
        return None
    return round((total_weighted / total_countable) * 100)
