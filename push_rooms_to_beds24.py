"""
RigaNest x Beds24 — push local rooms INTO Beds24.

Use this if you have NOT created the property/rooms in Beds24 yet, and
want to create them directly from the room data already in your local
RigaNest database, instead of typing everything again by hand in the
Beds24 control panel.

Requires an environment variable:
    BEDS24_REFRESH_TOKEN   -> from get_refresh_token.py

First run (no BEDS24_PROPERTY_ID set):
    - Creates a NEW property in Beds24 called BEDS24_PROPERTY_NAME
      (default "RigaNest"), with every local Room added under it as a
      Beds24 room type.
    - Prints the new property id -> save it as BEDS24_PROPERTY_ID in
      your .env so re-runs update the same property instead of making
      duplicates.
    - Saves each returned Beds24 room id back into Room.beds24_room_id,
      so set_beds24_mapping.py is no longer needed.

Later runs (BEDS24_PROPERTY_ID set):
    - Adds any local rooms that don't have a beds24_room_id yet to the
      existing property. Rooms already mapped are left untouched (this
      script does not overwrite Beds24 data on rooms already pushed).

Usage:
    python push_rooms_to_beds24.py
"""

import os
import sys

import requests

from app import app, db, Room

BEDS24_REFRESH_TOKEN = os.environ.get("BEDS24_REFRESH_TOKEN", "")
BEDS24_PROPERTY_ID = os.environ.get("BEDS24_PROPERTY_ID", "")
BEDS24_PROPERTY_NAME = os.environ.get("BEDS24_PROPERTY_NAME", "RigaNest")
BEDS24_CURRENCY = os.environ.get("BEDS24_CURRENCY", "QAR")

API_BASE = "https://beds24.com/api/v2"


def get_access_token():
    resp = requests.get(
        f"{API_BASE}/authentication/token",
        headers={"accept": "application/json", "refreshToken": BEDS24_REFRESH_TOKEN},
        timeout=20,
    )
    data = resp.json()
    if resp.status_code != 200 or "token" not in data:
        raise RuntimeError(f"Could not get access token: {data}")
    return data["token"]


def room_payload(room):
    """One Room row -> the shape Beds24 expects under properties[].roomTypes[]."""
    return {
        "name": room.name,
        "qty": 1,
        "maxPeople": room.capacity or 2,
        "roomUnits": [{"name": f"{room.name} 1"}],
    }


def create_property_with_rooms(access_token, rooms):
    payload = [
        {
            "name": BEDS24_PROPERTY_NAME,
            "propertyType": "hotel",
            "currency": BEDS24_CURRENCY,
            "roomTypes": [room_payload(r) for r in rooms],
        }
    ]
    resp = requests.post(
        f"{API_BASE}/properties",
        headers={"accept": "application/json", "token": access_token},
        json=payload,
        timeout=30,
    )
    data = resp.json()
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Could not create property: {data}")
    return data


def add_rooms_to_property(access_token, property_id, rooms):
    payload = [
        {
            "id": int(property_id),
            "roomTypes": [room_payload(r) for r in rooms],
        }
    ]
    resp = requests.post(
        f"{API_BASE}/properties",
        headers={"accept": "application/json", "token": access_token},
        json=payload,
        timeout=30,
    )
    data = resp.json()
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Could not add rooms: {data}")
    return data


def main():
    if not BEDS24_REFRESH_TOKEN:
        print("ERROR: BEDS24_REFRESH_TOKEN environment variable is not set.")
        print("Run get_refresh_token.py first.")
        sys.exit(1)

    with app.app_context():
        rooms = Room.query.filter(Room.beds24_room_id.is_(None)).order_by(Room.id).all()
        if not rooms:
            print("Every local room already has a beds24_room_id. Nothing to push.")
            return

        access_token = get_access_token()

        print(f"Pushing {len(rooms)} room(s) to Beds24...")
        if BEDS24_PROPERTY_ID:
            result = add_rooms_to_property(access_token, BEDS24_PROPERTY_ID, rooms)
        else:
            result = create_property_with_rooms(access_token, rooms)
            print("\nNOTE: this created a NEW property.")

        # Beds24 echoes back the created/updated property incl. new room ids.
        try:
            prop = result["data"][0] if isinstance(result, dict) else result[0]
        except (KeyError, IndexError, TypeError):
            print("Could not read response shape, here it is raw:")
            print(result)
            return

        property_id = prop.get("id")
        room_types = prop.get("roomTypes", [])

        if not BEDS24_PROPERTY_ID and property_id:
            print(f"\nSave this in your .env:\n  BEDS24_PROPERTY_ID={property_id}\n")

        # Match returned rooms back to local rooms by name (order should
        # line up with what we sent, but name-matching is safer).
        by_name = {r.name: r for r in rooms}
        mapped = 0
        for rt in room_types:
            local_room = by_name.get(rt.get("name"))
            if local_room and rt.get("id"):
                local_room.beds24_room_id = rt["id"]
                mapped += 1
                print(f"  Room #{local_room.id} ({local_room.name}) -> Beds24 room {rt['id']}")

        db.session.commit()
        print(f"\nDone. {mapped} local room(s) now mapped to Beds24 room ids.")
        print("Next: set BEDS24_PROPERTY_ID (if new) and run beds24_sync.py")


if __name__ == "__main__":
    main()
