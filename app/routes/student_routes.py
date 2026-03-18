from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_required, login_user, logout_user, current_user
from bson.objectid import ObjectId
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from app.decorators.role_required import role_required
import os
from werkzeug.utils import secure_filename
from app.services.notification_service import create_notification, get_notifications, mark_notifications_read
from app.services.file_converter import convert_to_pdf

student_bp = Blueprint("student", __name__, url_prefix="/student")


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

        flash("Invalid PRN or Password")

    return render_template("student/login.html")


# ---------------- STUDENT DASHBOARD ----------------
# @student_bp.route("/dashboard")
# @login_required
# @role_required("student")
# def dashboard():

#     student = current_app.db.students.find_one({
#         "_id": ObjectId(current_user.id)
#     })

#     batch = current_app.db.batches.find_one({
#         "_id": student["batch_id"]
#     })

#     stages = list(current_app.db.stages.find().sort("order", 1))

#     deadlines = list(current_app.db.deadlines.find({
#         "batch_id": batch["_id"]
#     }))

#     deadline_dict = {}
#     for d in deadlines:
#         deadline_dict[str(d["stage_id"])] = d["deadline"]

#     # submissions = list(current_app.db.submissions.find({
#     #     "student_id": ObjectId(current_user.id)
#     # }))
    
#     submissions = list(current_app.db.submissions.find({
#         "student_id": student["_id"]
#     }))

#     submission_dict = {}
#     for s in submissions:
#         submission_dict[str(s["stage_id"])] = s

    
#     total_stages = len(stages)

#     completed = 0

#     for stage in stages:
#         sub = submission_dict.get(str(stage["_id"]))

#         if sub and sub.get("status") == "approved":
#             completed += 1

#     progress = 0

#     if total_stages > 0:
#         progress = int((completed / total_stages) * 100)

#     return render_template(
#         "student/dashboard.html",
#         stages=stages,
#         deadline_dict=deadline_dict,
#         submission_dict=submission_dict,
#         progress=progress,
#         batch=batch
#     )

@student_bp.route("/dashboard")
@login_required
@role_required("student")
def dashboard():

    student = current_app.db.students.find_one({"_id": ObjectId(current_user.id)})

    batch = current_app.db.batches.find_one({"_id": student["batch_id"]})

    mentor = None

    if batch and batch.get("mentor_id"):
        mentor = current_app.db.users.find_one({"_id": batch["mentor_id"]})

    stages = list(current_app.db.stages.find())

    submissions = list(current_app.db.submissions.find({
        "student_id": student["_id"]
    }))

    approved = 0

    for s in submissions:
        if s["status"] == "approved":
            approved += 1

    progress = 0
    if len(stages) > 0:
        progress = int((approved / len(stages)) * 100)

    notifications, unread_count = get_notifications(current_user.id)

    return render_template(
        "student/dashboard.html",
        student=student,
        batch=batch,
        mentor=mentor,
        progress=progress,
        notifications=notifications,
        unread_count=unread_count
    )


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

    deadlines = list(current_app.db.deadlines.find({
        "batch_id": student["batch_id"]
    }))

    deadline_dict = {}

    for d in deadlines:
        deadline_dict[str(d["stage_id"])] = d["deadline"]

    return render_template(
        "student/submissions.html",
        stages=stages,
        submission_dict=submission_dict,
        deadline_dict=deadline_dict,
        
    )

@student_bp.route("/upload/<stage_id>", methods=["POST"])
@login_required
@role_required("student")
def upload_stage(stage_id):

    student = current_app.db.students.find_one({"_id": ObjectId(current_user.id)})

    batch_id = student["batch_id"]

    deadline_doc = current_app.db.deadlines.find_one({
        "batch_id": batch_id,
        "stage_id": ObjectId(stage_id)
    })

    deadline = deadline_doc["deadline"] if deadline_doc else None

    now = datetime.utcnow()

    late = False
    if deadline and now > deadline:
        late = True

    file = request.files["file"]
    filename = secure_filename(file.filename)

    upload_folder = current_app.config["UPLOAD_FOLDER"]

    if not os.path.exists(upload_folder):
        os.makedirs(upload_folder)

    filepath = os.path.join(upload_folder, filename)

    # ✅ FIXED
    file.save(filepath)

    # ✅ Convert to PDF
    pdf_file = convert_to_pdf(filepath, upload_folder)

    if not pdf_file:
        flash("File conversion failed. Upload PDF or check system.")
        return redirect(url_for("student.dashboard"))

    # ✅ STORE pdf_file ALSO
    current_app.db.submissions.update_one(
        {
            "student_id": ObjectId(current_user.id),
            "stage_id": ObjectId(stage_id)
        },
        {
            "$set": {
                "file_name": filename,
                "pdf_file": pdf_file,   # 🔥 IMPORTANT
                "submitted_at": now,
                "status": "pending",
                "late": late
            }
        },
        upsert=True
    )

    # ---------------- NOTIFICATION ----------------

    student_name = student["name"]

    stage = current_app.db.stages.find_one({"_id": ObjectId(stage_id)})
    stage_name = stage["name"]

    batch = current_app.db.batches.find_one({"_id": student["batch_id"]})
    faculty_id = batch["mentor_id"]

    create_notification(
        faculty_id,
        f"{student_name} submitted {stage_name}"
    )

    # Late submission notification for admin
    if late:
        admin = current_app.db.users.find_one({"role": "admin"})

        create_notification(
            admin["_id"],
            f"{student_name} submitted {stage_name} late"
        )

    flash("File uploaded successfully")

    return redirect(url_for("student.dashboard"))