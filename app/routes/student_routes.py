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
import threading

student_bp = Blueprint("student", __name__, url_prefix="/student")

PROJECT_CATEGORY_LABELS = {
    "mini_project": "Mini Project",
    "field_project": "Field Project",
    "major_project": "Major Project",
    "desk_research": "Desk Research",
    "research_project": "Research Project"
}

EVALUATION_TYPE_PRESENTATION_1 = "presentation1"
EVALUATION_TYPE_PRESENTATION_2 = "presentation2"
EVALUATION_TYPE_FINAL_MCA = "final_mca"
EVALUATION_TYPE_DESK_RESEARCH_MBA = "desk_research_mba"
EVALUATION_TYPES = {
    EVALUATION_TYPE_PRESENTATION_1,
    EVALUATION_TYPE_PRESENTATION_2,
    EVALUATION_TYPE_FINAL_MCA,
    EVALUATION_TYPE_DESK_RESEARCH_MBA
}


def normalize_project_category(value, program=None):
    selected_program = str(program or "MCA").strip().upper()
    if selected_program != "MBA":
        return "mini_project"
    normalized = str(value or "").strip().lower().replace(" ", "_")
    return normalized if normalized in PROJECT_CATEGORY_LABELS else "mini_project"


def project_category_label(value):
    return PROJECT_CATEGORY_LABELS.get(str(value or "").strip().lower(), "Mini Project")


def scoped_content_query(program, project_category):
    if str(program).upper() == "MBA":
        if project_category == "mini_project":
            return {
                "$and": [
                    {"program": "MBA"},
                    {
                        "$or": [
                            {"project_category": "mini_project"},
                            {"project_category": {"$exists": False}},
                            {"project_category": None},
                            {"project_category": ""}
                        ]
                    }
                ]
            }
        return {"program": "MBA", "project_category": project_category}

    return {
        "$and": [
            {"$or": [{"program": "MCA"}, {"program": {"$exists": False}}]},
            {
                "$or": [
                    {"project_category": "mini_project"},
                    {"project_category": {"$exists": False}},
                    {"project_category": None},
                    {"project_category": ""}
                ]
            }
        ]
    }

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


def _normalize_evaluation_type(value, program, project_category=None):
    normalized_program = normalize_program(program)
    normalized_category = str(project_category or "").strip().lower()
    if normalized_program == "MBA" and normalized_category == "desk_research":
        return EVALUATION_TYPE_DESK_RESEARCH_MBA

    normalized = str(value or "").strip().lower()
    if normalized not in EVALUATION_TYPES:
        return EVALUATION_TYPE_PRESENTATION_2
    if normalized_program != "MCA" and normalized == EVALUATION_TYPE_FINAL_MCA:
        return EVALUATION_TYPE_PRESENTATION_2
    if normalized != EVALUATION_TYPE_DESK_RESEARCH_MBA:
        return normalized
    if normalized_program == "MBA" and str(project_category or "").strip().lower() == "desk_research":
        return normalized
    return EVALUATION_TYPE_PRESENTATION_2


def _evaluation_type_query(eval_type):
    normalized_type = str(eval_type or "").strip().lower()
    if normalized_type == EVALUATION_TYPE_PRESENTATION_2:
        return {"$or": [{"evaluation_type": EVALUATION_TYPE_PRESENTATION_2}, {"evaluation_type": {"$exists": False}}]}
    return {"evaluation_type": normalized_type}


def normalize_program(value):
    normalized = str(value or "MCA").strip().upper()
    return normalized if normalized in {"MCA", "MBA"} else "MCA"


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


def save_final_project_file(file_storage, student_id, prefix, allowed_extensions, convert_preview=True):
    if not file_storage or not file_storage.filename:
        return None

    original_name = secure_filename(file_storage.filename)
    _, extension = os.path.splitext(original_name)
    extension = extension.lower()

    if extension not in allowed_extensions:
        return None

    upload_folder = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(upload_folder, exist_ok=True)

    filename = f"{prefix}-{student_id}-{int(datetime.utcnow().timestamp())}{extension}"
    file_path = os.path.join(upload_folder, filename)
    file_storage.save(file_path)

    if extension == ".pdf":
        pdf_file = filename
    elif extension == ".zip" or not convert_preview:
        pdf_file = None
    else:
        pdf_file = convert_to_pdf(file_path, upload_folder)

    return {
        "file_name": filename,
        "pdf_file": pdf_file
    }


def remove_uploaded_files(existing_document, keys):
    if not existing_document:
        return

    upload_folder = current_app.config["UPLOAD_FOLDER"]
    removable = set()
    for key in keys:
        value = existing_document.get(key)
        if value:
            removable.add(value)

    for file_name in removable:
        file_path = os.path.join(upload_folder, file_name)
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass


def process_final_submission_background(app, submission_id, student_id, student_name, project_title, category_name, mentor_id, program):
    with app.app_context():
        submission = current_app.db.final_submissions.find_one({"_id": ObjectId(submission_id)})
        if not submission:
            return

        upload_folder = current_app.config["UPLOAD_FOLDER"]
        pdf_updates = {}
        conversion_failed = False
        preview_pairs = [
            ("archive_file", "archive_pdf_file"),
            ("project_diary_file", "project_diary_pdf_file"),
            ("company_certificate_file", "company_certificate_pdf_file")
        ]
        convertible_extensions = {".doc", ".docx", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff"}

        for file_key, pdf_key in preview_pairs:
            file_name = submission.get(file_key)
            existing_pdf = submission.get(pdf_key)
            if not file_name or existing_pdf:
                continue

            extension = os.path.splitext(file_name)[1].lower()
            if extension == ".pdf":
                pdf_updates[pdf_key] = file_name
                continue

            if extension not in convertible_extensions:
                continue

            source_path = os.path.join(upload_folder, file_name)
            if not os.path.exists(source_path):
                conversion_failed = True
                continue

            converted_pdf = convert_to_pdf(source_path, upload_folder)
            if converted_pdf:
                pdf_updates[pdf_key] = converted_pdf
            else:
                conversion_failed = True

        update_payload = {}
        if pdf_updates:
            update_payload.update(pdf_updates)

        update_payload["processing_status"] = "failed" if conversion_failed else "ready"
        if update_payload:
            current_app.db.final_submissions.update_one(
                {"_id": ObjectId(submission_id)},
                {"$set": update_payload}
            )

        notification_message = f"{student_name} submitted {category_name} final project: {project_title}"
        emailed_addresses = set()
        mentor_obj_id = ObjectId(mentor_id) if mentor_id else None

        if mentor_obj_id:
            create_notification(mentor_obj_id, notification_message)
            mentor = current_app.db.users.find_one({"_id": mentor_obj_id})
            mentor_email = (mentor or {}).get("email")
            if mentor_email:
                try:
                    send_email(
                        mentor_email,
                        f"Final Project Submission - {category_name}",
                        final_project_submission_email(student_name, f"{project_title} ({category_name})")
                    )
                    emailed_addresses.add(mentor_email)
                except Exception as e:
                    print("Email error:", e)

        notify_leadership(
            notification_message,
            email_subject=f"Final Project Submission - {category_name}",
            email_html=final_project_submission_email(student_name, f"{project_title} ({category_name})"),
            exclude_ids=[mentor_obj_id] if mentor_obj_id else None,
            program=program
        )


def _leadership_users(exclude_ids=None):
    return _leadership_users_for_program(program=None, exclude_ids=exclude_ids)


def _leadership_users_for_program(program=None, exclude_ids=None):
    exclude_ids = {str(item) for item in (exclude_ids or []) if item}
    scoped_program = str(program or "MCA").strip().upper()
    if scoped_program not in {"MCA", "MBA"}:
        scoped_program = "MCA"

    assignments = list(
        current_app.db.role_assignments.find(
            {
                "$or": [
                    {"role": "director"},
                    {"role": "academic_coordinator", "program": {"$exists": False}},
                    {"role": "academic_coordinator", "program": scoped_program},
                    {"role": {"$in": ["project_coordinator", "hod"]}, "program": scoped_program}
                ]
            }
        )
    )
    user_ids = [entry.get("user_id") for entry in assignments if entry.get("user_id")]
    if not user_ids:
        return []
    users = list(
        current_app.db.users.find(
            {
                "_id": {"$in": user_ids},
                "role": {"$in": ["admin", "faculty"]}
            }
        )
    )
    return [user for user in users if str(user.get("_id")) not in exclude_ids]


def notify_leadership(message, email_subject=None, email_html=None, exclude_ids=None, program=None):
    recipients = _leadership_users_for_program(program=program, exclude_ids=exclude_ids)
    notified = set()
    emailed = set()

    for user in recipients:
        user_id = str(user["_id"])
        if user_id not in notified:
            create_notification(user["_id"], message)
            notified.add(user_id)

        if email_subject and email_html and user.get("email"):
            if user["email"] in emailed:
                continue
            try:
                send_email(user["email"], email_subject, email_html)
                emailed.add(user["email"])
            except Exception as e:
                print("Leadership email error:", e)


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
    batch_program = str((batch or {}).get("program") or student.get("program") or "MCA").strip().upper()
    batch_category = normalize_project_category((batch or {}).get("project_category") or student.get("project_category"), batch_program)

    mentor = None
    if batch and batch.get("mentor_id"):
        mentor = current_app.db.users.find_one({"_id": batch["mentor_id"]})

    stages = list(current_app.db.stages.find(scoped_content_query(batch_program, batch_category)).sort("order", 1))

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
        selected_project_category=batch_category,
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
    batch = current_app.db.batches.find_one({"_id": student.get("batch_id")}) if student.get("batch_id") else None
    batch_program = str((batch or {}).get("program") or student.get("program") or "MCA").strip().upper()
    batch_category = normalize_project_category((batch or {}).get("project_category") or student.get("project_category"), batch_program)

    stages = list(current_app.db.stages.find(scoped_content_query(batch_program, batch_category)).sort("order", 1))

    submissions = list(current_app.db.submissions.find({
        "student_id": student["_id"]
    }))

    submission_dict = {}

    for s in submissions:
        submission_dict[str(s["stage_id"])] = s

    deadline_query = get_session_deadline_query(batch)
    deadlines = list(current_app.db.deadlines.find(deadline_query)) if deadline_query else []

    deadline_dict = {}

    for d in deadlines:
        deadline_dict[str(d["stage_id"])] = d["deadline"]

    progress_document_dict = {}
    if batch and batch.get("session_id"):
        progress_documents = current_app.db.progress_documents.find({
            "session_id": batch["session_id"],
            "project_category": batch_category
        })
        for document in progress_documents:
            progress_document_dict[str(document.get("stage_id"))] = document

    return render_template(
        "student/submissions.html",
        stages=stages,
        submission_dict=submission_dict,
        deadline_dict=deadline_dict,
        selected_project_category=batch_category,
        progress_document_dict=progress_document_dict
    )


@student_bp.route("/evaluation")
@login_required
@role_required("student")
def evaluation_sheet():
    student = current_app.db.students.find_one({"_id": ObjectId(current_user.id)})
    student_program = normalize_program((student or {}).get("program"))
    batch = current_app.db.batches.find_one({"_id": student.get("batch_id")}) if student and student.get("batch_id") else None
    student_project_category = normalize_project_category((batch or {}).get("project_category") or (student or {}).get("project_category"), student_program)
    selected_eval_type = _normalize_evaluation_type(request.args.get("eval_type"), student_program, student_project_category)
    evaluation = None

    if student:
        evaluation = current_app.db.evaluations.find_one(
            {
                "student_id": student["_id"],
                "session_id": student.get("session_id"),
                **_evaluation_type_query(selected_eval_type)
            },
            sort=[("updated_at", -1)]
        )

        if not evaluation:
            evaluation = current_app.db.evaluations.find_one(
                {
                    "student_id": student["_id"],
                    **_evaluation_type_query(selected_eval_type)
                },
                sort=[("updated_at", -1)]
            )

    return render_template(
        "student/evaluation.html",
        student=student,
        evaluation=evaluation,
        selected_eval_type=selected_eval_type,
        student_program=student_program,
        student_project_category=student_project_category
    )


@student_bp.route("/final-project")
@login_required
@role_required("student")
def final_project_page():

    student = current_app.db.students.find_one({"_id": ObjectId(current_user.id)})
    batch = current_app.db.batches.find_one({"_id": student.get("batch_id")}) if student.get("batch_id") else None
    batch_program = str((batch or {}).get("program") or (student or {}).get("program") or "MCA").strip().upper()
    if batch_program not in {"MCA", "MBA"}:
        batch_program = "MCA"
    batch_category = normalize_project_category((batch or {}).get("project_category") or (student or {}).get("project_category"), batch_program)
    mentor = None

    if batch and batch.get("mentor_id"):
        mentor = current_app.db.users.find_one({"_id": batch["mentor_id"]})

    final_project_query = {"student_id": student["_id"]}
    final_project_query.update(scoped_content_query(batch_program, batch_category))
    final_project = current_app.db.final_submissions.find_one(final_project_query)
    if not final_project:
        # Fallback for legacy records created before program/category scoping.
        final_project = current_app.db.final_submissions.find_one({"student_id": student["_id"]})

    return render_template(
        "student/final_project.html",
        student=student,
        batch=batch,
        mentor=mentor,
        final_project=final_project,
        selected_project_category=batch_category,
        selected_program=batch_program
    )


@student_bp.route("/upload/<stage_id>", methods=["POST"])
@login_required
@role_required("student")
def upload_stage(stage_id):

    student = current_app.db.students.find_one({"_id": ObjectId(current_user.id)})

    batch_id = student["batch_id"]

    # -------- DEADLINE CHECK --------

    batch = current_app.db.batches.find_one({"_id": batch_id})
    batch_program = str((batch or {}).get("program") or (student or {}).get("program") or "MCA").strip().upper()
    if batch_program not in {"MCA", "MBA"}:
        batch_program = "MCA"
    batch_category = normalize_project_category((batch or {}).get("project_category") or (student or {}).get("project_category"), batch_program)
    category_name = project_category_label(batch_category)
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
                "reminder_sent": False,
                "program": batch_program,
                "project_category": batch_category
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

    faculty_id = batch.get("mentor_id")

    if faculty_id:
        faculty_id = ObjectId(faculty_id)

    # Notify mentor
    if faculty_id:
        create_notification(
            faculty_id,
            f"{student_name} submitted {stage_name} ({category_name})"
        )

        # EMAIL faculty
        faculty = current_app.db.users.find_one({"_id": faculty_id})

        if faculty and faculty.get("email"):
            try:
                send_email(
                    faculty["email"],
                    f"New Submission - {category_name}",
                    submission_email(student_name, f"{stage_name} ({category_name})")
                )
            except Exception as e:
                print("Email error:", e)

    # Notify leadership roles (Director, PC, AC, HOD)
    leadership_message = f"{student_name} submitted {stage_name} ({category_name}) (Pending review)"
    notify_leadership(
        leadership_message,
        email_subject=f"New Submission Alert - {category_name}",
        email_html=submission_email(student_name, f"{stage_name} ({category_name})"),
        exclude_ids=[faculty_id] if faculty_id else None,
        program=batch_program
    )

    # -------- LATE SUBMISSION --------
    if late:
        late_message = f"{student_name} submitted {stage_name} late ({category_name})"
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
                        f"Late Submission Alert - {category_name}",
                        late_submission_email(student_name, f"{stage_name} ({category_name})")
                    )
                    emailed_addresses.add(faculty_email)
                except Exception as e:
                    print("Email error:", e)

        notify_leadership(
            late_message,
            email_subject=f"Late Submission Alert - {category_name}",
            email_html=late_submission_email(student_name, f"{stage_name} ({category_name})"),
            exclude_ids=list(notified_user_ids),
            program=batch_program
        )

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
    batch_program = str((batch or {}).get("program") or (student or {}).get("program") or "MCA").strip().upper()
    if batch_program not in {"MCA", "MBA"}:
        batch_program = "MCA"
    batch_category = normalize_project_category((batch or {}).get("project_category") or (student or {}).get("project_category"), batch_program)
    category_name = project_category_label(batch_category)

    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    github_link = request.form.get("github_link", "").strip()
    live_link = request.form.get("live_link", "").strip()
    company_name = request.form.get("company_name", "").strip()
    archive = request.files.get("project_archive")
    project_diary = request.files.get("project_diary")
    company_certificate = request.files.get("company_certificate")
    is_mba = batch_program == "MBA"

    if not title:
        flash("Project title is required.", "warning")
        return redirect(url_for("student.final_project_page"))

    if not batch:
        flash("Final project submission is available after your batch is assigned.", "warning")
        return redirect(url_for("student.final_project_page"))

    if not archive or not archive.filename:
        flash("Project archive is required.", "warning")
        return redirect(url_for("student.final_project_page"))

    if is_mba:
        if not company_name:
            flash("Company Name is required for MBA final submission.", "warning")
            return redirect(url_for("student.final_project_page"))
        if not project_diary or not project_diary.filename:
            flash("Project Diary file is required for MBA final submission.", "warning")
            return redirect(url_for("student.final_project_page"))
        if not company_certificate or not company_certificate.filename:
            flash("Company Certificate file is required for MBA final submission.", "warning")
            return redirect(url_for("student.final_project_page"))
    else:
        if not is_valid_web_link(github_link):
            flash("Enter a valid GitHub link starting with http:// or https://", "warning")
            return redirect(url_for("student.final_project_page"))

        if not is_valid_web_link(live_link):
            flash("Enter a valid live/demo link starting with http:// or https://", "warning")
            return redirect(url_for("student.final_project_page"))

    # Keep one final submission record per student and update it for current scope.
    final_project_query = {"student_id": student["_id"]}
    existing_submission = current_app.db.final_submissions.find_one(final_project_query)

    archive_saved = save_final_project_file(
        archive,
        student["_id"],
        "final-project-archive",
        {".zip", ".pdf", ".doc", ".docx"},
        convert_preview=False
    )
    if not archive_saved:
        flash("Project archive supports only ZIP, PDF, DOC, and DOCX files.", "warning")
        return redirect(url_for("student.final_project_page"))

    project_diary_saved = None
    company_certificate_saved = None
    if is_mba:
        allowed_supporting_files = {".pdf", ".doc", ".docx", ".png", ".jpg", ".jpeg", ".webp"}
        project_diary_saved = save_final_project_file(
            project_diary,
            student["_id"],
            "project-diary",
            allowed_supporting_files,
            convert_preview=False
        )
        if not project_diary_saved:
            flash("Project Diary supports PDF, DOC, DOCX, and image files.", "warning")
            return redirect(url_for("student.final_project_page"))

        company_certificate_saved = save_final_project_file(
            company_certificate,
            student["_id"],
            "company-certificate",
            allowed_supporting_files,
            convert_preview=False
        )
        if not company_certificate_saved:
            flash("Company Certificate supports PDF, DOC, DOCX, and image files.", "warning")
            return redirect(url_for("student.final_project_page"))

    remove_uploaded_files(
        existing_submission,
        [
            "archive_file",
            "archive_pdf_file",
            "project_diary_file",
            "project_diary_pdf_file",
            "company_certificate_file",
            "company_certificate_pdf_file"
        ]
    )

    now = datetime.utcnow()
    mentor_id = batch.get("mentor_id") if batch else None
    if mentor_id:
        mentor_id = ObjectId(mentor_id)

    set_payload = {
        "student_id": student["_id"],
        "batch_id": batch["_id"] if batch else None,
        "mentor_id": mentor_id,
        "session_id": batch.get("session_id") if batch else student.get("session_id"),
        "project_title": title,
        "description": description,
        "archive_file": archive_saved["file_name"],
        "archive_pdf_file": archive_saved.get("pdf_file"),
        "status": "pending",
        "submitted_at": now,
        "updated_at": now,
        "processing_status": "processing",
        "program": batch_program,
        "project_category": batch_category
    }
    unset_payload = {
        "remark": "",
        "reviewed_at": "",
        "archive_original_name": "",
        "project_diary_original_name": "",
        "company_certificate_original_name": ""
    }

    if is_mba:
        set_payload.update({
            "company_name": company_name,
            "project_diary_file": project_diary_saved["file_name"],
            "project_diary_pdf_file": project_diary_saved.get("pdf_file"),
            "company_certificate_file": company_certificate_saved["file_name"],
            "company_certificate_pdf_file": company_certificate_saved.get("pdf_file")
        })
        unset_payload.update({
            "github_link": "",
            "live_link": ""
        })
        github_link = ""
        live_link = ""
    else:
        set_payload.update({
            "github_link": github_link,
            "live_link": live_link
        })
        unset_payload.update({
            "company_name": "",
            "project_diary_file": "",
            "project_diary_pdf_file": "",
            "company_certificate_file": "",
            "company_certificate_pdf_file": ""
        })

    current_app.db.final_submissions.update_one(
        final_project_query,
        {
            "$set": set_payload,
            "$unset": unset_payload
        },
        upsert=True
    )

    student_name = student.get("name", "Student")
    saved_submission = current_app.db.final_submissions.find_one(final_project_query, {"_id": 1})
    if saved_submission:
        app_obj = current_app._get_current_object()
        threading.Thread(
            target=process_final_submission_background,
            args=(
                app_obj,
                str(saved_submission["_id"]),
                str(student["_id"]),
                student_name,
                title,
                category_name,
                str(mentor_id) if mentor_id else None,
                batch_program
            ),
            daemon=True
        ).start()

    flash("Final project submitted successfully. Preview and email notifications are being processed.", "success")
    return redirect(url_for("student.final_project_page"))


@student_bp.route("/final-project-status")
@login_required
@role_required("student")
def final_project_status():
    student = current_app.db.students.find_one({"_id": ObjectId(current_user.id)})
    batch = current_app.db.batches.find_one({"_id": student.get("batch_id")}) if student and student.get("batch_id") else None
    batch_program = str((batch or {}).get("program") or (student or {}).get("program") or "MCA").strip().upper()
    if batch_program not in {"MCA", "MBA"}:
        batch_program = "MCA"
    batch_category = normalize_project_category((batch or {}).get("project_category") or (student or {}).get("project_category"), batch_program)

    query = {"student_id": student["_id"]}
    query.update(scoped_content_query(batch_program, batch_category))
    final_project = current_app.db.final_submissions.find_one(query)
    if not final_project:
        final_project = current_app.db.final_submissions.find_one({"student_id": student["_id"]})

    if not final_project:
        return jsonify({"found": False, "processing_status": "none"})

    return jsonify({
        "found": True,
        "processing_status": final_project.get("processing_status", "ready"),
        "has_archive": bool(final_project.get("archive_file")),
        "has_project_diary": bool(final_project.get("project_diary_file")),
        "has_company_certificate": bool(final_project.get("company_certificate_file")),
        "has_archive_preview": bool(final_project.get("archive_pdf_file")),
        "has_diary_preview": bool(final_project.get("project_diary_pdf_file")),
        "has_certificate_preview": bool(final_project.get("company_certificate_pdf_file")),
        "is_submitted": True
    })
