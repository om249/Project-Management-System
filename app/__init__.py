from flask import Flask
from flask_login import LoginManager, current_user
from flask_bcrypt import Bcrypt
from flask_mail import Mail
from flask_pymongo import PyMongo
from datetime import timezone
from zoneinfo import ZoneInfo
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

            notifications, unread_count = get_notifications(current_user.id)

            return dict(
                notifications=notifications,
                unread_count=unread_count,
                format_notification_time=format_notification_time
            )

        return dict(
            notifications=[],
            unread_count=0,
            format_notification_time=format_notification_time
        )

    return app


@login_manager.user_loader
def load_user(user_id):
    return User.get_by_id(user_id)
