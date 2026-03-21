import smtplib
from email.mime.text import MIMEText
from bson import ObjectId
from flask import current_app


def send_email(to_email, subject, html_content):

    sender = current_app.config["MAIL_USERNAME"]
    password = current_app.config["MAIL_PASSWORD"]

    print("MAIL",sender)
    print("PASS",password)

    msg = MIMEText(html_content, "html")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_email

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(sender, password)
        server.send_message(msg)
        server.quit()
    except Exception as e:
        print("Email Error:", e)




def student_welcome_email(name, prn, password):
    return f"""
    <h2>Welcome to ZIBACAR Portal</h2>
    <p>Hello <b>{name}</b>,</p>

    <p>Your account has been created.</p>

    <ul>
        <li><b>PRN:</b> {prn}</li>
        <li><b>Password:</b> {password}</li>
    </ul>

    <p>Please login and start your project submissions.</p>

    <br>
    <p>Regards,<br>ZIBACAR</p>
    """

def submission_email(student, stage):
    return f"""
    <h3>New Submission</h3>

    <p><b>{student}</b> has submitted <b>{stage}</b>.</p>

    <p>Please review it in the system.</p>
    """

def late_submission_email(student, stage):
    return f"""
    <h3>Late Submission Alert</h3>

    <p><b>{student}</b> submitted <b>{stage}</b> after deadline.</p>
    """

def status_email(stage, status, remark=""):
    return f"""
    <h3>Submission Update</h3>

    <p>Your submission for <b>{stage}</b> is <b>{status}</b>.</p>

    <p><b>Remark:</b> {remark}</p>
    """

def assign_mentor():
    # 🔥 SEND EMAIL TO ALL STUDENTS IN BATCH
    students = current_app.db.students.find({"batch_id": ObjectId(batch_id)})

    mentor = current_app.db.users.find_one({"_id": ObjectId(mentor_id)})

    for s in students:
        if s.get("email"):
            try:
                send_email(
                    s["email"],
                    "Mentor Assigned",
                    f"""
                    <h3>Mentor Assigned</h3>
                    <p>Hello {s['name']},</p>
                    <p>Your mentor is <b>{mentor['name']}</b></p>
                    <p>Email: {mentor['email']}</p>
                    """
                )
            except Exception as e:
                print("Email error:", e)