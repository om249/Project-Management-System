from flask import Flask
from flask_login import LoginManager
from flask_bcrypt import Bcrypt
from flask_mail import Mail
from flask_pymongo import PyMongo
from config import Config
from app.models.user_model import User

mongo = PyMongo()
login_manager = LoginManager()
bcrypt = Bcrypt()
mail = Mail()


def create_app():

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

    return app


@login_manager.user_loader
def load_user(user_id):
    return User.get_by_id(user_id)