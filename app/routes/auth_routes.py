from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_user, logout_user, login_required, current_user
from app import bcrypt
from app.models.user_model import User

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        username = request.form["email"]   # email OR prn
        password = request.form["password"]

        # Check admin/faculty (email login)
        user_data = current_app.db.users.find_one({"email": username})

        # If not found → check students (PRN login)
        if not user_data:
            user_data = current_app.db.students.find_one({"prn": username})

        if user_data and bcrypt.check_password_hash(user_data["password"], password):

            user = User(user_data)
            login_user(user)

            if not user_data.get("password_changed", True):
                return redirect(url_for("auth.change_password"))

            if user.role == "admin":
                return redirect(url_for("admin.dashboard"))

            elif user.role == "faculty":
                return redirect(url_for("admin.faculty_dashboard"))

            elif user.role == "student":
                return redirect(url_for("student.dashboard"))

        flash("Invalid credentials")

    return render_template("auth/login.html")

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
            return redirect(url_for("admin.dashboard"))

        elif current_user.role == "faculty":
            return redirect(url_for("admin.faculty_dashboard"))

        elif current_user.role == "student":
            return redirect(url_for("student.dashboard"))

    return render_template("auth/change_password.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))