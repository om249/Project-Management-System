from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, jsonify
from flask_login import login_required, login_user, logout_user, current_user
from bson.objectid import ObjectId
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from app.decorators.role_required import role_required
import os
import re
from werkzeug.utils import secure_filename
from app.services.email_service import late_submission_email
from app.services.notification_service import create_notification, get_notifications, mark_notifications_read
from app.services.file_converter import convert_to_pdf
from app.services.email_service import send_email, submission_email, late_submission_email, final_project_submission_email
from datetime import datetime, timedelta
from urllib.parse import urlparse

student_bp = Blueprint("student", __name__, url_prefix="/student")

def save_profile_photo(file_storage, user_id):
    if not file_storage or not file_storage.filename:
        return None

    upload_folder = os.path.join(current_app.root_path, "static", "uploads")
    os.makedirs(upload_folder, exist_ok=True)

    safe_name = secure_filename(file_storage.filename)
    _, extension = os.path.splitext(safe_name)
    extension = extension.lower() or ".png"
    filename = f"profile-{user_id}-{int(datetime.utcnow().timestamp())}{extension}"
    file_storage.save(os.path.join(upload_folder, filename))
    return filename


def get_session_deadline_query(batch, stage_id=None):
    if not batch or not batch.get("session_id"):
        return None

    query = {"session_id": batch["session_id"]}
    if stage_id is not None:
        query["stage_id"] = stage_id
    return query


def validate_password_rules(password):
    if len(password) < 8:
        return "Password must be at least 8 characters long."
    if not re.search(r"[A-Z]", password):
        return "Password must contain at least 1 capital letter."
    if not re.search(r"[a-z]", password):
        return "Password must contain at least 1 small letter."
    if not re.search(r"\d", password):
        return "Password must contain at least 1 digit."
    if not re.search(r"[^A-Za-z0-9]", password):
        return "Password must contain at least 1 symbol."
    return None


def is_valid_web_link(value):
    if not value:
        return True

    parsed = urlparse(value.strip())
    return parsed.scheme in ["http", "https"] and bool(parsed.netloc)


def save_final_project_archive(file_storage, student_id):
    if not file_storage or not file_storage.filename:
        return None

    original_name = secure_filename(file_storage.filename)
    _, extension = os.path.splitext(original_name)
    extension = extension.lower()

    if extension != ".zip":
        return None

    upload_folder = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(upload_folder, exist_ok=True)

    filename = f"final-project-{student_id}-{int(datetime.utcnow().timestamp())}{extension}"
    file_storage.save(os.path.join(upload_folder, filename))
    return filename


# ---------------- STUDENT LOGIN ----------------
@student_bp.route("/login", methods=["GET", "POST"])
def student_login():

    if request.method == "POST":

        prn = request.form["prn"]
        password = request.form["password"]

        student = current_app.db.students.find_one({"prn": prn})

        if student and check_password_hash(student["password"], password):

            login_user(student)

            return redirect(url_for("student.dashboard"))

        flash("Invalid PRN or password.", "danger")

    return render_template("student/login.html")


# ---------------- STUDENT DASHBOARD ----------------
@student_bp.route("/dashboard")
@login_required
@role_required("student")
def dashboard():

    student = current_app.db.students.find_one({"_id": ObjectId(current_user.id)})

    batch = current_app.db.batches.find_one({"_id": student["batch_id"]})

    mentor = None
    if batch and batch.get("mentor_id"):
        mentor = current_app.db.users.find_one({"_id": batch["mentor_id"]})

    stages = list(current_app.db.stages.find().sort("order", 1))

    submissions = list(current_app.db.submissions.find({
        "student_id": student["_id"]
    }))

    # ---------------- PROGRESS ----------------
    approved = 0
    for s in submissions:
        if s.get("status") == "approved":   # ✅ FIXED
            approved += 1

    progress = int((approved / len(stages)) * 100) if stages else 0

    notifications, unread_count = get_notifications(current_user.id)

    # ---------------- DEADLINE REMINDER ----------------
    today = datetime.utcnow().date()
    today_key = today.isoformat()

    for stage in stages:
        deadline_query = get_session_deadline_query(batch, stage["_id"])
        deadline_doc = current_app.db.deadlines.find_one(deadline_query) if deadline_query else None

        if not deadline_doc:
            continue

        deadline_date = deadline_doc["deadline"].date()
        days_left = (deadline_date - today).days

        # 🔥 GET SUBMISSION
        submission = current_app.db.submissions.find_one({
            "student_id": student["_id"],
            "stage_id": stage["_id"]
        })

        status = submission.get("status") if submission else None

        # ✅ SKIP if already submitted
        if status in ["approved", "pending"]:
            continue


        # ---------------- MESSAGE LOGIC ----------------
        message = None

        if days_left == 2:
            message = f"{stage['name']} deadline is in 2 days"

        elif days_left == 1:
            message = f"{stage['name']} deadline is tomorrow"

        elif days_left == 0:
            message = f"{stage['name']} deadline is today"

        # ---------------- SEND ALERT ----------------
        if message:
            reminder_result = current_app.db.deadline_reminders.update_one(
                {
                    "student_id": student["_id"],
                    "stage_id": stage["_id"],
                    "reminder_date": today_key
                },
                {
                    "$setOnInsert": {
                        "student_id": student["_id"],
                        "stage_id": stage["_id"],
                        "reminder_date": today_key,
                        "created_at": datetime.utcnow()
                    }
                },
                upsert=True
            )

            if reminder_result.matched_count > 0:
                continue

            print("REMINDER:", message)   # DEBUG

            # ✅ EMAIL
            if student.get("email"):
                try:
                    send_email(
                        student["email"],
                        "Deadline Reminder",
                        f"<p>{message}</p>"
                    )
                except Exception as e:
                    print("Email error:", e)

            # ✅ NOTIFICATION
            create_notification(student["_id"], message)

            # ✅ MARK REMINDER SENT (NO UPSERT)
            current_app.db.deadline_reminders.update_one(
                    {
                        "student_id": student["_id"],
                        "stage_id": stage["_id"],
                        "reminder_date": today_key
                    },
                    {
                        "$set": {
                            "student_id": student["_id"],
                            "stage_id": stage["_id"],
                            "reminder_date": today_key,
                            "created_at": datetime.utcnow()
                        }
                    },
                    upsert=True
                )

    return render_template(
        "student/dashboard.html",
        student=student,
        batch=batch,
        mentor=mentor,
        progress=progress,
        notifications=notifications,
        unread_count=unread_count
    )


@student_bp.route("/profile")
@login_required
@role_required("student")
def student_profile():

    student = current_app.db.students.find_one({
        "_id": ObjectId(current_user.id)
    })

    batch = current_app.db.batches.find_one({
        "_id": student.get("batch_id")
    })

    mentor = None
    if batch and batch.get("mentor_id"):
        mentor = current_app.db.users.find_one({
            "_id": batch["mentor_id"]
        })

    return render_template(
        "student/profile.html",
        student=student,
        batch=batch,
        mentor=mentor
    )

# update profile
@student_bp.route("/update-profile", methods=["POST"])
@login_required
@role_required("student")
def update_student_profile():

    name = request.form.get("name")
    email = request.form.get("email")
    file = request.files.get("photo")
    new_password = request.form.get("new_password", "").strip()
    confirm_password = request.form.get("confirm_password", "").strip()

    update_data = {
        "name": name,
        "email": email
    }

    if file and file.filename:
        filename = save_profile_photo(file, current_user.id)
        update_data["photo"] = filename

    if new_password or confirm_password:
        if new_password != confirm_password:
            flash("New password and confirm password must match.", "danger")
            return redirect(url_for("student.student_profile"))

        password_error = validate_password_rules(new_password)
        if password_error:
            flash(password_error, "danger")
            return redirect(url_for("student.student_profile"))

        update_data["password"] = generate_password_hash(new_password)
        update_data["password_changed"] = True

    current_app.db.students.update_one(
        {"_id": ObjectId(current_user.id)},
        {"$set": update_data}
    )

    flash("Profile updated successfully")
    return redirect(url_for("student.student_profile"))


@student_bp.route("/submissions")
@login_required
@role_required("student")
def submissions():

    student = current_app.db.students.find_one({"_id": ObjectId(current_user.id)})

    stages = list(current_app.db.stages.find().sort("order", 1))

    submissions = list(current_app.db.submissions.find({
        "student_id": student["_id"]
    }))

    submission_dict = {}

    for s in submissions:
        submission_dict[str(s["stage_id"])] = s

    deadline_query = get_session_deadline_query(
        current_app.db.batches.find_one({"_id": student["batch_id"]})
    )
    deadlines = list(current_app.db.deadlines.find(deadline_query)) if deadline_query else []

    deadline_dict = {}

    for d in deadlines:
        deadline_dict[str(d["stage_id"])] = d["deadline"]

    progress_document_dict = {}
    batch = current_app.db.batches.find_one({"_id": student.get("batch_id")}) if student.get("batch_id") else None
    if batch and batch.get("session_id"):
        progress_documents = current_app.db.progress_documents.find({
            "session_id": batch["session_id"]
        })
        for document in progress_documents:
            progress_document_dict[str(document.get("stage_id"))] = document

    return render_template(
        "student/submissions.html",
        stages=stages,
        submission_dict=submission_dict,
        deadline_dict=deadline_dict,
        progress_document_dict=progress_document_dict
    )


@student_bp.route("/final-project")
@login_required
@role_required("student")
def final_project_page():

    student = current_app.db.students.find_one({"_id": ObjectId(current_user.id)})
    batch = current_app.db.batches.find_one({"_id": student.get("batch_id")}) if student.get("batch_id") else None
    mentor = None

    if batch and batch.get("mentor_id"):
        mentor = current_app.db.users.find_one({"_id": batch["mentor_id"]})

    final_project = current_app.db.final_submissions.find_one({"student_id": student["_id"]})

    return render_template(
        "student/final_project.html",
        student=student,
        batch=batch,
        mentor=mentor,
        final_project=final_project
    )


@student_bp.route("/upload/<stage_id>", methods=["POST"])
@login_required
@role_required("student")
def upload_stage(stage_id):

    student = current_app.db.students.find_one({"_id": ObjectId(current_user.id)})

    batch_id = student["batch_id"]

    # -------- DEADLINE CHECK --------

    batch = current_app.db.batches.find_one({"_id": batch_id})
    deadline_query = get_session_deadline_query(batch, ObjectId(stage_id))
    deadline_doc = current_app.db.deadlines.find_one(deadline_query) if deadline_query else None

    deadline = deadline_doc["deadline"] if deadline_doc else None
    now = datetime.utcnow()

    late = False
    if deadline and now > deadline:
        late = True

    existing_submission = current_app.db.submissions.find_one({
        "student_id": student["_id"],
        "stage_id": ObjectId(stage_id)
    })

    # -------- FILE UPLOAD --------
    file = request.files["file"]
    if not file or not file.filename:
        return jsonify({'success': False, 'error': 'No file provided'}), 400

    original_name = secure_filename(file.filename)
    base_name, extension = os.path.splitext(original_name)
    filename = f"{base_name}-{student['_id']}-{int(now.timestamp())}{extension.lower()}"

    upload_folder = current_app.config["UPLOAD_FOLDER"]

    if not os.path.exists(upload_folder):
        os.makedirs(upload_folder)

    if existing_submission:
        old_files = {
            existing_submission.get("file_name"),
            existing_submission.get("pdf_file")
        }

        for old_file in old_files:
            if not old_file:
                continue

            old_path = os.path.join(upload_folder, old_file)
            if os.path.exists(old_path):
                try:
                    os.remove(old_path)
                except OSError:
                    pass

    file_path = os.path.join(upload_folder, filename)
    file.save(file_path)

    # -------- CONVERT TO PDF --------
    pdf_file = convert_to_pdf(file_path, upload_folder)

    # -------- SAVE TO DB --------
    current_app.db.submissions.update_one(
        {
            "student_id": student["_id"],
            "stage_id": ObjectId(stage_id)
        },
        {
            "$set": {
                "file_name": filename,
                "pdf_file": pdf_file,
                "submitted_at": now,
                "status": "pending",
                "late": late,
                "reminder_sent": False
            },
            "$unset": {
                "remark": "",
                "reviewed_at": ""
            }
        },
        upsert=True
    )

    # -------- NOTIFICATION --------
    student_name = student["name"]

    stage = current_app.db.stages.find_one({"_id": ObjectId(stage_id)})
    stage_name = stage["name"]

    batch = current_app.db.batches.find_one({"_id": student["batch_id"]})
    faculty_id = batch.get("mentor_id")

    if faculty_id:
        faculty_id = ObjectId(faculty_id)

    # Notify faculty
    if faculty_id:
        create_notification(
            faculty_id,
            f"{student_name} submitted {stage_name}"
        )

        # EMAIL faculty
        faculty = current_app.db.users.find_one({"_id": faculty_id})

        if faculty and faculty.get("email"):
            try:
                send_email(
                    faculty["email"],
                    "New Submission",
                    submission_email(student_name, stage_name)
                )
            except Exception as e:
                print("Email error:", e)

    # -------- LATE SUBMISSION --------
    if late:
        admin = current_app.db.users.find_one({"role": "admin"})
        late_message = f"{student_name} submitted {stage_name} late"
        notified_user_ids = set()
        emailed_addresses = set()

        if faculty_id:
            create_notification(faculty_id, late_message)
            notified_user_ids.add(str(faculty_id))

            faculty = current_app.db.users.find_one({"_id": faculty_id})
            faculty_email = (faculty or {}).get("email")
            if faculty_email and faculty_email not in emailed_addresses:
                try:
                    send_email(
                        faculty_email,
                        "Late Submission Alert",
                        late_submission_email(student_name, stage_name)
                    )
                    emailed_addresses.add(faculty_email)
                except Exception as e:
                    print("Email error:", e)

        if admin and str(admin["_id"]) not in notified_user_ids:
            create_notification(admin["_id"], late_message)
            notified_user_ids.add(str(admin["_id"]))

        if admin and admin.get("email") and admin["email"] not in emailed_addresses:
            try:
                send_email(
                    admin["email"],
                    "Late Submission Alert",
                    late_submission_email(student_name, stage_name)
                )
                emailed_addresses.add(admin["email"])
            except Exception as e:
                print("Email error:", e)

    # Return JSON for AJAX
    if request.headers.get('Accept') == 'application/json' or request.is_json:
        return jsonify({
            'success': True,
            'pdf_file': pdf_file,
            'filename': filename
        })

    flash("File uploaded successfully")
    return redirect(url_for("student.submissions"))


@student_bp.route("/final-project-submission", methods=["POST"])
@login_required
@role_required("student")
def final_project_submission():

    student = current_app.db.students.find_one({"_id": ObjectId(current_user.id)})
    batch = current_app.db.batches.find_one({"_id": student.get("batch_id")}) if student.get("batch_id") else None

    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    github_link = request.form.get("github_link", "").strip()
    live_link = request.form.get("live_link", "").strip()
    archive = request.files.get("project_archive")

    if not title:
        flash("Project title is required.", "warning")
        return redirect(url_for("student.submissions"))

    if not batch:
        flash("Final project submission is available after your batch is assigned.", "warning")
        return redirect(url_for("student.submissions"))

    if not archive or not archive.filename:
        flash("Project ZIP file is required.", "warning")
        return redirect(url_for("student.submissions"))

    if not is_valid_web_link(github_link):
        flash("Enter a valid GitHub link starting with http:// or https://", "warning")
        return redirect(url_for("student.submissions"))

    if not is_valid_web_link(live_link):
        flash("Enter a valid live/demo link starting with http:// or https://", "warning")
        return redirect(url_for("student.submissions"))

    existing_submission = current_app.db.final_submissions.find_one({"student_id": student["_id"]})
    upload_folder = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(upload_folder, exist_ok=True)

    if existing_submission and existing_submission.get("archive_file"):
        old_archive = os.path.join(upload_folder, existing_submission["archive_file"])
        if os.path.exists(old_archive):
            try:
                os.remove(old_archive)
            except OSError:
                pass

    archive_file = save_final_project_archive(archive, student["_id"])
    if not archive_file:
        flash("Only ZIP files are allowed for the final project archive.", "warning")
        return redirect(url_for("student.submissions"))

    now = datetime.utcnow()
    mentor_id = batch.get("mentor_id") if batch else None
    if mentor_id:
        mentor_id = ObjectId(mentor_id)

    current_app.db.final_submissions.update_one(
        {"student_id": student["_id"]},
        {
            "$set": {
                "student_id": student["_id"],
                "batch_id": batch["_id"] if batch else None,
                "mentor_id": mentor_id,
                "session_id": student.get("session_id"),
                "project_title": title,
                "description": description,
                "archive_file": archive_file,
                "github_link": github_link,
                "live_link": live_link,
                "status": "pending",
                "submitted_at": now,
                "updated_at": now
            },
            "$unset": {
                "remark": "",
                "reviewed_at": ""
            }
        },
        upsert=True
    )

    student_name = student.get("name", "Student")
    notification_message = f"{student_name} submitted final project: {title}"
    emailed_addresses = set()

    if mentor_id:
        create_notification(mentor_id, notification_message)
        mentor = current_app.db.users.find_one({"_id": mentor_id})
        mentor_email = (mentor or {}).get("email")
        if mentor_email:
            try:
                send_email(
                    mentor_email,
                    "Final Project Submission",
                    final_project_submission_email(student_name, title)
                )
                emailed_addresses.add(mentor_email)
            except Exception as e:
                print("Email error:", e)

    admin = current_app.db.users.find_one({"role": "admin"})
    if admin and (not mentor_id or str(admin["_id"]) != str(mentor_id)):
        create_notification(admin["_id"], notification_message)
        admin_email = admin.get("email")
        if admin_email and admin_email not in emailed_addresses:
            try:
                send_email(
                    admin_email,
                    "Final Project Submission",
                    final_project_submission_email(student_name, title)
                )
            except Exception as e:
                print("Email error:", e)

    flash("Final project submitted successfully.", "success")
    return redirect(url_for("student.submissions"))
