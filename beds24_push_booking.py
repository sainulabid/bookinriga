"""
RigaNest x Beds24 — push a confirmed website booking to Beds24.

When a guest completes payment on the website, call push_booking()
so Beds24 immediately blocks those dates for that room. This closes
availability on Airbnb/Booking.com/etc too, preventing double-bookings.

Requires the same environment variable beds24_sync.py already uses:
    BEDS24_REFRESH_TOKEN

Uses the token's write:bookings scope — make sure the refresh token
was generated with that scope enabled (Beds24 > Settings > API >
generate invite code > tick "bookings" write access).
"""

import os
import requests

BEDS24_REFRESH_TOKEN = os.environ.get("BEDS24_REFRESH_TOKEN", "")
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


def push_booking(booking):
    """
    booking: a Booking model instance (with .room and .user relationships
    already loaded, as they are in app.py's booking_success route).

    Returns (success: bool, detail: str)
    """
    room = booking.room
    guest = booking.user

    if not room.beds24_room_id:
        return False, f"Room '{room.name}' has no beds24_room_id set — cannot push."

    if not BEDS24_REFRESH_TOKEN:
        return False, "BEDS24_REFRESH_TOKEN is not set in environment."

    name_parts = (guest.name or "Guest").strip().split(" ", 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else "-"

    try:
        access_token = get_access_token()
    except Exception as e:
        return False, f"Token error: {e}"

    payload = [{
        "roomId": room.beds24_room_id,
        "status": "confirmed",
        "arrival": booking.check_in.isoformat(),
        "departure": booking.check_out.isoformat(),
        "numAdult": booking.guests,
        "firstName": first_name,
        "lastName": last_name,
        "email": guest.email,
        "price": booking.total_price,
        "notes": f"Booked via website (internal booking id {booking.id})",
    }]

    resp = requests.post(
        f"{API_BASE}/bookings",
        headers={"accept": "application/json", "token": access_token,
                 "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )

    if resp.status_code not in (200, 201):
        return False, f"Beds24 rejected the booking (status {resp.status_code}): {resp.text}"

    return True, resp.text
