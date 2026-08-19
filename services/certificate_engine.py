import io
import json
import uuid
from datetime import datetime
from extensions import db
from models import AcademicCertificate, User, Institution

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, KeepTogether
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT


def issue_academic_certificate(student_id, title, description, cert_type='course_completion', criteria=None, institution_id=None):
    """
    Generate and persist a verifiable academic certificate for a student.
    """
    student = db.session.get(User, student_id)
    if not student:
        return None

    if institution_id is None:
        institution_id = student.institution_id

    code = f"CP-CERT-{datetime.utcnow().year}-{uuid.uuid4().hex[:8].upper()}"

    cert = AcademicCertificate(
        institution_id=institution_id,
        certificate_code=code,
        student_id=student.id,
        title=title,
        description=description,
        certificate_type=cert_type,
        criteria_met_json=json.dumps(criteria or {})
    )
    db.session.add(cert)
    db.session.commit()
    return cert


def build_certificate_pdf(cert, base_url=''):
    """
    Generate a high-resolution, print-ready landscape Certificate PDF in Cyber-Gold styling.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=20*mm,
        rightMargin=20*mm,
        topMargin=20*mm,
        bottomMargin=20*mm
    )

    inst_name = "Campus Player Academic Portal"
    if cert.institution_id:
        inst = db.session.get(Institution, cert.institution_id)
        if inst and inst.name:
            inst_name = inst.name

    styles = getSampleStyleSheet()

    cert_heading = ParagraphStyle(
        'CertHdr',
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=16,
        textColor=colors.HexColor('#d4af37'),
        alignment=TA_CENTER
    )
    cert_title = ParagraphStyle(
        'CertTitle',
        fontName='Helvetica-Bold',
        fontSize=28,
        leading=34,
        textColor=colors.HexColor('#0f172a'),
        alignment=TA_CENTER
    )
    cert_sub = ParagraphStyle(
        'CertSub',
        fontName='Helvetica',
        fontSize=11,
        leading=16,
        textColor=colors.HexColor('#64748b'),
        alignment=TA_CENTER
    )
    student_name_style = ParagraphStyle(
        'StudentName',
        fontName='Helvetica-Bold',
        fontSize=24,
        leading=30,
        textColor=colors.HexColor('#d4af37'),
        alignment=TA_CENTER
    )
    desc_style = ParagraphStyle(
        'CertDesc',
        fontName='Helvetica',
        fontSize=10.5,
        leading=16,
        textColor=colors.HexColor('#334155'),
        alignment=TA_CENTER
    )
    sig_style = ParagraphStyle(
        'SigStyle',
        fontName='Helvetica',
        fontSize=8.5,
        leading=12,
        textColor=colors.HexColor('#64748b'),
        alignment=TA_CENTER
    )

    story = []

    # Outer border flowables
    story.append(Paragraph(f"<b>{inst_name.upper()}</b>", cert_heading))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph("CERTIFICATE OF ACADEMIC ACHIEVEMENT", cert_title))
    story.append(Spacer(1, 3*mm))
    story.append(HRFlowable(width='60%', thickness=2, color=colors.HexColor('#d4af37')))
    story.append(Spacer(1, 4*mm))

    story.append(Paragraph("THIS CERTIFICATE IS PROUDLY CONFERRED UPON", cert_sub))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(f"<u>{cert.student.name.upper()}</u>", student_name_style))
    story.append(Spacer(1, 4*mm))

    story.append(Paragraph(f"in recognition of outstanding academic performance and successful completion of <b>{cert.title}</b>.", desc_style))
    if cert.description:
        story.append(Spacer(1, 2*mm))
        story.append(Paragraph(f"<i>{cert.description}</i>", desc_style))

    story.append(Spacer(1, 8*mm))

    # Signatures and Verification block
    verify_url = f"{base_url}/certificates/verify/{cert.certificate_code}" if base_url else f"CampusPlayer Code: {cert.certificate_code}"
    
    footer_data = [
        [
            Paragraph("<b>Faculty Instructor</b><br/>Campus Player Academic Faculty", sig_style),
            Paragraph(f"<b>Verified Academic Credential</b><br/>Code: <font color='#d4af37'><b>{cert.certificate_code}</b></font><br/>Issued: {cert.issued_at.strftime('%B %d, %Y')}", sig_style),
            Paragraph("<b>Academic Dean / Principal</b><br/>Office of Academic Affairs", sig_style)
        ]
    ]
    footer_table = Table(footer_data, colWidths=[80*mm, 90*mm, 80*mm])
    footer_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('LINEABOVE', (0,0), (0,0), 1, colors.HexColor('#94a3b8')),
        ('LINEABOVE', (2,0), (2,0), 1, colors.HexColor('#94a3b8')),
    ]))
    story.append(footer_table)

    story.append(Spacer(1, 4*mm))
    story.append(Paragraph(f"To verify the authenticity of this official certificate, visit: {verify_url}", ParagraphStyle('VerifyNote', fontName='Helvetica', fontSize=7.5, textColor=colors.HexColor('#94a3b8'), alignment=TA_CENTER)))

    doc.build(story)
    buf.seek(0)
    return buf
