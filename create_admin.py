from app import create_app, bcrypt

app = create_app()

with app.app_context():
    existing = app.db.users.find_one({"email": "admin@zealeducation.com"})

    if existing:
        print("Admin already exists.")
    else:
        hashed = bcrypt.generate_password_hash("ADMINPRN*123").decode("utf-8")

        app.db.users.insert_one({
            "name": "Project Coordinator",
            "email": "admin@zealeducation.com",
            "role": "admin",
            "password": hashed,
            "password_changed": False
        })

        print("Admin created successfully.")