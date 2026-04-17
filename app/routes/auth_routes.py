from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_user, logout_user, login_required, current_user
from bson.objectid import ObjectId
from app import bcrypt
from app.models.user_model import User

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/", methods=["GET", "POST"])
def login():
    selected_role = "admin"

    if request.method == "POST":
        selected_role = request.form.get("login_role", "admin")
        identifier_label = "PRN" if selected_role == "student" else "email"

        username = request.form["email"].strip()
        password = request.form["password"]

        if not username or not password:
            flash(f"Enter both your {identifier_label} and password.", "warning")
            return render_template("auth/login.html", selected_role=selected_role)

        user_data = None

        if selected_role == "student":
            user_data = current_app.db.students.find_one({"prn": username})
        else:
            user_data = current_app.db.users.find_one({"email": username, "role": selected_role})

        if not user_data:
            flash(f"No account found with that {identifier_label}.", "warning")
            return render_template("auth/login.html", selected_role=selected_role)

        if bcrypt.check_password_hash(user_data["password"], password):

            user = User(user_data)
            login_user(user)
            designation = str(user_data.get("designation") or "").strip().lower().replace(" ", "_")

            if not user_data.get("password_changed", True):
                flash("Please update your password from your profile settings before continuing.", "warning")

                if user.role == "admin":
                    return redirect(url_for("admin.admin_profile"))

                elif user.role == "faculty":
                    return redirect(url_for("admin.faculty_profile"))

                elif user.role == "student":
                    return redirect(url_for("student.student_profile"))

            if user.role == "admin":
                if designation == "director":
                    return redirect(url_for("admin.director_dashboard"))
                return redirect(url_for("admin.dashboard"))

            elif user.role == "faculty":
                if designation == "project_coordinator":
                    return redirect(url_for("admin.dashboard"))
                return redirect(url_for("admin.faculty_dashboard"))

            elif user.role == "student":
                return redirect(url_for("student.dashboard"))

        flash("Incorrect password. Please try again.", "danger")

    return render_template("auth/login.html", selected_role=selected_role)


@auth_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():

    if request.method == "POST":

        new_password = request.form["password"]

        hashed = bcrypt.generate_password_hash(new_password).decode("utf-8")

        if current_user.role == "student":

            current_app.db.students.update_one(
                {"_id": ObjectId(current_user.id)},
                {
                    "$set": {
                        "password": hashed,
                        "password_changed": True
                    }
                }
            )

        else:

            current_app.db.users.update_one(
                {"_id": ObjectId(current_user.id)},
                {
                    "$set": {
                        "password": hashed,
                        "password_changed": True
                    }
                }
            )

        if current_user.role == "admin":
            current_user_record = current_app.db.users.find_one({"_id": ObjectId(current_user.id)})
            designation = str(current_user_record.get("designation") or "").strip().lower().replace(" ", "_") if current_user_record else ""
            if designation == "director":
                return redirect(url_for("admin.director_dashboard"))
            return redirect(url_for("admin.dashboard"))

        elif current_user.role == "faculty":
            current_user_record = current_app.db.users.find_one({"_id": ObjectId(current_user.id)})
            designation = str(current_user_record.get("designation") or "").strip().lower().replace(" ", "_") if current_user_record else ""
            if designation == "project_coordinator":
                return redirect(url_for("admin.dashboard"))
            return redirect(url_for("admin.faculty_dashboard"))

        elif current_user.role == "student":
            return redirect(url_for("student.dashboard"))

    return render_template("auth/change_password.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
