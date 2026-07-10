"""
RigaNest x Beds24 — periodic sync.

Pulls price + availability for every mapped room from Beds24 and writes
it into the RoomAvailability table (and updates Room.price to the
lowest upcoming price, used for room-list display/sorting).

Requires an environment variable:
    BEDS24_REFRESH_TOKEN   -> the long-life refresh token you got from
                               get_refresh_token.py

Run manually to test:
    python beds24_sync.py

Then schedule it to run every 30 minutes (see CRON_SETUP.txt).
"""

import os
import sys
from datetime import date, timedelta

import requests

from app import app, db, Room, RoomAvailability

BEDS24_REFRESH_TOKEN = os.environ.get("BEDS24_REFRESH_TOKEN", "")
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
    """Returns list of {'from':date, 'to':date, 'price1':float} ranges."""
    resp = requests.get(
        f"{API_BASE}/inventory/rooms/calendar",
        headers={"accept": "application/json", "token": access_token},
        params={
            "roomId": room_id,
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
        },
        timeout=30,
    )
    data = resp.json()
    if resp.status_code != 200 or not data.get("data"):
        print(f"  [warn] calendar fetch failed for room {room_id}: {data}")
        return []
    return data["data"][0].get("calendar", [])


def fetch_availability(access_token, room_id, start_date, end_date):
    """Returns dict {date_str: bool}."""
    resp = requests.get(
        f"{API_BASE}/inventory/rooms/availability",
        headers={"accept": "application/json", "token": access_token},
        params={
            "roomId": room_id,
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
        },
        timeout=30,
    )
    data = resp.json()
    if resp.status_code != 200 or not data.get("data"):
        print(f"  [warn] availability fetch failed for room {room_id}: {data}")
        return {}
    return data["data"][0].get("availability", {})


def expand_calendar_to_daily_price(calendar_ranges, start_date, end_date):
    """Turn [{'from','to','price1'}, ...] ranges into {date: price}."""
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


def sync_room(access_token, room):
    start_date = date.today()
    end_date = start_date + timedelta(days=SYNC_DAYS_AHEAD)

    calendar = fetch_calendar(access_token, room.beds24_room_id, start_date, end_date)
    availability = fetch_availability(access_token, room.beds24_room_id, start_date, end_date)
    daily_prices = expand_calendar_to_daily_price(calendar, start_date, end_date)

    if not daily_prices and not availability:
        print(f"  [skip] no data returned for room {room.id} ({room.name})")
        return 0

    existing = {
        r.date: r
        for r in RoomAvailability.query.filter(
            RoomAvailability.room_id == room.id,
            RoomAvailability.date >= start_date,
            RoomAvailability.date <= end_date,
        ).all()
    }

    all_dates = set(daily_prices.keys()) | {
        date.fromisoformat(k) for k in availability.keys()
    }

    written = 0
    lowest_price = None
    d = start_date
    while d <= end_date:
        if d not in all_dates:
            d += timedelta(days=1)
            continue
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
        written += 1
        d += timedelta(days=1)

    if lowest_price is not None:
        room.price = lowest_price

    db.session.commit()
    return written


def main():
    if not BEDS24_REFRESH_TOKEN:
        print("ERROR: BEDS24_REFRESH_TOKEN environment variable is not set.")
        sys.exit(1)

    with app.app_context():
        access_token = get_access_token()

        rooms = Room.query.filter(Room.beds24_room_id.isnot(None)).all()
        if not rooms:
            print("No rooms have a beds24_room_id set yet. Run set_beds24_mapping.py first.")
            return

        print(f"Syncing {len(rooms)} room(s)...")
        total = 0
        for room in rooms:
            print(f"- Room #{room.id} ({room.name}) <- Beds24 room {room.beds24_room_id}")
            count = sync_room(access_token, room)
            total += count
            print(f"  wrote/updated {count} date rows")

        print(f"Done. {total} date rows synced across {len(rooms)} room(s).")


if __name__ == "__main__":
    main()
