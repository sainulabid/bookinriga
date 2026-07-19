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

def _clean_env(value):
    """Defensively clean an env var value: if it accidentally contains
    a newline (e.g. from pasting 'TOKEN\\nOTHER_KEY=value' into a single
    Render env var box), keep only the first line, and strip stray
    whitespace/quotes."""
    if not value:
        return value
    return value.split("\n")[0].split("\r")[0].strip().strip('"').strip("'")


BEDS24_REFRESH_TOKEN = _clean_env(os.environ.get("BEDS24_REFRESH_TOKEN", ""))
SYNC_DAYS_AHEAD = int(os.environ.get("BEDS24_SYNC_DAYS", "365"))

API_BASE = "https://beds24.com/api/v2"


def fetch_room_specs(access_token, room_id):
    """
    Fetches room-level details (not date-specific) from Beds24:
    max adults/children, min/max stay length, unit type. Kept as a
    SEPARATE function/sync from the price+calendar sync above — specs
    rarely change, so this is meant to be run occasionally (manually,
    via /admin/run-beds24-specs-sync), not every 30 minutes, to avoid
    burning through the account's API credit limit.

    Beds24 v2's exact field names for /inventory/rooms aren't fully
    confirmed for this account yet, so this prints the raw response
    for the first room so we can see what's actually available and
    adjust the extraction if needed.
    """
    resp = requests.get(
        f"{API_BASE}/inventory/rooms",
        headers={"accept": "application/json", "token": access_token},
        params={"roomId": room_id},
        timeout=30,
    )
    data = resp.json()
    if resp.status_code != 200 or not data.get("data"):
        print(f"  [warn] room-details fetch failed for room {room_id}: {data}")
        return None

    info = data["data"][0]
    print(f"  [debug] room {room_id} raw room-details: {info}")
    return info


def _first_present(d, keys, default=None):
    for k in keys:
        if d.get(k) is not None:
            return d.get(k)
    return default


def sync_room_specs(access_token, room):
    """Updates Room.bed_type/max_adults/max_children/min_stay/max_stay
    from Beds24, UNLESS the admin has manually overridden them
    (room.specs_manual_override)."""
    if room.specs_manual_override:
        print(f"  [skip] room {room.id} ({room.name}): specs manually overridden by admin, not touching")
        return False

    info = fetch_room_specs(access_token, room.beds24_room_id)
    if not info:
        return False

    room.bed_type = _first_present(info, ["unitType", "roomType", "type"], room.bed_type or "")
    room.max_adults = int(_first_present(info, ["maxAdult", "maxAdults"], room.max_adults or 2))
    room.max_children = int(_first_present(info, ["maxChildren", "maxChild"], room.max_children or 0))
    room.min_stay = int(_first_present(info, ["minStay", "minPeriod"], room.min_stay or 1))
    room.max_stay = int(_first_present(info, ["maxStay", "maxPeriod"], room.max_stay or 365))
    db.session.commit()
    return True


def main_specs():
    """Entry point for the separate, occasional specs sync."""
    if not BEDS24_REFRESH_TOKEN:
        print("ERROR: BEDS24_REFRESH_TOKEN environment variable is not set.")
        sys.exit(1)

    with app.app_context():
        access_token = get_access_token()
        rooms = Room.query.filter(Room.beds24_room_id.isnot(None)).all()
        if not rooms:
            print("No rooms have a beds24_room_id set yet.")
            return

        print(f"Syncing specs for {len(rooms)} room(s)...")
        updated = 0
        for room in rooms:
            print(f"- Room #{room.id} ({room.name}) <- Beds24 room {room.beds24_room_id}")
            if sync_room_specs(access_token, room):
                updated += 1
        print(f"Done. Specs updated for {updated}/{len(rooms)} room(s).")


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
    """Returns list of {'from':date, 'to':date, 'price1':float} ranges.

    IMPORTANT FIX: Beds24 API v2 does NOT return price data from
    /inventory/rooms/calendar unless you explicitly ask for it with
    includePrices=true. Without this param, the availability/numAvail
    part comes back fine but every price field is missing — which is
    exactly why the calendar looked "ready" while prices stayed empty.
    """
    resp = requests.get(
        f"{API_BASE}/inventory/rooms/calendar",
        headers={"accept": "application/json", "token": access_token},
        params={
            "roomId": room_id,
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "includePrices": "true",
            "includeNumAvail": "true",
        },
        timeout=30,
    )
    data = resp.json()
    if resp.status_code != 200 or not data.get("data"):
        print(f"  [warn] calendar fetch failed for room {room_id}: {data}")
        return []

    calendar = data["data"][0].get("calendar", [])
    if calendar:
        # One-time debug print so we can confirm the actual field name
        # Beds24 sends the price back as for this account (usually
        # price1, but some accounts/plans use a different key).
        print(f"  [debug] room {room_id} sample calendar entry: {calendar[0]}")
    else:
        print(f"  [debug] room {room_id}: empty calendar array returned")
    return calendar


def _extract_price(rng):
    """Beds24 accounts can label the first price tier differently
    depending on plan/settings. Try the common variants in order
    instead of only ever looking for 'price1'."""
    for key in ("price1", "rate1", "price", "roomRate", "rate"):
        if rng.get(key) is not None:
            return rng.get(key)
    return None


def expand_calendar(calendar_ranges, start_date, end_date):
    """Turn [{'from','to','price1','numAvail'}, ...] ranges into
    {date: (price, available)}.

    IMPORTANT: this used to require a SEPARATE call to
    /inventory/rooms/availability for the available/blocked flag. But
    the calendar endpoint already returns numAvail per range when we
    ask for it (includeNumAvail=true) — so we derive availability from
    numAvail > 0 here instead, cutting API calls per room from 2 to 1.
    This matters a lot on accounts with a tight Beds24 API credit
    limit: with 35+ rooms, 2 calls/room was hitting 'Credit limit
    exceeded' (429) partway through every sync.
    """
    daily = {}
    for rng in calendar_ranges:
        try:
            rfrom = date.fromisoformat(rng["from"])
            rto = date.fromisoformat(rng["to"])
            price = _extract_price(rng)
        except (KeyError, ValueError):
            continue
        num_avail = rng.get("numAvail")
        available = True if num_avail is None else (num_avail > 0)
        d = max(rfrom, start_date)
        last = min(rto, end_date)
        while d <= last:
            daily[d] = (price, available)
            d += timedelta(days=1)
    return daily


def sync_room(access_token, room):
    start_date = date.today()
    end_date = start_date + timedelta(days=SYNC_DAYS_AHEAD)

    calendar = fetch_calendar(access_token, room.beds24_room_id, start_date, end_date)
    daily = expand_calendar(calendar, start_date, end_date)

    if not daily:
        print(f"  [skip] no data returned for room {room.id} ({room.name})")
        return 0

    if not any(price is not None for price, _avail in daily.values()):
        print(f"  [warn] room {room.id} ({room.name}): availability synced but NO prices found. "
              f"Check the [debug] sample entry above for the real price field name.")

    existing = {
        r.date: r
        for r in RoomAvailability.query.filter(
            RoomAvailability.room_id == room.id,
            RoomAvailability.date >= start_date,
            RoomAvailability.date <= end_date,
        ).all()
    }

    written = 0
    lowest_price = None
    d = start_date
    while d <= end_date:
        if d not in daily:
            d += timedelta(days=1)
            continue
        price, avail = daily[d]
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
