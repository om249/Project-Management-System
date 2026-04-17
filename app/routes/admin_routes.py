import email
from unittest import result
import re
import os
import threading
from io import BytesIO
from functools import wraps

from numpy import rint
import pandas as pd
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, jsonify
from flask import send_file
from flask_login import login_required, current_user
from bson.objectid import ObjectId
from datetime import datetime
from app.decorators.role_required import role_required
from app import bcrypt
from flask import send_from_directory
from werkzeug.utils import secure_filename
from app.services.notification_service import get_notifications, create_notification,  mark_notifications_read
from app.services.email_service import send_email, student_welcome_email, faculty_welcome_email, submission_email, late_submission_email, status_email, mentor_assignment_email, student_mentor_assigned_email, final_project_status_email, progress_document_email, designation_update_email, evaluation_update_email
from app.services.file_converter import convert_to_pdf
from app.routes.student_routes import submissions

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


SESSION_NAME_PATTERN = re.compile(r"^\d{4}-\d{2}$")
EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def is_valid_session_name(value):
    return bool(value and SESSION_NAME_PATTERN.match(str(value).strip()))


def normalize_excel_header(value):
    return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())


def is_valid_email(value):
    return bool(value and EMAIL_PATTERN.match(str(value).strip()))


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


def _session_label_from_year(year_text):
    if is_valid_session_name(year_text):
        return str(year_text).strip()

    year = datetime.utcnow().year
    return f"{year}-{str(year + 2)[-2:]}"


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


def save_progress_document(file_storage, session_id):
    if not file_storage or not file_storage.filename:
        return None

    upload_folder = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(upload_folder, exist_ok=True)

    safe_name = secure_filename(file_storage.filename)
    _, extension = os.path.splitext(safe_name)
    extension = extension.lower()
    allowed_extensions = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}

    if extension not in allowed_extensions:
        return None

    filename = f"progress-doc-{session_id}-{ObjectId()}{extension}"
    file_path = os.path.join(upload_folder, filename)
    file_storage.save(file_path)

    pdf_file = filename if extension == ".pdf" else None
    preview_status = "ready" if pdf_file else "processing"

    return {
        "file_name": filename,
        "original_name": safe_name,
        "pdf_file": pdf_file,
        "preview_status": preview_status
    }


def notify_progress_document_recipients(session, stage, document_name, is_reupload=False):
    action_label = "reuploaded" if is_reupload else "uploaded"
    message = f"{stage['name']} reference document has been {action_label} for {session['name']}."

    session_batches = list(current_app.db.batches.find(
        {"session_id": session["_id"]},
        {"_id": 1}
    ))
    batch_ids = [batch["_id"] for batch in session_batches]

    students = list(current_app.db.students.find({
        "$or": [
            {"session_id": session["_id"]},
            {"batch_id": {"$in": batch_ids}}
        ]
    }))
    faculty_members = list(current_app.db.users.find({
        "role": {"$in": ["faculty", "admin"]}
    }))

    for student in students:
        create_notification(student["_id"], message, "document")
        if student.get("email"):
            try:
                send_email(
                    student["email"],
                    f"Progress Report Document {action_label.title()}",
                    progress_document_email(
                        student.get("name", "Student"),
                        stage["name"],
                        session["name"],
                        document_name,
                        action_label
                    )
                )
            except Exception as e:
                print("Progress document student email error:", e)

    for faculty in faculty_members:
        create_notification(faculty["_id"], message, "document")
        if faculty.get("email"):
            try:
                send_email(
                    faculty["email"],
                    f"Progress Report Document {action_label.title()}",
                    progress_document_email(
                        faculty.get("name", "Faculty"),
                        stage["name"],
                        session["name"],
                        document_name,
                        action_label
                    )
                )
            except Exception as e:
                print("Progress document faculty email error:", e)


def ensure_progress_document_preview(document):
    return document


def process_progress_document_background(app, document_id, session_id, stage_id, document_name, is_reupload=False):
    with app.app_context():
        document = current_app.db.progress_documents.find_one({"_id": ObjectId(document_id)})
        session = current_app.db.academic_sessions.find_one({"_id": ObjectId(session_id)})
        stage = current_app.db.stages.find_one({"_id": ObjectId(stage_id)})

        if not document or not session or not stage:
            return

        if not document.get("pdf_file"):
            upload_folder = current_app.config["UPLOAD_FOLDER"]
            file_path = os.path.join(upload_folder, document["file_name"])
            pdf_file = convert_to_pdf(file_path, upload_folder) if os.path.exists(file_path) else None

            if pdf_file:
                current_app.db.progress_documents.update_one(
                    {"_id": document["_id"]},
                    {"$set": {"pdf_file": pdf_file, "preview_status": "ready"}}
                )
            else:
                current_app.db.progress_documents.update_one(
                    {"_id": document["_id"]},
                    {"$set": {"preview_status": "failed"}}
                )

        notify_progress_document_recipients(session, stage, document_name, is_reupload=is_reupload)


def get_session_deadline_query(session_id, stage_id=None):
    query = {"session_id": session_id}
    if stage_id is not None:
        query["stage_id"] = stage_id
    return query


DESIGNATION_DIRECTOR = "director"
DESIGNATION_PROJECT_COORDINATOR = "project_coordinator"
DESIGNATION_ACADEMIC_COORDINATOR = "academic_coordinator"
DESIGNATION_HOD = "hod"
DESIGNATION_FACULTY = "faculty"

STAFF_DESIGNATIONS = {
    DESIGNATION_DIRECTOR,
    DESIGNATION_PROJECT_COORDINATOR,
    DESIGNATION_ACADEMIC_COORDINATOR,
    DESIGNATION_HOD,
    DESIGNATION_FACULTY
}


def normalize_designation(value):
    normalized = str(value or "").strip().lower().replace(" ", "_")
    return normalized if normalized in STAFF_DESIGNATIONS else DESIGNATION_FACULTY


def get_staff_user(user_id=None):
    target_id = user_id or current_user.id
    return current_app.db.users.find_one({"_id": ObjectId(target_id)})


def get_user_designation(user):
    if not user:
        return DESIGNATION_FACULTY
    return normalize_designation(user.get("designation"))


def is_director(user):
    return get_user_designation(user) == DESIGNATION_DIRECTOR


def is_project_coordinator(user):
    return get_user_designation(user) == DESIGNATION_PROJECT_COORDINATOR


def can_manage_operations(user):
    return get_user_designation(user) in {DESIGNATION_PROJECT_COORDINATOR}


def can_view_reports(user):
    return get_user_designation(user) in {
        DESIGNATION_DIRECTOR,
        DESIGNATION_PROJECT_COORDINATOR,
        DESIGNATION_ACADEMIC_COORDINATOR,
        DESIGNATION_HOD
    }


def can_view_all_students(user):
    return get_user_designation(user) in {
        DESIGNATION_DIRECTOR,
        DESIGNATION_PROJECT_COORDINATOR,
        DESIGNATION_ACADEMIC_COORDINATOR,
        DESIGNATION_HOD
    }


def can_view_evaluation_sheet(user):
    if not user:
        return False
    if user.get("role") not in ["admin", "faculty"]:
        return False
    return get_user_designation(user) in {
        DESIGNATION_DIRECTOR,
        DESIGNATION_PROJECT_COORDINATOR,
        DESIGNATION_ACADEMIC_COORDINATOR,
        DESIGNATION_HOD,
        DESIGNATION_FACULTY
    }


def can_edit_evaluation_all(user):
    return get_user_designation(user) in {
        DESIGNATION_PROJECT_COORDINATOR,
        DESIGNATION_ACADEMIC_COORDINATOR,
        DESIGNATION_HOD
    }


def can_edit_evaluation_student(user, student):
    if not user or not student:
        return False

    if can_edit_evaluation_all(user):
        return True

    if get_user_designation(user) != DESIGNATION_FACULTY:
        return False

    student_batch_id = student.get("batch_id")
    if not student_batch_id:
        return False

    batch = current_app.db.batches.find_one({"_id": student_batch_id})
    if not batch or not batch.get("mentor_id"):
        return False

    return str(batch["mentor_id"]) == str(user["_id"])


def can_access_mentor_tools(user):
    if not user:
        return False
    if user.get("role") not in ["admin", "faculty"]:
        return False
    return get_user_designation(user) in {
        DESIGNATION_PROJECT_COORDINATOR,
        DESIGNATION_ACADEMIC_COORDINATOR,
        DESIGNATION_HOD,
        DESIGNATION_FACULTY
    }


def designation_label(designation):
    mapping = {
        DESIGNATION_DIRECTOR: "Director",
        DESIGNATION_PROJECT_COORDINATOR: "Project Coordinator",
        DESIGNATION_ACADEMIC_COORDINATOR: "Academic Coordinator",
        DESIGNATION_HOD: "HOD",
        DESIGNATION_FACULTY: "Faculty"
    }
    return mapping.get(normalize_designation(designation), "Faculty")


def send_designation_update_email(user_record, new_designation, previous_designation=None):
    if not user_record or not user_record.get("email"):
        return

    actor = get_staff_user()
    changed_by_name = actor.get("name", "System Administrator") if actor else "System Administrator"

    try:
        send_email(
            user_record["email"],
            "Your Account Access Has Been Updated",
            designation_update_email(
                user_record.get("name", "User"),
                designation_label(new_designation),
                changed_by_name,
                designation_label(previous_designation) if previous_designation else None
            )
        )
    except Exception as e:
        print("Designation update email error:", e)


def ensure_admin_access_state():
    staff = list(current_app.db.users.find({"role": {"$in": ["admin", "faculty"]}}).sort("created_at", 1))
    if not staff:
        return []

    director = next((user for user in staff if normalize_designation(user.get("designation")) == DESIGNATION_DIRECTOR), None)
    if not director:
        director = next((user for user in staff if user.get("role") == "admin" and user.get("can_manage_admins")), None)
    if not director:
        director = next((user for user in staff if user.get("role") == "admin"), None)
    if not director:
        director = staff[0]

    project_coordinators = [
        user for user in staff
        if normalize_designation(user.get("designation")) == DESIGNATION_PROJECT_COORDINATOR
    ]
    if not project_coordinators:
        legacy_pc = next((user for user in staff if user.get("is_project_coordinator")), None)
        if legacy_pc and str(legacy_pc["_id"]) != str(director["_id"]):
            project_coordinators = [legacy_pc]

    chosen_pc = next((user for user in project_coordinators if str(user["_id"]) != str(director["_id"])), None)
    chosen_ac = next(
        (
            user for user in staff
            if normalize_designation(user.get("designation")) == DESIGNATION_ACADEMIC_COORDINATOR
            and str(user["_id"]) != str(director["_id"])
            and (not chosen_pc or str(user["_id"]) != str(chosen_pc["_id"]))
        ),
        None
    )
    chosen_hod = next(
        (
            user for user in staff
            if normalize_designation(user.get("designation")) == DESIGNATION_HOD
            and str(user["_id"]) != str(director["_id"])
            and (not chosen_pc or str(user["_id"]) != str(chosen_pc["_id"]))
            and (not chosen_ac or str(user["_id"]) != str(chosen_ac["_id"]))
        ),
        None
    )

    for user in staff:
        desired_role = "faculty"
        desired_designation = DESIGNATION_FACULTY
        desired_can_manage_admins = False
        desired_is_pc = False

        if str(user["_id"]) == str(director["_id"]):
            desired_role = "admin"
            desired_designation = DESIGNATION_DIRECTOR
            desired_can_manage_admins = True
        elif chosen_pc and str(user["_id"]) == str(chosen_pc["_id"]):
            desired_designation = DESIGNATION_PROJECT_COORDINATOR
            desired_is_pc = True
        elif chosen_ac and str(user["_id"]) == str(chosen_ac["_id"]):
            desired_designation = DESIGNATION_ACADEMIC_COORDINATOR
        elif chosen_hod and str(user["_id"]) == str(chosen_hod["_id"]):
            desired_designation = DESIGNATION_HOD
        else:
            desired_designation = DESIGNATION_FACULTY

        update_data = {}
        if user.get("role") != desired_role:
            update_data["role"] = desired_role
        if normalize_designation(user.get("designation")) != desired_designation:
            update_data["designation"] = desired_designation
        if bool(user.get("can_manage_admins")) != desired_can_manage_admins:
            update_data["can_manage_admins"] = desired_can_manage_admins
        if bool(user.get("is_project_coordinator")) != desired_is_pc:
            update_data["is_project_coordinator"] = desired_is_pc

        if update_data:
            current_app.db.users.update_one({"_id": user["_id"]}, {"$set": update_data})

    return list(
        current_app.db.users.find(
            {"role": {"$in": ["admin", "faculty"]}}
        ).sort("created_at", 1)
    )


def get_director():
    ensure_admin_access_state()
    return current_app.db.users.find_one(
        {"role": "admin", "designation": DESIGNATION_DIRECTOR},
        sort=[("created_at", 1)]
    )


def current_user_can_manage_admins():
    director = get_director()
    return bool(director and str(director["_id"]) == str(current_user.id))


def mentor_access_required(view_function):
    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        user = get_staff_user()
        if is_director(user):
            flash("Director has a separate dashboard and does not use mentor modules.", "info")
            return redirect(url_for("admin.director_dashboard"))
        if not can_access_mentor_tools(user):
            flash("Only mentor accounts can access that page.", "warning")
            return redirect(url_for("auth.login"))
        return view_function(*args, **kwargs)

    return wrapped_view


def director_only_required(view_function):
    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        ensure_admin_access_state()
        user = get_staff_user()
        if not is_director(user):
            flash("Only Director can access that dashboard.", "warning")
            return redirect(url_for("admin.faculty_dashboard"))
        return view_function(*args, **kwargs)

    return wrapped_view


def operations_access_required(view_function):
    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        ensure_admin_access_state()
        user = get_staff_user()
        if not can_manage_operations(user):
            if is_director(user):
                flash("Director has view governance access, not daily operation management.", "info")
                return redirect(url_for("admin.director_dashboard"))
            flash("Only Project Coordinator can manage operations.", "warning")
            return redirect(url_for("admin.faculty_dashboard"))
        return view_function(*args, **kwargs)

    return wrapped_view


def reports_access_required(view_function):
    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        ensure_admin_access_state()
        user = get_staff_user()
        if not can_view_reports(user):
            flash("You do not have report access.", "warning")
            return redirect(url_for("admin.faculty_dashboard"))
        return view_function(*args, **kwargs)

    return wrapped_view


def student_directory_access_required(view_function):
    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        ensure_admin_access_state()
        user = get_staff_user()
        if not can_view_all_students(user):
            flash("You do not have access to the full student directory.", "warning")
            return redirect(url_for("admin.faculty_dashboard"))
        return view_function(*args, **kwargs)

    return wrapped_view


def evaluation_access_required(view_function):
    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        ensure_admin_access_state()
        user = get_staff_user()
        if not can_view_evaluation_sheet(user):
            flash("You do not have access to evaluation sheets.", "warning")
            return redirect(url_for("admin.faculty_dashboard"))
        return view_function(*args, **kwargs)

    return wrapped_view


def director_access_required(view_function):
    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        ensure_admin_access_state()
        if not current_user_can_manage_admins():
            flash("Only Director can manage role designations and admin transfer.", "danger")
            return redirect(url_for("admin.dashboard"))
        return view_function(*args, **kwargs)

    return wrapped_view


def transfer_director_access(target_user):
    previous_director = get_director()
    target_before = current_app.db.users.find_one({"_id": target_user["_id"]})
    now = datetime.utcnow()

    current_app.db.users.update_many(
        {"role": "admin"},
        {
            "$set": {
                "role": "faculty",
                "designation": DESIGNATION_FACULTY,
                "can_manage_admins": False
            }
        }
    )

    current_app.db.users.update_one(
        {"_id": target_user["_id"]},
        {
            "$set": {
                "role": "admin",
                "designation": DESIGNATION_DIRECTOR,
                "can_manage_admins": True,
                "granted_by": ObjectId(current_user.id),
                "granted_at": now
            }
        }
    )

    target_after = current_app.db.users.find_one({"_id": target_user["_id"]})
    send_designation_update_email(
        target_after,
        DESIGNATION_DIRECTOR,
        get_user_designation(target_before) if target_before else None
    )

    if previous_director and str(previous_director["_id"]) != str(target_user["_id"]):
        current_app.db.users.update_one(
            {"_id": previous_director["_id"]},
            {"$set": {"role": "faculty", "designation": DESIGNATION_FACULTY, "can_manage_admins": False}}
        )
        previous_director_after = current_app.db.users.find_one({"_id": previous_director["_id"]})
        send_designation_update_email(
            previous_director_after,
            DESIGNATION_FACULTY,
            DESIGNATION_DIRECTOR
        )

    ensure_admin_access_state()


def assign_designation(target_user, designation):
    designation = normalize_designation(designation)
    if designation == DESIGNATION_DIRECTOR:
        transfer_director_access(target_user)
        return

    target_before = current_app.db.users.find_one({"_id": target_user["_id"]})
    update_data = {
        "role": "faculty",
        "designation": designation,
        "can_manage_admins": False,
        "updated_at": datetime.utcnow(),
        "updated_by": ObjectId(current_user.id)
    }
    update_data["is_project_coordinator"] = designation == DESIGNATION_PROJECT_COORDINATOR

    displaced_designation = None
    if designation in {
        DESIGNATION_PROJECT_COORDINATOR,
        DESIGNATION_ACADEMIC_COORDINATOR,
        DESIGNATION_HOD
    }:
        displaced_designation = designation
        displaced_members = list(
            current_app.db.users.find(
                {"designation": designation, "_id": {"$ne": target_user["_id"]}}
            )
        )
        current_app.db.users.update_many(
            {"designation": designation, "_id": {"$ne": target_user["_id"]}},
            {"$set": {"designation": DESIGNATION_FACULTY, "is_project_coordinator": False}}
        )
        for displaced_member in displaced_members:
            displaced_after = current_app.db.users.find_one({"_id": displaced_member["_id"]})
            send_designation_update_email(
                displaced_after,
                DESIGNATION_FACULTY,
                displaced_designation
            )

    current_app.db.users.update_one({"_id": target_user["_id"]}, {"$set": update_data})
    target_after = current_app.db.users.find_one({"_id": target_user["_id"]})
    send_designation_update_email(
        target_after,
        designation,
        get_user_designation(target_before) if target_before else None
    )
    ensure_admin_access_state()


def redirect_after_admin_access_change():
    acting_user = get_staff_user()

    if acting_user and is_director(acting_user):
        return redirect(url_for("admin.manage_admin_access"))

    if acting_user and can_manage_operations(acting_user):
        return redirect(url_for("admin.dashboard"))

    return redirect(url_for("admin.faculty_dashboard"))


def ensure_academic_sessions():
    sessions = list(
        current_app.db.academic_sessions.find().sort("created_at", -1)
    )

    if not sessions:
        default_label = _session_label_from_year(None)
        current_app.db.academic_sessions.insert_one({
            "name": default_label,
            "is_active": True,
            "created_at": datetime.utcnow()
        })
        sessions = list(current_app.db.academic_sessions.find().sort("created_at", -1))

    if not any(session.get("is_active") for session in sessions):
        current_app.db.academic_sessions.update_one(
            {"_id": sessions[0]["_id"]},
            {"$set": {"is_active": True}}
        )
        sessions[0]["is_active"] = True

    existing_names = {
        session["name"] for session in sessions if is_valid_session_name(session.get("name"))
    }
    legacy_years = set(
        value for value in current_app.db.students.distinct("year") if is_valid_session_name(value)
    )
    legacy_years.update(
        value for value in current_app.db.batches.distinct("year") if is_valid_session_name(value)
    )

    for legacy_year in legacy_years:
        if legacy_year not in existing_names:
            current_app.db.academic_sessions.insert_one({
                "name": legacy_year,
                "is_active": False,
                "created_at": datetime.utcnow()
            })

    sessions = [
        session
        for session in current_app.db.academic_sessions.find().sort("created_at", -1)
        if is_valid_session_name(session.get("name"))
    ]

    if not sessions:
        default_label = _session_label_from_year(None)
        current_app.db.academic_sessions.insert_one({
            "name": default_label,
            "is_active": True,
            "created_at": datetime.utcnow()
        })
        sessions = list(current_app.db.academic_sessions.find({"name": default_label}))

    return sessions


def get_selected_session(selected_session_id=None):
    sessions = ensure_academic_sessions()
    selected_session = None

    if selected_session_id:
        try:
            selected_session = next(
                (session for session in sessions if session["_id"] == ObjectId(selected_session_id)),
                None
            )
        except Exception:
            selected_session = None

    if not selected_session:
        selected_session = next(
            (session for session in sessions if session.get("is_active")),
            sessions[0]
        )

    return sessions, selected_session


def session_filter(selected_session):
    return {
        "$or": [
            {"session_id": selected_session["_id"]},
            {
                "session_id": {"$exists": False},
                "year": selected_session["name"]
            }
        ]
    }


def get_faculty_assigned_batch(faculty_id, selected_session_id=None):
    sessions, selected_session = get_selected_session(selected_session_id)
    scoped_filter = session_filter(selected_session)

    batch = current_app.db.batches.find_one(
        {
            "mentor_id": faculty_id,
            "$or": scoped_filter["$or"]
        },
        sort=[("created_at", -1)]
    )

    if not batch:
        batch = current_app.db.batches.find_one(
            {"mentor_id": faculty_id},
            sort=[("created_at", -1)]
        )

    return batch, sessions, selected_session


# ===================== DASHBOARD =====================
@admin_bp.route("/dashboard")
@login_required
@operations_access_required
def dashboard():
    ensure_admin_access_state()
    total_batches = current_app.db.batches.count_documents({})
    total_stages = current_app.db.stages.count_documents({})
    total_students = current_app.db.students.count_documents({})
    total_faculty = current_app.db.users.count_documents({"role": "faculty"})
    pending_submissions = current_app.db.submissions.count_documents({"status": "pending"})
    approved_submissions = current_app.db.submissions.count_documents({"status": "approved"})
    late_submissions = current_app.db.submissions.count_documents({"late": True})
    batches = list(current_app.db.batches.find().sort("created_at", -1))

    batch_summaries = []
    for batch in batches[:5]:
        student_count = current_app.db.students.count_documents({"batch_id": batch["_id"]})
        mentor_name = "Not Assigned"

        if batch.get("mentor_id"):
            mentor = current_app.db.users.find_one({"_id": batch["mentor_id"]})
            if mentor:
                mentor_name = mentor.get("name", "Not Assigned")

        batch_summaries.append({
            "name": batch["name"],
            "mentor_name": mentor_name,
            "student_count": student_count
        })

    notifications, unread_count = get_notifications(current_user.id)

    return render_template(
        "admin/dashboard.html",
        total_batches=total_batches,
        total_stages=total_stages,
        total_students=total_students,
        total_faculty=total_faculty,
        pending_submissions=pending_submissions,
        approved_submissions=approved_submissions,
        late_submissions=late_submissions,
        batch_summaries=batch_summaries,
        notifications=notifications,
        unread_count=unread_count
    )


@admin_bp.route("/director/dashboard")
@login_required
@director_only_required
def director_dashboard():
    ensure_admin_access_state()
    total_batches = current_app.db.batches.count_documents({})
    total_stages = current_app.db.stages.count_documents({})
    total_students = current_app.db.students.count_documents({})
    total_faculty = current_app.db.users.count_documents({"role": "faculty"})
    total_submissions = current_app.db.submissions.count_documents({})
    pending_submissions = current_app.db.submissions.count_documents({"status": "pending"})
    approved_submissions = current_app.db.submissions.count_documents({"status": "approved"})
    late_submissions = current_app.db.submissions.count_documents({"late": True})
    final_projects_pending = current_app.db.final_submissions.count_documents({"status": "pending"})

    recent_submissions = list(
        current_app.db.submissions.find().sort("submitted_at", -1).limit(6)
    )
    recent_final_projects = list(
        current_app.db.final_submissions.find().sort("submitted_at", -1).limit(6)
    )

    student_ids = {
        item.get("student_id")
        for item in recent_submissions + recent_final_projects
        if item.get("student_id")
    }
    student_map = {
        str(student["_id"]): student
        for student in current_app.db.students.find({"_id": {"$in": list(student_ids)}})
    } if student_ids else {}

    stage_ids = {
        item.get("stage_id")
        for item in recent_submissions
        if item.get("stage_id")
    }
    stage_map = {
        str(stage["_id"]): stage
        for stage in current_app.db.stages.find({"_id": {"$in": list(stage_ids)}})
    } if stage_ids else {}

    notifications, unread_count = get_notifications(current_user.id)

    return render_template(
        "admin/director_dashboard.html",
        total_batches=total_batches,
        total_stages=total_stages,
        total_students=total_students,
        total_faculty=total_faculty,
        total_submissions=total_submissions,
        pending_submissions=pending_submissions,
        approved_submissions=approved_submissions,
        late_submissions=late_submissions,
        final_projects_pending=final_projects_pending,
        recent_submissions=recent_submissions,
        recent_final_projects=recent_final_projects,
        student_map=student_map,
        stage_map=stage_map,
        notifications=notifications,
        unread_count=unread_count
    )

@admin_bp.route("/profile")
@login_required
@role_required("admin")
def admin_profile():
    ensure_admin_access_state()

    admin = current_app.db.users.find_one({
        "_id": ObjectId(current_user.id)
    })

    return render_template(
        "admin/profile.html",
        admin=admin
    )


@admin_bp.route("/update-admin-profile", methods=["POST"])
@login_required
@role_required("admin")
def update_admin_profile():

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
            return redirect(url_for("admin.admin_profile"))

        password_error = validate_password_rules(new_password)
        if password_error:
            flash(password_error, "danger")
            return redirect(url_for("admin.admin_profile"))

        update_data["password"] = bcrypt.generate_password_hash(new_password).decode("utf-8")
        update_data["password_changed"] = True

    current_app.db.users.update_one(
        {"_id": ObjectId(current_user.id)},
        {"$set": update_data}
    )

    flash("Profile updated successfully")
    return redirect(url_for("admin.admin_profile"))


@admin_bp.route("/academic-sessions", methods=["POST"])
@login_required
@operations_access_required
def create_academic_session():
    name = request.form.get("name", "").strip()
    next_url = request.form.get("next_url") or url_for("admin.manage_students")
    make_active = request.form.get("make_active") == "on"

    if not name:
        flash("Session name is required.", "warning")
        return redirect(next_url)

    if not is_valid_session_name(name):
        flash("Session format must be like 2025-27.", "warning")
        return redirect(next_url)

    existing = current_app.db.academic_sessions.find_one({"name": name})
    if existing:
        flash("Academic session already exists.", "warning")
        return redirect(next_url)

    if make_active:
        current_app.db.academic_sessions.update_many({}, {"$set": {"is_active": False}})

    current_app.db.academic_sessions.insert_one({
        "name": name,
        "is_active": make_active,
        "created_at": datetime.utcnow()
    })

    flash("Academic session created successfully.", "success")
    return redirect(next_url)


@admin_bp.route("/academic-sessions/<session_id>/activate", methods=["POST"])
@login_required
@operations_access_required
def activate_academic_session(session_id):
    next_url = request.form.get("next_url") or url_for("admin.manage_students", session=session_id)

    current_app.db.academic_sessions.update_many({}, {"$set": {"is_active": False}})
    current_app.db.academic_sessions.update_one(
        {"_id": ObjectId(session_id)},
        {"$set": {"is_active": True}}
    )

    flash("Current academic session updated.", "success")
    return redirect(next_url)


# ===================== BATCH MANAGEMENT =====================
# @admin_bp.route("/batches", methods=["GET", "POST"])
# @login_required
# @role_required("admin")
# def manage_batches():

#     if request.method == "POST":
#         name = request.form["name"].strip()

#         if not name:
#             flash("Batch name cannot be empty.")
#             return redirect(url_for("admin.manage_batches"))

#         existing = current_app.db.batches.find_one({"name": name})
#         if existing:
#             flash("Batch already exists.")
#         else:
#             current_app.db.batches.insert_one({
#                 "name": name,
#                 "mentor_id": None,
#                 "created_at": datetime.utcnow()
#             })
#             flash("Batch created successfully.")

#         return redirect(url_for("admin.manage_batches"))

#     batches = list(current_app.db.batches.find().sort("created_at", -1))

#     return render_template("admin/batches.html", batches=batches)

@admin_bp.route("/batches", methods=["GET", "POST"])
@login_required
@operations_access_required
def manage_batches():
    selected_session_id = request.values.get("session")
    sessions, selected_session = get_selected_session(selected_session_id)

    if request.method == "POST":
        name = request.form.get("name")

        if name:
            name = name.strip()
            existing = current_app.db.batches.find_one({
                "name": name,
                "session_id": selected_session["_id"]
            })

            if existing:
                flash("Batch already exists.")
            else:
                current_app.db.batches.insert_one({
                    "name": name,
                    "year": selected_session["name"],
                    "session_id": selected_session["_id"],
                    "mentor_id": None,
                    "created_at": datetime.utcnow()
                })
                flash("Batch created successfully.")

        return redirect(url_for("admin.manage_batches", session=str(selected_session["_id"])))

    batches = list(current_app.db.batches.find(session_filter(selected_session)).sort("created_at", -1))

    # Get all assigned mentor_ids
    assigned_mentors = current_app.db.batches.distinct("mentor_id", session_filter(selected_session))

    # Remove None if exists
    assigned_mentors = [m for m in assigned_mentors if m]

    # Fetch only faculty NOT assigned
    faculty = list(current_app.db.users.find({
    "role": {"$in": ["faculty", "admin"]},
    "_id": {"$nin": assigned_mentors}
    })) 

    assigned_mentors = current_app.db.batches.distinct("mentor_id")
    assigned_mentors = [m for m in assigned_mentors if m]

    faculty = list(current_app.db.users.find({
        "role": {"$in": ["faculty", "admin"]},
        "$or": [
        {"_id": {"$nin": assigned_mentors}},
        {"_id": {"$in": assigned_mentors}}
        ]
        }))

    # Attach mentor name
    for batch in batches:
        if batch.get("mentor_id"):
            mentor = current_app.db.users.find_one(
                {"_id": batch["mentor_id"]}
            )
            batch["mentor_name"] = mentor["name"] if mentor else "N/A"
        else:
            batch["mentor_name"] = None

    return render_template(
        "admin/batches.html",
        batches=batches,
        faculty=faculty,
        sessions=sessions,
        selected_session=selected_session
    )

# ===================== ASSIGN MENTOR =====================
@admin_bp.route("/assign-mentor", methods=["POST"])
@login_required
@operations_access_required
def assign_mentor():

    batch_id = request.form["batch_id"]
    mentor_id = request.form.get("mentor_id")

    batch = current_app.db.batches.find_one({"_id": ObjectId(batch_id)})
    session_query = {"session": str(batch["session_id"])} if batch and batch.get("session_id") else {}

    if not batch:
        return redirect(url_for("admin.manage_batches"))

    # If removing mentor
    if mentor_id == "remove":
        current_app.db.batches.update_one(
            {"_id": ObjectId(batch_id)},
            {"$set": {"mentor_id": None}}
        )
        flash("Mentor removed successfully.")
        return redirect(url_for("admin.manage_batches", **session_query))

    # Check if mentor already assigned to another batch
    already_assigned = current_app.db.batches.find_one({
        "mentor_id": ObjectId(mentor_id),
        "session_id": batch.get("session_id"),
        "_id": {"$ne": ObjectId(batch_id)}
    })

    if already_assigned:
        flash("This mentor is already assigned to another batch.")
        return redirect(url_for("admin.manage_batches", **session_query))

    # Assign / Replace mentor
    current_app.db.batches.update_one(
        {"_id": ObjectId(batch_id)},
        {"$set": {"mentor_id": ObjectId(mentor_id)}}
    )

    mentor = current_app.db.users.find_one({"_id": ObjectId(mentor_id)})
    students = list(current_app.db.students.find({"batch_id": ObjectId(batch_id)}).sort("prn", 1))

    if mentor and mentor.get("email"):
        student_rows_html = "".join(
            f"<tr>"
            f"<td style='padding:12px;border-top:1px solid #e2e8f0;'>{student.get('prn', '-')}</td>"
            f"<td style='padding:12px;border-top:1px solid #e2e8f0;'>{student.get('name', '-')}</td>"
            f"<td style='padding:12px;border-top:1px solid #e2e8f0;'>{student.get('email', 'Not Available')}</td>"
            f"</tr>"
            for student in students
        )
        send_email(
            mentor["email"],
            f"Batch Assigned: {batch['name']}",
            mentor_assignment_email(
                mentor["name"],
                batch["name"],
                batch.get("year"),
                student_rows_html
            )
        )

    print("STUDENTS FOUND:", students)

    for s in students:
        if s.get("email"):
            print("SENDING EMAIL:", s["email"])

            try:
                send_email(
                    s["email"],
                    "Mentor Assigned",
                    student_mentor_assigned_email(
                        s["name"],
                        mentor["name"],
                        mentor.get("email", "Not Available"),
                        batch.get("name"),
                        batch.get("year")
                    )
                )
            except Exception as e:
                print("EMAIL ERROR:", e)

    flash("Mentor updated successfully.")
    return redirect(url_for("admin.manage_batches", **session_query))

# ===================== DELETE BATCH =====================
@admin_bp.route("/delete-batch/<batch_id>")
@login_required
@operations_access_required
def delete_batch(batch_id):
    batch = current_app.db.batches.find_one({"_id": ObjectId(batch_id)})
    session_query = {"session": str(batch["session_id"])} if batch and batch.get("session_id") else {}
    current_app.db.batches.delete_one({"_id": ObjectId(batch_id)})
    flash("Batch deleted.")
    return redirect(url_for("admin.manage_batches", **session_query))


# ===================== STAGE MANAGEMENT =====================
@admin_bp.route("/stages", methods=["GET", "POST"])
@login_required
@operations_access_required
def manage_stages():
    selected_session_id = request.values.get("session")
    sessions, selected_session = get_selected_session(selected_session_id)

    # -------- Add New Stage --------
    if request.method == "POST" and "name" in request.form:
        name = request.form["name"].strip()

        if not name:
            flash("Progress Report name cannot be empty.")
            return redirect(url_for("admin.manage_stages", session=str(selected_session["_id"])))

        last_stage = current_app.db.stages.find_one(sort=[("order", -1)])
        next_order = last_stage["order"] + 1 if last_stage else 1

        current_app.db.stages.insert_one({
            "name": name,
            "order": next_order
        })

        flash("Progress Report added successfully.")
        return redirect(url_for("admin.manage_stages", session=str(selected_session["_id"])))

    # -------- GET DATA --------
    stages = list(current_app.db.stages.find().sort("order", 1))

    deadline_dict = {}
    deadlines = current_app.db.deadlines.find(get_session_deadline_query(selected_session["_id"]))

    for d in deadlines:
        deadline_dict[str(d["stage_id"])] = d["deadline"].strftime("%Y-%m-%d")

    progress_documents = current_app.db.progress_documents.find({
        "session_id": selected_session["_id"]
    })
    progress_document_dict = {
        str(document.get("stage_id")): ensure_progress_document_preview(document)
        for document in progress_documents
    }

    return render_template(
        "admin/stages.html",
        stages=stages,
        sessions=sessions,
        selected_session=selected_session,
        deadline_dict=deadline_dict,
        progress_document_dict=progress_document_dict
    )


@admin_bp.route("/progress-documents/upload", methods=["POST"])
@login_required
@operations_access_required
def upload_progress_document():

    session_id = request.form.get("session_id")
    stage_id = request.form.get("stage_id")
    document = request.files.get("document")

    if not session_id or not stage_id:
        flash("Select a valid progress report before uploading a document.", "warning")
        return redirect(url_for("admin.manage_stages"))

    session = current_app.db.academic_sessions.find_one({"_id": ObjectId(session_id)})
    if not session:
        flash("Selected academic session was not found.", "warning")
        return redirect(url_for("admin.manage_stages"))

    stage = current_app.db.stages.find_one({"_id": ObjectId(stage_id)})
    if not stage:
        flash("Selected progress report was not found.", "warning")
        return redirect(url_for("admin.manage_stages", session=session_id))

    saved_document = save_progress_document(document, session_id)
    if not saved_document:
        flash("Upload a valid previewable document: PDF, DOC, DOCX, PPT, PPTX, XLS, or XLSX.", "warning")
        return redirect(url_for("admin.manage_stages", session=session_id))

    existing_document = current_app.db.progress_documents.find_one({
        "session_id": session["_id"],
        "stage_id": stage["_id"]
    })

    if existing_document and existing_document.get("file_name"):
        files_to_remove = {
            existing_document.get("file_name"),
            existing_document.get("pdf_file")
        }
        for old_file in files_to_remove:
            if not old_file:
                continue

            old_file_path = os.path.join(current_app.config["UPLOAD_FOLDER"], old_file)
            if os.path.exists(old_file_path):
                try:
                    os.remove(old_file_path)
                except OSError:
                    pass

    document_payload = {
        "session_id": session["_id"],
        "session_name": session["name"],
        "stage_id": stage["_id"],
        "stage_name": stage["name"],
        "file_name": saved_document["file_name"],
        "pdf_file": saved_document["pdf_file"],
        "preview_status": saved_document["preview_status"],
        "original_name": saved_document["original_name"],
        "uploaded_by": ObjectId(current_user.id),
        "updated_at": datetime.utcnow()
    }

    app = current_app._get_current_object()

    if existing_document:
        current_app.db.progress_documents.update_one(
            {"_id": existing_document["_id"]},
            {"$set": document_payload}
        )
        document_id = existing_document["_id"]
        is_reupload = True
        flash("Reference document reuploaded. Preview and notifications are processing in the background.", "success")
    else:
        document_payload["created_at"] = datetime.utcnow()
        result = current_app.db.progress_documents.insert_one(document_payload)
        document_id = result.inserted_id
        is_reupload = False
        flash("Reference document uploaded. Preview and notifications are processing in the background.", "success")

    threading.Thread(
        target=process_progress_document_background,
        args=(app, str(document_id), session_id, stage_id, saved_document["original_name"], is_reupload),
        daemon=True
    ).start()

    return redirect(url_for("admin.manage_stages", session=session_id))


@admin_bp.route("/progress-documents/<document_id>/delete", methods=["POST"])
@login_required
@operations_access_required
def delete_progress_document(document_id):

    document = current_app.db.progress_documents.find_one({"_id": ObjectId(document_id)})
    if not document:
        flash("Document not found.", "warning")
        return redirect(url_for("admin.manage_stages"))

    session_id = str(document.get("session_id")) if document.get("session_id") else None

    files_to_remove = {
        document.get("file_name"),
        document.get("pdf_file")
    }
    for filename in files_to_remove:
        if not filename:
            continue

        file_path = os.path.join(current_app.config["UPLOAD_FOLDER"], filename)
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass

    current_app.db.progress_documents.delete_one({"_id": document["_id"]})

    flash("Reference document deleted successfully.", "success")
    if session_id:
        return redirect(url_for("admin.manage_stages", session=session_id))
    return redirect(url_for("admin.manage_stages"))


# ===================== SAVE SINGLE DEADLINE =====================
@admin_bp.route("/save-single-deadline", methods=["POST"])
@login_required
@operations_access_required
def save_single_deadline():

    stage_id = request.form.get("stage_id")
    deadline_value = request.form.get("deadline")
    session_id = request.form.get("session_id")

    if not session_id or not stage_id:
        return redirect(url_for("admin.manage_stages"))

    if deadline_value:
        deadline_date = datetime.strptime(deadline_value, "%Y-%m-%d")

        current_app.db.deadlines.update_one(
            get_session_deadline_query(ObjectId(session_id), ObjectId(stage_id)),
            {
                "$set": {
                    "session_id": ObjectId(session_id),
                    "stage_id": ObjectId(stage_id),
                    "deadline": deadline_date
                }
            },
            upsert=True
        )

    redirect_kwargs = {}
    if session_id:
        redirect_kwargs["session"] = session_id

    return redirect(url_for("admin.manage_stages", **redirect_kwargs))


# ===================== DRAG & DROP REORDER =====================
@admin_bp.route("/update-stage-order", methods=["POST"])
@login_required
@operations_access_required
def update_stage_order():

    data = request.get_json()

    for item in data:
        current_app.db.stages.update_one(
            {"_id": ObjectId(item["id"])},
            {"$set": {"order": item["order"]}}
        )

    return jsonify({"status": "success"})


# ===================== DELETE STAGE =====================
@admin_bp.route("/delete-stage/<stage_id>")
@login_required
@operations_access_required
def delete_stage(stage_id):

    current_app.db.stages.delete_one({"_id": ObjectId(stage_id)})
    current_app.db.deadlines.delete_many({"stage_id": ObjectId(stage_id)})

    flash("Stage deleted successfully.")
    return redirect(url_for("admin.manage_stages"))

# ===================== FACULTY MANAGEMENT =====================
@admin_bp.route("/faculty", methods=["GET", "POST"])
@login_required
@operations_access_required
def manage_faculty():
    ensure_admin_access_state()
    faculty_list = list(current_app.db.users.find({"role": "faculty"}))
    form_data = {"name": "", "email": ""}

    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        form_data = {"name": name, "email": email}

        if not name or not email or not password:
            flash("All fields are required.", "danger")
            return render_template("admin/faculty.html", faculty=faculty_list, form_data=form_data)

        if not is_valid_email(email):
            flash("Username must be a valid email address.", "danger")
            return render_template("admin/faculty.html", faculty=faculty_list, form_data=form_data)

        password_error = validate_password_rules(password)
        if password_error:
            flash(password_error, "danger")
            return render_template("admin/faculty.html", faculty=faculty_list, form_data=form_data)

        existing = current_app.db.users.find_one({
            "email": {
                "$regex": f"^{re.escape(email)}$",
                "$options": "i"
            }
        })
        if existing:
            flash("Faculty already exists.", "danger")
            return render_template("admin/faculty.html", faculty=faculty_list, form_data=form_data)

        hashed_password = bcrypt.generate_password_hash(password).decode("utf-8")

        current_app.db.users.insert_one({
            "name": name,
            "email": email,
            "password": hashed_password,
            "role": "faculty",
            "designation": DESIGNATION_FACULTY,
            "created_at": datetime.utcnow()
        })

        mail_sent = send_email(
            email,
            "Your Faculty Account Credentials",
            faculty_welcome_email(name, email, password)
        )

        if mail_sent:
            flash("Faculty created successfully and welcome email sent.", "success")
        else:
            flash("Faculty created successfully, but the welcome email could not be sent. Please check mail settings.", "warning")
        return redirect(url_for("admin.manage_faculty"))

    return render_template("admin/faculty.html", faculty=faculty_list, form_data=form_data)


@admin_bp.route("/admin-access")
@login_required
@director_access_required
def manage_admin_access():
    ensure_admin_access_state()
    staff_members = list(
        current_app.db.users.find({"role": {"$in": ["admin", "faculty"]}}).sort("created_at", 1)
    )
    director = get_director()

    staff_rows = []
    for member in staff_members:
        member_designation = get_user_designation(member)
        staff_rows.append(
            {
                "_id": member["_id"],
                "name": member.get("name", "Not Available"),
                "email": member.get("email", "Not Available"),
                "designation": member_designation,
                "is_director": member_designation == DESIGNATION_DIRECTOR,
                "is_project_coordinator": member_designation == DESIGNATION_PROJECT_COORDINATOR
            }
        )

    return render_template(
        "admin/admin_access.html",
        staff_rows=staff_rows,
        current_admin=director,
        designation_labels={
            DESIGNATION_DIRECTOR: "Director",
            DESIGNATION_PROJECT_COORDINATOR: "Project Coordinator",
            DESIGNATION_ACADEMIC_COORDINATOR: "Academic Coordinator",
            DESIGNATION_HOD: "HOD",
            DESIGNATION_FACULTY: "Faculty"
        }
    )


@admin_bp.route("/admin-access/promote/<user_id>", methods=["POST"])
@login_required
@director_access_required
def promote_user_to_admin(user_id):
    ensure_admin_access_state()
    user = current_app.db.users.find_one({"_id": ObjectId(user_id)})
    if not user or user.get("role") not in ["faculty", "admin"]:
        flash("Selected user was not found.", "danger")
        return redirect(url_for("admin.manage_admin_access"))

    transfer_director_access(user)

    flash("Director access transferred successfully.", "success")
    return redirect_after_admin_access_change()


@admin_bp.route("/admin-access/transfer-privilege/<user_id>", methods=["POST"])
@login_required
@director_access_required
def transfer_admin_privilege(user_id):
    return promote_user_to_admin(user_id)


@admin_bp.route("/admin-access/assign-coordinator/<user_id>", methods=["POST"])
@login_required
@director_access_required
def assign_project_coordinator(user_id):
    ensure_admin_access_state()
    target = current_app.db.users.find_one({"_id": ObjectId(user_id)})
    if not target or target.get("role") not in ["faculty", "admin"]:
        flash("Selected user was not found.", "danger")
        return redirect(url_for("admin.manage_admin_access"))
    assign_designation(target, DESIGNATION_PROJECT_COORDINATOR)
    flash("Project Coordinator assigned successfully.", "success")
    return redirect_after_admin_access_change()


@admin_bp.route("/admin-access/assign-admin-coordinator/<user_id>", methods=["POST"])
@login_required
@director_access_required
def assign_admin_and_project_coordinator(user_id):
    target = current_app.db.users.find_one({"_id": ObjectId(user_id)})
    if not target or target.get("role") not in ["faculty", "admin"]:
        flash("Selected user was not found.", "danger")
        return redirect(url_for("admin.manage_admin_access"))
    transfer_director_access(target)
    assign_designation(target, DESIGNATION_PROJECT_COORDINATOR)
    flash("Director and Project Coordinator updated successfully.", "success")
    return redirect(url_for("admin.manage_admin_access"))


@admin_bp.route("/admin-access/demote/<user_id>", methods=["POST"])
@login_required
@director_access_required
def demote_admin_user(user_id):
    ensure_admin_access_state()
    target = current_app.db.users.find_one({"_id": ObjectId(user_id)})
    if not target:
        flash("Selected user was not found.", "danger")
        return redirect(url_for("admin.manage_admin_access"))

    if str(target["_id"]) == str(current_user.id):
        flash("Transfer Director access first before changing your own designation.", "warning")
        return redirect(url_for("admin.manage_admin_access"))

    if is_director(target):
        flash("Transfer Director access before demoting this account.", "warning")
        return redirect(url_for("admin.manage_admin_access"))

    assign_designation(target, DESIGNATION_FACULTY)
    flash("Designation reset to Faculty successfully.", "success")
    return redirect(url_for("admin.manage_admin_access"))


@admin_bp.route("/admin-access/set-designation/<user_id>", methods=["POST"])
@login_required
@director_access_required
def set_staff_designation(user_id):
    ensure_admin_access_state()
    designation = normalize_designation(request.form.get("designation"))
    allowed_designations = {
        DESIGNATION_DIRECTOR,
        DESIGNATION_PROJECT_COORDINATOR,
        DESIGNATION_ACADEMIC_COORDINATOR,
        DESIGNATION_HOD
    }
    target = current_app.db.users.find_one({"_id": ObjectId(user_id)})
    if not target or target.get("role") not in ["faculty", "admin"]:
        flash("Selected user was not found.", "danger")
        return redirect(url_for("admin.manage_admin_access"))

    if designation not in allowed_designations:
        flash("Invalid designation request.", "warning")
        return redirect(url_for("admin.manage_admin_access"))

    if str(target["_id"]) == str(current_user.id) and designation != DESIGNATION_DIRECTOR:
        flash("Transfer Director role first before changing your own designation.", "warning")
        return redirect(url_for("admin.manage_admin_access"))

    if designation == DESIGNATION_DIRECTOR:
        transfer_director_access(target)
        flash("Director role transferred successfully.", "success")
        return redirect_after_admin_access_change()

    assign_designation(target, designation)
    flash("Designation updated successfully.", "success")
    return redirect(url_for("admin.manage_admin_access"))


@admin_bp.route("/admin-access/remove-designation/<user_id>", methods=["POST"])
@login_required
@director_access_required
def remove_staff_designation(user_id):
    ensure_admin_access_state()
    target = current_app.db.users.find_one({"_id": ObjectId(user_id)})
    if not target or target.get("role") not in ["faculty", "admin"]:
        flash("Selected user was not found.", "danger")
        return redirect(url_for("admin.manage_admin_access"))

    current_designation = get_user_designation(target)
    removable_designations = {
        DESIGNATION_PROJECT_COORDINATOR,
        DESIGNATION_ACADEMIC_COORDINATOR,
        DESIGNATION_HOD
    }
    if current_designation not in removable_designations:
        flash("This user does not have a removable AC/PC/HOD designation.", "warning")
        return redirect(url_for("admin.manage_admin_access"))

    assign_designation(target, DESIGNATION_FACULTY)
    flash(f"{designation_label(current_designation)} role removed successfully.", "success")
    return redirect(url_for("admin.manage_admin_access"))


@admin_bp.route("/faculty/profile", methods=["GET", "POST"])
@login_required
@role_required("faculty")
def faculty_profile():

    faculty = current_app.db.users.find_one({
        "_id": ObjectId(current_user.id)
    })
    batch, _, _ = get_faculty_assigned_batch(faculty["_id"], request.args.get("session"))

    if request.method == "POST":

        name = request.form.get("name")
        email = request.form.get("email")

        current_app.db.users.update_one(
            {"_id": faculty["_id"]},
            {
                "$set": {
                    "name": name,
                    "email": email
                }
            }
        )

        flash("Profile updated successfully")
        return redirect(url_for("admin.faculty_profile"))

    return render_template("faculty/profile.html", faculty=faculty, batch=batch)


# ===================== DELETE FACULTY =====================
@admin_bp.route("/delete-faculty/<faculty_id>")
@login_required
@operations_access_required
def delete_faculty(faculty_id):

    current_app.db.users.delete_one({"_id": ObjectId(faculty_id)})
    flash("Faculty deleted.")
    return redirect(url_for("admin.manage_faculty"))

# ===================== FACULTY DASHBOARD =====================
@admin_bp.route("/faculty/dashboard")
@login_required
@mentor_access_required
def faculty_dashboard():

    mentor_id = ObjectId(current_user.id)
    batch, _, _ = get_faculty_assigned_batch(mentor_id, request.args.get("session"))

    students = []
    stages = list(current_app.db.stages.find().sort("order", 1))

    if batch:
        students = list(current_app.db.students.find({
            "batch_id": batch["_id"]
        }))

    submissions = list(current_app.db.submissions.find({
        "student_id": {"$in": [s["_id"] for s in students]}
    }))

    submission_dict = {}

    for s in submissions:
        key = str(s["student_id"]) + "_" + str(s["stage_id"])
        submission_dict[key] = s

    deadlines = list(current_app.db.deadlines.find(
        get_session_deadline_query(batch["session_id"])
    )) if batch and batch.get("session_id") else []

    deadline_dict = {}

    for d in deadlines:
        deadline_dict[str(d["stage_id"])] = d["deadline"]

    progress_document_dict = {}
    if batch and batch.get("session_id"):
        progress_documents = current_app.db.progress_documents.find({
            "session_id": batch["session_id"]
        })
        progress_document_dict = {
            str(document.get("stage_id")): ensure_progress_document_preview(document)
            for document in progress_documents
        }

    total_students = len(students)

    pending_reviews = 0
    approved_count = 0
    late_submissions = 0

    for s in submissions:

        if s.get("status") == "pending":
            pending_reviews += 1

        if s.get("status") == "approved":
            approved_count += 1

        if s.get("late") == True:
            late_submissions += 1

    alerts = []

    if pending_reviews > 0:
        alerts.append(f"{pending_reviews} submissions pending review")

    if late_submissions > 0:
        alerts.append(f"{late_submissions} late submissions detected")

    if approved_count > 0:
        alerts.append(f"{approved_count} submissions approved")
    
    notifications, unread_count = get_notifications(current_user.id)

    return render_template(
        "faculty/dashboard.html",
        batch=batch,
        students=students,
        stages=stages,
        submission_dict=submission_dict,
        deadline_dict=deadline_dict,
        progress_document_dict=progress_document_dict,
        total_students=total_students,
        pending_reviews=pending_reviews,
        approved_count=approved_count,
        late_submissions=late_submissions,
        alerts=alerts,
        notifications=notifications,
        unread_count=unread_count
    )

# ---------------- STUDENT MANAGEMENT ----------------
@admin_bp.route("/students")
@login_required
@student_directory_access_required
def manage_students():
    user = get_staff_user()
    can_edit_students = can_manage_operations(user)

    selected_session_id = request.args.get("session")
    sessions, selected_session = get_selected_session(selected_session_id)

    students = list(current_app.db.students.find(session_filter(selected_session)).sort("prn", 1))

    batches = list(current_app.db.batches.find(session_filter(selected_session)).sort("created_at", -1))

    # Optional: map batch_id → batch name if you still need it elsewhere
    batch_map = {str(batch["_id"]): batch["name"] for batch in batches}

    for student in students:
        student["batch_name"] = batch_map.get(str(student.get("batch_id")), "Not Assigned")

    return render_template(
        "admin/students.html",
        students=students,
        batches=batches,
        sessions=sessions,
        selected_session=selected_session,
        can_edit_students=can_edit_students
    )

# @admin_bp.route("/add-student", methods=["POST"])
# @login_required
# @role_required("admin")
# def add_student():

#     name = request.form["name"]
#     prn = request.form["prn"]
#     batch_id = request.form["batch_id"]

#     existing = current_app.db.students.find_one({"prn": prn})

#     if existing:
#         flash("Student already exists")
#         return redirect(url_for("admin.manage_students"))

#     raw_password = prn
#     password = bcrypt.generate_password_hash(raw_password).decode("utf-8")

#     # 🔥 CREATE STUDENT OBJECT
#     student_data = {
#         "name": name,
#         "prn": prn,
#         "email": "",  # optional
#         "password": password,
#         "batch_id": ObjectId(batch_id),
#         "role": "student",
#         "password_changed": False,
#         "created_at": datetime.utcnow()
#     }

#     current_app.db.students.insert_one(student_data)

#     # 🔥 SEND EMAIL (if email exists)
#     if student_data["email"]:
#         send_email(
#             student_data["email"],
#             "Welcome to ZIBACAR",
#             student_welcome_email(name, prn, raw_password)
#         )

#     flash("Student added successfully")
#     return redirect(url_for("admin.manage_students"))


@admin_bp.route("/add-student", methods=["POST"])
@login_required
@operations_access_required
def add_student():

    name = request.form["name"]
    prn = request.form["prn"]
    email = request.form["email"].strip().replace(" ", "")
    student_mobile = request.form.get("student_mobile", "").strip()
    parent_mobile = request.form.get("parent_mobile", "").strip()
    class_name = request.form.get("student_class", "").strip()
    division = request.form.get("division", "").strip()
    roll_no = request.form.get("roll_no", "").strip()
    batch_id = request.form["batch_id"]
    session_id = request.form["session_id"]
    session = current_app.db.academic_sessions.find_one({"_id": ObjectId(session_id)})

    if not session:
        flash("Select a valid academic session.", "warning")
        return redirect(url_for("admin.manage_students"))

    batch = current_app.db.batches.find_one({"_id": ObjectId(batch_id)})

    if not batch or batch.get("session_id") != session["_id"]:
        flash("Select a batch from the active academic session.", "warning")
        return redirect(url_for("admin.manage_students", session=str(session["_id"])))

    existing = current_app.db.students.find_one({
        "prn": prn,
        "$or": [
            {"session_id": session["_id"]},
            {
                "session_id": {"$exists": False},
                "year": session["name"]
            }
        ]
    })

    if existing:
        flash("Student with this PRN already exists in the selected session.")
        return redirect(url_for("admin.manage_students", session=str(session["_id"])))

    existing_email = current_app.db.students.find_one({
        "email": email,
        "$or": [
            {"session_id": session["_id"]},
            {
                "session_id": {"$exists": False},
                "year": session["name"]
            }
        ]
    })

    if existing_email:
        flash("Email already exists in the selected session", "warning")
        return redirect(url_for("admin.manage_students", session=str(session["_id"])))

    # ✅ ALWAYS DEFINE BEFORE USING
    raw_password = prn
    password = bcrypt.generate_password_hash(raw_password).decode("utf-8")

    student_data = {
        "name": name,
        "prn": prn,
        "email": email,
        "student_mobile": student_mobile,
        "parent_mobile": parent_mobile,
        "class": class_name,
        "division": division,
        "roll_no": roll_no,
        "year": session["name"],
        "session_id": session["_id"],
        "batch_id": batch["_id"],
        "role": "student",
        "password": password,
        "password_changed": False,
        "created_at": datetime.utcnow()
    }

    # ✅ INSERT
    result = current_app.db.students.insert_one(student_data)
    print("INSERTED:", result.inserted_id)

    # ✅ EMAIL (SAFE)
    try:
        send_email(
            email,
            "Welcome to ZIBACAR",
            student_welcome_email(name, prn, raw_password)
        )
    except Exception as e:
        print("EMAIL ERROR:", e)

    flash("Student added successfully")

    return redirect(url_for("admin.manage_students", session=str(session["_id"])))


# ===================== UPLOAD STUDENTS =====================
@admin_bp.route("/upload-students", methods=["POST"])
@login_required
@operations_access_required
def upload_students():

    file = request.files["file"]
    session_id = request.form.get("session_id")

    if not session_id:
        flash("Select an academic session before bulk upload.", "warning")
        return redirect(url_for("admin.manage_students"))

    session = current_app.db.academic_sessions.find_one({"_id": ObjectId(session_id)})

    if not session:
        flash("Selected academic session was not found.", "warning")
        return redirect(url_for("admin.manage_students"))

    # df = pd.read_excel(file)
    # df.columns = df.columns.str.strip().str.lower()  # remove any leading/trailing spaces from column names
    # for _, row in df.iterrows():

    #     prn = str(row["PRN"]).strip()
    #     name = str(row["Name"]).strip()
    #     year = str(row["Year"]).strip()

    #     # email may or may not exist in excel
    #     email = row.get("Email", "")

    #     existing = current_app.db.students.find_one({"prn": prn})

    #     password = bcrypt.generate_password_hash(prn).decode("utf-8")

    #     if existing:

    #         # FIX old records missing role/password
    #         current_app.db.students.update_one(
    #             {"_id": existing["_id"]},
    #             {
    #                 "$set": {
    #                     "name": name,
    #                     "year": year,
    #                     "email": email,
    #                     "role": "student",
    #                     "password": password,
    #                     "password_changed": False
    #                 }
    #             }
    #         )

    #     else:

    #         current_app.db.students.insert_one({

    #             "prn": prn,
    #             "name": name,
    #             "email": email,
    #             "year": year,
    #             "batch_id": None,

    #             "role": "student",
    #             "password": password,
    #             "password_changed": False,

    #             "created_at": datetime.utcnow()

    #         })

    #         # 🔥 SEND EMAIL IF EMAIL EXISTS
    #         if email:
    #             send_email(
    #                 email,
    #                 "Welcome to ZIBACAR",
    #                 student_welcome_email(name, prn, raw_password)
    #             )  


    df = pd.read_excel(file, header=None)

    column_aliases = {
        "prn": ["prn", "rollno", "rollnumber", "studentid"],
        "name": ["name", "studentname", "fullname"],
        "email": ["email", "emailid", "emailaddress", "mail"],
        "student_mobile": ["studentmobile", "studentmobileno", "studentphone", "studentcontact", "mobileno", "mobile"],
        "parent_mobile": ["parentmobile", "parentmobileno", "parentphone", "parentcontact", "guardianmobile", "guardianphone", "parentno"],
        "class": ["class", "classname", "studentclass"],
        "division": ["division", "div", "section", "classdivision"],
        "roll_no": ["rollno", "rollnumber", "roll", "classrollno"]
    }

    header_row_index = None
    original_columns = []
    normalized_columns = []

    for idx in range(min(len(df), 10)):
        row_values = ["" if pd.isna(value) else str(value).strip() for value in df.iloc[idx].tolist()]
        normalized_row = [normalize_excel_header(value) for value in row_values]

        has_prn = any(value in column_aliases["prn"] for value in normalized_row)
        has_name = any(value in column_aliases["name"] for value in normalized_row)

        if has_prn and has_name:
            header_row_index = idx
            original_columns = row_values
            normalized_columns = normalized_row
            break

    if header_row_index is None:
        flash("Excel must contain PRN and Name columns. Column order can be anything.", "warning")
        return redirect(url_for("admin.manage_students", session=str(session["_id"])))

    df = df.iloc[header_row_index + 1:].reset_index(drop=True)
    df.columns = normalized_columns

    resolved_columns = {}
    for key, aliases in column_aliases.items():
        resolved_columns[key] = next((alias for alias in aliases if alias in normalized_columns), None)

    if not resolved_columns["prn"] or not resolved_columns["name"]:
        flash("Excel must contain PRN and Name columns. Column order can be anything.", "warning")
        return redirect(url_for("admin.manage_students", session=str(session["_id"])))

    print("COLUMNS:", original_columns)
    print(df.head())

    inserted_count = 0
    skipped_prns = []

    for _, row in df.iterrows():

        # ✅ ACCESS BY POSITION (SAFE)
        prn = str(row.get(resolved_columns["prn"], "")).strip()
        name = str(row.get(resolved_columns["name"], "")).strip()
        email = ""
        if resolved_columns["email"]:
            email = str(row.get(resolved_columns["email"], "")).strip()
        student_mobile = ""
        if resolved_columns["student_mobile"]:
            student_mobile = str(row.get(resolved_columns["student_mobile"], "")).strip()
        parent_mobile = ""
        if resolved_columns["parent_mobile"]:
            parent_mobile = str(row.get(resolved_columns["parent_mobile"], "")).strip()
        class_name = ""
        if resolved_columns["class"]:
            class_name = str(row.get(resolved_columns["class"], "")).strip()
        division = ""
        if resolved_columns["division"]:
            division = str(row.get(resolved_columns["division"], "")).strip()
        roll_no = ""
        if resolved_columns["roll_no"]:
            roll_no = str(row.get(resolved_columns["roll_no"], "")).strip()

        if student_mobile.lower() == "nan":
            student_mobile = ""
        if parent_mobile.lower() == "nan":
            parent_mobile = ""
        if class_name.lower() == "nan":
            class_name = ""
        if division.lower() == "nan":
            division = ""
        if roll_no.lower() == "nan":
            roll_no = ""
        year = session["name"]

        print("PROCESSING:", prn, name)

        # ❌ Skip empty rows
        if not prn or prn.lower() == "nan":
            continue

        if not name or name.lower() == "nan":
            continue

        raw_password = prn
        password = bcrypt.generate_password_hash(raw_password).decode("utf-8")

        existing = current_app.db.students.find_one({
            "prn": prn,
            "$or": [
                {"session_id": session["_id"]},
                {
                    "session_id": {"$exists": False},
                    "year": session["name"]
                }
            ]
        })

        if existing:
            print("SKIPPED:", prn)
            skipped_prns.append(prn)
            continue

        current_app.db.students.insert_one({
            "prn": prn,
            "name": name,
            "email": email,
            "student_mobile": student_mobile,
            "parent_mobile": parent_mobile,
            "class": class_name,
            "division": division,
            "roll_no": roll_no,
            "year": year,
            "session_id": session["_id"],
            "batch_id": None,
            "role": "student",
            "password": password,
            "password_changed": False,
            "created_at": datetime.utcnow()
        })

        print("INSERTED:", prn)
        inserted_count += 1

    if inserted_count and skipped_prns:
        flash(
            f"{inserted_count} students uploaded. {len(skipped_prns)} skipped because PRN already exists: {', '.join(skipped_prns[:5])}"
            + (" ..." if len(skipped_prns) > 5 else ""),
            "warning"
        )
    elif inserted_count:
        flash(f"{inserted_count} students uploaded successfully.", "success")
    elif skipped_prns:
        flash(
            f"No new students were added. All rows were skipped because PRN already exists: {', '.join(skipped_prns[:5])}"
            + (" ..." if len(skipped_prns) > 5 else ""),
            "warning"
        )
    else:
        flash("No valid student rows were found in the uploaded sheet.", "warning")

    return redirect(url_for("admin.manage_students", session=str(session["_id"])))

@admin_bp.route("/download-template")
@login_required
@operations_access_required
def download_template():

    df = pd.DataFrame({
        "PRN": [],
        "Name": [],
        "Email": [],
        "Student Mobile": [],
        "Parent Mobile": [],
        "Class": [],
        "Division": [],
        "Roll No": []
    })

    path = "student_template.xlsx"

    df.to_excel(path, index=False)

    return send_file(path, as_attachment=True)


@admin_bp.route("/assign-students/<batch_id>", methods=["POST"])
@login_required
@operations_access_required
def assign_students(batch_id):

    student_ids = request.form.getlist("students")
    batch = current_app.db.batches.find_one({"_id": ObjectId(batch_id)})
    session_query = {"session": str(batch["session_id"])} if batch and batch.get("session_id") else {}

    for sid in student_ids:

        current_app.db.students.update_one(
            {"_id": ObjectId(sid)},
            {"$set": {"batch_id": ObjectId(batch_id)}}
        )

    flash("Students assigned successfully")

    return redirect(url_for("admin.manage_batches", **session_query))

# ---------------- ASSIGN STUDENTS PAGE ----------------
@admin_bp.route("/assign-students/<batch_id>")
@login_required
@operations_access_required
def assign_students_page(batch_id):

    batch = current_app.db.batches.find_one({"_id": ObjectId(batch_id)})

    if not batch:
        flash("Batch not found.", "warning")
        return redirect(url_for("admin.manage_batches"))

    students = list(current_app.db.students.find({
        "$and": [
            session_filter({
                "_id": batch.get("session_id"),
                "name": batch.get("year")
            }),
            {
                "$or": [
                    {"batch_id": None},
                    {"batch_id": ObjectId(batch_id)}
                ]
            }
        ]
    }).sort("prn", 1))

    selected_student_ids = {
        str(student["_id"]) for student in students if student.get("batch_id") == batch["_id"]
    }

    return render_template(
        "admin/assign_students.html",
        batch=batch,
        students=students,
        selected_student_ids=selected_student_ids
    )

# ---------------- SAVE ASSIGNED STUDENTS ----------------
@admin_bp.route("/save-students/<batch_id>", methods=["POST"])
@login_required
@operations_access_required
def save_assigned_students(batch_id):

    student_ids = request.form.getlist("students")
    batch = current_app.db.batches.find_one({"_id": ObjectId(batch_id)})
    session_query = {"session": str(batch["session_id"])} if batch and batch.get("session_id") else {}

    if not batch:
        flash("Batch not found.", "warning")
        return redirect(url_for("admin.manage_batches"))

    normalized_student_ids = [ObjectId(student_id) for student_id in student_ids]

    # remove students already in this batch
    current_app.db.students.update_many(
        {"batch_id": ObjectId(batch_id)},
        {"$set": {"batch_id": None}}
    )

    # assign selected students
    for sid in normalized_student_ids:
        current_app.db.students.update_one(
            {"_id": sid},
            {"$set": {"batch_id": ObjectId(batch_id)}}
        )
 
    mentor = None
    
    if batch and batch.get("mentor_id"):
        mentor = current_app.db.users.find_one({"_id": batch["mentor_id"]})

        # print("MENTOR:", mentor)

    for sid in normalized_student_ids:

        student = current_app.db.students.find_one({"_id": sid})

        print("STUDENT:", student)

        if student and student.get("email") and mentor:

            print("SENDING EMAIL TO:", student["email"])

            try:
                send_email(
                    student["email"],
                    "Mentor Assigned",
                    student_mentor_assigned_email(
                        student["name"],
                        mentor["name"],
                        mentor.get("email", "Not Available"),
                        batch.get("name"),
                        batch.get("year")
                    )
                )
            except Exception as e:
                print("EMAIL ERROR:", e)


    # ================= SEND EMAIL TO FACULTY =================

    batch = current_app.db.batches.find_one({"_id": ObjectId(batch_id)})

    if batch and batch.get("mentor_id"):

        mentor = current_app.db.users.find_one({"_id": batch["mentor_id"]})

        if mentor and mentor.get("email"):

            # Get assigned students
            assigned_students = list(current_app.db.students.find({
                "batch_id": ObjectId(batch_id)
            }))

            # Build student list HTML
            student_list_html = ""

            for s in assigned_students:
                student_list_html += (
                    f"<tr>"
                    f"<td style='padding:12px;border-top:1px solid #e2e8f0;'>{s.get('prn', '-')}</td>"
                    f"<td style='padding:12px;border-top:1px solid #e2e8f0;'>{s.get('name', '-')}</td>"
                    f"<td style='padding:12px;border-top:1px solid #e2e8f0;'>{s.get('email', 'Not Available')}</td>"
                    f"</tr>"
                )

            try:
                send_email(
                    mentor["email"],
                    f"Updated Student Roster: {batch['name']}",
                    mentor_assignment_email(
                        mentor["name"],
                        batch["name"],
                        batch.get("year"),
                        student_list_html
                    )
                )

                print("FACULTY EMAIL SENT:", mentor["email"])

            except Exception as e:
                print("EMAIL ERROR (FACULTY):", e)


    if normalized_student_ids:
        flash("Students assigned successfully", "success")
    else:
        flash("No students were selected. Existing assignments for this batch were cleared.", "warning")

    return redirect(url_for("admin.manage_batches", **session_query))

# ---------------- FACULTY STUDENTS ----------------
@admin_bp.route("/faculty/students")
@login_required
@mentor_access_required
def faculty_students():

    faculty_id = current_user.id

    faculty= current_app.db.users.find_one({
        "_id": ObjectId(faculty_id)
    })

    batch, _, _ = get_faculty_assigned_batch(ObjectId(current_user.id), request.args.get("session"))

    if not batch:
        return render_template("faculty/students.html", students=[], batch=None)

    students = list(current_app.db.students.find({
        "batch_id": batch["_id"]
    }))

    for s in students:
        total = current_app.db.stages.count_documents({})
        approved = current_app.db.submissions.count_documents({
            "student_id": s["_id"],
            "status": "approved"
        })

        s["progress"] = int((approved / total) * 100) if total > 0 else 0

    return render_template(
        "faculty/students.html",
        students=students,
        batch=batch
    )


# update faculty profile
@admin_bp.route("/update-faculty-profile", methods=["POST"])
@login_required
@role_required("faculty")
def update_faculty_profile():

    name = request.form.get("name")
    email = request.form.get("email")
    mobile = request.form.get("mobile", "").strip()
    file = request.files.get("photo")
    new_password = request.form.get("new_password", "").strip()
    confirm_password = request.form.get("confirm_password", "").strip()

    update_data = {
        "name": name,
        "email": email,
        "mobile": mobile
    }

    if file and file.filename:
        filename = save_profile_photo(file, current_user.id)
        update_data["photo"] = filename

    if new_password or confirm_password:
        if new_password != confirm_password:
            flash("New password and confirm password must match.", "danger")
            return redirect(url_for("admin.faculty_profile"))

        password_error = validate_password_rules(new_password)
        if password_error:
            flash(password_error, "danger")
            return redirect(url_for("admin.faculty_profile"))

        update_data["password"] = bcrypt.generate_password_hash(new_password).decode("utf-8")
        update_data["password_changed"] = True

    current_app.db.users.update_one(
        {"_id": ObjectId(current_user.id)},
        {"$set": update_data}
    )

    flash("Profile updated successfully")
    return redirect(url_for("admin.faculty_profile"))


# ---------------- MENTOR SUBMISSIONS ----------------
@admin_bp.route("/mentor-submissions")
@login_required
@mentor_access_required
def mentor_submissions():

    mentor_id = ObjectId(current_user.id)
    batch, _, _ = get_faculty_assigned_batch(mentor_id, request.args.get("session"))

    if not batch:
        return render_template(
            "faculty/submissions.html",
            submissions=[],
            student_map={},
            stage_map={},
            batch=None,
            batch_notice="No batch is allocated to you till now."
        )

    students = list(current_app.db.students.find({
        "batch_id": batch["_id"]
    }))

    student_map = {str(s["_id"]): s["name"] for s in students}

    stages = list(current_app.db.stages.find())
    stage_map = {str(s["_id"]): s["name"] for s in stages}

    student_ids = [s["_id"] for s in students]

    submissions = list(current_app.db.submissions.find({
        "student_id": {"$in": student_ids}
    }))

    return render_template(
        "faculty/submissions.html",
        submissions=submissions,
        student_map=student_map,
        stage_map=stage_map,
        batch=batch,
        batch_notice=None
    )


@admin_bp.route("/mentor-final-projects")
@login_required
@mentor_access_required
def mentor_final_projects():

    mentor_id = ObjectId(current_user.id)
    batch, _, _ = get_faculty_assigned_batch(mentor_id, request.args.get("session"))

    if not batch:
        return render_template(
            "faculty/final_projects.html",
            final_projects=[],
            student_map={},
            batch=None,
            batch_notice="No batch is allocated to you till now."
        )

    students = list(current_app.db.students.find({"batch_id": batch["_id"]}))
    student_ids = [student["_id"] for student in students]
    student_map = {str(student["_id"]): student for student in students}

    final_projects = list(
        current_app.db.final_submissions.find({"student_id": {"$in": student_ids}}).sort("submitted_at", -1)
    )

    return render_template(
        "faculty/final_projects.html",
        final_projects=final_projects,
        student_map=student_map,
        batch=batch,
        batch_notice=None
    )


@admin_bp.route("/final-projects")
@login_required
@reports_access_required
def admin_final_projects():

    selected_session_id = request.args.get("session")
    sessions, selected_session = get_selected_session(selected_session_id)
    final_projects = list(
        current_app.db.final_submissions.find({
            "$or": [
                {"session_id": selected_session["_id"]},
                {"session_id": {"$exists": False}}
            ]
        }).sort("submitted_at", -1)
    )

    student_map = {}
    mentor_map = {}
    batch_map = {}
    current_admin_batch, _, _ = get_faculty_assigned_batch(ObjectId(current_user.id), selected_session_id)
    reviewable_batch_id = str(current_admin_batch["_id"]) if current_admin_batch else None

    for item in final_projects:
        student = current_app.db.students.find_one({"_id": item["student_id"]})
        if student:
            student_map[str(student["_id"])] = student

        mentor_id = item.get("mentor_id")
        if mentor_id and str(mentor_id) not in mentor_map:
            mentor_map[str(mentor_id)] = current_app.db.users.find_one({"_id": mentor_id})

        batch_id = item.get("batch_id")
        if batch_id and str(batch_id) not in batch_map:
            batch_map[str(batch_id)] = current_app.db.batches.find_one({"_id": batch_id})

        item["can_review"] = bool(reviewable_batch_id and batch_id and str(batch_id) == reviewable_batch_id)

    return render_template(
        "admin/final_projects.html",
        final_projects=final_projects,
        student_map=student_map,
        mentor_map=mentor_map,
        batch_map=batch_map,
        sessions=sessions,
        selected_session=selected_session
    )


def can_review_final_project(final_project):
    if not final_project:
        return False

    batch = current_app.db.batches.find_one({"_id": final_project.get("batch_id")}) if final_project.get("batch_id") else None
    if not batch:
        return False

    return batch.get("mentor_id") and str(batch.get("mentor_id")) == str(current_user.id)


def final_project_review_redirect():
    user = get_staff_user()
    if can_view_reports(user):
        return redirect(url_for("admin.admin_final_projects"))
    return redirect(url_for("admin.mentor_final_projects"))


@admin_bp.route("/approve-final-project/<submission_id>", methods=["POST"])
@login_required
@mentor_access_required
def approve_final_project(submission_id):

    final_project = current_app.db.final_submissions.find_one({"_id": ObjectId(submission_id)})
    if not can_review_final_project(final_project):
        flash("You can only review final projects from students assigned to your batch.", "warning")
        return final_project_review_redirect()

    remark = request.form.get("remark")

    current_app.db.final_submissions.update_one(
        {"_id": final_project["_id"]},
        {
            "$set": {
                "status": "approved",
                "remark": remark,
                "reviewed_at": datetime.utcnow()
            }
        }
    )

    student = current_app.db.students.find_one({"_id": final_project["student_id"]})
    project_title = final_project.get("project_title", "Final Project")

    create_notification(student["_id"], f"Final project '{project_title}' approved by mentor")

    if student and student.get("email"):
        try:
            send_email(
                student["email"],
                "Final Project Approved",
                final_project_status_email(project_title, "Approved", remark)
            )
        except Exception as e:
            print("Email error:", e)

    flash("Final project approved.", "success")
    return final_project_review_redirect()


@admin_bp.route("/reject-final-project/<submission_id>", methods=["POST"])
@login_required
@mentor_access_required
def reject_final_project(submission_id):

    final_project = current_app.db.final_submissions.find_one({"_id": ObjectId(submission_id)})
    if not can_review_final_project(final_project):
        flash("You can only review final projects from students assigned to your batch.", "warning")
        return final_project_review_redirect()

    remark = request.form.get("remark")

    current_app.db.final_submissions.update_one(
        {"_id": final_project["_id"]},
        {
            "$set": {
                "status": "rejected",
                "remark": remark,
                "reviewed_at": datetime.utcnow()
            }
        }
    )

    student = current_app.db.students.find_one({"_id": final_project["student_id"]})
    project_title = final_project.get("project_title", "Final Project")

    create_notification(student["_id"], f"Final project '{project_title}' rejected by mentor")

    if student and student.get("email"):
        try:
            send_email(
                student["email"],
                "Final Project Rejected",
                final_project_status_email(project_title, "Rejected", remark)
            )
        except Exception as e:
            print("Email error:", e)

    flash("Final project rejected.", "success")
    return final_project_review_redirect()

@admin_bp.route("/approve-submission/<submission_id>", methods=["POST"])
@login_required
@mentor_access_required
def approve_submission(submission_id):

    remark = request.form.get("remark")

    current_app.db.submissions.update_one(
        {"_id": ObjectId(submission_id)},
        {
            "$set": {
                "status": "approved",
                "remark": remark,
                "reviewed_at": datetime.utcnow()
            }
        }
    )

    submission = current_app.db.submissions.find_one({"_id": ObjectId(submission_id)})

    student_id = submission["student_id"]

    stage = current_app.db.stages.find_one({"_id": submission["stage_id"]})

    create_notification(
    student_id,
    f"{stage['name']} approved by mentor"
    )

    student = current_app.db.students.find_one({"_id": student_id})

    if student and student.get("email"):
        try:
            send_email(
                student["email"],
                "Submission Approved",
                status_email(stage["name"], "Approved", remark)
            )
        except Exception as e:
            print("Email error:", e)

    flash("Submission approved")
    return redirect(url_for("admin.mentor_submissions"))

@admin_bp.route("/reject-submission/<submission_id>", methods=["POST"])
@login_required
@mentor_access_required
def reject_submission(submission_id):

    remark = request.form.get("remark")

    current_app.db.submissions.update_one(
        {"_id": ObjectId(submission_id)},
        {
            "$set": {
                "status": "rejected",
                "remark": remark,
                "reviewed_at": datetime.utcnow()
            }
        }
    )
   
    submission = current_app.db.submissions.find_one({"_id": ObjectId(submission_id)})

    student_id = submission["student_id"]

    stage = current_app.db.stages.find_one({"_id": submission["stage_id"]})

    create_notification(
    student_id,
    f"{stage['name']} rejected by mentor"
    )

    student = current_app.db.students.find_one({"_id": student_id})

    if student and student.get("email"):
        try:
            send_email(
                student["email"],
                "Submission Rejected",
                status_email(stage["name"], "Rejected", remark)
            )
        except Exception as e:
            print("Email error:", e)
    
    flash("Submission rejected")
    return redirect(url_for("admin.mentor_submissions"))


def _safe_mark(value):
    try:
        parsed = float(str(value or "").strip())
        if parsed < 0:
            return 0.0
        if parsed > 10:
            return 10.0
        return round(parsed, 2)
    except Exception:
        return 0.0


def _evaluation_student_scope(user, selected_session, selected_batch_id=None):
    students_query = session_filter(selected_session)
    selected_batch = None

    if can_edit_evaluation_all(user) or is_director(user):
        if selected_batch_id:
            try:
                selected_batch = current_app.db.batches.find_one({"_id": ObjectId(selected_batch_id)})
            except Exception:
                selected_batch = None
            if selected_batch:
                students_query = {
                    "$and": [
                        session_filter(selected_session),
                        {"batch_id": selected_batch["_id"]}
                    ]
                }
    else:
        mentor_batch, _, _ = get_faculty_assigned_batch(user["_id"], str(selected_session["_id"]))
        if not mentor_batch:
            return [], [], None
        selected_batch = mentor_batch
        students_query = {
            "$and": [
                session_filter(selected_session),
                {"batch_id": mentor_batch["_id"]}
            ]
        }

    students = list(current_app.db.students.find(students_query).sort("roll_no", 1))
    if not students:
        students = list(current_app.db.students.find(students_query).sort("prn", 1))

    batches = list(current_app.db.batches.find(session_filter(selected_session)).sort("name", 1))
    return students, batches, selected_batch


@admin_bp.route("/evaluation-sheet")
@login_required
@evaluation_access_required
def evaluation_sheet():
    user = get_staff_user()
    selected_session_id = request.args.get("session")
    selected_batch_id = request.args.get("batch")

    sessions, selected_session = get_selected_session(selected_session_id)
    students, batches, selected_batch = _evaluation_student_scope(user, selected_session, selected_batch_id)

    student_ids = [student["_id"] for student in students]
    evaluations = list(
        current_app.db.evaluations.find({
            "student_id": {"$in": student_ids},
            "session_id": selected_session["_id"]
        })
    ) if student_ids else []
    evaluation_map = {str(item["student_id"]): item for item in evaluations}

    guide_map = {}
    for student in students:
        guide_name = "Not Assigned"
        batch = None
        if student.get("batch_id"):
            batch = current_app.db.batches.find_one({"_id": student["batch_id"]})
        if batch and batch.get("mentor_id"):
            mentor = current_app.db.users.find_one({"_id": batch["mentor_id"]})
            if mentor:
                guide_name = mentor.get("name", "Not Assigned")
        guide_map[str(student["_id"])] = guide_name

    return render_template(
        "admin/evaluation_sheet.html",
        sessions=sessions,
        selected_session=selected_session,
        batches=batches,
        selected_batch=selected_batch,
        students=students,
        evaluation_map=evaluation_map,
        guide_map=guide_map,
        can_edit_evaluation=bool(can_edit_evaluation_all(user) or get_user_designation(user) == DESIGNATION_FACULTY),
        can_filter_all=bool(can_edit_evaluation_all(user) or is_director(user))
    )


@admin_bp.route("/evaluation-sheet/save/<student_id>", methods=["POST"])
@login_required
@evaluation_access_required
def save_evaluation_row(student_id):
    user = get_staff_user()
    student = current_app.db.students.find_one({"_id": ObjectId(student_id)})
    if not student:
        flash("Student not found.", "warning")
        return redirect(url_for("admin.evaluation_sheet"))

    if not can_edit_evaluation_student(user, student):
        flash("You can edit only permitted student evaluations.", "danger")
        return redirect(url_for("admin.evaluation_sheet"))

    selected_session_id = request.form.get("session_id")
    selected_batch_id = request.form.get("batch_id")
    session_doc = current_app.db.academic_sessions.find_one({"_id": ObjectId(selected_session_id)}) if selected_session_id else None
    if not session_doc:
        flash("Academic session not found.", "warning")
        return redirect(url_for("admin.evaluation_sheet"))

    chapter3 = _safe_mark(request.form.get("chapter3"))
    chapter4 = _safe_mark(request.form.get("chapter4"))
    execution = _safe_mark(request.form.get("execution_demo"))
    total = round(chapter3 + chapter4 + execution, 2)

    synopsis_status = (request.form.get("synopsis_status") or "No").strip().title()
    if synopsis_status not in ["Yes", "No"]:
        synopsis_status = "No"

    guide_name = (request.form.get("guide_name") or "").strip()
    project_title = (request.form.get("project_title") or "").strip()
    signature = (request.form.get("signature") or "").strip()

    now = datetime.utcnow()
    current_app.db.evaluations.update_one(
        {"student_id": student["_id"], "session_id": session_doc["_id"]},
        {
            "$set": {
                "student_id": student["_id"],
                "batch_id": student.get("batch_id"),
                "session_id": session_doc["_id"],
                "roll_no": student.get("roll_no") or student.get("prn"),
                "student_name": student.get("name"),
                "guide_name": guide_name or "Not Assigned",
                "project_title": project_title,
                "synopsis_status": synopsis_status,
                "chapter3": chapter3,
                "chapter4": chapter4,
                "execution_demo": execution,
                "total": total,
                "signature": signature,
                "updated_by": ObjectId(current_user.id),
                "updated_at": now
            },
            "$setOnInsert": {
                "created_at": now
            }
        },
        upsert=True
    )

    create_notification(student["_id"], f"Your evaluation sheet was updated. Total: {total}/30")
    if student.get("email"):
        try:
            send_email(
                student["email"],
                "Evaluation Sheet Updated",
                evaluation_update_email(
                    student.get("name", "Student"),
                    user.get("name", "Evaluator"),
                    total,
                    "updated"
                )
            )
        except Exception as e:
            print("Evaluation update email error:", e)

    flash("Evaluation row saved.", "success")
    return redirect(url_for("admin.evaluation_sheet", session=str(session_doc["_id"]), batch=selected_batch_id))


@admin_bp.route("/evaluation-sheet/delete/<student_id>", methods=["POST"])
@login_required
@evaluation_access_required
def delete_evaluation_row(student_id):
    user = get_staff_user()
    student = current_app.db.students.find_one({"_id": ObjectId(student_id)})
    if not student:
        flash("Student not found.", "warning")
        return redirect(url_for("admin.evaluation_sheet"))

    if not can_edit_evaluation_student(user, student):
        flash("You can delete only permitted student evaluations.", "danger")
        return redirect(url_for("admin.evaluation_sheet"))

    selected_session_id = request.form.get("session_id")
    selected_batch_id = request.form.get("batch_id")
    session_doc = current_app.db.academic_sessions.find_one({"_id": ObjectId(selected_session_id)}) if selected_session_id else None
    if not session_doc:
        flash("Academic session not found.", "warning")
        return redirect(url_for("admin.evaluation_sheet"))

    current_app.db.evaluations.delete_one({
        "student_id": student["_id"],
        "session_id": session_doc["_id"]
    })

    flash("Evaluation row removed.", "success")
    return redirect(url_for("admin.evaluation_sheet", session=str(session_doc["_id"]), batch=selected_batch_id))


@admin_bp.route("/evaluation-sheet/export")
@login_required
@evaluation_access_required
def export_evaluation_sheet():
    user = get_staff_user()
    selected_session_id = request.args.get("session")
    selected_batch_id = request.args.get("batch")
    sessions, selected_session = get_selected_session(selected_session_id)
    _ = sessions

    students, _, selected_batch = _evaluation_student_scope(user, selected_session, selected_batch_id)
    student_ids = [student["_id"] for student in students]
    evaluations = list(
        current_app.db.evaluations.find({
            "student_id": {"$in": student_ids},
            "session_id": selected_session["_id"]
        })
    ) if student_ids else []
    evaluation_map = {str(item["student_id"]): item for item in evaluations}

    rows = []
    for idx, student in enumerate(students, start=1):
        evaluation = evaluation_map.get(str(student["_id"]), {})
        guide_name = evaluation.get("guide_name") or "Not Assigned"
        if guide_name == "Not Assigned" and student.get("batch_id"):
            batch = current_app.db.batches.find_one({"_id": student["batch_id"]})
            if batch and batch.get("mentor_id"):
                mentor = current_app.db.users.find_one({"_id": batch["mentor_id"]})
                if mentor:
                    guide_name = mentor.get("name", "Not Assigned")

        rows.append({
            "SN": idx,
            "Roll Number": student.get("roll_no") or student.get("prn"),
            "Student Name": student.get("name", ""),
            "Guide Name": guide_name,
            "Project Title": evaluation.get("project_title", ""),
            "Synopsis Submission Status (Yes/No)": evaluation.get("synopsis_status", "No"),
            "Chapter 3 (10)": evaluation.get("chapter3", 0),
            "Chapter 4 (10)": evaluation.get("chapter4", 0),
            "Project execution/demo/github link (10)": evaluation.get("execution_demo", 0),
            "Total (30)": evaluation.get("total", 0),
            "Signature": evaluation.get("signature", "")
        })

    df = pd.DataFrame(rows)
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Evaluation")
    output.seek(0)

    batch_part = f"_{selected_batch['name']}" if selected_batch else ""
    filename = f"evaluation_{selected_session['name']}{batch_part}.xlsx".replace(" ", "_")
    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

@admin_bp.route("/view-file/<filename>")
@login_required
def view_file(filename):

    return send_from_directory(
        current_app.config["UPLOAD_FOLDER"],
        filename
    )

@admin_bp.route("/download/<filename>")
@login_required
def download_file(filename):

    return send_from_directory(
        current_app.config["UPLOAD_FOLDER"],
        filename,
        as_attachment=True
    )

@admin_bp.route("/notifications/read", methods=["POST"])
@login_required
def mark_notifications():

    mark_notifications_read(current_user.id)

    return "", 204
