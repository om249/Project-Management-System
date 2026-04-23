from flask import Flask, current_app, redirect, url_for, flash, session
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
        project_category_map = {
            "mini_project": "Mini Project",
            "field_project": "Field Project",
            "major_project": "Major Project",
            "desk_research": "Desk Research",
            "research_project": "Research Project"
        }

        def normalize_project_category(value, selected_program):
            if selected_program != "MBA":
                return "mini_project"
            normalized = str(value or "").strip().lower().replace(" ", "_")
            return normalized if normalized in project_category_map else "mini_project"

        def normalize_program(value):
            normalized = str(value or "").strip().upper()
            return normalized if normalized in {"MCA", "MBA"} else "MCA"

        def get_program_scope(record):
            if not record:
                return None
            assignments = list(
                current_app.db.role_assignments.find({"user_id": record["_id"]})
            )
            if assignments:
                if any(item.get("role") == "director" for item in assignments):
                    return None
                if any(item.get("role") == "academic_coordinator" for item in assignments):
                    return None
                scoped_role = next(
                    (
                        item for item in assignments
                        if item.get("program") and item.get("role") in {"project_coordinator", "hod"}
                    ),
                    None
                )
                if scoped_role:
                    return normalize_program(scoped_role.get("program"))
            program = str(record.get("program") or "").strip().upper()
            return normalize_program(program) if program in {"MCA", "MBA"} else None

        def normalize_designation(record, selected_program):
            if not record:
                return "faculty"

            assignments = list(
                current_app.db.role_assignments.find({"user_id": record["_id"]})
            )
            if assignments:
                for item in assignments:
                    if item.get("role") == "director":
                        return "director"
                for item in assignments:
                    if item.get("role") == "academic_coordinator":
                        return "academic_coordinator"
                for item in assignments:
                    if item.get("program") == selected_program and item.get("role") in {"project_coordinator", "hod"}:
                        return item.get("role")
                return "faculty"

            designation = str(record.get("designation") or "").strip().lower().replace(" ", "_")
            if designation in {"director", "project_coordinator", "academic_coordinator", "hod", "faculty"}:
                return designation
            if record.get("role") == "admin":
                return "director"
            if record.get("is_project_coordinator"):
                return "project_coordinator"
            return "faculty"

        def format_notification_time(dt):
            if not dt:
                return "Just now"

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            return dt.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p")

        if current_user.is_authenticated:
            current_user_record = None
            current_designation = None
            can_manage_designations = False
            can_manage_operations = False
            can_view_reports = False
            can_view_student_directory = False
            can_access_mentor_tools = False

            selected_program = str(session.get("selected_program", "MCA")).strip().upper()
            if selected_program not in {"MCA", "MBA"}:
                selected_program = "MCA"
            session["selected_program"] = selected_program
            can_switch_program = True
            selected_project_category = normalize_project_category(
                session.get("selected_project_category"),
                selected_program
            )
            session["selected_project_category"] = selected_project_category
            can_switch_project_category = selected_program == "MBA"

            if current_user.role in ["admin", "faculty"]:
                current_user_record = current_app.db.users.find_one({"_id": ObjectId(current_user.id)})
                locked_program = get_program_scope(current_user_record)
                if locked_program:
                    selected_program = locked_program
                    session["selected_program"] = selected_program
                    can_switch_program = False
                selected_project_category = normalize_project_category(
                    session.get("selected_project_category"),
                    selected_program
                )
                session["selected_project_category"] = selected_project_category
                can_switch_project_category = selected_program == "MBA"
                current_designation = normalize_designation(current_user_record, selected_program)
                can_manage_designations = current_designation == "director"
                can_manage_operations = current_designation in {"project_coordinator"}
                can_view_reports = current_designation in {"director", "project_coordinator", "academic_coordinator", "hod"}
                can_view_student_directory = current_designation in {"director", "project_coordinator", "academic_coordinator", "hod"}
                can_access_mentor_tools = current_designation in {"project_coordinator", "academic_coordinator", "hod", "faculty"}
            elif current_user.role == "student":
                current_user_record = current_app.db.students.find_one({"_id": ObjectId(current_user.id)})

            notifications, unread_count = get_notifications(current_user.id)

            return dict(
                notifications=notifications,
                unread_count=unread_count,
                format_notification_time=format_notification_time,
                current_user_record=current_user_record,
                current_designation=current_designation,
                can_manage_designations=can_manage_designations,
                can_manage_operations=can_manage_operations,
                can_view_reports=can_view_reports,
                can_view_student_directory=can_view_student_directory,
                can_access_mentor_tools=can_access_mentor_tools,
                selected_program=selected_program,
                selected_project_category=selected_project_category,
                selected_project_category_label=project_category_map.get(selected_project_category, "Mini Project"),
                can_switch_program=can_switch_program,
                can_switch_project_category=can_switch_project_category,
                program_options=["MCA", "MBA"],
                project_category_options=list(project_category_map.items())
            )

        return dict(
            notifications=[],
            unread_count=0,
            format_notification_time=format_notification_time,
            current_user_record=None,
            current_designation=None,
            can_manage_designations=False,
            can_manage_operations=False,
            can_view_reports=False,
            can_view_student_directory=False,
            can_access_mentor_tools=False,
            selected_program="MCA",
            selected_project_category="mini_project",
            selected_project_category_label="Mini Project",
            can_switch_program=False,
            can_switch_project_category=False,
            program_options=["MCA", "MBA"],
            project_category_options=list(project_category_map.items())
        )

    @app.errorhandler(403)
    def handle_forbidden(_error):
        if current_user.is_authenticated:
            flash("Your access was updated. You have been moved to the correct dashboard.", "info")

            if current_user.role in {"admin", "faculty"}:
                user_record = current_app.db.users.find_one({"_id": ObjectId(current_user.id)})
                selected_program = str(session.get("selected_program", "MCA")).strip().upper()
                if selected_program not in {"MCA", "MBA"}:
                    selected_program = "MCA"
                designation = "faculty"
                if user_record:
                    assignments = list(current_app.db.role_assignments.find({"user_id": user_record["_id"]}))
                    if assignments:
                        if any(item.get("role") == "director" for item in assignments):
                            designation = "director"
                        elif any(item.get("role") == "academic_coordinator" for item in assignments):
                            designation = "academic_coordinator"
                        else:
                            scoped = next(
                                (
                                    item for item in assignments
                                    if item.get("program") == selected_program and item.get("role") in {"project_coordinator", "hod"}
                                ),
                                None
                            )
                            designation = scoped.get("role") if scoped else "faculty"
                    else:
                        designation = str(user_record.get("designation") or "").strip().lower().replace(" ", "_")
                        if designation not in {"director", "project_coordinator", "academic_coordinator", "hod", "faculty"}:
                            designation = "director" if user_record.get("role") == "admin" else "faculty"

                if designation == "director":
                    return redirect(url_for("admin.director_dashboard"))

                if designation == "project_coordinator":
                    return redirect(url_for("admin.dashboard"))

                return redirect(url_for("admin.faculty_dashboard"))

            if current_user.role == "student":
                return redirect(url_for("student.dashboard"))

        return redirect(url_for("auth.login"))

    return app


@login_manager.user_loader
def load_user(user_id):
    return User.get_by_id(user_id)
