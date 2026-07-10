"""
RigaNest x Beds24 — one-time room mapping tool.

Run this ONCE (after deploying the app.py changes) to tell the system
which Beds24 "roomId" corresponds to which RigaNest room. Run it from
the same environment where DATABASE_URL points at your real database
(e.g. via Render's Shell tab, or locally with the same .env).

Usage:
    python set_beds24_mapping.py
"""

from app import app, db, Room

with app.app_context():
    rooms = Room.query.order_by(Room.id).all()
    if not rooms:
        print("No rooms found in the database.")
        raise SystemExit

    print("=" * 60)
    print("RigaNest rooms — enter the matching Beds24 room ID for each.")
    print("(Find the Beds24 room ID in your Beds24 control panel under")
    print(" Settings -> Properties -> Rooms -> Setup, or via the API")
    print(" GET /properties endpoint.)")
    print("Press Enter to skip a room (leave it unmapped / not synced).")
    print("=" * 60)

    for r in rooms:
        current = f" (currently: {r.beds24_room_id})" if r.beds24_room_id else ""
        val = input(f"\nRoom #{r.id} — {r.name}{current}\n  Beds24 room ID: ").strip()
        if val:
            try:
                r.beds24_room_id = int(val)
                print(f"  -> mapped to Beds24 room {r.beds24_room_id}")
            except ValueError:
                print("  -> invalid number, skipped")

    db.session.commit()
    print("\nDone. Mapping saved.")
