from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_required, login_user, logout_user, current_user
from bson.objectid import ObjectId
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from app.decorators.role_required import role_required
import os
from werkzeug.utils import secure_filename
from app.services.email_service import late_submission_email
from app.services.notification_service import create_notification, get_notifications, mark_notifications_read
from app.services.file_converter import convert_to_pdf
from app.services.email_service import send_email, submission_email, late_submission_email
from datetime import datetime, timedelta

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

    for stage in stages:

        deadline_doc = current_app.db.deadlines.find_one({
            "batch_id": batch["_id"],
            "stage_id": stage["_id"]
        })

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

        # ✅ SKIP if reminder already sent
        if submission and submission.get("reminder_sent") == True:
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
            if submission:
                current_app.db.submissions.update_one(
                    {"_id": submission["_id"]},
                    {"$set": {"reminder_sent": True}}
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

    update_data = {
        "name": name,
        "email": email
    }

    if file and file.filename:
        filename = file.filename
        file.save(os.path.join(current_app.config["UPLOAD_FOLDER"], filename))
        update_data["photo"] = filename

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

# @student_bp.route("/upload/<stage_id>", methods=["POST"])
# @login_required
# @role_required("student")
# def upload_stage(stage_id):

#     student = current_app.db.students.find_one({"_id": ObjectId(current_user.id)})

#     batch_id = student["batch_id"]

#     deadline_doc = current_app.db.deadlines.find_one({
#         "batch_id": batch_id,
#         "stage_id": ObjectId(stage_id)
#     })

#     deadline = deadline_doc["deadline"] if deadline_doc else None

#     now = datetime.utcnow()

#     late = False
#     if deadline and now > deadline:
#         late = True

#     file = request.files["file"]
#     filename = secure_filename(file.filename)

#     upload_folder = current_app.config["UPLOAD_FOLDER"]

#     if not os.path.exists(upload_folder):
#         os.makedirs(upload_folder)

#     filepath = os.path.join(upload_folder, filename)

#     # ✅ FIXED
#     file.save(filepath)

#     # ✅ Convert to PDF
#     pdf_file = convert_to_pdf(filepath, upload_folder)

#     if not pdf_file:
#         flash("File conversion failed. Upload PDF or check system.")
#         return redirect(url_for("student.dashboard"))

#     # ✅ STORE pdf_file ALSO
#     current_app.db.submissions.update_one(
#         {
#             "student_id": ObjectId(current_user.id),
#             "stage_id": ObjectId(stage_id)
#         },
#         {
#             "$set": {
#                 "file_name": filename,
#                 "pdf_file": pdf_file,   # 🔥 IMPORTANT
#                 "submitted_at": now,
#                 "status": "pending",
#                 "late": late
#             }
#         },
#         upsert=True
#     )

#     # ---------------- NOTIFICATION ----------------

#     student_name = student["name"]

#     stage = current_app.db.stages.find_one({"_id": ObjectId(stage_id)})
#     stage_name = stage["name"]

#     batch = current_app.db.batches.find_one({"_id": student["batch_id"]})
#     faculty_id = batch["mentor_id"]

#     create_notification(
#         faculty_id,
#         f"{student_name} submitted {stage_name}"
#     )

#     # Late submission notification for admin
#     if late:
#         admin = current_app.db.users.find_one({"role": "admin"})

#         if admin:
#             create_notification(
#                 admin["_id"],
#                 f"{student_name} submitted {stage_name} late"
#             )
 
#     if admin and admin.get("email"):
#         send_email(
#             admin["email"],
#             "Late Submission Alert",
#             late_submission_email(student_name, stage_name)
#         )

#     faculty = current_app.db.users.find_one({"_id": faculty_id})

#     if faculty and faculty.get("email"):
#         try:
#             send_email(
#                 faculty["email"],
#                 "New Submission",
#                 submission_email(student_name, stage_name)
#             )
#         except Exception as e:
#             print("Email error:", e)
#         except Exception as e:
#             print("Email error:", e)

# # Late submission email to admin
#     if late:
#         admin = current_app.db.users.find_one({"role": "admin"})

#         if admin and admin.get("email"):
#             try:
#                 send_email(
#                     admin["email"],
#                     "Late Submission Alert",
#                     late_submission_email(student_name, stage_name)
#                 )
#             except Exception as e:
#                 print("Email error:", e)
        
#     flash("File uploaded successfully")

#     return redirect(url_for("student.dashboard"))


@student_bp.route("/upload/<stage_id>", methods=["POST"])
@login_required
@role_required("student")
def upload_stage(stage_id):

    student = current_app.db.students.find_one({"_id": ObjectId(current_user.id)})

    batch_id = student["batch_id"]

    # -------- DEADLINE CHECK --------
    deadline_doc = current_app.db.deadlines.find_one({
        "batch_id": batch_id,
        "stage_id": ObjectId(stage_id)
    })

    deadline = deadline_doc["deadline"] if deadline_doc else None
    now = datetime.utcnow()

    late = False
    if deadline and now > deadline:
        late = True

    # -------- FILE UPLOAD --------
    file = request.files["file"]
    filename = secure_filename(file.filename)

    upload_folder = current_app.config["UPLOAD_FOLDER"]

    if not os.path.exists(upload_folder):
        os.makedirs(upload_folder)

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
            }
        },
        upsert=True
    )

    # -------- NOTIFICATION --------
    student_name = student["name"]

    stage = current_app.db.stages.find_one({"_id": ObjectId(stage_id)})
    stage_name = stage["name"]

    batch = current_app.db.batches.find_one({"_id": student["batch_id"]})
    # faculty_id = batch.get("mentor_id")
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

        if admin:
            create_notification(
                admin["_id"],
                f"{student_name} submitted {stage_name} late"
            )

            # EMAIL admin
            if admin.get("email"):
                try:
                    send_email(
                        admin["email"],
                        "Late Submission Alert",
                        late_submission_email(student_name, stage_name)
                    )
                except Exception as e:
                    print("Email error:", e)

    flash("File uploaded successfully")
    return redirect(url_for("student.dashboard"))