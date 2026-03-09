from flask_login import UserMixin
from bson.objectid import ObjectId
from flask import current_app

class User(UserMixin):

    def __init__(self, data):
        self.id = str(data["_id"])
        self.role = data.get("role")   # SAFE (no KeyError)
        self.password_changed = data.get("password_changed", True)
        self.data = data

    @staticmethod
    def get_by_id(user_id):

        user = current_app.db.users.find_one({"_id": ObjectId(user_id)})

        if not user:
            user = current_app.db.students.find_one({"_id": ObjectId(user_id)})

        return User(user) if user else None