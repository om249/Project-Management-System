import email
from unittest import result

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
from app.services.notification_service import get_notifications, create_notification,  mark_notifications_read
from app.services.email_service import send_email, student_welcome_email, submission_email, late_submission_email, status_email
from app.routes.student_routes import submissions

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


# ===================== DASHBOARD =====================
@admin_bp.route("/dashboard")
@login_required
@role_required("admin")
def dashboard():
    total_batches = current_app.db.batches.count_documents({})
    total_stages = current_app.db.stages.count_documents({})
    notifications, unread_count = get_notifications(current_user.id)

    return render_template(
        "admin/dashboard.html",
        total_batches=total_batches,
        total_stages=total_stages,
        notifications=notifications,
        unread_count=unread_count
    )


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
@role_required("admin")
def manage_batches():

    if request.method == "POST":
        name = request.form.get("name")

        if name:
            name = name.strip()
            existing = current_app.db.batches.find_one({"name": name})

            if existing:
                flash("Batch already exists.")
            else:
                current_app.db.batches.insert_one({
                    "name": name,
                    "year_id": ObjectId(year_id),
                    "mentor_id": None,
                    "created_at": datetime.utcnow()
                })
                flash("Batch created successfully.")

        return redirect(url_for("admin.manage_batches"))

    batches = list(current_app.db.batches.find().sort("created_at", -1))

    # Get all assigned mentor_ids
    assigned_mentors = current_app.db.batches.distinct("mentor_id")

    # Remove None if exists
    assigned_mentors = [m for m in assigned_mentors if m]

    # Fetch only faculty NOT assigned
    faculty = list(current_app.db.users.find({
    "role": "faculty",
    "_id": {"$nin": assigned_mentors}
    })) 

    assigned_mentors = current_app.db.batches.distinct("mentor_id")
    assigned_mentors = [m for m in assigned_mentors if m]

    faculty = list(current_app.db.users.find({
        "role": "faculty",
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
        faculty=faculty
    )

# ===================== ASSIGN MENTOR =====================
@admin_bp.route("/assign-mentor", methods=["POST"])
@login_required
@role_required("admin")
def assign_mentor():

    batch_id = request.form["batch_id"]
    mentor_id = request.form.get("mentor_id")

    batch = current_app.db.batches.find_one({"_id": ObjectId(batch_id)})

    if not batch:
        return redirect(url_for("admin.manage_batches"))

    # If removing mentor
    if mentor_id == "remove":
        current_app.db.batches.update_one(
            {"_id": ObjectId(batch_id)},
            {"$set": {"mentor_id": None}}
        )
        flash("Mentor removed successfully.")
        return redirect(url_for("admin.manage_batches"))

    # Check if mentor already assigned to another batch
    already_assigned = current_app.db.batches.find_one({
        "mentor_id": ObjectId(mentor_id),
        "_id": {"$ne": ObjectId(batch_id)}
    })

    if already_assigned:
        flash("This mentor is already assigned to another batch.")
        return redirect(url_for("admin.manage_batches"))

    # Assign / Replace mentor
    current_app.db.batches.update_one(
        {"_id": ObjectId(batch_id)},
        {"$set": {"mentor_id": ObjectId(mentor_id)}}
    )

    mentor = current_app.db.users.find_one({"_id": ObjectId(mentor_id)})

    if mentor:
        send_email(
            mentor["email"],
            "Batch Assigned",
            f"<h3>You have been assigned to batch: {batch['name']}</h3>"
        )

    students = list(current_app.db.students.find({"batch_id": ObjectId(batch_id)}))

    print("STUDENTS FOUND:", students)

    for s in students:
        if s.get("email"):
            print("SENDING EMAIL:", s["email"])

            try:
                send_email(
                    s["email"],
                    "Mentor Assigned",
                    f"""
                    <h3>Mentor Assigned</h3>
                    <p>Hello {s['name']},</p>
                    <p>Your mentor is <b>{mentor['name']}</b></p>
                    """
                )
            except Exception as e:
                print("EMAIL ERROR:", e)

    flash("Mentor updated successfully.")
    return redirect(url_for("admin.manage_batches"))

# ===================== DELETE BATCH =====================
@admin_bp.route("/delete-batch/<batch_id>")
@login_required
@role_required("admin")
def delete_batch(batch_id):
    current_app.db.batches.delete_one({"_id": ObjectId(batch_id)})
    current_app.db.deadlines.delete_many({"batch_id": ObjectId(batch_id)})
    flash("Batch deleted.")
    return redirect(url_for("admin.manage_batches"))


# ===================== STAGE MANAGEMENT =====================
@admin_bp.route("/stages", methods=["GET", "POST"])
@login_required
@role_required("admin")
def manage_stages():

    # -------- Add New Stage --------
    if request.method == "POST" and "name" in request.form:
        name = request.form["name"].strip()

        if not name:
            flash("Stage name cannot be empty.")
            return redirect(url_for("admin.manage_stages"))

        last_stage = current_app.db.stages.find_one(sort=[("order", -1)])
        next_order = last_stage["order"] + 1 if last_stage else 1

        current_app.db.stages.insert_one({
            "name": name,
            "order": next_order
        })

        flash("Stage added successfully.")
        return redirect(url_for("admin.manage_stages"))

    # -------- GET DATA --------
    selected_batch_id = request.args.get("batch")

    batches = list(current_app.db.batches.find().sort("created_at", -1))
    stages = list(current_app.db.stages.find().sort("order", 1))

    deadline_dict = {}

    if selected_batch_id:
        deadlines = current_app.db.deadlines.find({
            "batch_id": ObjectId(selected_batch_id)
        })

        for d in deadlines:
            deadline_dict[str(d["stage_id"])] = d["deadline"].strftime("%Y-%m-%d")

    return render_template(
        "admin/stages.html",
        stages=stages,
        batches=batches,
        selected_batch_id=selected_batch_id,
        deadline_dict=deadline_dict
    )


# ===================== SAVE SINGLE DEADLINE =====================
@admin_bp.route("/save-single-deadline", methods=["POST"])
@login_required
@role_required("admin")
def save_single_deadline():

    batch_id = request.form.get("batch_id")
    stage_id = request.form.get("stage_id")
    deadline_value = request.form.get("deadline")

    if not batch_id or not stage_id:
        return redirect(url_for("admin.manage_stages"))

    if deadline_value:
        deadline_date = datetime.strptime(deadline_value, "%Y-%m-%d")

        current_app.db.deadlines.update_one(
            {
                "batch_id": ObjectId(batch_id),
                "stage_id": ObjectId(stage_id)
            },
            {
                "$set": {"deadline": deadline_date}
            },
            upsert=True
        )

    return redirect(url_for("admin.manage_stages", batch=batch_id))


# ===================== DRAG & DROP REORDER =====================
@admin_bp.route("/update-stage-order", methods=["POST"])
@login_required
@role_required("admin")
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
@role_required("admin")
def delete_stage(stage_id):

    current_app.db.stages.delete_one({"_id": ObjectId(stage_id)})
    current_app.db.deadlines.delete_many({"stage_id": ObjectId(stage_id)})

    flash("Stage deleted successfully.")
    return redirect(url_for("admin.manage_stages"))

# ===================== FACULTY MANAGEMENT =====================
@admin_bp.route("/faculty", methods=["GET", "POST"])
@login_required
@role_required("admin")
def manage_faculty():

    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip()
        password = request.form["password"]

        if not name or not email or not password:
            flash("All fields are required.")
            return redirect(url_for("admin.manage_faculty"))

        existing = current_app.db.users.find_one({"email": email})
        if existing:
            flash("Faculty already exists.")
            return redirect(url_for("admin.manage_faculty"))

        hashed_password = bcrypt.generate_password_hash(password).decode("utf-8")

        current_app.db.users.insert_one({
            "name": name,
            "email": email,
            "password": hashed_password,
            "role": "faculty",
            "created_at": datetime.utcnow()
        })

        flash("Faculty created successfully.")
        return redirect(url_for("admin.manage_faculty"))

    faculty_list = list(current_app.db.users.find({"role": "faculty"}))

    return render_template("admin/faculty.html", faculty=faculty_list)

# ===================== DELETE FACULTY =====================
@admin_bp.route("/delete-faculty/<faculty_id>")
@login_required
@role_required("admin")
def delete_faculty(faculty_id):

    current_app.db.users.delete_one({"_id": ObjectId(faculty_id)})
    flash("Faculty deleted.")
    return redirect(url_for("admin.manage_faculty"))

# ===================== FACULTY DASHBOARD =====================
@admin_bp.route("/faculty/dashboard")
@login_required
@role_required("faculty")
def faculty_dashboard():

    mentor_id = ObjectId(current_user.id)

    batch = current_app.db.batches.find_one({
        "mentor_id": mentor_id
    })

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

    deadlines = list(current_app.db.deadlines.find({
        "batch_id": batch["_id"]
    })) if batch else []

    deadline_dict = {}

    for d in deadlines:
        deadline_dict[str(d["stage_id"])] = d["deadline"]


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
@role_required("admin")
def manage_students():

    students = list(current_app.db.students.find())
    # print("STUDENTS:", students)

    batches = list(current_app.db.batches.find())

    # Optional: map batch_id → batch name if you still need it elsewhere
    batch_map = {str(b["_id"]): b["name"] for b in batches}

    return render_template(
        "admin/students.html",
        students=students,
        batches=batches
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
@role_required("admin")
def add_student():

    name = request.form["name"]
    prn = request.form["prn"]
    email = request.form["email"].strip().replace(" ", "")
    year = request.form["year"]
    batch_id = request.form["batch_id"]

    existing = current_app.db.students.find_one({"prn": prn})

    if existing:
        flash("Student already exists")
        return redirect(url_for("admin.manage_students"))

    existing_email = current_app.db.students.find_one({"email": email})

    if existing_email:
        flash("Email already exists", "warning")
        return redirect(url_for("admin.manage_students"))

    # ✅ ALWAYS DEFINE BEFORE USING
    raw_password = prn
    password = bcrypt.generate_password_hash(raw_password).decode("utf-8")

    student_data = {
        "name": name,
        "prn": prn,
        "email": email,
        "year": year,
        "batch_id": ObjectId(batch_id),
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

    return redirect(url_for("admin.manage_students"))


# ===================== UPLOAD STUDENTS =====================
@admin_bp.route("/upload-students", methods=["POST"])
@login_required
@role_required("admin")
def upload_students():

    file = request.files["file"]

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


    df = pd.read_excel(file)

    print("COLUMNS:", df.columns)
    print(df.head())

    for _, row in df.iterrows():

        # ✅ ACCESS BY POSITION (SAFE)
        prn = str(row.iloc[0]).strip()
        name = str(row.iloc[1]).strip()
        email = str(row.iloc[2]).strip() if len(row) > 2 else ""
        year = str(row.iloc[3]).strip() if len(row) > 3 else ""

        print("PROCESSING:", prn, name)

        # ❌ Skip empty rows
        if not prn or prn.lower() == "nan":
            continue

        if not name or name.lower() == "nan":
            continue

        raw_password = prn
        password = bcrypt.generate_password_hash(raw_password).decode("utf-8")

        existing = current_app.db.students.find_one({"prn": prn})

        if existing:
            print("SKIPPED:", prn)
            continue

        current_app.db.students.insert_one({
            "prn": prn,
            "name": name,
            "email": email,
            "year": year,
            "batch_id": None,
            "role": "student",
            "password": password,
            "password_changed": False,
            "created_at": datetime.utcnow()
        })

        print("INSERTED:", prn)

    flash("Students uploaded successfully")

    return redirect(url_for("admin.manage_students"))

@admin_bp.route("/download-template")
@login_required
@role_required("admin")
def download_template():

    df = pd.DataFrame({
        "PRN": [],
        "Name": [],
        "Email": [],
        "Year": []
    })

    path = "student_template.xlsx"

    df.to_excel(path, index=False)

    return send_file(path, as_attachment=True)


@admin_bp.route("/assign-students/<batch_id>", methods=["POST"])
@login_required
@role_required("admin")
def assign_students(batch_id):

    student_ids = request.form.getlist("students")

    for sid in student_ids:

        current_app.db.students.update_one(
            {"_id": ObjectId(sid)},
            {"$set": {"batch_id": ObjectId(batch_id)}}
        )

    flash("Students assigned successfully")

    return redirect(url_for("admin.manage_batches"))

# ---------------- ASSIGN STUDENTS PAGE ----------------
@admin_bp.route("/assign-students/<batch_id>")
@login_required
@role_required("admin")
def assign_students_page(batch_id):

    batch = current_app.db.batches.find_one({"_id": ObjectId(batch_id)})

    students = list(current_app.db.students.find({
        "$or": [
            {"batch_id": None},
            {"batch_id": ObjectId(batch_id)}
        ]
    }))

    return render_template(
        "admin/assign_students.html",
        batch=batch,
        students=students
    )

# ---------------- SAVE ASSIGNED STUDENTS ----------------
@admin_bp.route("/save-students/<batch_id>", methods=["POST"])
@login_required
@role_required("admin")
def save_assigned_students(batch_id):

    student_ids = request.form.getlist("students")

    # remove students already in this batch
    current_app.db.students.update_many(
        {"batch_id": ObjectId(batch_id)},
        {"$set": {"batch_id": None}}
    )

    # assign selected students
    for sid in student_ids:
        current_app.db.students.update_one(
            {"_id": ObjectId(sid)},
            {"$set": {"batch_id": ObjectId(batch_id)}}
        )
 
    batch = current_app.db.batches.find_one({"_id": ObjectId(batch_id)})

    mentor = None
    
    if batch and batch.get("mentor_id"):
        mentor = current_app.db.users.find_one({"_id": batch["mentor_id"]})

        # print("MENTOR:", mentor)

    for sid in student_ids:

        student = current_app.db.students.find_one({"_id": ObjectId(sid)})

        print("STUDENT:", student)

        if student and student.get("email") and mentor:

            print("SENDING EMAIL TO:", student["email"])

            try:
                send_email(
                    student["email"],
                    "Mentor Assigned",
                    f"""
                    <h3>Mentor Assigned</h3>
                    <p>Hello {student['name']},</p>
                    <p>Your mentor is <b>{mentor['name']}</b></p>
                    <p>Email: {mentor['email']}</p>
                    """
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
                student_list_html += f"<li>{s['name']} ({s['prn']})</li>"

            try:
                send_email(
                    mentor["email"],
                    "Students Assigned to You",
                    f"""
                    <h3>Students Assigned</h3>

                    <p>Hello {mentor['name']},</p>

                    <p>You have been assigned the following students:</p>

                    <ul>
                        {student_list_html}
                    </ul>

                    <p><b>Batch:</b> {batch['name']}</p>
                    """
                )

                print("FACULTY EMAIL SENT:", mentor["email"])

            except Exception as e:
                print("EMAIL ERROR (FACULTY):", e)


    flash("Students assigned successfully")

    return redirect(url_for("admin.manage_batches"))

# ---------------- FACULTY STUDENTS ----------------
@admin_bp.route("/faculty/students")
@login_required
@role_required("faculty")
def faculty_students():

    faculty_id = current_user.id

    batch = current_app.db.batches.find_one({
        "mentor_id": ObjectId(faculty_id)
    })

    if not batch:
        return render_template("faculty/students.html", students=[])

    students = list(current_app.db.students.find({
        "batch_id": batch["_id"]
    }))

    return render_template(
        "faculty/students.html",
        students=students,
        batch=batch
    )

# ---------------- MENTOR SUBMISSIONS ----------------
@admin_bp.route("/mentor-submissions")
@login_required
@role_required("faculty")
def mentor_submissions():

    mentor_id = ObjectId(current_user.id)

    batch = current_app.db.batches.find_one({
        "mentor_id": mentor_id
    })

    if not batch:
        flash("No batch assigned")
        return redirect(url_for("admin.faculty_dashboard"))

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
        stage_map=stage_map
    )

@admin_bp.route("/approve-submission/<submission_id>", methods=["POST"])
@login_required
@role_required("faculty")
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
@role_required("faculty")
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
        as_attachment=False
    )

@admin_bp.route("/notifications/read", methods=["POST"])
@login_required
def mark_notifications():

    mark_notifications_read(current_user.id)

    return "", 204