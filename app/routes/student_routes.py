from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_required, login_user, logout_user, current_user
from bson.objectid import ObjectId
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from app.decorators.role_required import role_required
import os
from werkzeug.utils import secure_filename

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
@role_required("student")
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

    deadline_dict = {}
    for d in deadlines:
        deadline_dict[str(d["stage_id"])] = d["deadline"]

    # submissions = list(current_app.db.submissions.find({
    #     "student_id": ObjectId(current_user.id)
    # }))
    
    submissions = list(current_app.db.submissions.find({
        "student_id": student["_id"]
    }))

    submission_dict = {}
    for s in submissions:
        submission_dict[str(s["stage_id"])] = s

    return render_template(
        "student/dashboard.html",
        stages=stages,
        deadline_dict=deadline_dict,
        submission_dict=submission_dict,
        batch=batch
    )

@student_bp.route("/upload/<stage_id>", methods=["POST"])
@login_required
@role_required("student")
def upload_stage(stage_id):

    file = request.files["file"]

    filename = secure_filename(file.filename)

    filepath = os.path.join(
        current_app.config["UPLOAD_FOLDER"],
        filename
    )

    file.save(filepath)

    current_app.db.submissions.update_one(
        {
            "student_id": ObjectId(current_user.id),
            "stage_id": ObjectId(stage_id)
        },
        {
            "$set": {
                "file_name": filename,
                "uploaded_at": datetime.utcnow(),
                "status": "pending"
            }
        },
        upsert=True
    )

    flash("File uploaded successfully")

    return redirect(url_for("student.dashboard"))