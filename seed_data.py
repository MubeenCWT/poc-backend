from app.database import SessionLocal, Base, engine
from app.models.models import Property, Vendor, User
from app.services.auth import hash_password

Base.metadata.create_all(bind=engine)

def run_seed():
    db = SessionLocal()
    try:
        # 1. Ensure Admin exists
        admin = db.query(User).filter(User.email == "admin@dar.ae").first()
        if not admin:
            admin = User(
                role="admin",
                full_name="UAE Stays Admin",
                email="admin@dar.ae",
                password_hash=hash_password("admin123"),
            )
            db.add(admin)
            db.commit()
            print("Admin created.")
        
        # 2. Seed Vendors
        vendors_data = [
            {"name": "Quick Fix Plumbing", "service_type": "plumbing", "phone": "+971501111111", "email": "plumb@dar.ae", "coverage_areas": ["Marina", "JBR", "Palm"]},
            {"name": "Spark Electric", "service_type": "electrical", "phone": "+971502222222", "email": "spark@dar.ae", "coverage_areas": ["Downtown", "Business Bay"]},
            {"name": "Cool Breeze AC", "service_type": "AC", "phone": "+971503333333", "email": "ac@dar.ae", "coverage_areas": ["Marina", "Downtown", "Palm", "JBR", "Business Bay"]},
            {"name": "Spotless Cleaning", "service_type": "cleaning", "phone": "+971504444444", "email": "clean@dar.ae", "coverage_areas": ["Marina", "Downtown", "Palm", "JBR", "Business Bay"]},
            {"name": "All-in-One Handyman", "service_type": "other", "phone": "+971505555555", "email": "handy@dar.ae", "coverage_areas": ["Marina", "Downtown", "Palm", "JBR", "Business Bay"]}
        ]
        
        if db.query(Vendor).count() == 0:
            for v in vendors_data:
                db.add(Vendor(**v))
            db.commit()
            print("Vendors seeded.")

        # 3. Seed Properties
        properties_data = [
            {
                "title": "Luxury Marina Apartment", "description": "Beautiful view of Dubai Marina", 
                "property_type": "apartment", "emirate": "Dubai", "area": "Marina", 
                "bedrooms": 2, "bathrooms": 2, "max_guests": 4, 
                "price_daily": 800, "price_monthly": 15000, "price_yearly": 150000,
                "images": ["https://images.unsplash.com/photo-1512453979798-5ea266f8880c?q=80&w=800"],
                "amenities": ["Pool", "Gym", "Balcony"]
            },
            {
                "title": "Downtown Studio", "description": "Cozy studio near Burj Khalifa", 
                "property_type": "studio", "emirate": "Dubai", "area": "Downtown", 
                "bedrooms": 1, "bathrooms": 1, "max_guests": 2, 
                "price_daily": 500, "price_monthly": 10000, "price_yearly": 100000,
                "images": ["https://images.unsplash.com/photo-1582582494705-f8ce0b0c24f0?q=80&w=800"],
                "amenities": ["Gym", "Burj View"]
            },
            {
                "title": "JBR Penthouse", "description": "Stunning penthouse with sea views", 
                "property_type": "penthouse", "emirate": "Dubai", "area": "JBR", 
                "bedrooms": 4, "bathrooms": 5, "max_guests": 8, 
                "price_daily": 2500, "price_monthly": 45000, "price_yearly": 450000,
                "images": ["https://images.unsplash.com/photo-1522708323590-d24dbb6b0267?q=80&w=800"],
                "amenities": ["Private Pool", "Beach Access", "Gym"]
            },
            {
                "title": "Palm Villa", "description": "Exclusive villa on the Palm Jumeirah fronds", 
                "property_type": "villa", "emirate": "Dubai", "area": "Palm", 
                "bedrooms": 5, "bathrooms": 6, "max_guests": 10, 
                "price_daily": 3500, "price_monthly": 80000, "price_yearly": 850000,
                "images": ["https://images.unsplash.com/photo-1600596542815-ffad4c1539a9?q=80&w=800"],
                "amenities": ["Private Beach", "Pool", "Garden"]
            },
            {
                "title": "Business Bay 2BR", "description": "Modern living in Business Bay", 
                "property_type": "apartment", "emirate": "Dubai", "area": "Business Bay", 
                "bedrooms": 2, "bathrooms": 2, "max_guests": 4, 
                "price_daily": 650, "price_monthly": 12000, "price_yearly": 125000,
                "images": ["https://images.unsplash.com/photo-1522771739844-6a9f6d5f14af?q=80&w=800"],
                "amenities": ["Canal View", "Gym", "Pool"]
            },
            {
                "title": "Jumeirah Beach Residence", "description": "Walk to the beach from this lovely 3BR", 
                "property_type": "apartment", "emirate": "Dubai", "area": "JBR", 
                "bedrooms": 3, "bathrooms": 3, "max_guests": 6, 
                "price_daily": 1200, "price_monthly": 22000, "price_yearly": 220000,
                "images": ["https://images.unsplash.com/photo-1560448204-e02f11c3d0e2?q=80&w=800"],
                "amenities": ["Beach Access", "Pool", "Gym", "Maid Room"]
            }
        ]
        
        if db.query(Property).count() == 0:
            for p in properties_data:
                p["owner_id"] = admin.id
                db.add(Property(**p))
            db.commit()
            print("Properties seeded.")
            
    except Exception as e:
        print(f"Error seeding data: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    run_seed()
