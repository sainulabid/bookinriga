"""
RigaNest x Beds24 — fuzzy-match remaining unmatched rooms.

For local rooms that had no EXACT name match in discover_homestate_mapping.py,
this finds the closest-sounding Homestate room names, so Abid/the client
can confirm which one is really the same apartment (spelling differences,
extra words, etc). READ-ONLY — suggests only, changes nothing.

Usage:
    python fuzzy_match_remaining.py
"""

import difflib
import io
import os
import sys

import requests
from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
load_dotenv()

from app import app, Room  # noqa: E402

BEDS24_REFRESH_TOKEN = os.environ.get("BEDS24_REFRESH_TOKEN", "")
API_BASE = "https://beds24.com/api/v2"

# Local room ids that had NO exact match (from the last discover_homestate_mapping.py run)
UNMATCHED_LOCAL_IDS = {8, 9, 10, 15, 17, 18, 19, 20, 22, 23, 24, 32, 37, 41, 45}


def get_access_token():
    resp = requests.get(
        f"{API_BASE}/authentication/token",
        headers={"accept": "application/json", "refreshToken": BEDS24_REFRESH_TOKEN},
        timeout=15,
    )
    data = resp.json()
    if resp.status_code != 200 or "token" not in data:
        raise RuntimeError(f"Could not get access token: {data}")
    return data["token"]


def fetch_all_properties(access_token):
    all_props, page = [], 1
    while True:
        resp = requests.get(
            f"{API_BASE}/properties",
            headers={"accept": "application/json", "token": access_token},
            params={"includeAllRooms": "true", "page": page},
            timeout=30,
        )
        data = resp.json()
        props = data.get("data", [])
        if not props:
            break
        all_props.extend(props)
        pages_info = data.get("pages", {})
        total_pages = pages_info.get("pages") if isinstance(pages_info, dict) else None
        if not total_pages or page >= total_pages:
            break
        page += 1
    return all_props


def main():
    access_token = get_access_token()
    print("Fetching all properties...")
    all_props = fetch_all_properties(access_token)

    # exclude our own BookinRiga junk property from suggestions
    homestate_rooms = []  # (room_name, property_id, property_name, room_id)
    for p in all_props:
        if p.get("id") == 341384:
            continue
        for rt in p.get("roomTypes", []) or []:
            name = (rt.get("name") or "").strip()
            if name:
                homestate_rooms.append((name, p.get("id"), p.get("name"), rt.get("id")))

    homestate_names = [r[0] for r in homestate_rooms]

    with app.app_context():
        rooms = Room.query.filter(Room.id.in_(UNMATCHED_LOCAL_IDS)).order_by(Room.id).all()
        for r in rooms:
            print(f"\nLocal #{r.id} '{r.name}':")
            close = difflib.get_close_matches(r.name, homestate_names, n=3, cutoff=0.5)
            if not close:
                print("  No similar names found at all.")
                continue
            for name in close:
                for hr_name, prop_id, prop_name, room_id in homestate_rooms:
                    if hr_name == name:
                        print(f"  ~ '{name}'  ->  property {prop_id} ({prop_name}), room {room_id}")


if __name__ == "__main__":
    main()
