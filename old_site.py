"""The apartments published on the old site (bookinriga.sites.gas.travel).

Only these apartments are shown on bookinriga.onrender.com. The Beds24
account holds many more rooms (other properties and duplicate listings);
those stay hidden.

Map: Beds24 room ID -> old-site (GAS) unit ID, taken from the old site's
public API (`cm_room_id` of every unit on /book-now/, 13 Oct 2026, 50 units).

To publish a different set later, set PUBLISHED_BEDS24_ROOM_IDS in Render
(comma-separated Beds24 room IDs); it replaces this list.
"""
import os
from urllib.parse import urlparse

import requests

GAS_API = "https://admin.gas.travel/api/public"

OLD_SITE_ROOMS = {
    61706: 2009,   # Old Riga Galleria Apartment
    84656: 2010,   # Old Riga Terrace Apartment
    104315: 2011,  # Riga City Center Apartment
    123712: 2012,  # Old Riga Jekaba street Apartment
    134518: 2013,  # Riga Embassy center apartment with parking
    139822: 2014,  # Old Riga Central Apartment
    149472: 2015,  # Old Riga Central Family Apartment
    150173: 2016,  # Kalnina Quiet Apartment in city center
    167152: 2017,  # Old Riga Kaleju Studio Apartment With Terrace
    174603: 2018,  # Old Riga 2 Bedroom Vecpilsetas street Apartment
    210520: 2019,  # Old Riga Palasta Loft Apartment with river view
    212582: 2020,  # Riga Centre Apartment Merkela street
    224378: 2021,  # One Bedroom Apartment
    224379: 2022,  # Studio Junior Apartment
    225166: 2023,  # Studio Junior Apartment
    225172: 2024,  # Studio Apartment
    416357: 2025,  # Residential Barona ... One Bedroom Apartment
    416358: 2026,  # Residential Barona ... Studio Apartment
    416359: 2027,  # Residential Barona One Bedroom Apartment
    260389: 2028,  # Design King Bed Studio Apartment in Old Town
    262357: 2029,  # Modern Design Studio Apartment In Riga Center
    416364: 2030,  # Old Riga Ridzenes Residence Studio Apartment
    416365: 2031,  # Old Riga Ridzenes Residence Studio Apartment
    416366: 2032,  # Ridzenes Residence One Bedroom Apartment
    416367: 2033,  # Old Riga Ridzenes Residence Studio Apartment
    416368: 2034,  # Old Riga Ridzenes Residence Studio Apartment
    301735: 2035,  # Brivibas Yard Apartment With Parking In City Center
    415949: 2036,  # M7 Design Studio In City Center
    416247: 2037,  # Riga Riverside Spacious One Bedroom Apartment
    415886: 2038,  # Riga Riverside Design One Bedroom Apartment
    416237: 2039,  # Old Riga Smilsu street Quiet One Bedroom Apartment
    416249: 2040,  # Valdemara Design Studio Apartment in City Center
    416553: 2041,  # Valentina Design Studio Apartment
    416427: 2042,  # Kalnina Street Modern Studio Apartment in Riga
    416225: 2043,  # Spacious 2 bedroom Apartment on Vecpilsetas street
    416243: 2044,  # One Bedroom Promenade Apartment
    416465: 2045,  # Old Riga Cozy One Bedroom Apartment
    420486: 2046,  # Maria Studio in Riga centre
    430912: 2047,  # Design Kalnina studio I Fast Wi-fi I Near Park
    435507: 2048,  # Central House Riverside Studio Apartment
    442020: 2049,  # Old Riga Kaleju Cozy apartment2
    456800: 2050,  # Kalnina Design Studio in Centre
    462948: 2051,  # Raina Boulevard Exclusive Design Apartment At Park
    463425: 2052,  # Raina Boulevard Exclusive Studio Design Studio
    494331: 2053,  # One bedroom Sunny Tallinn Street Apartment With Parking
    496217: 2054,  # Park Studio DeLuxe Apartment
    501538: 2055,  # Kalnina Park Residence Studio
    523361: 2056,  # City Retreat Design Apartment
    551185: 2057,  # Designer Parkside Apartment I Fast wi-fi
    574677: 2058,  # Park Residence I Quiet Center I Fast Wi-fi I Queen Bed
}

# Hosts of photos that Beds24 Marketplace publishes for these apartments
# (listing photos imported into Beds24, and the old site's copies).
MARKETPLACE_HOSTS = ("muscache.com", "pub-543ea0ea60674fffbc40f7ca17a21278.r2.dev", "gas.travel")


def published_room_ids():
    raw = os.environ.get("PUBLISHED_BEDS24_ROOM_IDS", "")
    ids = {int(x) for x in raw.replace(";", ",").split(",") if x.strip().isdigit()}
    return ids or set(OLD_SITE_ROOMS)


def is_marketplace_photo(url):
    host = (urlparse(url or "").hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in MARKETPLACE_HOSTS)


def unit_photos(unit_id, beds24_room_id):
    """Photo URLs of one old-site unit, verified to belong to that Beds24 room."""
    r = requests.get(f"{GAS_API}/unit/{unit_id}", timeout=30)
    r.raise_for_status()
    data = r.json()
    unit = data.get("unit") or {}
    if data.get("success") is not True or unit.get("id") != unit_id \
            or str(unit.get("cm_room_id")) != str(beds24_room_id):
        raise RuntimeError(f"old site unit {unit_id} does not match Beds24 room {beds24_room_id}")
    return list(dict.fromkeys(
        i["url"] for i in data.get("images") or []
        if isinstance(i.get("url"), str) and i["url"].startswith(("https://", "http://"))
        and len(i["url"]) <= 255))


def import_missing_photos(only_missing=True):
    """Give published apartments that have no photos the photos shown for
    them on the old site. Returns a summary dict."""
    from app import db, Room, RoomImage
    done, failed, skipped = [], [], 0
    for room in Room.query.filter(Room.beds24_room_id.in_(list(OLD_SITE_ROOMS))).all():
        if only_missing and (room.image or room.photos):
            skipped += 1
            continue
        try:
            urls = unit_photos(OLD_SITE_ROOMS[room.beds24_room_id], room.beds24_room_id)
        except Exception as e:
            failed.append(f"{room.name}: {e}")
            continue
        if not urls:
            failed.append(f"{room.name}: old site has no photos")
            continue
        room.image = urls[0]
        RoomImage.query.filter_by(room_id=room.id).delete()
        for u in urls[1:]:
            db.session.add(RoomImage(room_id=room.id, filename=u))
        db.session.commit()
        done.append(f"{room.name}: {len(urls)} photos")
    return {"imported": done, "failed": failed, "already_had_photos": skipped}
