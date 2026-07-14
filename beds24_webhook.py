"""
RigaNest x Beds24 — real-time booking webhook.

Configure in Beds24: SETTINGS > PROPERTIES > Access > Booking Webhooks
Webhook URL to enter there:
    https://bookinriga.onrender.com/webhook/beds24-booking

Whenever a booking is created, modified or cancelled in Beds24 — including
bookings that arrive from Airbnb, Booking.com, etc — Beds24 POSTs the
booking data to this URL. We use that only as a trigger: we then pull a
fresh calendar (price + availability) for that one room from the API and
overwrite our RoomAvailability rows for it. This keeps the website's
availability accurate within seconds instead of waiting for the next
scheduled beds24_sync.py run.

Requires the same BEDS24_REFRESH_TOKEN environment variable used by
beds24_sync.py (needs at least read access to inventory).

Optional but recommended: set BEDS24_WEBHOOK_SECRET to a random string,
and add a custom header in the Beds24 webhook settings with that same
value (header name: X-Webhook-Secret). This stops random requests to the
URL from triggering a resync.
"""

import os
from datetime import date, timedelta

import requests
from flask import request, jsonify

BEDS24_REFRESH_TOKEN = os.environ.get("BEDS24_REFRESH_TOKEN", "")
BEDS24_WEBHOOK_SECRET = os.environ.get("BEDS24_WEBHOOK_SECRET", "")
SYNC_DAYS_AHEAD = int(os.environ.get("BEDS24_SYNC_DAYS", "365"))
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


def fetch_calendar(access_token, room_id, start_date, end_date):
    resp = requests.get(
        f"{API_BASE}/inventory/rooms/calendar",
        headers={"accept": "application/json", "token": access_token},
        params={"roomId": room_id, "startDate": start_date.isoformat(), "endDate": end_date.isoformat()},
        timeout=30,
    )
    data = resp.json()
    if resp.status_code != 200 or not data.get("data"):
        return []
    return data["data"][0].get("calendar", [])


def fetch_availability(access_token, room_id, start_date, end_date):
    resp = requests.get(
        f"{API_BASE}/inventory/rooms/availability",
        headers={"accept": "application/json", "token": access_token},
        params={"roomId": room_id, "startDate": start_date.isoformat(), "endDate": end_date.isoformat()},
        timeout=30,
    )
    data = resp.json()
    if resp.status_code != 200 or not data.get("data"):
        return {}
    return data["data"][0].get("availability", {})


def expand_calendar_to_daily_price(calendar_ranges, start_date, end_date):
    daily = {}
    for rng in calendar_ranges:
        try:
            rfrom = date.fromisoformat(rng["from"])
            rto = date.fromisoformat(rng["to"])
            price = rng.get("price1")
        except (KeyError, ValueError):
            continue
        d = max(rfrom, start_date)
        last = min(rto, end_date)
        while d <= last:
            if price is not None:
                daily[d] = price
            d += timedelta(days=1)
    return daily


def resync_room(room):
    """Pull fresh calendar+availability for one room and overwrite our
    RoomAvailability rows. Mirrors beds24_sync.py's sync_room()."""
    from app import db, RoomAvailability  # lazy import avoids circular import

    access_token = get_access_token()
    start_date = date.today()
    end_date = start_date + timedelta(days=SYNC_DAYS_AHEAD)

    calendar = fetch_calendar(access_token, room.beds24_room_id, start_date, end_date)
    availability = fetch_availability(access_token, room.beds24_room_id, start_date, end_date)
    daily_prices = expand_calendar_to_daily_price(calendar, start_date, end_date)

    existing = {
        r.date: r
        for r in RoomAvailability.query.filter(
            RoomAvailability.room_id == room.id,
            RoomAvailability.date >= start_date,
            RoomAvailability.date <= end_date,
        ).all()
    }

    all_dates = set(daily_prices.keys()) | {date.fromisoformat(k) for k in availability.keys()}

    lowest_price = None
    d = start_date
    while d <= end_date:
        if d in all_dates:
            price = daily_prices.get(d)
            avail = availability.get(d.isoformat(), True)
            if price is not None and avail and (lowest_price is None or price < lowest_price):
                lowest_price = price
            row = existing.get(d)
            if row:
                row.price = price
                row.available = avail
            else:
                db.session.add(RoomAvailability(room_id=room.id, date=d, price=price, available=avail))
        d += timedelta(days=1)

    if lowest_price is not None:
        room.price = lowest_price

    db.session.commit()


def extract_room_ids(payload):
    """Beds24 webhook bodies can be a single booking object or a list of
    them. Pull out every roomId mentioned, wherever it appears."""
    items = payload if isinstance(payload, list) else [payload]
    room_ids = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        rid = item.get("roomId") or (item.get("booking") or {}).get("roomId")
        if rid:
            try:
                room_ids.add(int(rid))
            except (TypeError, ValueError):
                pass
    return room_ids


def handle_webhook():
    from app import app, Room  # lazy import avoids circular import

    if BEDS24_WEBHOOK_SECRET:
        got = request.headers.get("X-Webhook-Secret", "")
        if got != BEDS24_WEBHOOK_SECRET:
            app.logger.warning("Beds24 webhook: rejected request with bad/missing secret")
            return jsonify({"ok": False, "error": "unauthorized"}), 401

    payload = request.get_json(force=True, silent=True) or {}
    room_ids = extract_room_ids(payload)

    if not room_ids:
        app.logger.warning(f"Beds24 webhook: no roomId found in payload: {payload}")
        return jsonify({"ok": True, "note": "no roomId found, nothing to resync"}), 200

    for beds24_room_id in room_ids:
        room = Room.query.filter_by(beds24_room_id=beds24_room_id).first()
        if not room:
            app.logger.warning(f"Beds24 webhook: no local room mapped to beds24_room_id {beds24_room_id}")
            continue
        try:
            resync_room(room)
            app.logger.info(f"Beds24 webhook: resynced room {room.id} ({room.name})")
        except Exception as e:
            app.logger.warning(f"Beds24 webhook: resync failed for room {room.id}: {e}")

    return jsonify({"ok": True}), 200
