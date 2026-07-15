"""
RigaNest x Beds24 — show the CURRENT production mapping.

Run this in the RENDER SHELL (not locally!) so it reads the real,
live database that the deployed website actually uses. This tells us
exactly which Beds24 room ids are "in use" right now, so any other
room id with the same name in Beds24 can be identified as leftover
duplicate junk and safely deleted.

Usage (in Render Shell):
    python list_production_mappings.py
"""

from app import app, Room


def main():
    with app.app_context():
        rooms = Room.query.order_by(Room.id).all()
        print(f"{'Local ID':<10}{'Beds24 Room ID':<18}Name")
        print("-" * 70)
        for r in rooms:
            print(f"{r.id:<10}{str(r.beds24_room_id or '(none)'):<18}{r.name}")
        print(f"\nTotal local rooms: {len(rooms)}")
        mapped = sum(1 for r in rooms if r.beds24_room_id)
        print(f"Rooms with a beds24_room_id set: {mapped}")


if __name__ == "__main__":
    main()
