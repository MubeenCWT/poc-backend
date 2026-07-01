"""
Run once to create your first admin login:
    python seed_admin.py
"""
from app.database import SessionLocal, Base, engine
from app.models.models import User
from app.services.auth import hash_password

Base.metadata.create_all(bind=engine)

db = SessionLocal()

email = input("Admin email: ").strip()
password = input("Admin password: ").strip()
name = input("Admin full name: ").strip()

existing = db.query(User).filter(User.email == email).first()
if existing:
    print("A user with that email already exists.")
else:
    admin = User(
        role="admin",
        full_name=name,
        email=email,
        password_hash=hash_password(password),
    )
    db.add(admin)
    db.commit()
    print(f"Admin user created: {email}")

db.close()
