from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_required, login_user, logout_user, current_user
from bson.objectid import ObjectId
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

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
@student_bp.route("/dashboard")
@login_required
def dashboard():

    student = current_app.db.students.find_one({
        "_id": ObjectId(current_user.id)
    })

    batch = current_app.db.batches.find_one({
        "_id": student["batch_id"]
    })

    stages = list(current_app.db.stages.find().sort("order", 1))

    deadlines = list(current_app.db.deadlines.find({
        "batch_id": batch["_id"]
    }))

    deadline_dict = {
        str(d["stage_id"]): d["deadline"]
        for d in deadlines
    }

    return render_template(
        "student/dashboard.html",
        student=student,
        batch=batch,
        stages=stages,
        deadline_dict=deadline_dict
    )

@student_bp.route("/upload/<stage_id>", methods=["POST"])
@login_required
def upload_stage(stage_id):

    file = request.files["file"]

    filename = secure_filename(file.filename)

    filepath = os.path.join(current_app.config["UPLOAD_FOLDER"], filename)

    file.save(filepath)

    current_app.db.submissions.insert_one({

        "student_id": ObjectId(current_user.id),
        "stage_id": ObjectId(stage_id),
        "file_name": filename,
        "uploaded_at": datetime.utcnow(),
        "status": "pending",
        "remarks": ""

    })

    flash("File uploaded successfully")

    return redirect(url_for("student.dashboard"))