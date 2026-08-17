import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr, formatdate, make_msgid
from datetime import datetime
from flask import request, has_request_context, render_template
from extensions import db, mail
from models import User, Classroom, student_classes
from crypto_helper import decrypt_password


def send_profile_email_confirmation(student, old_email, new_email):
    teacher = None
    class_name = 'Campus Player'
    support_contact = 'support@campusplayer.com'
    enrollment = db.session.execute(
        student_classes.select().where(student_classes.c.student_id == student.id)
    ).first()
    if enrollment:
        cls = Classroom.query.get(enrollment.classroom_id)
        if cls:
            class_name = cls.name
            t = User.query.get(cls.teacher_id)
            if t:
                support_contact = t.email_sender_address or support_contact
                if t.email_sender_address and t.encrypted_app_password and t.email_enabled:
                    teacher = t

    login_link = (request.url_root.rstrip('/') if has_request_context() else 'http://127.0.0.1:5000') + '/login'
    changed_at = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    remote_ip = request.remote_addr if has_request_context() else 'Unknown'
    browser_info = request.user_agent.string if has_request_context() else 'Unknown'
    teacher_name = teacher.display_name or teacher.username if teacher else 'Campus Player System'
    teacher_contact = f"{teacher_name} <{teacher.email_sender_address}>" if teacher and teacher.email_sender_address else support_contact

    body_text = (
        f"Campus Player - Email Address Updated\n"
        f"\n"
        f"Hello {student.display_name or student.username},\n"
        f"\n"
        f"Your Campus Player email address has been updated.\n"
        f"Previous Email: {old_email or '(not set)'}\n"
        f"Updated To: {new_email}\n"
        f"Account Username: {student.username}\n"
        f"Student ID: CP-{student.id:04d}\n"
        f"Class: {class_name}\n"
        f"Changed At: {changed_at}\n"
        f"IP Address: {remote_ip}\n"
        f"Browser: {browser_info}\n"
        f"\n"
        f"If you did not authorize this change, contact {teacher_contact} immediately.\n"
        f"Login: {login_link}\n"
    )

    try:
        body_html = render_template(
            'email_profile_update.html',
            student_name=student.display_name or student.username,
            account_username=student.username,
            student_id=f"{student.id:04d}",
            old_email=old_email or '(not set)',
            new_email=new_email,
            class_name=class_name,
            changed_at=changed_at,
            remote_ip=remote_ip,
            browser_info=browser_info,
            login_link=login_link,
            teacher_name=teacher_name,
            teacher_contact=teacher_contact,
            support_contact=support_contact,
            notification_type='Email Address Update'
        )
    except Exception:
        body_html = (
            f"<p>Hello {student.display_name or student.username},<br>"
            f"Your Campus Player email address has been updated from <strong>{old_email or 'none'}</strong> to <strong>{new_email}</strong> on {changed_at}.<br><br>"
            f"If you did not make this change, contact {support_contact} immediately and change your password in Campus Player.</p>"
        )

    subject = 'Campus Player - Email Address Updated'

    if teacher:
        sender = teacher.email_sender_address
        decrypted_pw = decrypt_password(teacher.encrypted_app_password)

        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = formataddr((teacher.display_name or teacher.username, sender))
        msg['To'] = formataddr((student.display_name or student.username, new_email))
        msg['Date'] = formatdate(localtime=True)
        msg['Message-ID'] = make_msgid()
        msg.attach(MIMEText(body_text, 'plain', 'utf-8'))
        msg.attach(MIMEText(body_html, 'html', 'utf-8'))

        server = smtplib.SMTP('smtp.gmail.com', 587, timeout=15)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(sender, decrypted_pw)
        server.send_message(msg)
        server.quit()
        return True

    try:
        from flask_mail import Message as MailMessage
        msg_fm = MailMessage(subject=subject, recipients=[new_email], body=body_text, html=body_html)
        mail.send(msg_fm)
        return True
    except Exception:
        return False
