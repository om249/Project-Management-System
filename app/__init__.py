from flask import Flask, current_app, redirect, url_for, flash
from flask_login import LoginManager, current_user
from flask_bcrypt import Bcrypt
from flask_mail import Mail
from flask_pymongo import PyMongo
from datetime import timezone
from zoneinfo import ZoneInfo
from bson.objectid import ObjectId
from config import Config
from app.models.user_model import User
from app.services.notification_service import get_notifications, create_notification, mark_notifications_read

mongo = PyMongo()
login_manager = LoginManager()
bcrypt = Bcrypt()
mail = Mail()


def create_app():

    # from dotenv import load_dotenv
    # load_dotenv()

    app = Flask(__name__)
    app.config.from_object(Config)

    # Initialize extensions
    mongo.init_app(app)
    login_manager.init_app(app)
    bcrypt.init_app(app)
    mail.init_app(app)

    # database shortcut
    app.db = mongo.db

    # -------- IMPORT BLUEPRINTS --------
    from app.routes.auth_routes import auth_bp
    from app.routes.admin_routes import admin_bp
    from app.routes.student_routes import student_bp

    # -------- REGISTER BLUEPRINTS --------
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(student_bp)

    @app.context_processor
    def inject_notifications():
        def format_notification_time(dt):
            if not dt:
                return "Just now"

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            return dt.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p")

        if current_user.is_authenticated:
            current_user_record = None
            if current_user.role in ["admin", "faculty"]:
                current_user_record = current_app.db.users.find_one({"_id": ObjectId(current_user.id)})
            elif current_user.role == "student":
                current_user_record = current_app.db.students.find_one({"_id": ObjectId(current_user.id)})

            notifications, unread_count = get_notifications(current_user.id)

            return dict(
                notifications=notifications,
                unread_count=unread_count,
                format_notification_time=format_notification_time,
                current_user_record=current_user_record
            )

        return dict(
            notifications=[],
            unread_count=0,
            format_notification_time=format_notification_time,
            current_user_record=None
        )

    @app.errorhandler(403)
    def handle_forbidden(_error):
        if current_user.is_authenticated:
            flash("Your access was updated. You have been moved to the correct dashboard.", "info")

            if current_user.role == "admin":
                return redirect(url_for("admin.dashboard"))

            if current_user.role == "faculty":
                return redirect(url_for("admin.faculty_dashboard"))

            if current_user.role == "student":
                return redirect(url_for("student.dashboard"))

        return redirect(url_for("auth.login"))

    return app


@login_manager.user_loader
def load_user(user_id):
    return User.get_by_id(user_id)
