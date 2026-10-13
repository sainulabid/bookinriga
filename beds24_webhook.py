"""BookinRiga x Beds24 — real-time booking webhook.

Beds24 setup (Settings > Properties > Access > Booking Webhook, or the
account-level webhook), for every published property:
    URL: https://bookinriga.onrender.com/webhook/beds24-booking?key=<BEDS24_WEBHOOK_SECRET>

Whenever a booking is created, modified or cancelled in Beds24 — including
bookings from Airbnb, Booking.com etc. — Beds24 calls this URL. The body is
only used as a trigger: we pull that room's calendar (price, availability,
min stay) from the Beds24 API and replace the website's rows for it, using
exactly the same rules as the 30-minute full sync (beds24_direct.py), so a
booked date is closed on the website within seconds.

BEDS24_WEBHOOK_SECRET (Render env) is required; requests without the
matching ?key= (or X-Webhook-Secret header) are rejected.
"""
import hmac
import os
import threading
import time
from datetime import date, timedelta

from flask import jsonify, request

_recent = {}
_lock = threading.Lock()
DEBOUNCE_SECONDS = 15
LAST_EVENTS = []          # small in-memory log shown on the admin status page


def _log(msg):
    LAST_EVENTS.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}")
    del LAST_EVENTS[:-30]


def extract_room_ids(payload):
    """Beds24 bodies can be one booking, a list, or {"booking": {...}}."""
    items = payload if isinstance(payload, list) else [payload]
    ids = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        for obj in (item, item.get("booking") or {}):
            rid = obj.get("roomId") if isinstance(obj, dict) else None
            try:
                if rid:
                    ids.add(int(rid))
            except (TypeError, ValueError):
                pass
    return ids


def resync_room(beds24_room_id):
    """Refresh one room's prices/availability from Beds24 (same rules as the full sync)."""
    import beds24_direct as b
    from app import app, db, Room, RoomAvailability

    days = int(os.environ.get("BEDS24_SYNC_DAYS", "365"))
    start = date.today()
    end = start + timedelta(days=days - 1)
    token = b.access_token()
    rows = b.api_get("/inventory/rooms/calendar", {
        "roomId": beds24_room_id, "startDate": start.isoformat(), "endDate": end.isoformat(),
        "includeNumAvail": "true", "includePrices": "true", "includeMinStay": "true"}, token)
    ranges = next((r.get("calendar") or [] for r in rows if r.get("roomId") == beds24_room_id), None)
    if ranges is None:
        raise RuntimeError("Beds24 returned no calendar for this room")
    daily = b.expand_calendar(ranges, start, end)

    with app.app_context():
        room = Room.query.filter_by(beds24_room_id=beds24_room_id).first()
        if not room:
            return None
        try:
            RoomAvailability.query.filter(RoomAvailability.room_id == room.id,
                                          RoomAvailability.date >= start).delete()
            d = start
            while d <= end:
                price, available, _min = daily.get(d, (None, False, None))
                db.session.add(RoomAvailability(room_id=room.id, date=d, price=price, available=available))
                d += timedelta(days=1)
            prices = [p for p, a, _ in daily.values() if a and p]
            room.price = min(prices) if prices else 0
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return room.name


def _worker(room_ids):
    # Never write at the same time as the full sync: wait for it to finish
    # (its calendars may predate this booking), then refresh these rooms.
    from app import _get_sync_lock
    lock = _get_sync_lock()
    if not lock.acquire(timeout=900):
        _log(f"rooms {room_ids}: full sync still busy; it will catch up")
        return
    try:
        _update(room_ids)
    finally:
        lock.release()


def _update(room_ids):
    for rid in room_ids:
        try:
            name = resync_room(rid)
            _log(f"room {rid}: {'updated ' + name if name else 'not on website, skipped'}")
        except Exception as e:
            _log(f"room {rid}: update failed ({e}); the 30-minute sync will catch up")


def handle_webhook():
    secret = os.environ.get("BEDS24_WEBHOOK_SECRET", "")
    given = request.args.get("key", "") or request.headers.get("X-Webhook-Secret", "")
    if not secret or not hmac.compare_digest(given, secret):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    if request.method == "GET":
        return jsonify({"ok": True, "note": "webhook is reachable"}), 200

    room_ids = extract_room_ids(request.get_json(force=True, silent=True) or {})
    now = time.monotonic()
    with _lock:
        due = {r for r in room_ids if now - _recent.get(r, 0) > DEBOUNCE_SECONDS}
        for r in due:
            _recent[r] = now
    if due:
        # Answer Beds24 immediately; the API calls run in the background.
        threading.Thread(target=_worker, args=(sorted(due),), daemon=True,
                         name="beds24-webhook").start()
    elif not room_ids:
        _log("call without roomId ignored")
    return jsonify({"ok": True, "rooms": sorted(room_ids)}), 200
