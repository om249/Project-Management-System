import email
from unittest import result
import re
import os
import threading
from io import BytesIO
from functools import wraps

from numpy import rint
import pandas as pd
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, jsonify, session
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
PROGRAM_OPTIONS = {"MCA", "MBA"}
DEFAULT_PROGRAM = "MCA"
PROJECT_CATEGORY_OPTIONS = [
    ("mini_project", "Mini Project"),
    ("field_project", "Field Project"),
    ("major_project", "Major Project"),
    ("desk_research", "Desk Research"),
    ("research_project", "Research Project")
]
DEFAULT_PROJECT_CATEGORY = "mini_project"


def get_current_program():
    selected = str(session.get("selected_program", DEFAULT_PROGRAM)).strip().upper()
    if selected not in PROGRAM_OPTIONS:
        selected = DEFAULT_PROGRAM
    if current_user.is_authenticated and getattr(current_user, "role", None) in {"admin", "faculty"}:
        user = get_staff_user()
        locked_program = get_user_program_scope(user)
        if locked_program:
            selected = locked_program
    session["selected_program"] = selected
    if selected != "MBA":
        session["selected_project_category"] = DEFAULT_PROJECT_CATEGORY
    elif "selected_project_category" not in session:
        session["selected_project_category"] = DEFAULT_PROJECT_CATEGORY
    return selected


def project_category_label(value):
    mapping = {key: label for key, label in PROJECT_CATEGORY_OPTIONS}
    return mapping.get(str(value or "").strip().lower(), "Mini Project")


def normalize_project_category(value, program=None):
    selected_program = (program or get_current_program()).strip().upper()
    if selected_program != "MBA":
        return DEFAULT_PROJECT_CATEGORY
    allowed = {key for key, _ in PROJECT_CATEGORY_OPTIONS}
    normalized = str(value or "").strip().lower().replace(" ", "_")
    return normalized if normalized in allowed else DEFAULT_PROJECT_CATEGORY


def get_current_project_category(program=None):
    selected_program = (program or get_current_program()).strip().upper()
    if selected_program != "MBA":
        session["selected_project_category"] = DEFAULT_PROJECT_CATEGORY
        return DEFAULT_PROJECT_CATEGORY
    selected_category = normalize_project_category(session.get("selected_project_category"), selected_program)
    session["selected_project_category"] = selected_category
    return selected_category


def project_category_filter_query(program=None, project_category=None):
    selected_program = (program or get_current_program()).strip().upper()
    if selected_program != "MBA":
        return {}
    selected_category = normalize_project_category(project_category or get_current_project_category(selected_program), selected_program)
    if selected_category == DEFAULT_PROJECT_CATEGORY:
        return {
            "$or": [
                {"project_category": selected_category},
                {"project_category": {"$exists": False}},
                {"project_category": None},
                {"project_category": ""}
            ]
        }
    return {"project_category": selected_category}


def program_filter_query(program=None):
    selected = (program or get_current_program()).strip().upper()
    if selected == "MCA":
        return {"$or": [{"program": "MCA"}, {"program": {"$exists": False}}]}
    return {"program": selected}


def with_program_scope(query, program=None, project_category=None):
    base_query = query or {}
    filters = [base_query, program_filter_query(program)]
    category_query = project_category_filter_query(program, project_category)
    if category_query:
        filters.append(category_query)
    return {
        "$and": filters
    }


def user_program_filter_query(program=None):
    selected = (program or get_current_program()).strip().upper()
    if selected == "MCA":
        return {
            "$or": [
                {"program": "MCA"},
                {"program": {"$exists": False}},
                {"program": None},
                {"program": ""}
            ]
        }
    return {"program": selected}


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
    scoped_program = normalize_program(stage.get("program") or DEFAULT_PROGRAM)

    session_batches = list(current_app.db.batches.find(
        {"session_id": session["_id"], **program_filter_query(scoped_program)},
        {"_id": 1}
    ))
    batch_ids = [batch["_id"] for batch in session_batches]

    students = list(current_app.db.students.find({
        "$and": [
            {
                "$or": [
                    {"session_id": session["_id"]},
                    {"batch_id": {"$in": batch_ids}}
                ]
            },
            program_filter_query(scoped_program)
        ]
    }))
    faculty_members = list(current_app.db.users.find({
        "role": {"$in": ["faculty", "admin"]},
        **user_program_filter_query(scoped_program)
    }))
    leadership_members = get_program_leadership_users(scoped_program)
    leadership_ids = {str(item["_id"]) for item in leadership_members}

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
        if str(faculty["_id"]) in leadership_ids:
            continue
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

    for leader in leadership_members:
        create_notification(leader["_id"], message, "document")
        if leader.get("email"):
            try:
                send_email(
                    leader["email"],
                    f"Progress Report Document {action_label.title()}",
                    progress_document_email(
                        leader.get("name", "Leadership"),
                        stage["name"],
                        session["name"],
                        document_name,
                        action_label
                    )
                )
            except Exception as e:
                print("Progress document leadership email error:", e)


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
PROGRAM_SCOPED_DESIGNATIONS = {DESIGNATION_PROJECT_COORDINATOR, DESIGNATION_HOD}
GLOBAL_DESIGNATIONS = {DESIGNATION_DIRECTOR, DESIGNATION_ACADEMIC_COORDINATOR}

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


def normalize_program(value):
    normalized = str(value or "").strip().upper()
    return normalized if normalized in PROGRAM_OPTIONS else DEFAULT_PROGRAM


def _role_assignments_collection():
    return current_app.db.role_assignments


def get_program_leadership_users(program=None, exclude_ids=None):
    scoped_program = normalize_program(program or get_current_program())
    excluded = {str(item) for item in (exclude_ids or []) if item}
    assignments = list(
        _role_assignments_collection().find({
            "$or": [
                {"role": DESIGNATION_DIRECTOR},
                {"role": DESIGNATION_ACADEMIC_COORDINATOR, "program": {"$exists": False}},
                {"role": DESIGNATION_ACADEMIC_COORDINATOR, "program": scoped_program},
                {"role": {"$in": [DESIGNATION_PROJECT_COORDINATOR, DESIGNATION_HOD]}, "program": scoped_program}
            ]
        })
    )
    user_ids = [item.get("user_id") for item in assignments if item.get("user_id")]
    if not user_ids:
        return []
    users = list(current_app.db.users.find({"_id": {"$in": user_ids}, "role": {"$in": ["admin", "faculty"]}}))
    return [user for user in users if str(user.get("_id")) not in excluded]


def _bootstrap_role_assignments(staff):
    collection = _role_assignments_collection()
    if collection.count_documents({}) > 0 or not staff:
        return

    director = next((user for user in staff if normalize_designation(user.get("designation")) == DESIGNATION_DIRECTOR), None)
    if not director:
        director = next((user for user in staff if user.get("role") == "admin" and user.get("can_manage_admins")), None)
    if not director:
        director = next((user for user in staff if user.get("role") == "admin"), None)
    if not director:
        director = staff[0]

    collection.insert_one({
        "role": DESIGNATION_DIRECTOR,
        "user_id": director["_id"],
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    })

    ac = next(
        (
            user for user in staff
            if user["_id"] != director["_id"] and normalize_designation(user.get("designation")) == DESIGNATION_ACADEMIC_COORDINATOR
        ),
        None
    )
    if ac:
        collection.insert_one({
            "role": DESIGNATION_ACADEMIC_COORDINATOR,
            "user_id": ac["_id"],
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        })

    legacy_pc = next(
        (
            user for user in staff
            if user["_id"] != director["_id"] and normalize_designation(user.get("designation")) == DESIGNATION_PROJECT_COORDINATOR
        ),
        None
    )
    if legacy_pc:
        collection.insert_one({
            "role": DESIGNATION_PROJECT_COORDINATOR,
            "program": DEFAULT_PROGRAM,
            "user_id": legacy_pc["_id"],
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        })

    legacy_hod = next(
        (
            user for user in staff
            if user["_id"] != director["_id"] and normalize_designation(user.get("designation")) == DESIGNATION_HOD
        ),
        None
    )
    if legacy_hod:
        collection.insert_one({
            "role": DESIGNATION_HOD,
            "program": DEFAULT_PROGRAM,
            "user_id": legacy_hod["_id"],
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        })


def get_staff_user(user_id=None):
    target_id = user_id or current_user.id
    return current_app.db.users.find_one({"_id": ObjectId(target_id)})


def _get_user_role_profile(user):
    program_roles = {program: DESIGNATION_FACULTY for program in PROGRAM_OPTIONS}
    profile = {
        "is_director": False,
        "is_ac": False,
        "program_scope": None,
        "program_roles": program_roles
    }
    if not user:
        return profile

    assignments = list(
        _role_assignments_collection().find({"user_id": user["_id"]})
    )
    if not assignments:
        legacy = normalize_designation(user.get("designation"))
        if legacy == DESIGNATION_DIRECTOR:
            profile["is_director"] = True
        elif legacy == DESIGNATION_ACADEMIC_COORDINATOR:
            profile["is_ac"] = True
        elif legacy in PROGRAM_SCOPED_DESIGNATIONS:
            profile["program_roles"][DEFAULT_PROGRAM] = legacy
            profile["program_scope"] = DEFAULT_PROGRAM
        elif legacy == DESIGNATION_FACULTY:
            legacy_program = str(user.get("program") or "").strip().upper()
            if legacy_program in PROGRAM_OPTIONS:
                profile["program_scope"] = legacy_program
        return profile

    for assignment in assignments:
        role = normalize_designation(assignment.get("role"))
        program = assignment.get("program")
        if role == DESIGNATION_DIRECTOR:
            profile["is_director"] = True
        elif role == DESIGNATION_ACADEMIC_COORDINATOR:
            profile["is_ac"] = True
        elif role in PROGRAM_SCOPED_DESIGNATIONS and program:
            normalized_program = normalize_program(program)
            profile["program_roles"][normalized_program] = role
            profile["program_scope"] = normalized_program

    if not profile["program_scope"]:
        user_program = str(user.get("program") or "").strip().upper()
        if user_program in PROGRAM_OPTIONS:
            profile["program_scope"] = user_program

    return profile


def get_user_designation(user, program=None):
    if not user:
        return DESIGNATION_FACULTY
    effective_program = normalize_program(program or session.get("selected_program", DEFAULT_PROGRAM))
    role_profile = _get_user_role_profile(user)
    if role_profile["is_director"]:
        return DESIGNATION_DIRECTOR
    if role_profile["is_ac"]:
        return DESIGNATION_ACADEMIC_COORDINATOR
    scoped_designation = role_profile["program_roles"].get(effective_program, DESIGNATION_FACULTY)
    if scoped_designation in PROGRAM_SCOPED_DESIGNATIONS:
        return scoped_designation
    scoped_program = role_profile.get("program_scope")
    if scoped_program and scoped_program == effective_program:
        return DESIGNATION_FACULTY
    return DESIGNATION_FACULTY


def get_user_program_scope(user):
    if not user:
        return None
    role_profile = _get_user_role_profile(user)
    if role_profile["is_director"]:
        return None
    if role_profile["is_ac"]:
        return None
    return role_profile.get("program_scope")


def get_user_role_display_label(user):
    role_profile = _get_user_role_profile(user)
    if role_profile["is_director"]:
        return "Director"
    if role_profile["is_ac"]:
        return "AC"

    for program in sorted(PROGRAM_OPTIONS):
        role_name = role_profile["program_roles"].get(program)
        if role_name in PROGRAM_SCOPED_DESIGNATIONS:
            short = "PC" if role_name == DESIGNATION_PROJECT_COORDINATOR else "HOD"
            return f"{program} {short}"

    scoped_program = role_profile.get("program_scope")
    return f"{scoped_program} Faculty" if scoped_program else "Faculty"


def is_director(user, program=None):
    return get_user_designation(user, program) == DESIGNATION_DIRECTOR


def is_project_coordinator(user, program=None):
    return get_user_designation(user, program) == DESIGNATION_PROJECT_COORDINATOR


def can_manage_operations(user, program=None):
    return get_user_designation(user, program) in {DESIGNATION_PROJECT_COORDINATOR}


def can_view_reports(user, program=None):
    return get_user_designation(user, program) in {
        DESIGNATION_DIRECTOR,
        DESIGNATION_PROJECT_COORDINATOR,
        DESIGNATION_ACADEMIC_COORDINATOR,
        DESIGNATION_HOD
    }


def can_view_all_students(user, program=None):
    return get_user_designation(user, program) in {
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
    normalized = normalize_designation(designation)
    if normalized in mapping:
        return mapping[normalized]
    if designation:
        return str(designation)
    return "Faculty"


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

    _bootstrap_role_assignments(staff)
    ac_assignments = list(_role_assignments_collection().find({"role": DESIGNATION_ACADEMIC_COORDINATOR}))
    if ac_assignments:
        preferred_ac = next((item for item in ac_assignments if not item.get("program")), ac_assignments[0])
        preferred_user_id = preferred_ac.get("user_id")
        _role_assignments_collection().delete_many({"role": DESIGNATION_ACADEMIC_COORDINATOR})
        if preferred_user_id:
            _role_assignments_collection().insert_one({
                "role": DESIGNATION_ACADEMIC_COORDINATOR,
                "user_id": preferred_user_id,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow()
            })
    director_assignment = _role_assignments_collection().find_one({"role": DESIGNATION_DIRECTOR})
    director = None
    if director_assignment and director_assignment.get("user_id"):
        director = current_app.db.users.find_one({"_id": director_assignment["user_id"]})

    for user in staff:
        is_current_director = bool(director and str(user["_id"]) == str(director["_id"]))
        scoped_program = get_user_program_scope(user) or DEFAULT_PROGRAM
        compatible_designation = get_user_designation(user, scoped_program)
        update_data = {
            "role": "admin" if is_current_director else "faculty",
            "can_manage_admins": is_current_director,
            "designation": compatible_designation,
            "is_project_coordinator": compatible_designation == DESIGNATION_PROJECT_COORDINATOR,
            "program": scoped_program if not is_current_director else None
        }
        current_app.db.users.update_one({"_id": user["_id"]}, {"$set": update_data})

    return list(
        current_app.db.users.find(
            {"role": {"$in": ["admin", "faculty"]}}
        ).sort("created_at", 1)
    )


def get_director():
    ensure_admin_access_state()
    assignment = _role_assignments_collection().find_one({"role": DESIGNATION_DIRECTOR})
    if assignment and assignment.get("user_id"):
        return current_app.db.users.find_one({"_id": assignment["user_id"]})
    return current_app.db.users.find_one({"role": "admin"}, sort=[("created_at", 1)])


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

    _role_assignments_collection().delete_many({
        "user_id": target_user["_id"],
        "role": {"$ne": DESIGNATION_DIRECTOR}
    })
    _role_assignments_collection().delete_many({"role": DESIGNATION_DIRECTOR})
    _role_assignments_collection().insert_one({
        "role": DESIGNATION_DIRECTOR,
        "user_id": target_user["_id"],
        "created_at": now,
        "updated_at": now
    })

    current_app.db.users.update_many({"role": "admin"}, {"$set": {"role": "faculty", "can_manage_admins": False}})
    current_app.db.users.update_one({"_id": target_user["_id"]}, {"$set": {"role": "admin", "can_manage_admins": True, "granted_by": ObjectId(current_user.id), "granted_at": now}})

    target_after = current_app.db.users.find_one({"_id": target_user["_id"]})
    send_designation_update_email(
        target_after,
        DESIGNATION_DIRECTOR,
        get_user_designation(target_before) if target_before else None
    )

    if previous_director and str(previous_director["_id"]) != str(target_user["_id"]):
        _role_assignments_collection().delete_many({
            "user_id": previous_director["_id"],
            "role": {"$ne": DESIGNATION_DIRECTOR}
        })
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


def assign_designation(target_user, designation, program=None):
    designation = normalize_designation(designation)
    if designation == DESIGNATION_DIRECTOR:
        transfer_director_access(target_user)
        return

    normalized_program = normalize_program(program) if program else None
    target_before = current_app.db.users.find_one({"_id": target_user["_id"]})
    now = datetime.utcnow()
    role_collection = _role_assignments_collection()

    # Single-role model: clear all non-director assignments from this user first.
    role_collection.delete_many({
        "user_id": target_user["_id"],
        "role": {"$ne": DESIGNATION_DIRECTOR}
    })

    if designation == DESIGNATION_FACULTY:
        if not normalized_program:
            normalized_program = normalize_program(get_current_program())
    elif designation == DESIGNATION_ACADEMIC_COORDINATOR:
        displaced = role_collection.find_one({
            "role": DESIGNATION_ACADEMIC_COORDINATOR,
            "user_id": {"$ne": target_user["_id"]}
        })
        role_collection.delete_many({"role": DESIGNATION_ACADEMIC_COORDINATOR})
        role_collection.insert_one({
            "role": DESIGNATION_ACADEMIC_COORDINATOR,
            "user_id": target_user["_id"],
            "created_at": now,
            "updated_at": now
        })
        if displaced:
            displaced_after = current_app.db.users.find_one({"_id": displaced["user_id"]})
            send_designation_update_email(displaced_after, DESIGNATION_FACULTY, DESIGNATION_ACADEMIC_COORDINATOR)
    elif designation in PROGRAM_SCOPED_DESIGNATIONS:
        if not normalized_program:
            normalized_program = normalize_program(get_current_program())
        displaced = role_collection.find_one({
            "role": designation,
            "program": normalized_program,
            "user_id": {"$ne": target_user["_id"]}
        })
        role_collection.delete_many({"role": designation, "program": normalized_program})
        role_collection.insert_one({
            "role": designation,
            "program": normalized_program,
            "user_id": target_user["_id"],
            "created_at": now,
            "updated_at": now
        })
        if displaced:
            displaced_after = current_app.db.users.find_one({"_id": displaced["user_id"]})
            send_designation_update_email(displaced_after, DESIGNATION_FACULTY, designation)

    current_app.db.users.update_one(
        {"_id": target_user["_id"]},
        {
            "$set": {
                "role": "faculty",
                "can_manage_admins": False,
                "program": normalized_program if normalized_program else None,
                "updated_at": now,
                "updated_by": ObjectId(current_user.id)
            }
        }
    )
    target_after = current_app.db.users.find_one({"_id": target_user["_id"]})
    send_designation_update_email(
        target_after,
        get_user_role_display_label(target_after),
        get_user_designation(target_before, normalized_program or DEFAULT_PROGRAM) if target_before else None
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


def get_faculty_assigned_batch(faculty_id, selected_session_id=None, selected_program=None, selected_project_category=None):
    sessions, selected_session = get_selected_session(selected_session_id)
    scoped_filter = session_filter(selected_session)
    program = (selected_program or get_current_program()).upper()
    program_query = program_filter_query(program)
    category_query = project_category_filter_query(program, selected_project_category)

    batch = current_app.db.batches.find_one(
        {
            "mentor_id": faculty_id,
            **program_query,
            **category_query,
            "$or": scoped_filter["$or"]
        },
        sort=[("created_at", -1)]
    )

    if not batch:
        batch = current_app.db.batches.find_one(
            {"mentor_id": faculty_id, **program_query, **category_query},
            sort=[("created_at", -1)]
        )

    return batch, sessions, selected_session


# ===================== DASHBOARD =====================
@admin_bp.route("/dashboard")
@login_required
@operations_access_required
def dashboard():
    ensure_admin_access_state()
    selected_program = get_current_program()
    selected_project_category = get_current_project_category(selected_program)
    scope_query = with_program_scope({}, selected_program, selected_project_category)
    program_students = list(current_app.db.students.find(scope_query, {"_id": 1}))
    program_student_ids = [student["_id"] for student in program_students]
    submission_query = {"student_id": {"$in": program_student_ids}} if program_student_ids else {"_id": None}

    total_batches = current_app.db.batches.count_documents(scope_query)
    total_stages = current_app.db.stages.count_documents(scope_query)
    total_students = len(program_student_ids)
    total_faculty = current_app.db.users.count_documents({
        "role": "faculty",
        **user_program_filter_query(selected_program)
    })
    pending_submissions = current_app.db.submissions.count_documents({**submission_query, "status": "pending"})
    approved_submissions = current_app.db.submissions.count_documents({**submission_query, "status": "approved"})
    late_submissions = current_app.db.submissions.count_documents({**submission_query, "late": True})
    batches = list(current_app.db.batches.find(scope_query).sort("created_at", -1))

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
        selected_program=selected_program,
        selected_project_category=selected_project_category,
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
    admin_role_label = get_user_role_display_label(admin)

    return render_template(
        "admin/profile.html",
        admin=admin,
        admin_role_label=admin_role_label
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


@admin_bp.route("/set-program", methods=["POST"])
@login_required
def set_program_context():
    user = get_staff_user() if current_user.role in {"admin", "faculty"} else None
    locked_program = get_user_program_scope(user) if user else None

    if locked_program:
        session["selected_program"] = locked_program
    else:
        selected_program = str(request.form.get("program", DEFAULT_PROGRAM)).strip().upper()
        if selected_program not in PROGRAM_OPTIONS:
            flash("Invalid program selected.", "warning")
        else:
            session["selected_program"] = selected_program
            if selected_program != "MBA":
                session["selected_project_category"] = DEFAULT_PROJECT_CATEGORY
            else:
                session["selected_project_category"] = normalize_project_category(
                    session.get("selected_project_category"),
                    selected_program
                )

    next_url = request.form.get("next_url") or request.referrer or url_for("admin.dashboard")
    return redirect(next_url)


@admin_bp.route("/set-project-category", methods=["POST"])
@login_required
def set_project_category_context():
    selected_program = get_current_program()
    if selected_program != "MBA":
        session["selected_project_category"] = DEFAULT_PROJECT_CATEGORY
    else:
        session["selected_project_category"] = normalize_project_category(
            request.form.get("project_category"),
            selected_program
        )
    next_url = request.form.get("next_url") or request.referrer or url_for("admin.dashboard")
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
    selected_program = get_current_program()
    selected_project_category = get_current_project_category(selected_program)
    selected_session_id = request.values.get("session")
    sessions, selected_session = get_selected_session(selected_session_id)

    if request.method == "POST":
        name = request.form.get("name")

        if name:
            name = name.strip()
            existing = current_app.db.batches.find_one(
                with_program_scope(
                    {
                        "name": name,
                        "session_id": selected_session["_id"]
                    },
                    selected_program
                )
            )

            if existing:
                flash("Batch already exists.")
            else:
                current_app.db.batches.insert_one({
                    "name": name,
                    "year": selected_session["name"],
                    "session_id": selected_session["_id"],
                    "program": selected_program,
                    "project_category": selected_project_category,
                    "mentor_id": None,
                    "created_at": datetime.utcnow()
                })
                flash("Batch created successfully.")

        return redirect(url_for("admin.manage_batches", session=str(selected_session["_id"])))

    batches = list(
        current_app.db.batches.find(
            with_program_scope(session_filter(selected_session), selected_program)
        ).sort("created_at", -1)
    )

    # Get all assigned mentor_ids
    assigned_mentors = current_app.db.batches.distinct(
        "mentor_id",
        with_program_scope(session_filter(selected_session), selected_program)
    )

    # Remove None if exists
    assigned_mentors = [m for m in assigned_mentors if m]

    faculty_candidates = list(current_app.db.users.find({
        "role": {"$in": ["faculty", "admin"]},
        **user_program_filter_query(selected_program)
    }))
    faculty = [member for member in faculty_candidates if not is_director(member)]

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
        selected_session=selected_session,
        selected_project_category=selected_project_category
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
    mentor_record = current_app.db.users.find_one({"_id": ObjectId(mentor_id)}) if mentor_id and mentor_id != "remove" else None
    batch_program = (batch.get("program") or DEFAULT_PROGRAM).strip().upper() if batch else DEFAULT_PROGRAM
    if mentor_record and get_user_program_scope(mentor_record) not in {None, batch_program}:
        flash("This mentor belongs to a different program.", "warning")
        return redirect(url_for("admin.manage_batches", **session_query))

    already_assigned = current_app.db.batches.find_one({
        "mentor_id": ObjectId(mentor_id),
        "session_id": batch.get("session_id"),
        "program": batch.get("program"),
        "project_category": batch.get("project_category"),
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
    selected_program = get_current_program()
    selected_project_category = get_current_project_category(selected_program)
    selected_session_id = request.values.get("session")
    sessions, selected_session = get_selected_session(selected_session_id)

    # -------- Add New Stage --------
    if request.method == "POST" and "name" in request.form:
        name = request.form["name"].strip()

        if not name:
            flash("Progress Report name cannot be empty.")
            return redirect(url_for("admin.manage_stages", session=str(selected_session["_id"])))

        last_stage = current_app.db.stages.find_one(
            with_program_scope({}, selected_program, selected_project_category),
            sort=[("order", -1)]
        )
        next_order = last_stage["order"] + 1 if last_stage else 1

        current_app.db.stages.insert_one({
            "name": name,
            "program": selected_program,
            "project_category": selected_project_category,
            "order": next_order
        })

        flash("Progress Report added successfully.")
        return redirect(url_for("admin.manage_stages", session=str(selected_session["_id"])))

    # -------- GET DATA --------
    stages = list(
        current_app.db.stages.find(
            with_program_scope({}, selected_program, selected_project_category)
        ).sort("order", 1)
    )

    deadline_dict = {}
    deadlines = current_app.db.deadlines.find(get_session_deadline_query(selected_session["_id"]))

    for d in deadlines:
        deadline_dict[str(d["stage_id"])] = d["deadline"].strftime("%Y-%m-%d")

    progress_documents = current_app.db.progress_documents.find({
        "session_id": selected_session["_id"],
        **project_category_filter_query(selected_program, selected_project_category)
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
        selected_project_category=selected_project_category,
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
    expects_json = "application/json" in (request.headers.get("Accept", "").lower())

    if not session_id or not stage_id:
        if expects_json:
            return jsonify({"success": False, "error": "Select a valid progress report before uploading a document."}), 400
        flash("Select a valid progress report before uploading a document.", "warning")
        return redirect(url_for("admin.manage_stages"))

    session = current_app.db.academic_sessions.find_one({"_id": ObjectId(session_id)})
    if not session:
        if expects_json:
            return jsonify({"success": False, "error": "Selected academic session was not found."}), 404
        flash("Selected academic session was not found.", "warning")
        return redirect(url_for("admin.manage_stages"))

    stage = current_app.db.stages.find_one({"_id": ObjectId(stage_id)})
    if not stage:
        if expects_json:
            return jsonify({"success": False, "error": "Selected progress report was not found."}), 404
        flash("Selected progress report was not found.", "warning")
        return redirect(url_for("admin.manage_stages", session=session_id))

    selected_program = get_current_program()
    selected_project_category = get_current_project_category(selected_program)

    saved_document = save_progress_document(document, session_id)
    if not saved_document:
        if expects_json:
            return jsonify({"success": False, "error": "Upload a valid previewable document: PDF, DOC, DOCX, PPT, PPTX, XLS, or XLSX."}), 400
        flash("Upload a valid previewable document: PDF, DOC, DOCX, PPT, PPTX, XLS, or XLSX.", "warning")
        return redirect(url_for("admin.manage_stages", session=session_id))

    existing_document = current_app.db.progress_documents.find_one({
        "session_id": session["_id"],
        "stage_id": stage["_id"],
        **project_category_filter_query(selected_program, selected_project_category)
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
        "program": selected_program,
        "project_category": selected_project_category,
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

    if expects_json:
        return jsonify({
            "success": True,
            "filename": saved_document["file_name"],
            "pdf_file": saved_document["pdf_file"],
            "preview_status": saved_document["preview_status"],
            "is_reupload": is_reupload
        })

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
        selected_program = get_current_program()
        selected_project_category = get_current_project_category(selected_program)

        current_app.db.deadlines.update_one(
            get_session_deadline_query(ObjectId(session_id), ObjectId(stage_id)),
            {
                "$set": {
                    "session_id": ObjectId(session_id),
                    "stage_id": ObjectId(stage_id),
                    "deadline": deadline_date,
                    "program": selected_program,
                    "project_category": selected_project_category
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
    selected_program = get_current_program()
    faculty_list = list(current_app.db.users.find({
        "role": "faculty",
        **user_program_filter_query(selected_program)
    }))
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
            "program": selected_program,
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
        role_profile = _get_user_role_profile(member)
        global_designation = DESIGNATION_DIRECTOR if role_profile["is_director"] else (DESIGNATION_ACADEMIC_COORDINATOR if role_profile["is_ac"] else None)
        role_label = get_user_role_display_label(member)
        mca_designation = role_profile["program_roles"].get("MCA", DESIGNATION_FACULTY)
        mba_designation = role_profile["program_roles"].get("MBA", DESIGNATION_FACULTY)
        staff_rows.append(
            {
                "_id": member["_id"],
                "name": member.get("name", "Not Available"),
                "email": member.get("email", "Not Available"),
                "role_label": role_label,
                "global_designation": global_designation,
                "mca_designation": mca_designation,
                "mba_designation": mba_designation,
                "is_director": global_designation == DESIGNATION_DIRECTOR
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
        },
        program_options=sorted(PROGRAM_OPTIONS)
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
    target_program = request.form.get("program")
    normalized_program = normalize_program(target_program) if target_program else None
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

    if designation in PROGRAM_SCOPED_DESIGNATIONS and not normalized_program:
        flash("Program selection is required for program-scoped role changes.", "warning")
        return redirect(url_for("admin.manage_admin_access"))

    if str(target["_id"]) == str(current_user.id) and designation != DESIGNATION_DIRECTOR:
        flash("Transfer Director role first before changing your own designation.", "warning")
        return redirect(url_for("admin.manage_admin_access"))

    if designation == DESIGNATION_DIRECTOR:
        transfer_director_access(target)
        flash("Director role transferred successfully.", "success")
        return redirect_after_admin_access_change()

    assign_designation(target, designation, normalized_program)
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

    role_to_remove = normalize_designation(request.form.get("designation"))
    target_program = request.form.get("program")
    normalized_program = normalize_program(target_program) if target_program else None
    removable_designations = {DESIGNATION_PROJECT_COORDINATOR, DESIGNATION_ACADEMIC_COORDINATOR, DESIGNATION_HOD}
    if role_to_remove not in removable_designations:
        flash("Invalid removal request.", "warning")
        return redirect(url_for("admin.manage_admin_access"))
    if role_to_remove in PROGRAM_SCOPED_DESIGNATIONS and not normalized_program:
        flash("Program is required to remove PC/HOD designation.", "warning")
        return redirect(url_for("admin.manage_admin_access"))

    delete_query = {"role": role_to_remove, "user_id": target["_id"]}
    if role_to_remove in PROGRAM_SCOPED_DESIGNATIONS:
        delete_query["program"] = normalized_program
    _role_assignments_collection().delete_many(delete_query)
    current_app.db.users.update_one(
        {"_id": target["_id"]},
        {
            "$set": {
                "role": "faculty",
                "can_manage_admins": False,
                "program": normalized_program if normalized_program else target.get("program"),
                "updated_at": datetime.utcnow(),
                "updated_by": ObjectId(current_user.id)
            }
        }
    )
    ensure_admin_access_state()
    flash(f"{designation_label(role_to_remove)} role removed successfully.", "success")
    return redirect(url_for("admin.manage_admin_access"))


@admin_bp.route("/faculty/profile", methods=["GET", "POST"])
@login_required
@role_required("faculty")
def faculty_profile():

    faculty = current_app.db.users.find_one({
        "_id": ObjectId(current_user.id)
    })
    selected_program = get_current_program()
    faculty_role_label = get_user_role_display_label(faculty)
    batch, _, _ = get_faculty_assigned_batch(faculty["_id"], request.args.get("session"), selected_program)

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

    return render_template(
        "faculty/profile.html",
        faculty=faculty,
        batch=batch,
        selected_program=selected_program,
        faculty_role_label=faculty_role_label
    )


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
    selected_program = get_current_program()
    selected_project_category = get_current_project_category(selected_program)
    user_record = get_staff_user()
    current_designation = get_user_designation(user_record, selected_program)
    batch, _, _ = get_faculty_assigned_batch(
        mentor_id,
        request.args.get("session"),
        selected_program,
        selected_project_category
    )

    students = []
    stages = list(
        current_app.db.stages.find(
            with_program_scope({}, selected_program, selected_project_category)
        ).sort("order", 1)
    )

    if batch:
        students = list(current_app.db.students.find({
            "batch_id": batch["_id"]
        }))
    elif current_designation == DESIGNATION_HOD:
        students = list(
            current_app.db.students.find(
                with_program_scope({}, selected_program, selected_project_category)
            )
        )

    student_ids = [s["_id"] for s in students]
    submissions = list(current_app.db.submissions.find({
        "student_id": {"$in": student_ids}
    })) if student_ids else []

    submission_dict = {}

    for s in submissions:
        key = str(s["student_id"]) + "_" + str(s["stage_id"])
        submission_dict[key] = s

    deadlines = []
    if batch and batch.get("session_id"):
        deadlines = list(current_app.db.deadlines.find(get_session_deadline_query(batch["session_id"])))
    elif current_designation == DESIGNATION_HOD:
        _, selected_session = get_selected_session(request.args.get("session"))
        if selected_session:
            deadlines = list(current_app.db.deadlines.find(get_session_deadline_query(selected_session["_id"])))

    deadline_dict = {}

    for d in deadlines:
        deadline_dict[str(d["stage_id"])] = d["deadline"]

    progress_document_dict = {}
    if batch and batch.get("session_id"):
        progress_documents = current_app.db.progress_documents.find({
            "session_id": batch["session_id"],
            **project_category_filter_query(selected_program, selected_project_category)
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
        selected_program=selected_program,
        selected_project_category=selected_project_category,
        dashboard_scope_label=f"{selected_program} {get_user_role_display_label(user_record)}".strip(),
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
    selected_program = get_current_program()
    selected_project_category = get_current_project_category(selected_program)

    selected_session_id = request.args.get("session")
    sessions, selected_session = get_selected_session(selected_session_id)

    students = list(
        current_app.db.students.find(
            {
                "$and": [
                    session_filter(selected_session),
                    program_filter_query(selected_program)
                ]
            }
        ).sort("prn", 1)
    )

    batches = list(
        current_app.db.batches.find(
            with_program_scope(session_filter(selected_session), selected_program)
        ).sort("created_at", -1)
    )

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
        selected_project_category=selected_project_category,
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
    batch_id = request.form.get("batch_id", "").strip()
    session_id = request.form["session_id"]
    session = current_app.db.academic_sessions.find_one({"_id": ObjectId(session_id)})

    if not session:
        flash("Select a valid academic session.", "warning")
        return redirect(url_for("admin.manage_students"))

    selected_program = get_current_program()
    batch = None
    if batch_id:
        batch = current_app.db.batches.find_one({"_id": ObjectId(batch_id)})

        if (
            not batch
            or batch.get("session_id") != session["_id"]
            or (batch.get("program") or DEFAULT_PROGRAM) != selected_program
        ):
            flash("Select a valid batch from the active academic session, or keep it unassigned.", "warning")
            return redirect(url_for("admin.manage_students", session=str(session["_id"])))

    existing = current_app.db.students.find_one({
        "prn": prn,
        "$and": [
            {
                "$or": [
                    {"session_id": session["_id"]},
                    {
                        "session_id": {"$exists": False},
                        "year": session["name"]
                    }
                ]
            },
            program_filter_query(selected_program)
        ]
    })

    if existing:
        flash("Student with this PRN already exists in the selected session.")
        return redirect(url_for("admin.manage_students", session=str(session["_id"])))

    existing_email = current_app.db.students.find_one({
        "email": email,
        "$and": [
            {
                "$or": [
                    {"session_id": session["_id"]},
                    {
                        "session_id": {"$exists": False},
                        "year": session["name"]
                    }
                ]
            },
            program_filter_query(selected_program)
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
        "batch_id": batch["_id"] if batch else None,
        "program": selected_program,
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
    selected_program = get_current_program()

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
            "$and": [
                {
                    "$or": [
                        {"session_id": session["_id"]},
                        {
                            "session_id": {"$exists": False},
                            "year": session["name"]
                        }
                    ]
                },
                program_filter_query(selected_program)
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
            "program": selected_program,
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

    batch_program = (batch.get("program") or DEFAULT_PROGRAM).upper()

    students = list(current_app.db.students.find({
        "$and": [
            session_filter({
                "_id": batch.get("session_id"),
                "name": batch.get("year")
            }),
            program_filter_query(batch_program),
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

    batch_program = (batch.get("program") or DEFAULT_PROGRAM).upper()
    normalized_student_ids = [ObjectId(student_id) for student_id in student_ids]

    # remove students already in this batch
    current_app.db.students.update_many(
        {"batch_id": ObjectId(batch_id)},
        {"$set": {"batch_id": None}}
    )

    # assign selected students
    for sid in normalized_student_ids:
        current_app.db.students.update_one(
            {
                "_id": sid,
                **program_filter_query(batch_program)
            },
            {
                "$set": {
                    "batch_id": ObjectId(batch_id),
                    "program": batch_program
                }
            }
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

    selected_program = get_current_program()
    selected_project_category = get_current_project_category(selected_program)
    batch, _, _ = get_faculty_assigned_batch(
        ObjectId(current_user.id),
        request.args.get("session"),
        selected_program,
        selected_project_category
    )

    if not batch:
        return render_template("faculty/students.html", students=[], batch=None)

    students = list(current_app.db.students.find({
        "batch_id": batch["_id"]
    }))

    for s in students:
        total = current_app.db.stages.count_documents(
            with_program_scope({}, selected_program, selected_project_category)
        )
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
    selected_program = get_current_program()
    selected_project_category = get_current_project_category(selected_program)
    batch, _, _ = get_faculty_assigned_batch(
        mentor_id,
        request.args.get("session"),
        selected_program,
        selected_project_category
    )

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

    stages = list(
        current_app.db.stages.find(
            with_program_scope({}, selected_program, selected_project_category)
        )
    )
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
    selected_program = get_current_program()
    selected_project_category = get_current_project_category(selected_program)
    batch, _, _ = get_faculty_assigned_batch(
        mentor_id,
        request.args.get("session"),
        selected_program,
        selected_project_category
    )

    if not batch:
        return render_template(
            "faculty/final_projects.html",
            final_projects=[],
            student_map={},
            batch=None,
            batch_notice="No batch is allocated to you till now.",
            selected_program=selected_program,
            selected_project_category_label=project_category_label(selected_project_category)
        )

    students = list(current_app.db.students.find({"batch_id": batch["_id"]}))
    student_ids = [student["_id"] for student in students]
    student_map = {str(student["_id"]): student for student in students}

    final_projects = list(
        current_app.db.final_submissions.find({
            "student_id": {"$in": student_ids},
            **project_category_filter_query(selected_program, selected_project_category)
        }).sort("submitted_at", -1)
    )

    return render_template(
        "faculty/final_projects.html",
        final_projects=final_projects,
        student_map=student_map,
        batch=batch,
        batch_notice=None,
        selected_program=selected_program,
        selected_project_category_label=project_category_label(selected_project_category)
    )


@admin_bp.route("/final-projects")
@login_required
@reports_access_required
def admin_final_projects():

    selected_program = get_current_program()
    selected_project_category = get_current_project_category(selected_program)
    selected_session_id = request.args.get("session")
    sessions, selected_session = get_selected_session(selected_session_id)
    final_projects = list(
        current_app.db.final_submissions.find({
            "$and": [
                {
                    "$or": [
                        {"session_id": selected_session["_id"]},
                        {"session_id": {"$exists": False}}
                    ]
                },
                program_filter_query(selected_program),
                project_category_filter_query(selected_program, selected_project_category)
            ]
        }).sort("submitted_at", -1)
    )

    student_map = {}
    mentor_map = {}
    batch_map = {}
    current_admin_batch, _, _ = get_faculty_assigned_batch(
        ObjectId(current_user.id),
        selected_session_id,
        selected_program,
        selected_project_category
    )
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
        selected_session=selected_session,
        selected_project_category=selected_project_category,
        selected_program=selected_program,
        selected_project_category_label=project_category_label(selected_project_category)
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


def _normalize_evaluation_type(value):
    normalized = str(value or "").strip().lower()
    return normalized if normalized in EVALUATION_TYPES else EVALUATION_TYPE_PRESENTATION_2


def _program_allowed_eval_types(program, project_category=None):
    normalized_program = normalize_program(program)
    if normalized_program == "MCA":
        return {
            EVALUATION_TYPE_PRESENTATION_1,
            EVALUATION_TYPE_PRESENTATION_2,
            EVALUATION_TYPE_FINAL_MCA
        }
    allowed = {EVALUATION_TYPE_PRESENTATION_1, EVALUATION_TYPE_PRESENTATION_2}
    if str(project_category or "").strip().lower() == "desk_research":
        allowed.add(EVALUATION_TYPE_DESK_RESEARCH_MBA)
    return allowed


def _resolve_eval_type_for_program(eval_type, program, project_category=None):
    normalized_program = normalize_program(program)
    normalized_category = str(project_category or "").strip().lower()
    if normalized_program == "MBA" and normalized_category == "desk_research":
        return EVALUATION_TYPE_DESK_RESEARCH_MBA

    requested = _normalize_evaluation_type(eval_type)
    allowed_types = _program_allowed_eval_types(program, project_category)
    if requested in allowed_types:
        return requested
    return EVALUATION_TYPE_PRESENTATION_2


def _evaluation_total_denominator(eval_type):
    normalized_type = _normalize_evaluation_type(eval_type)
    if normalized_type == EVALUATION_TYPE_PRESENTATION_1:
        return 20
    if normalized_type in {EVALUATION_TYPE_FINAL_MCA, EVALUATION_TYPE_DESK_RESEARCH_MBA}:
        return 50
    return 30


def _evaluation_type_query(eval_type):
    eval_type = _normalize_evaluation_type(eval_type)
    if eval_type == EVALUATION_TYPE_PRESENTATION_2:
        return {"$or": [{"evaluation_type": EVALUATION_TYPE_PRESENTATION_2}, {"evaluation_type": {"$exists": False}}]}
    return {"evaluation_type": eval_type}


def _sync_shared_fields_from_presentation1(student, session_doc, project_title, synopsis_status):
    if not student or not session_doc:
        return

    guide_name = "Not Assigned"
    batch_id = student.get("batch_id")
    if batch_id:
        batch = current_app.db.batches.find_one({"_id": batch_id})
        if batch and batch.get("mentor_id"):
            mentor = current_app.db.users.find_one({"_id": batch["mentor_id"]})
            if mentor:
                guide_name = mentor.get("name", "Not Assigned")

    now = datetime.utcnow()
    current_app.db.evaluations.update_one(
        {
            "student_id": student["_id"],
            "session_id": session_doc["_id"],
            **_evaluation_type_query(EVALUATION_TYPE_PRESENTATION_2)
        },
        {
            "$set": {
                "student_id": student["_id"],
                "batch_id": student.get("batch_id"),
                "session_id": session_doc["_id"],
                "evaluation_type": EVALUATION_TYPE_PRESENTATION_2,
                "roll_no": student.get("roll_no") or student.get("prn"),
                "student_name": student.get("name"),
                "project_title": project_title,
                "synopsis_status": synopsis_status,
                "updated_at": now
            },
            "$setOnInsert": {
                "created_at": now,
                "guide_name": guide_name,
                "chapter3": 0,
                "chapter4": 0,
                "execution_demo": 0,
                "total": 0,
                "signature": ""
            }
        },
        upsert=True
    )

    if normalize_program(student.get("program")) != "MCA":
        return

    current_app.db.evaluations.update_one(
        {
            "student_id": student["_id"],
            "session_id": session_doc["_id"],
            **_evaluation_type_query(EVALUATION_TYPE_FINAL_MCA)
        },
        {
            "$set": {
                "student_id": student["_id"],
                "batch_id": student.get("batch_id"),
                "session_id": session_doc["_id"],
                "evaluation_type": EVALUATION_TYPE_FINAL_MCA,
                "roll_no": student.get("roll_no") or student.get("prn"),
                "student_name": student.get("name"),
                "project_title": project_title,
                "synopsis_status": synopsis_status,
                "updated_at": now
            },
            "$setOnInsert": {
                "created_at": now,
                "guide_name": guide_name,
                "system_design_analysis": 0,
                "coding_implementation": 0,
                "testing_results": 0,
                "report_documentation": 0,
                "viva_presentation": 0,
                "total": 0,
                "signature": ""
            }
        },
        upsert=True
    )


def _evaluation_student_scope(user, selected_session, selected_batch_id=None):
    selected_program = get_current_program()
    selected_project_category = get_current_project_category(selected_program)
    base_session_program_filters = [
        session_filter(selected_session),
        program_filter_query(selected_program)
    ]

    scoped_batches_query = {
        "$and": base_session_program_filters + (
            [project_category_filter_query(selected_program, selected_project_category)]
            if selected_program == "MBA" else []
        )
    }
    scoped_batches = list(current_app.db.batches.find(scoped_batches_query).sort("name", 1))
    scoped_batch_ids = [batch["_id"] for batch in scoped_batches]

    students_query = {"$and": base_session_program_filters}
    selected_batch = None

    if can_edit_evaluation_all(user) or is_director(user):
        if selected_batch_id:
            try:
                selected_batch = current_app.db.batches.find_one({
                    "_id": ObjectId(selected_batch_id),
                    "$and": base_session_program_filters + (
                        [project_category_filter_query(selected_program, selected_project_category)]
                        if selected_program == "MBA" else []
                    )
                })
            except Exception:
                selected_batch = None
            if selected_batch:
                students_query = {
                    "$and": [
                        *base_session_program_filters,
                        {"batch_id": selected_batch["_id"]}
                    ]
                }
            else:
                if selected_program == "MBA":
                    students_query = {
                        "$and": [
                            *base_session_program_filters,
                            {"batch_id": {"$in": scoped_batch_ids}}
                        ]
                    }
                else:
                    students_query = {"$and": base_session_program_filters}
        else:
            if selected_program == "MBA":
                students_query = {
                    "$and": [
                        *base_session_program_filters,
                        {"batch_id": {"$in": scoped_batch_ids}}
                    ]
                }
            else:
                students_query = {"$and": base_session_program_filters}
    else:
        mentor_batch, _, _ = get_faculty_assigned_batch(
            user["_id"],
            str(selected_session["_id"]),
            selected_program,
            selected_project_category
        )
        if not mentor_batch:
            return [], [], None
        selected_batch = mentor_batch
        students_query = {
            "$and": [
                *base_session_program_filters,
                {"batch_id": mentor_batch["_id"]}
            ]
        }

    students = list(current_app.db.students.find(students_query).sort("roll_no", 1))
    if not students:
        students = list(current_app.db.students.find(students_query).sort("prn", 1))

    batches = scoped_batches
    return students, batches, selected_batch


@admin_bp.route("/evaluation-sheet")
@login_required
@evaluation_access_required
def evaluation_sheet():
    user = get_staff_user()
    selected_program = get_current_program()
    selected_project_category = get_current_project_category(selected_program)
    selected_session_id = request.args.get("session")
    selected_batch_id = request.args.get("batch")
    selected_eval_type = _resolve_eval_type_for_program(
        request.args.get("eval_type"),
        selected_program,
        selected_project_category
    )

    sessions, selected_session = get_selected_session(selected_session_id)
    students, batches, selected_batch = _evaluation_student_scope(user, selected_session, selected_batch_id)

    student_ids = [student["_id"] for student in students]
    evaluations = list(
        current_app.db.evaluations.find({
            "student_id": {"$in": student_ids},
            "session_id": selected_session["_id"],
            **_evaluation_type_query(selected_eval_type)
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
        selected_eval_type=selected_eval_type,
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
    selected_program = get_current_program()
    selected_project_category = get_current_project_category(selected_program)
    student = current_app.db.students.find_one({"_id": ObjectId(student_id)})
    if not student:
        flash("Student not found.", "warning")
        return redirect(url_for("admin.evaluation_sheet"))

    if not can_edit_evaluation_student(user, student):
        flash("You can edit only permitted student evaluations.", "danger")
        return redirect(url_for("admin.evaluation_sheet"))

    selected_session_id = request.form.get("session_id")
    selected_batch_id = request.form.get("batch_id")
    selected_eval_type = _resolve_eval_type_for_program(
        request.form.get("eval_type"),
        selected_program,
        selected_project_category
    )
    session_doc = current_app.db.academic_sessions.find_one({"_id": ObjectId(selected_session_id)}) if selected_session_id else None
    if not session_doc:
        flash("Academic session not found.", "warning")
        return redirect(url_for("admin.evaluation_sheet"))

    chapter1 = _safe_mark(request.form.get("chapter1"))
    chapter2 = _safe_mark(request.form.get("chapter2"))
    chapter3 = _safe_mark(request.form.get("chapter3"))
    chapter4 = _safe_mark(request.form.get("chapter4"))
    execution = _safe_mark(request.form.get("execution_demo"))
    system_design_analysis = _safe_mark(request.form.get("system_design_analysis"))
    coding_implementation = _safe_mark(request.form.get("coding_implementation"))
    testing_results = _safe_mark(request.form.get("testing_results"))
    report_documentation = _safe_mark(request.form.get("report_documentation"))
    viva_presentation = _safe_mark(request.form.get("viva_presentation"))
    c1 = _safe_mark(request.form.get("c1"))
    c2 = _safe_mark(request.form.get("c2"))
    c3 = _safe_mark(request.form.get("c3"))
    c4 = _safe_mark(request.form.get("c4"))
    c5 = _safe_mark(request.form.get("c5"))
    evaluation_date = (request.form.get("evaluation_date") or "").strip()
    if selected_eval_type == EVALUATION_TYPE_PRESENTATION_1:
        total = round(chapter1 + chapter2, 2)
    elif selected_eval_type == EVALUATION_TYPE_FINAL_MCA:
        total = round(system_design_analysis + coding_implementation + testing_results + report_documentation + viva_presentation, 2)
    elif selected_eval_type == EVALUATION_TYPE_DESK_RESEARCH_MBA:
        total = round(c1 + c2 + c3 + c4 + c5, 2)
    else:
        total = round(chapter3 + chapter4 + execution, 2)

    synopsis_status = (request.form.get("synopsis_status") or "No").strip().title()
    if synopsis_status not in ["Yes", "No"]:
        synopsis_status = "No"

    guide_name = (request.form.get("guide_name") or "").strip()
    project_title = (request.form.get("project_title") or "").strip()
    signature = (request.form.get("signature") or "").strip()

    now = datetime.utcnow()
    current_app.db.evaluations.update_one(
        {
            "student_id": student["_id"],
            "session_id": session_doc["_id"],
            **_evaluation_type_query(selected_eval_type)
        },
        {
            "$set": {
                "student_id": student["_id"],
                "batch_id": student.get("batch_id"),
                "session_id": session_doc["_id"],
                "evaluation_type": selected_eval_type,
                "roll_no": student.get("roll_no") or student.get("prn"),
                "student_name": student.get("name"),
                "guide_name": guide_name or "Not Assigned",
                "project_title": project_title,
                "synopsis_status": synopsis_status,
                "chapter1": chapter1,
                "chapter2": chapter2,
                "chapter3": chapter3,
                "chapter4": chapter4,
                "execution_demo": execution,
                "system_design_analysis": system_design_analysis,
                "coding_implementation": coding_implementation,
                "testing_results": testing_results,
                "report_documentation": report_documentation,
                "viva_presentation": viva_presentation,
                "c1": c1,
                "c2": c2,
                "c3": c3,
                "c4": c4,
                "c5": c5,
                "evaluation_date": evaluation_date,
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

    if selected_eval_type == EVALUATION_TYPE_PRESENTATION_1:
        _sync_shared_fields_from_presentation1(student, session_doc, project_title, synopsis_status)

    total_denominator = _evaluation_total_denominator(selected_eval_type)
    create_notification(student["_id"], f"Your evaluation sheet was updated. Total: {total}/{total_denominator}")
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
    return redirect(url_for("admin.evaluation_sheet", session=str(session_doc["_id"]), batch=selected_batch_id, eval_type=selected_eval_type))


@admin_bp.route("/evaluation-sheet/save-all", methods=["POST"])
@login_required
@evaluation_access_required
def save_evaluation_sheet():
    user = get_staff_user()
    selected_program = get_current_program()
    selected_project_category = get_current_project_category(selected_program)
    if not (can_edit_evaluation_all(user) or get_user_designation(user) == DESIGNATION_FACULTY):
        flash("You do not have permission to edit the evaluation sheet.", "danger")
        return redirect(url_for("admin.evaluation_sheet"))

    selected_session_id = request.form.get("session_id")
    selected_batch_id = request.form.get("batch_id")
    selected_eval_type = _resolve_eval_type_for_program(
        request.form.get("eval_type"),
        selected_program,
        selected_project_category
    )
    session_doc = current_app.db.academic_sessions.find_one({"_id": ObjectId(selected_session_id)}) if selected_session_id else None
    if not session_doc:
        flash("Academic session not found.", "warning")
        return redirect(url_for("admin.evaluation_sheet"))

    editable_ids = list(dict.fromkeys(request.form.getlist("editable_rows")))
    if not editable_ids:
        flash("No editable rows found. Click Edit on saved rows before saving changes.", "info")
        return redirect(url_for("admin.evaluation_sheet", session=str(session_doc["_id"]), batch=selected_batch_id, eval_type=selected_eval_type))

    saved_count = 0
    for student_id in editable_ids:
        try:
            student_object_id = ObjectId(student_id)
        except Exception:
            continue

        student = current_app.db.students.find_one({"_id": student_object_id})
        if not student:
            continue

        if not can_edit_evaluation_student(user, student):
            continue

        chapter1 = _safe_mark(request.form.get(f"chapter1_{student_id}"))
        chapter2 = _safe_mark(request.form.get(f"chapter2_{student_id}"))
        chapter3 = _safe_mark(request.form.get(f"chapter3_{student_id}"))
        chapter4 = _safe_mark(request.form.get(f"chapter4_{student_id}"))
        execution = _safe_mark(request.form.get(f"execution_demo_{student_id}"))
        system_design_analysis = _safe_mark(request.form.get(f"system_design_analysis_{student_id}"))
        coding_implementation = _safe_mark(request.form.get(f"coding_implementation_{student_id}"))
        testing_results = _safe_mark(request.form.get(f"testing_results_{student_id}"))
        report_documentation = _safe_mark(request.form.get(f"report_documentation_{student_id}"))
        viva_presentation = _safe_mark(request.form.get(f"viva_presentation_{student_id}"))
        c1 = _safe_mark(request.form.get(f"c1_{student_id}"))
        c2 = _safe_mark(request.form.get(f"c2_{student_id}"))
        c3 = _safe_mark(request.form.get(f"c3_{student_id}"))
        c4 = _safe_mark(request.form.get(f"c4_{student_id}"))
        c5 = _safe_mark(request.form.get(f"c5_{student_id}"))
        evaluation_date = (request.form.get(f"evaluation_date_{student_id}") or "").strip()
        if selected_eval_type == EVALUATION_TYPE_PRESENTATION_1:
            total = round(chapter1 + chapter2, 2)
        elif selected_eval_type == EVALUATION_TYPE_FINAL_MCA:
            total = round(system_design_analysis + coding_implementation + testing_results + report_documentation + viva_presentation, 2)
        elif selected_eval_type == EVALUATION_TYPE_DESK_RESEARCH_MBA:
            total = round(c1 + c2 + c3 + c4 + c5, 2)
        else:
            total = round(chapter3 + chapter4 + execution, 2)

        synopsis_status = (request.form.get(f"synopsis_status_{student_id}") or "No").strip().title()
        if synopsis_status not in ["Yes", "No"]:
            synopsis_status = "No"

        guide_name = (request.form.get(f"guide_name_{student_id}") or "").strip()
        project_title = (request.form.get(f"project_title_{student_id}") or "").strip()
        signature = (request.form.get(f"signature_{student_id}") or "").strip()

        now = datetime.utcnow()
        current_app.db.evaluations.update_one(
            {
                "student_id": student["_id"],
                "session_id": session_doc["_id"],
                **_evaluation_type_query(selected_eval_type)
            },
            {
                "$set": {
                    "student_id": student["_id"],
                    "batch_id": student.get("batch_id"),
                    "session_id": session_doc["_id"],
                    "evaluation_type": selected_eval_type,
                    "roll_no": student.get("roll_no") or student.get("prn"),
                    "student_name": student.get("name"),
                    "guide_name": guide_name or "Not Assigned",
                    "project_title": project_title,
                    "synopsis_status": synopsis_status,
                    "chapter1": chapter1,
                    "chapter2": chapter2,
                    "chapter3": chapter3,
                    "chapter4": chapter4,
                    "execution_demo": execution,
                    "system_design_analysis": system_design_analysis,
                    "coding_implementation": coding_implementation,
                    "testing_results": testing_results,
                    "report_documentation": report_documentation,
                    "viva_presentation": viva_presentation,
                    "c1": c1,
                    "c2": c2,
                    "c3": c3,
                    "c4": c4,
                    "c5": c5,
                    "evaluation_date": evaluation_date,
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

        if selected_eval_type == EVALUATION_TYPE_PRESENTATION_1:
            _sync_shared_fields_from_presentation1(student, session_doc, project_title, synopsis_status)

        total_denominator = _evaluation_total_denominator(selected_eval_type)
        create_notification(student["_id"], f"Your evaluation sheet was updated. Total: {total}/{total_denominator}")
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

        saved_count += 1

    if saved_count:
        flash(f"Evaluation sheet saved for {saved_count} student(s).", "success")
    else:
        flash("No rows were saved. Check access scope or selected rows.", "warning")

    return redirect(url_for("admin.evaluation_sheet", session=str(session_doc["_id"]), batch=selected_batch_id, eval_type=selected_eval_type))


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
    selected_program = get_current_program()
    selected_project_category = get_current_project_category(selected_program)
    selected_eval_type = _resolve_eval_type_for_program(
        request.form.get("eval_type"),
        selected_program,
        selected_project_category
    )
    session_doc = current_app.db.academic_sessions.find_one({"_id": ObjectId(selected_session_id)}) if selected_session_id else None
    if not session_doc:
        flash("Academic session not found.", "warning")
        return redirect(url_for("admin.evaluation_sheet"))

    current_app.db.evaluations.delete_one({
        "student_id": student["_id"],
        "session_id": session_doc["_id"],
        **_evaluation_type_query(selected_eval_type)
    })

    flash("Evaluation row removed.", "success")
    return redirect(url_for("admin.evaluation_sheet", session=str(session_doc["_id"]), batch=selected_batch_id, eval_type=selected_eval_type))


@admin_bp.route("/evaluation-sheet/export")
@login_required
@evaluation_access_required
def export_evaluation_sheet():
    user = get_staff_user()
    selected_program = get_current_program()
    selected_project_category = get_current_project_category(selected_program)
    selected_session_id = request.args.get("session")
    selected_batch_id = request.args.get("batch")
    selected_eval_type = _resolve_eval_type_for_program(
        request.args.get("eval_type"),
        selected_program,
        selected_project_category
    )
    sessions, selected_session = get_selected_session(selected_session_id)
    _ = sessions

    students, _, selected_batch = _evaluation_student_scope(user, selected_session, selected_batch_id)
    student_ids = [student["_id"] for student in students]
    evaluations = list(
        current_app.db.evaluations.find({
            "student_id": {"$in": student_ids},
            "session_id": selected_session["_id"],
            **_evaluation_type_query(selected_eval_type)
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

        if selected_eval_type == EVALUATION_TYPE_PRESENTATION_1:
            rows.append({
                "SN": idx,
                "Roll Number": student.get("roll_no") or student.get("prn"),
                "Student Name": student.get("name", ""),
                "Guide Name": guide_name,
                "Project Title": evaluation.get("project_title", ""),
                "Synopsis Submission Status (Yes/No)": evaluation.get("synopsis_status", "No"),
                "Chapter 1 (10)": evaluation.get("chapter1", 0),
                "Chapter 2 (10)": evaluation.get("chapter2", 0),
                "Total (20)": evaluation.get("total", 0),
                "Signature": evaluation.get("signature", "")
            })
        elif selected_eval_type == EVALUATION_TYPE_PRESENTATION_2:
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
        elif selected_eval_type == EVALUATION_TYPE_FINAL_MCA:
            rows.append({
                "SN": idx,
                "Roll Number": student.get("roll_no") or student.get("prn"),
                "Student Name": student.get("name", ""),
                "Guide Name": guide_name,
                "Project Title": evaluation.get("project_title", ""),
                "Synopsis Submission Status (Yes/No)": evaluation.get("synopsis_status", "No"),
                "System Design & Analysis (10)": evaluation.get("system_design_analysis", 0),
                "Coding & Implementation (10)": evaluation.get("coding_implementation", 0),
                "Testing & Results (10)": evaluation.get("testing_results", 0),
                "Report & Documentation (10)": evaluation.get("report_documentation", 0),
                "Viva & Presentation Skills (10)": evaluation.get("viva_presentation", 0),
                "Total (50)": evaluation.get("total", 0),
                "Student Signature": evaluation.get("signature", "")
            })
        else:
            rows.append({
                "SN": idx,
                "Roll Number": student.get("roll_no") or student.get("prn"),
                "Student Name": student.get("name", ""),
                "C1 (10)": evaluation.get("c1", 0),
                "C2 (10)": evaluation.get("c2", 0),
                "C3 (10)": evaluation.get("c3", 0),
                "C4 (10)": evaluation.get("c4", 0),
                "C5 (10)": evaluation.get("c5", 0),
                "Marks out of 50": evaluation.get("total", 0),
                "Date": evaluation.get("evaluation_date", ""),
                "Student Signature": evaluation.get("signature", "")
            })

    df = pd.DataFrame(rows)
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Evaluation")
    output.seek(0)

    batch_part = f"_{selected_batch['name']}" if selected_batch else ""
    if selected_eval_type == EVALUATION_TYPE_PRESENTATION_1:
        eval_label = "presentation1"
    elif selected_eval_type == EVALUATION_TYPE_FINAL_MCA:
        eval_label = "final_mca"
    elif selected_eval_type == EVALUATION_TYPE_DESK_RESEARCH_MBA:
        eval_label = "desk_research_mba"
    else:
        eval_label = "presentation2"
    filename = f"evaluation_{eval_label}_{selected_session['name']}{batch_part}.xlsx".replace(" ", "_")
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
