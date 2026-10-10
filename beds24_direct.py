"""Mirror the BookinRiga catalog directly from Beds24 (API v2). Read-only.

Everything shown on the website comes from the Beds24 account behind
BEDS24_REFRESH_TOKEN: properties, room names, descriptions, photos,
amenities, capacity, stay limits, nightly prices and availability.

Optional:
    BEDS24_PROPERTY_IDS   comma-separated property IDs to publish
                          (default: every property the token can read)
    BEDS24_SYNC_DAYS      days of prices/availability to mirror (default 365)

Safety rules (same as the previous reference sync):
- The catalog is replaced only after every property and calendar validated;
  any API error keeps the previous catalog intact.
- Rooms that disappear from Beds24 are hidden, never deleted, so bookings
  are kept.
- Unknown dates or dates without a price are never bookable.
- Rooms an admin hid on the website stay hidden after every sync.
"""
import json
import os
import time
import threading
from datetime import date, timedelta
from html import unescape
from urllib.parse import urlparse

import requests

from beds24_catalog import RoomText, english_text, feature_codes

API = "https://beds24.com/api/v2"
XMLCAL = "https://media.xmlcal.com/pic/p{block:04d}/{rest}/{n}.{ext}"
_lock = threading.Lock()
_token = None
_token_until = 0.0


class Beds24Error(RuntimeError):
    pass


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------
def _refresh_token():
    value = os.environ.get("BEDS24_REFRESH_TOKEN", "")
    return value.split("\n")[0].split("\r")[0].strip().strip("\"'")


def access_token():
    global _token, _token_until
    refresh = _refresh_token()
    if not refresh:
        raise Beds24Error("BEDS24_REFRESH_TOKEN is not set")
    with _lock:
        if _token and time.monotonic() < _token_until:
            return _token
        try:
            r = requests.get(API + "/authentication/token", headers={"refreshToken": refresh}, timeout=20)
        except requests.RequestException:
            raise Beds24Error("Beds24 authentication unreachable") from None
        if r.status_code != 200:
            raise Beds24Error(f"Beds24 authentication failed (HTTP {r.status_code})")
        data = r.json()
        if not data.get("token"):
            raise Beds24Error("Beds24 authentication returned no token")
        _token = data["token"]
        _token_until = time.monotonic() + max(60, int(data.get("expiresIn", 3600)) - 120)
        return _token


def api_get(path, params, token):
    """GET with pagination. Raises on any non-success so nothing partial is applied."""
    global _token_until
    rows, page = [], 1
    while True:
        try:
            r = requests.get(API + path, headers={"accept": "application/json", "token": token},
                             params=dict(params, page=page), timeout=40)
        except requests.RequestException:
            raise Beds24Error(f"Beds24 {path} unreachable") from None
        if r.status_code == 401:
            _token_until = 0
        if r.status_code != 200:
            raise Beds24Error(f"Beds24 {path} failed (HTTP {r.status_code})")
        payload = r.json()
        if payload.get("success") is False or not isinstance(payload.get("data"), list):
            raise Beds24Error(f"Beds24 {path} returned an invalid response")
        rows.extend(payload["data"])
        if not (payload.get("pages") or {}).get("nextPageExists"):
            return rows
        page += 1
        if page > 100:
            raise Beds24Error(f"Beds24 {path} pagination did not finish")


# ----------------------------------------------------------------------
# Fetch
# ----------------------------------------------------------------------
def selected_property_ids():
    raw = os.environ.get("BEDS24_PROPERTY_IDS", "")
    return [int(x) for x in raw.replace(";", ",").split(",") if x.strip().isdigit()]


def fetch_properties(token):
    params = {"includeAllRooms": "true", "includeTexts": "all", "includePictures": "true",
              "includeLanguages": "all"}
    wanted = selected_property_ids()
    if wanted:
        params["id"] = wanted
    props = api_get("/properties", params, token)
    if wanted:
        props = [p for p in props if p.get("id") in wanted]
        missing = set(wanted) - {p.get("id") for p in props}
        if missing:
            raise Beds24Error(f"Beds24 token cannot read properties {sorted(missing)}")
    props = [p for p in props if isinstance(p.get("id"), int) and isinstance(p.get("roomTypes"), list)]
    rooms = [r for p in props for r in p["roomTypes"]]
    if not rooms:
        raise Beds24Error("Beds24 returned no rooms; kept the previous catalog")
    if any(not isinstance(r.get("id"), int) or not r.get("name") for r in rooms):
        raise Beds24Error("Beds24 returned incomplete room data; kept the previous catalog")
    if len({r["id"] for r in rooms}) != len(rooms):
        raise Beds24Error("Beds24 returned duplicate room IDs")
    return props


def _price(rng):
    for key in ["price1"] + [f"price{i}" for i in range(2, 17)] + ["price"]:
        try:
            value = float(rng.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def expand_calendar(ranges, start, end):
    """Beds24 returns date ranges; expand to {day: (price, available, min_stay)}."""
    days = {}
    for rng in ranges or []:
        try:
            first = date.fromisoformat(str(rng.get("from"))[:10])
            last = date.fromisoformat(str(rng.get("to") or rng.get("from"))[:10])
        except ValueError:
            continue
        price = _price(rng)
        try:
            num_avail = int(rng.get("numAvail", 0) or 0)
        except (TypeError, ValueError):
            num_avail = 0
        min_stay = rng.get("minStay")
        d = max(first, start)
        while d <= min(last, end):
            days[d] = (price, num_avail > 0 and price is not None, min_stay)
            d += timedelta(days=1)
    return days


def fetch_calendars(token, props, start, end):
    """One request per property: {room_id: calendar ranges}."""
    result = {}
    for i, prop in enumerate(props):
        if i:
            time.sleep(0.4)  # stay well inside the Beds24 API credit limit
        rows = api_get("/inventory/rooms/calendar", {
            "propertyId": prop["id"], "startDate": start.isoformat(), "endDate": end.isoformat(),
            "includeNumAvail": "true", "includePrices": "true", "includeMinStay": "true"}, token)
        for row in rows:
            if isinstance(row.get("roomId"), int):
                result[row["roomId"]] = row.get("calendar") or []
        for room in prop["roomTypes"]:
            if room["id"] not in result:
                raise Beds24Error(f"Beds24 returned no calendar for room {room['id']}; kept the previous catalog")
    return result


def _picture_list(obj):
    out = []
    for key in ("pictures", "images", "photos"):
        for item in obj.get(key) or []:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                out.append(item.get("url") or item.get("src") or item.get("link") or "")
    return [u for u in out if isinstance(u, str) and urlparse(u).scheme in {"http", "https"} and len(u) <= 255]


def xmlcal_pictures(property_id, limit=30):
    """Beds24 hosts property photos at media.xmlcal.com; probe them in order."""
    urls = []
    for n in range(1, limit + 1):
        found = None
        for ext in ("png", "jpg"):
            url = XMLCAL.format(block=property_id // 10000, rest=property_id % 10000, n=n, ext=ext)
            try:
                if requests.head(url, timeout=8, allow_redirects=True).status_code == 200:
                    found = url
                    break
            except requests.RequestException:
                return urls
        if not found:
            break
        urls.append(found)
    return urls


def room_pictures(prop, info, description_html, probe=True):
    """Room photos first, then property photos for single-apartment properties."""
    parser = RoomText()
    parser.feed(description_html or "")
    urls = _picture_list(info) + [u for u in parser.images if urlparse(u).scheme in {"http", "https"}]
    if not urls and len(prop.get("roomTypes", [])) == 1:
        urls = _picture_list(prop)
        if not urls and probe:
            urls = xmlcal_pictures(prop["id"])
    return list(dict.fromkeys(urls))


def fetch_catalog(days=None):
    days = days or int(os.environ.get("BEDS24_SYNC_DAYS", "365"))
    token = access_token()
    props = fetch_properties(token)
    start = date.today()
    end = start + timedelta(days=days - 1)
    calendars = fetch_calendars(token, props, start, end)
    return props, calendars, start, end


# ----------------------------------------------------------------------
# Apply
# ----------------------------------------------------------------------
def _plain(html):
    parser = RoomText()
    parser.feed((html or "").replace("\\n", "\n"))
    text = unescape("".join(parser.text))
    lines = [ln.strip() for ln in text.splitlines()]
    out, blank = [], False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank and out:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def _int(value, default=0):
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return default


def apply_catalog(props, calendars, start, end, probe_photos=True):
    from app import db, Room, RoomImage, RoomAvailability

    existing = Room.query.order_by(Room.id).all()
    by_remote = {r.beds24_room_id: r for r in existing if r.beds24_room_id}
    next_order = max([r.reference_order or 0 for r in existing] + [0]) + 1
    selected = set()
    result = {"properties": len(props), "rooms": 0, "created": 0, "hidden_by_admin": 0,
              "deactivated": 0, "rooms_without_photos": 0, "rooms_without_prices": 0}
    try:
        for prop in props:
            address = ", ".join(str(prop.get(k)).strip() for k in ("address", "city") if prop.get(k))
            for info in prop["roomTypes"]:
                rid = info["id"]
                room = by_remote.get(rid)
                if room is None:
                    room = next((r for r in existing if r.id not in selected and not r.beds24_room_id
                                 and r.name.strip().casefold() == info["name"].strip().casefold()), None)
                if room is None:
                    room = Room(name=info["name"][:120], room_type="Apartment", price=0,
                                capacity=0, beds=0, bathrooms=0, size_sqm=0)
                    db.session.add(room)
                    db.session.flush()
                    existing.append(room)
                    result["created"] += 1
                selected.add(room.id)
                result["rooms"] += 1
                room.beds24_room_id = rid
                room.beds24_property_id = prop["id"]
                if not room.reference_order:
                    room.reference_order = next_order
                    next_order += 1

                text = english_text(info)
                ptext = english_text(prop)
                display = text.get("displayName") or text.get("roomName") or info["name"]
                room.name = str(display).strip()[:120]
                room.room_type = str(info.get("roomType") or "Apartment").replace("_", " ").title()[:80]
                raw = (text.get("roomDescription") or text.get("contentDescription")
                       or ptext.get("propertyDescription") or ptext.get("contentDescription") or "")
                description = _plain(raw)
                if description:
                    room.description = description
                if address:
                    room.location = address[:200]
                if not room.specs_manual_override:
                    for source, target in [("maxPeople", "capacity"), ("maxAdult", "max_adults"),
                                           ("maxChildren", "max_children"), ("minStay", "min_stay"),
                                           ("maxStay", "max_stay"), ("roomSize", "size_sqm")]:
                        if info.get(source) not in (None, ""):
                            setattr(room, target, _int(info[source]))
                    if not room.max_stay:
                        room.max_stay = 365
                    if not room.min_stay:
                        room.min_stay = 1
                codes = feature_codes(info.get("featureCodes")) + feature_codes(prop.get("featureCodes"))
                if codes:
                    names = list(dict.fromkeys(c.replace("_", " ").title() for c in codes))
                    room.amenities = ", ".join(names)[:255]
                    room.features = json.dumps({"amenities": names})

                urls = room_pictures(prop, info, raw, probe=probe_photos and not room.image)
                if urls:
                    room.image = urls[0]
                    RoomImage.query.filter_by(room_id=room.id).delete()
                    for url in urls[1:]:
                        db.session.add(RoomImage(room_id=room.id, filename=url))
                elif not room.image:
                    result["rooms_without_photos"] += 1

                daily = expand_calendar(calendars.get(rid), start, end)
                RoomAvailability.query.filter_by(room_id=room.id).delete()
                d = start
                while d <= end:
                    price, available, _min = daily.get(d, (None, False, None))
                    db.session.add(RoomAvailability(room_id=room.id, date=d, price=price, available=available))
                    d += timedelta(days=1)
                prices = [p for p, a, _ in daily.values() if a and p]
                room.price = min(prices) if prices else 0
                if not prices:
                    result["rooms_without_prices"] += 1

                hidden = bool(getattr(room, "website_hidden", False))
                room.is_active = _int(info.get("qty"), 1) > 0 and not hidden
                if hidden:
                    result["hidden_by_admin"] += 1

        for room in existing:
            if room.id not in selected and room.is_active:
                room.is_active = False
                result["deactivated"] += 1
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return result


LAST_REPORT = {}


def property_report(props, previously_published):
    """Which Beds24 properties the token sees, and whether their rooms were on
    the website before (via gas.travel). Helps choose BEDS24_PROPERTY_IDS."""
    rows = []
    for p in props:
        rooms = p.get("roomTypes", [])
        was = [r["id"] for r in rooms if r["id"] in previously_published]
        rows.append({"property_id": p["id"], "name": str(p.get("name") or "")[:80],
                     "rooms": len(rooms), "rooms_on_website_before": len(was),
                     "room_names": [str(r.get("name"))[:60] for r in rooms]})
    rows.sort(key=lambda r: (-r["rooms_on_website_before"], r["property_id"]))
    before = [r["property_id"] for r in rows if r["rooms_on_website_before"]]
    return {
        "properties_seen": len(rows),
        "rooms_seen": sum(r["rooms"] for r in rows),
        "properties_with_rooms_on_website_before": len(before),
        "rooms_from_website_before_found_in_beds24": sum(r["rooms_on_website_before"] for r in rows),
        "rooms_on_website_before_total": len(previously_published),
        "suggested_BEDS24_PROPERTY_IDS": ",".join(str(i) for i in before),
        "properties": rows,
    }


def main():
    global LAST_REPORT
    from app import app, Room
    props, calendars, start, end = fetch_catalog()
    with app.app_context():
        before = {r.beds24_room_id for r in Room.query.filter(Room.reference_unit_id.isnot(None),
                                                              Room.beds24_room_id.isnot(None)).all()}
        LAST_REPORT = property_report(props, before)
        result = apply_catalog(props, calendars, start, end)
    print(f"[beds24] Direct catalog sync: {result}")
    print(f"[beds24] Properties seen: {LAST_REPORT['properties_seen']}, rooms: {LAST_REPORT['rooms_seen']}, "
          f"previously published rooms found: {LAST_REPORT['rooms_from_website_before_found_in_beds24']}"
          f"/{LAST_REPORT['rooms_on_website_before_total']}")
    return result


if __name__ == "__main__":
    main()
