"""Read the BookinRiga catalog from Beds24. Never write to Beds24."""
import json
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests

PROPERTY_ID = 341384
API_BASE = "https://beds24.com/api/v2"


class RoomText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.images = []

    def handle_data(self, data):
        self.text.append(data)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "img" and attrs.get("src"):
            self.images.append(attrs["src"])
        if tag in {"p", "br", "div", "li"}:
            self.text.append("\n")


def fetch_property(token):
    """Fetch the complete selected property; reject partial/error responses."""
    properties = []
    page = 1
    while True:
        response = requests.get(
            API_BASE + "/properties",
            headers={"accept": "application/json", "token": token},
            params={"id": PROPERTY_ID, "includeAllRooms": "true",
                    "includeTexts": "all", "includeLanguages": "all",
                    "includePictures": "true", "page": page},
            timeout=30,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Beds24 catalog request failed (HTTP {response.status_code})")
        payload = response.json()
        if not isinstance(payload.get("data"), list) or payload.get("success") is False:
            raise RuntimeError("Beds24 did not return a valid property catalog")
        properties.extend(payload["data"])
        if not payload.get("pages", {}).get("nextPageExists"):
            break
        page += 1
        if page > 100:
            raise RuntimeError("Beds24 catalog pagination did not finish")
    matches = [p for p in properties if p.get("id") == PROPERTY_ID]
    if len(matches) != 1 or not isinstance(matches[0].get("roomTypes"), list):
        raise RuntimeError(f"Beds24 property {PROPERTY_ID} is unavailable to this API token")
    prop = matches[0]
    # An empty result must never deactivate the entire website after an API error.
    rooms = prop["roomTypes"]
    if not rooms or any(not isinstance(r.get("id"), int) or not r.get("name") for r in rooms):
        raise RuntimeError("Beds24 returned an empty or incomplete room catalog")
    if len({r["id"] for r in rooms}) != len(rooms):
        raise RuntimeError("Beds24 returned duplicate room IDs")
    return prop


def english_text(info):
    texts = info.get("texts") or []
    if not isinstance(texts, list):
        return {}
    return next((t for t in texts if t.get("language", "").lower() == "en"),
                next((t for t in texts if not t.get("language")), {}))


def feature_codes(value):
    """Beds24 featureCodes is an array of arrays, not a flat string list."""
    result = []
    for entry in value or []:
        entries = entry if isinstance(entry, list) else [entry]
        result.extend(c for c in entries if isinstance(c, str) and c)
    return list(dict.fromkeys(result))


def picture_urls(info, text):
    """Use room-specific media only; never substitute another room's photos."""
    parser = RoomText()
    parser.feed(text)
    values = parser.images
    present = "pictures" in info or "images" in info or bool(values)
    for item in info.get("pictures", info.get("images", [])) or []:
        if isinstance(item, str):
            values.append(item)
        elif isinstance(item, dict):
            value = item.get("url") or item.get("src")
            if value:
                values.append(value)
    urls = list(dict.fromkeys(u for u in values if isinstance(u, str)
                            and urlparse(u).scheme in {"http", "https"}
                            and len(u) <= 255))
    return present, urls


def apply_catalog(prop):
    """Upsert by remote ID; exact-name migration preserves existing bookings."""
    from app import db, Room, RoomAvailability, RoomImage

    if prop.get("id") != PROPERTY_ID or not prop.get("roomTypes"):
        raise ValueError("Refusing an empty or different property catalog")
    existing = Room.query.order_by(Room.id).all()
    selected_ids = set()
    result = {"property_id": PROPERTY_ID, "created": 0, "updated": 0,
              "deactivated": 0, "photos_unavailable": 0}
    try:
        for info in prop["roomTypes"]:
            matches = [r for r in existing if r.beds24_property_id == PROPERTY_ID
                       and r.beds24_room_id == info["id"]]
            if not matches:
                matches = [r for r in existing if r.name.strip().casefold() == info["name"].strip().casefold()]
            room = matches[0] if matches else None
            if room is None:
                room = Room(name=info["name"], room_type="Apartment", price=0,
                            capacity=0, beds=0, bathrooms=0, size_sqm=0)
                db.session.add(room)
                db.session.flush()
                existing.append(room)
                result["created"] += 1
            else:
                result["updated"] += 1
            if (room.beds24_property_id, room.beds24_room_id) != (PROPERTY_ID, info["id"]):
                # Previously imported Homestate rates/blocked dates belong to a different ID.
                RoomAvailability.query.filter_by(room_id=room.id).delete()
                room.price = 0
            selected_ids.add(room.id)
            room.beds24_property_id = PROPERTY_ID
            room.beds24_room_id = info["id"]
            room.name = info["name"][:120]
            room.room_type = (info.get("roomType") or "Apartment")[:80]
            room.is_active = info.get("qty", 1) > 0
            room.specs_manual_override = False
            text = english_text(info)
            raw_description = text.get("roomDescription") or text.get("contentDescription") or ""
            if "texts" in info:
                parser = RoomText()
                parser.feed(raw_description)
                room.description = unescape("".join(parser.text)).strip()
            for source, target in [("maxPeople", "capacity"), ("maxAdult", "max_adults"),
                                   ("maxChildren", "max_children"), ("minStay", "min_stay"),
                                   ("maxStay", "max_stay"), ("roomSize", "size_sqm")]:
                if info.get(source) is not None:
                    setattr(room, target, max(0, int(info[source])))
            room.location = ", ".join(str(prop.get(k) or "").strip()
                                      for k in ("address", "city") if prop.get(k))[:200]
            if "featureCodes" in info:
                codes = feature_codes(info["featureCodes"])
                room.amenities = ", ".join(c.replace("_", " ").title() for c in codes)[:255]
                # Old manual feature tags must not compete with the source catalog.
                room.features = json.dumps({"beds24": codes})
            media_present, urls = picture_urls(info, raw_description)
            if media_present:
                room.image = urls[0] if urls else ""
                RoomImage.query.filter_by(room_id=room.id).delete()
                for url in urls[1:]:
                    db.session.add(RoomImage(room_id=room.id, filename=url))
            else:
                result["photos_unavailable"] += 1
        # Hide unrelated and removed listings; keep rows and bookings recoverable.
        for room in existing:
            if room.id not in selected_ids and room.is_active:
                room.is_active = False
                result["deactivated"] += 1
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return result


def sync_catalog(token):
    result = apply_catalog(fetch_property(token))
    print(f"[catalog] BookinRiga {PROPERTY_ID}: {result}")
    if result["photos_unavailable"]:
        print("[catalog] Some rooms had no media field in the API response; kept existing matching-room photos.")
    return result
