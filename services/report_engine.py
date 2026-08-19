import io
import json
from datetime import datetime, date, timedelta
from extensions import db
from models import (
    Classroom, User, Attendance, Quiz, QuizResult, ViewAnalytics,
    VideoProgress, Assignment, AssignmentSubmission, ClassWeeklyReport,
    Institution, SiteSettings
)
from attendance_utils import compute_attendance_stats, get_class_marked_dates

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, KeepTogether
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT


def get_current_week_bounds(target_date=None):
    """Return (monday, sunday) date objects for the week containing target_date (default: today)."""
    if target_date is None:
        target_date = datetime.utcnow().date()
    monday = target_date - timedelta(days=target_date.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def aggregate_class_weekly_data(classroom_id, period_start=None, period_end=None):
    """
    Consolidate multi-metric performance telemetry for all students in a classroom
    over the given period (defaulting to current/latest full week).
    """
    classroom = db.session.get(Classroom, classroom_id)
    if not classroom:
        return None

    if period_start is None or period_end is None:
        period_start, period_end = get_current_week_bounds()

    start_dt = datetime.combine(period_start, datetime.min.time())
    end_dt = datetime.combine(period_end, datetime.max.time())

    students = classroom.students.order_by(User.username.asc()).all()
    marked_dates = get_class_marked_dates(classroom_id, period_start, period_end)

    # Class-associated videos and quizzes
    class_video_ids = [v.id for v in classroom.videos]
    class_quizzes = Quiz.query.filter_by(classroom_id=classroom_id).all()
    class_quiz_ids = [q.id for q in class_quizzes]
    class_assignments = Assignment.query.filter_by(classroom_id=classroom_id).all()
    class_assignment_ids = [a.id for a in class_assignments]

    student_rows = []
    total_class_xp = 0
    total_attendance_pct_sum = 0
    valid_attendance_count = 0
    total_quiz_pct_sum = 0
    valid_quiz_count = 0
    total_watch_seconds = 0
    at_risk_students = []

    for s in students:
        level = (s.xp // 500) + 1 if s.xp is not None else 1
        total_xp = s.xp or 0

        # Attendance Stats
        att_stats = compute_attendance_stats(
            student_id=s.id,
            classroom_id=classroom_id,
            start_date=period_start,
            end_date=period_end,
            marked_dates=marked_dates
        )
        att_pct = att_stats.get('percentage')
        if att_pct is not None:
            total_attendance_pct_sum += att_pct
            valid_attendance_count += 1
            att_display = f"{int(round(att_pct))}%"
        else:
            att_display = "100%" if not marked_dates else "—"
            att_pct = 100.0 if not marked_dates else 0.0

        # Quiz Mastery in period
        q_results = QuizResult.query.filter(
            QuizResult.student_id == s.id,
            QuizResult.quiz_id.in_(class_quiz_ids) if class_quiz_ids else db.text('1=0'),
            QuizResult.timestamp >= start_dt,
            QuizResult.timestamp <= end_dt
        ).all() if class_quiz_ids else []

        quizzes_taken = len(q_results)
        if quizzes_taken > 0:
            avg_quiz_score = sum((r.score * 100.0 / r.total_questions) if r.total_questions and r.total_questions > 0 else 0.0 for r in q_results) / quizzes_taken
            total_quiz_pct_sum += avg_quiz_score
            valid_quiz_count += 1
            quiz_display = f"{int(round(avg_quiz_score))}%"
        else:
            avg_quiz_score = None
            quiz_display = "—"

        # Video Watch Telemetry in period
        view_recs = ViewAnalytics.query.filter(
            ViewAnalytics.user_id == s.id,
            ViewAnalytics.video_id.in_(class_video_ids) if class_video_ids else db.text('1=0'),
            ViewAnalytics.start_time >= start_dt,
            ViewAnalytics.start_time <= end_dt
        ).all() if class_video_ids else []

        s_watch_secs = sum(v.duration_seconds for v in view_recs if v.duration_seconds)
        total_watch_seconds += s_watch_secs
        watch_hours_str = f"{(s_watch_secs / 3600):.1f}h" if s_watch_secs >= 3600 else f"{int(s_watch_secs // 60)}m"

        # Assignment Submissions in period
        sub_recs = AssignmentSubmission.query.filter(
            AssignmentSubmission.student_id == s.id,
            AssignmentSubmission.assignment_id.in_(class_assignment_ids) if class_assignment_ids else db.text('1=0'),
            AssignmentSubmission.submitted_at >= start_dt,
            AssignmentSubmission.submitted_at <= end_dt
        ).all() if class_assignment_ids else []
        assignments_done = len(sub_recs)

        # Health & Performance Standing
        health_status = 'excellent'
        health_label = 'Honor Scholar'
        health_color = '#3d8fa3'

        is_low_att = att_pct is not None and att_pct < 75.0 and len(marked_dates) > 0
        is_low_quiz = avg_quiz_score is not None and avg_quiz_score < 50.0

        if is_low_att and is_low_quiz:
            health_status = 'critical'
            health_label = 'High Risk'
            health_color = '#d9822b'
            at_risk_students.append({
                'id': s.id,
                'name': s.name,
                'reason': 'Low Attendance & Low Quiz Score',
                'attendance_pct': att_display,
                'quiz_pct': quiz_display
            })
        elif is_low_att:
            health_status = 'attention'
            health_label = 'Attendance Risk'
            health_color = '#d9822b'
            at_risk_students.append({
                'id': s.id,
                'name': s.name,
                'reason': f'Attendance below 75% ({att_display})',
                'attendance_pct': att_display,
                'quiz_pct': quiz_display
            })
        elif is_low_quiz:
            health_status = 'attention'
            health_label = 'Academic Support'
            health_color = '#d9822b'
            at_risk_students.append({
                'id': s.id,
                'name': s.name,
                'reason': f'Quiz Mastery below 50% ({quiz_display})',
                'attendance_pct': att_display,
                'quiz_pct': quiz_display
            })
        elif att_pct is not None and att_pct >= 90.0 and (avg_quiz_score is None or avg_quiz_score >= 80.0):
            health_status = 'excellent'
            health_label = 'Honor Scholar'
            health_color = '#3d8fa3'
        else:
            health_status = 'good'
            health_label = 'Good Standing'
            health_color = '#2541b2'

        student_rows.append({
            'student_id': s.id,
            'username': s.name,
            'name': s.name,
            'level': level,
            'total_xp': total_xp,
            'attendance_pct': att_pct if att_pct is not None else 100.0,
            'attendance_display': att_display,
            'present_days': att_stats.get('Present', 0),
            'absent_days': att_stats.get('Absent', 0),
            'late_days': att_stats.get('Late', 0),
            'quizzes_taken': quizzes_taken,
            'avg_quiz_score': avg_quiz_score,
            'quiz_display': quiz_display,
            'watch_seconds': s_watch_secs,
            'watch_display': watch_hours_str,
            'assignments_submitted': assignments_done,
            'health_status': health_status,
            'health_label': health_label,
            'health_color': health_color
        })
        total_class_xp += total_xp

    total_students = len(students)
    avg_class_att = (total_attendance_pct_sum / valid_attendance_count) if valid_attendance_count > 0 else 100.0
    avg_class_quiz = (total_quiz_pct_sum / valid_quiz_count) if valid_quiz_count > 0 else 0.0

    return {
        'classroom_id': classroom.id,
        'classroom_name': classroom.name,
        'teacher_name': classroom.teacher.name if classroom.teacher else 'Faculty',
        'period_start': period_start.strftime('%Y-%m-%d'),
        'period_end': period_end.strftime('%Y-%m-%d'),
        'period_label': f"{period_start.strftime('%b %d')} – {period_end.strftime('%b %d, %Y')}",
        'total_students': total_students,
        'avg_attendance_pct': round(avg_class_att, 1),
        'avg_quiz_score_pct': round(avg_class_quiz, 1),
        'total_xp': total_class_xp,
        'total_watch_seconds': total_watch_seconds,
        'total_watch_hours': round(total_watch_seconds / 3600, 1),
        'students': student_rows,
        'at_risk_students': at_risk_students,
    }


def generate_or_get_weekly_report(classroom_id, teacher_id, period_start=None, period_end=None, remarks=None):
    """
    Fetch existing or compile fresh ClassWeeklyReport for classroom and period.
    """
    if period_start is None or period_end is None:
        period_start, period_end = get_current_week_bounds()

    # Look for existing report
    report = ClassWeeklyReport.query.filter_by(
        classroom_id=classroom_id,
        period_start=period_start,
        period_end=period_end
    ).first()

    data = aggregate_class_weekly_data(classroom_id, period_start, period_end)
    if not data:
        return None

    classroom = db.session.get(Classroom, classroom_id)
    inst_id = classroom.institution_id if classroom else None

    if not report:
        report = ClassWeeklyReport(
            institution_id=inst_id,
            classroom_id=classroom_id,
            teacher_id=teacher_id,
            period_start=period_start,
            period_end=period_end,
            total_students=data['total_students'],
            avg_attendance_pct=data['avg_attendance_pct'],
            total_xp_gained=data['total_xp'],
            avg_quiz_score_pct=data['avg_quiz_score_pct'],
            total_video_watch_seconds=data['total_watch_seconds'],
            teacher_remarks=remarks or f"Weekly performance digest for {data['classroom_name']}.",
            status='generated'
        )
        report.set_report_data(data)
        db.session.add(report)
    else:
        if not report.institution_id and inst_id:
            report.institution_id = inst_id
        report.total_students = data['total_students']
        report.avg_attendance_pct = data['avg_attendance_pct']
        report.total_xp_gained = data['total_xp']
        report.avg_quiz_score_pct = data['avg_quiz_score_pct']
        report.total_video_watch_seconds = data['total_watch_seconds']
        if remarks:
            report.teacher_remarks = remarks
        report.set_report_data(data)
        report.total_xp_gained = data['total_xp']
        report.avg_quiz_score_pct = data['avg_quiz_score_pct']
        report.total_video_watch_seconds = data['total_watch_seconds']
        if remarks:
            report.teacher_remarks = remarks
        report.set_report_data(data)

    db.session.commit()
    return report


def build_weekly_report_pdf(report):
    """
    Generate an executive, print-ready ReportLab PDF document for a ClassWeeklyReport.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=14*mm,
        rightMargin=14*mm,
        topMargin=16*mm,
        bottomMargin=16*mm
    )

    data = report.get_report_data()
    inst_name = "Campus Player Academic Portal"
    inst_id = report.institution_id or (report.classroom.institution_id if report.classroom else None)
    if inst_id:
        inst = db.session.get(Institution, inst_id)
        if inst and inst.name:
            inst_name = inst.name

    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle(
        'DocTitle',
        fontName='Helvetica-Bold',
        fontSize=18,
        leading=22,
        textColor=colors.HexColor('#0f172a'),
        alignment=TA_LEFT
    )
    sub_style = ParagraphStyle(
        'DocSub',
        fontName='Helvetica',
        fontSize=9,
        leading=13,
        textColor=colors.HexColor('#64748b'),
        alignment=TA_LEFT
    )
    section_title = ParagraphStyle(
        'SecTitle',
        fontName='Helvetica-Bold',
        fontSize=11,
        leading=15,
        textColor=colors.HexColor('#0f172a'),
        alignment=TA_LEFT
    )
    kpi_num = ParagraphStyle(
        'KpiNum',
        fontName='Helvetica-Bold',
        fontSize=14,
        leading=16,
        textColor=colors.HexColor('#d4af37'),
        alignment=TA_CENTER
    )
    kpi_lbl = ParagraphStyle(
        'KpiLbl',
        fontName='Helvetica-Bold',
        fontSize=7.5,
        leading=10,
        textColor=colors.HexColor('#64748b'),
        alignment=TA_CENTER
    )
    tbl_hdr = ParagraphStyle(
        'TblHdr',
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=10,
        textColor=colors.white,
        alignment=TA_CENTER
    )
    tbl_cell = ParagraphStyle(
        'TblCell',
        fontName='Helvetica',
        fontSize=8,
        leading=10,
        textColor=colors.HexColor('#1e293b'),
        alignment=TA_CENTER
    )
    tbl_cell_left = ParagraphStyle(
        'TblCellLeft',
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=10,
        textColor=colors.HexColor('#0f172a'),
        alignment=TA_LEFT
    )

    story = []

    # 1. Header with branding
    story.append(Paragraph(f"<b>{inst_name.upper()}</b>", sub_style))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph("Weekly Classroom Performance &amp; XP Digest", title_style))
    meta_text = (
        f"<b>Class:</b> {data.get('classroom_name', 'Class')} &nbsp;|&nbsp; "
        f"<b>Faculty In-Charge:</b> {data.get('teacher_name', 'Faculty')} &nbsp;|&nbsp; "
        f"<b>Period:</b> {data.get('period_label', '')} &nbsp;|&nbsp; "
        f"<b>Generated:</b> {report.generated_at.strftime('%b %d, %Y %I:%M %p')}"
    )
    story.append(Paragraph(meta_text, sub_style))
    story.append(Spacer(1, 4*mm))
    story.append(HRFlowable(width='100%', thickness=2, color=colors.HexColor('#d4af37')))
    story.append(Spacer(1, 6*mm))

    # 2. KPI Stat Cards (4 columns)
    kpi_data = [
        [
            Paragraph(f"{data.get('avg_attendance_pct', 0)}%", kpi_num),
            Paragraph(f"{data.get('total_xp', 0):,} XP", kpi_num),
            Paragraph(f"{data.get('avg_quiz_score_pct', 0)}%", kpi_num),
            Paragraph(f"{data.get('total_watch_hours', 0)}h", kpi_num)
        ],
        [
            Paragraph("AVG ATTENDANCE", kpi_lbl),
            Paragraph("TOTAL CLASS XP", kpi_lbl),
            Paragraph("AVG QUIZ SCORE", kpi_lbl),
            Paragraph("VIDEO STREAMED", kpi_lbl)
        ]
    ]
    kpi_table = Table(kpi_data, colWidths=[45*mm, 45*mm, 45*mm, 45*mm])
    kpi_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#f8fafc')),
        ('BOX', (0,0), (-1,-1), 1, colors.HexColor('#e2e8f0')),
        ('INNERGRID', (0,0), (-1,-1), 0.5, colors.HexColor('#e2e8f0')),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    story.append(kpi_table)
    story.append(Spacer(1, 6*mm))

    # 3. At-Risk / Attention Required Callout (if any)
    at_risk = data.get('at_risk_students', [])
    if at_risk:
        story.append(Paragraph("<b>Attention Required — Scholars Needing Intervention</b>", section_title))
        story.append(Spacer(1, 2*mm))
        risk_rows = [[
            Paragraph("Scholar Name", tbl_hdr),
            Paragraph("Reason / Alert", tbl_hdr),
            Paragraph("Attendance", tbl_hdr),
            Paragraph("Quiz Avg", tbl_hdr)
        ]]
        for r in at_risk:
            risk_rows.append([
                Paragraph(r['name'], tbl_cell_left),
                Paragraph(r['reason'], tbl_cell_left),
                Paragraph(r['attendance_pct'], tbl_cell),
                Paragraph(r['quiz_pct'], tbl_cell)
            ])
        risk_table = Table(risk_rows, colWidths=[45*mm, 75*mm, 30*mm, 30*mm])
        risk_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#d9822b')),
            ('BACKGROUND', (0,1), (-1,-1), colors.HexColor('#fff7ed')),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#fed7aa')),
            ('TOPPADDING', (0,0), (-1,-1), 4),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ]))
        story.append(risk_table)
        story.append(Spacer(1, 6*mm))

    # 4. Consolidated Student Roster Table
    story.append(Paragraph("<b>Consolidated Scholar Performance Roster</b>", section_title))
    story.append(Spacer(1, 2*mm))

    roster_rows = [[
        Paragraph("#", tbl_hdr),
        Paragraph("Scholar Name", tbl_hdr),
        Paragraph("Level", tbl_hdr),
        Paragraph("Total XP", tbl_hdr),
        Paragraph("Attendance", tbl_hdr),
        Paragraph("Quizzes", tbl_hdr),
        Paragraph("Watch Time", tbl_hdr),
        Paragraph("Status", tbl_hdr),
    ]]

    students_list = data.get('students', [])
    for idx, s in enumerate(students_list, 1):
        roster_rows.append([
            Paragraph(str(idx), tbl_cell),
            Paragraph(s['username'], tbl_cell_left),
            Paragraph(f"Lvl {s['level']}", tbl_cell),
            Paragraph(f"{s['total_xp']:,}", tbl_cell),
            Paragraph(f"{s['attendance_display']}", tbl_cell),
            Paragraph(f"{s['quiz_display']} ({s['quizzes_taken']})", tbl_cell),
            Paragraph(s['watch_display'], tbl_cell),
            Paragraph(s['health_label'], tbl_cell),
        ])

    roster_table = Table(roster_rows, colWidths=[10*mm, 42*mm, 18*mm, 24*mm, 26*mm, 26*mm, 20*mm, 24*mm])
    roster_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#0f172a')),
        ('GRID', (0,0), (-1,-1), 0.4, colors.HexColor('#cbd5e1')),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f8fafc')]),
    ]))
    story.append(roster_table)
    story.append(Spacer(1, 6*mm))

    # 5. Teacher Commentary & Remarks
    if report.teacher_remarks:
        story.append(KeepTogether([
            Paragraph("<b>Faculty Executive Remarks:</b>", section_title),
            Spacer(1, 2*mm),
            Paragraph(f"<i>{report.teacher_remarks}</i>", sub_style),
            Spacer(1, 6*mm)
        ]))

    # 6. Signatures & Footer
    footer_data = [
        [
            Paragraph(f"<b>Faculty In-Charge:</b> {data.get('teacher_name', 'Teacher')}<br/>Signature: _______________________", sub_style),
            Paragraph("<b>Principal / Academic Dean:</b><br/>Signature &amp; Seal: _______________________", sub_style)
        ]
    ]
    footer_table = Table(footer_data, colWidths=[90*mm, 90*mm])
    footer_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('TOPPADDING', (0,0), (-1,-1), 10),
    ]))
    story.append(KeepTogether([
        HRFlowable(width='100%', thickness=1, color=colors.HexColor('#e2e8f0')),
        Spacer(1, 4*mm),
        footer_table,
        Spacer(1, 6*mm),
        Paragraph("Generated automatically by Campus Player — Institutional Learning &amp; Video Management System", ParagraphStyle('FootNote', fontName='Helvetica', fontSize=7.5, textColor=colors.HexColor('#94a3b8'), alignment=TA_CENTER))
    ]))

    doc.build(story)
    buf.seek(0)
    return buf
