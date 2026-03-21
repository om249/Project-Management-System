from datetime import datetime
from bson import ObjectId
from flask import current_app


# ================= CREATE =================
def create_notification(user_id, message, type="info"):

    db = current_app.db

    # ✅ ensure ObjectId
    user_id = ObjectId(user_id)

    db.notifications.insert_one({
        "user_id": user_id,
        "message": message,
        "type": type,
        "read": False,
        "created_at": datetime.utcnow()
    })

    # ✅ keep only latest 5
    notifications = list(
        db.notifications.find({"user_id": user_id})
        .sort("created_at", -1)
    )

    if len(notifications) > 5:
        for old in notifications[5:]:
            db.notifications.delete_one({"_id": old["_id"]})


# ================= GET =================
def get_notifications(user_id):

    db = current_app.db

    user_id = ObjectId(user_id)

    notifications = list(
        db.notifications.find({"user_id": user_id})
        .sort("created_at", -1)
    )

    unread = db.notifications.count_documents({
        "user_id": user_id,
        "read": False
    })

    return notifications, unread


# ================= MARK READ =================
def mark_notifications_read(user_id):

    db = current_app.db

    user_id = ObjectId(user_id)

    db.notifications.update_many(
        {"user_id": user_id},
        {"$set": {"read": True}}
    )