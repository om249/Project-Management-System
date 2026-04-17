import smtplib
import socket
import time
from email.mime.text import MIMEText
from flask import current_app


def send_email(to_email, subject, html_content):

    sender = current_app.config["MAIL_USERNAME"]
    password = current_app.config["MAIL_PASSWORD"]
    mail_server = current_app.config["MAIL_SERVER"] or "smtp.gmail.com"
    mail_port = current_app.config["MAIL_PORT"] or 587
    use_tls = current_app.config.get("MAIL_USE_TLS", True)
    smtp_timeout = current_app.config.get("MAIL_TIMEOUT", 15)
    retry_limit = current_app.config.get("MAIL_RETRY_LIMIT", 2)

    msg = MIMEText(html_content, "html")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_email

    last_error = None

    for attempt in range(1, retry_limit + 1):
        server = None
        try:
            server = smtplib.SMTP(mail_server, mail_port, timeout=smtp_timeout)
            server.ehlo()
            if use_tls:
                server.starttls()
                server.ehlo()
            server.login(sender, password)
            server.send_message(msg)
            server.quit()
            return True
        except (smtplib.SMTPException, OSError, socket.timeout) as e:
            last_error = e
            print(f"Email Error (attempt {attempt}/{retry_limit}):", e)

            if server:
                try:
                    server.quit()
                except Exception:
                    try:
                        server.close()
                    except Exception:
                        pass

            if attempt < retry_limit:
                time.sleep(1)

    return False


def email_shell(category, title, intro_html, body_html):
    return f"""
    <div style="max-width:760px;margin:0 auto;padding:32px;background:#f4f8ff;font-family:Segoe UI,sans-serif;color:#1e293b;">
        <div style="background:#ffffff;border-radius:20px;padding:32px;box-shadow:0 12px 28px rgba(15,23,42,0.08);">
            <p style="margin:0 0 10px;color:#1d4ed8;font-size:13px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;">{category}</p>
            <h2 style="margin:0 0 12px;color:#0f172a;">{title}</h2>
            <div style="margin:0 0 18px;line-height:1.7;color:#475569;">{intro_html}</div>
            <div style="line-height:1.7;color:#475569;">{body_html}</div>
            <p style="margin:24px 0 0;line-height:1.7;color:#475569;">
                Regards,<br><b>ZIBACAR Project Management Portal</b>
            </p>
        </div>
    </div>
    """


def student_welcome_email(name, prn, password):
    return email_shell(
        "ZIBACAR Student Onboarding",
        "Your student account is ready",
        f"Hello <b>{name}</b>, your account has been created successfully.",
        f"""
        <div style="padding:18px;border-radius:16px;background:linear-gradient(135deg,#eff6ff,#f8fafc);border:1px solid #dbeafe;">
            <p style="margin:0 0 8px;"><b>PRN:</b> {prn}</p>
            <p style="margin:0;"><b>Temporary Password:</b> {password}</p>
        </div>
        <p style="margin:18px 0 0;">
            Please log in to the portal, update your profile if needed, and begin tracking your project submissions.
        </p>
        """
    )


def faculty_welcome_email(name, email, password):
    return email_shell(
        "ZIBACAR Faculty Onboarding",
        "Your faculty account is ready",
        f"Hello <b>{name}</b>, your faculty account has been created successfully.",
        f"""
        <div style="padding:18px;border-radius:16px;background:linear-gradient(135deg,#eff6ff,#f8fafc);border:1px solid #dbeafe;">
            <p style="margin:0 0 8px;"><b>Username:</b> {email}</p>
            <p style="margin:0;"><b>Temporary Password:</b> {password}</p>
        </div>
        <p style="margin:18px 0 0;">
            Please log in to the portal, update your profile, and change your password from the profile settings.
        </p>
        """
    )


def mentor_assignment_email(mentor_name, batch_name, session_name=None, student_rows_html=""):
    session_line = f"<p><b>Academic Session:</b> {session_name}</p>" if session_name else ""

    if student_rows_html:
        student_section = f"""
        <div style="margin-top:24px;">
            <h3 style="margin:0 0 12px;color:#0f172a;">Assigned Students</h3>
            <table style="width:100%;border-collapse:collapse;background:#f8fbff;border-radius:12px;overflow:hidden;">
                <thead>
                    <tr style="background:#dbeafe;color:#1e3a8a;">
                        <th style="padding:12px;text-align:left;">PRN</th>
                        <th style="padding:12px;text-align:left;">Student Name</th>
                        <th style="padding:12px;text-align:left;">Email</th>
                    </tr>
                </thead>
                <tbody>
                    {student_rows_html}
                </tbody>
            </table>
        </div>
        """
    else:
        student_section = """
        <div style="margin-top:24px;padding:16px;border-radius:12px;background:#f8fafc;color:#475569;">
            No students are assigned to this batch yet. You will receive an updated roster once students are added.
        </div>
        """

    return email_shell(
        "ZIBACAR Faculty Assignment",
        "You have been assigned to a batch",
        f"Hello <b>{mentor_name}</b>, your mentor account has been linked to the batch <b>{batch_name}</b>.",
        f"""
        <div style="padding:18px;border-radius:16px;background:linear-gradient(135deg,#eff6ff,#f8fafc);border:1px solid #dbeafe;">
            <p style="margin:0 0 8px;"><b>Batch:</b> {batch_name}</p>
            {session_line}
            <p style="margin:0;"><b>Role:</b> Faculty Mentor</p>
        </div>
        {student_section}
        <p style="margin:24px 0 0;">
            Please log in to the portal to view submissions, deadlines, and the latest student activity.
        </p>
        """
    )


def student_mentor_assigned_email(student_name, mentor_name, mentor_email, batch_name=None, session_name=None):
    batch_line = f"<p style='margin:0 0 8px;'><b>Batch:</b> {batch_name}</p>" if batch_name else ""
    session_line = f"<p style='margin:0;'><b>Academic Session:</b> {session_name}</p>" if session_name else ""

    return email_shell(
        "ZIBACAR Student Update",
        "Your mentor has been assigned",
        f"Hello <b>{student_name}</b>, your project mentor is now <b>{mentor_name}</b>.",
        f"""
        <div style="padding:18px;border-radius:16px;background:linear-gradient(135deg,#eff6ff,#f8fafc);border:1px solid #dbeafe;">
            <p style="margin:0 0 8px;"><b>Mentor:</b> {mentor_name}</p>
            <p style="margin:0 0 8px;"><b>Email:</b> {mentor_email}</p>
            {batch_line}
            {session_line}
        </div>
        <p style="margin:24px 0 0;">
            Please log in to the portal to track deadlines, upload documents, and review any feedback shared by your mentor.
        </p>
        """
    )

# Progress report is stage
def submission_email(student, stage):
    return email_shell(
        "ZIBACAR Faculty Notification",
        "A new submission needs review",
        f"<b>{student}</b> has submitted work for the Progress Report <b>{stage}</b>.",
        """
        <div style="padding:18px;border-radius:16px;background:linear-gradient(135deg,#eff6ff,#f8fafc);border:1px solid #dbeafe;">
            Please log in to the faculty dashboard to review the file, check remarks, and update the submission status.
        </div>
        """
    )


def late_submission_email(student, stage):
    return email_shell(
        "ZIBACAR Deadline Alert",
        "Late submission detected",
        f"<b>{student}</b> submitted <b>{stage}</b> after the deadline.",
        """
        <div style="padding:18px;border-radius:16px;background:linear-gradient(135deg,#fff7ed,#fff1f2);border:1px solid #fdba74;color:#9a3412;">
            Please review the submission timeline in the portal and take any required academic action.
        </div>
        """
    )


def status_email(stage, status, remark=""):
    remark_html = (
        f"<p style='margin:18px 0 0;'><b>Mentor Remark:</b><br>{remark}</p>"
        if remark else
        "<p style='margin:18px 0 0;'><b>Mentor Remark:</b><br>No additional remark was provided.</p>"
    )
    status_color = "#166534" if str(status).lower() == "approved" else "#991b1b"

    return email_shell(
        "ZIBACAR Submission Update",
        f"Your submission was {status}",
        f"Your submission for <b>{stage}</b> has been marked as <b style='color:{status_color};'>{status}</b>.",
        f"""
        <div style="padding:18px;border-radius:16px;background:linear-gradient(135deg,#eff6ff,#f8fafc);border:1px solid #dbeafe;">
            Please review the update in the portal and make any required changes for the next Progress Report.
        </div>
        {remark_html}
        """
    )


def final_project_submission_email(student_name, project_title):
    return email_shell(
        "ZIBACAR Final Project",
        "A final project is ready for review",
        f"<b>{student_name}</b> submitted the final project <b>{project_title}</b>.",
        """
        <div style="padding:18px;border-radius:16px;background:linear-gradient(135deg,#eff6ff,#f8fafc);border:1px solid #dbeafe;">
            Please log in to the portal to download the project archive, open any shared repository or live link, and review the final submission.
        </div>
        """
    )


def final_project_status_email(project_title, status, remark=""):
    remark_html = (
        f"<p style='margin:18px 0 0;'><b>Mentor Remark:</b><br>{remark}</p>"
        if remark else
        "<p style='margin:18px 0 0;'><b>Mentor Remark:</b><br>No additional remark was provided.</p>"
    )
    status_color = "#166534" if str(status).lower() == "approved" else "#991b1b"

    return email_shell(
        "ZIBACAR Final Project",
        f"Your final project was {status}",
        f"Your final project <b>{project_title}</b> has been marked as <b style='color:{status_color};'>{status}</b>.",
        f"""
        <div style="padding:18px;border-radius:16px;background:linear-gradient(135deg,#eff6ff,#f8fafc);border:1px solid #dbeafe;">
            Please sign in to the portal to review the final decision and any mentor feedback.
        </div>
        {remark_html}
        """
    )


def progress_document_email(name, stage_name, session_name, document_name, action_label):
    return email_shell(
        "ZIBACAR Progress Report Resource",
        f"Progress Report document {action_label}",
        f"Hello <b>{name}</b>, a shared document for <b>{stage_name}</b> has been {action_label}.",
        f"""
        <div style="padding:18px;border-radius:16px;background:linear-gradient(135deg,#eff6ff,#f8fafc);border:1px solid #dbeafe;">
            <p style="margin:0 0 8px;"><b>Academic Session:</b> {session_name}</p>
            <p style="margin:0 0 8px;"><b>Progress Report:</b> {stage_name}</p>
            <p style="margin:0;"><b>Document:</b> {document_name}</p>
        </div>
        <p style="margin:24px 0 0;">
            Please log in to the portal and open the Progress Report section to view the latest reference file.
        </p>
        """
    )


def designation_update_email(name, designation_label, changed_by_name, previous_designation_label=None):
    previous_line = ""
    if previous_designation_label and previous_designation_label != designation_label:
        previous_line = f"<p style='margin:0 0 8px;'><b>Previous Designation:</b> {previous_designation_label}</p>"

    return email_shell(
        "ZIBACAR Access Update",
        "Your account designation has been updated",
        f"Hello <b>{name}</b>, your staff designation has been updated in the Project Management System.",
        f"""
        <div style="padding:18px;border-radius:16px;background:linear-gradient(135deg,#eff6ff,#f8fafc);border:1px solid #dbeafe;">
            {previous_line}
            <p style="margin:0 0 8px;"><b>New Designation:</b> {designation_label}</p>
            <p style="margin:0;"><b>Updated By:</b> {changed_by_name}</p>
        </div>
        <p style="margin:24px 0 0;">
            Please sign in again if needed to refresh your access and sidebar modules.
        </p>
        """
    )
