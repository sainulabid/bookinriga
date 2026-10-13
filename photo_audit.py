"""Apartment photo audit: which photos on the website come from Beds24 and
which don't (old gas.travel copies, manual uploads, anything else).

Used by /admin/photo-audit. Read-only unless clean_to_beds24() is called.
"""
from urllib.parse import urlparse


def is_marketplace_photo(url):
    from old_site import is_marketplace_photo as f
    return f(url)


def allowed(url):
    return is_beds24_host(url) or is_marketplace_photo(url)

BEDS24_HOSTS = ("beds24.com", "xmlcal.com")


def is_beds24_host(url):
    host = (urlparse(url or "").hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in BEDS24_HOSTS)


def source_label(url, gas_host):
    if not url:
        return "empty"
    if not url.startswith(("http://", "https://")):
        return "Uploaded to website (server)"
    host = (urlparse(url).hostname or "").lower()
    if is_beds24_host(url):
        return "Beds24"
    if is_marketplace_photo(url):
        return "Beds24 Marketplace (old site)"
    if host == gas_host or "gas.travel" in host:
        return "Old gas.travel site"
    if host.endswith("cloudinary.com"):
        return "Uploaded to website (Cloudinary)"
    return "Other: " + host


def live_beds24_photos():
    """{beds24_room_id: [photo urls]} straight from the Beds24 API.
    Returns (photos, error_message)."""
    try:
        import beds24_direct as b
        from beds24_catalog import english_text
        token = b.access_token()
        props = b.fetch_properties(token)
    except Exception as e:  # API down / token missing: fall back to host check
        return None, str(e) or e.__class__.__name__
    out = {}
    for prop in props:
        ptext = english_text(prop)
        for info in prop["roomTypes"]:
            text = english_text(info)
            raw = (text.get("roomDescription") or text.get("contentDescription")
                   or ptext.get("propertyDescription") or ptext.get("contentDescription") or "")
            out[info["id"]] = b.room_pictures(prop, info, raw, probe=False)
    return out, None


def _room_photos(room):
    """(kind, id, stored url) for the cover and every extra photo."""
    rows = []
    if room.image:
        rows.append(("cover", room.id, room.image))
    for p in room.photos:
        rows.append(("photo", p.id, p.filename))
    return rows


def audit(rooms, gas_host, live):
    """Per-room photo report. `live` may be None (API unreachable)."""
    report, extras = [], []
    totals = {"rooms": 0, "photos": 0, "beds24": 0, "not_beds24": 0,
              "rooms_with_extras": 0, "rooms_not_linked": 0, "beds24_missing": 0}
    for room in rooms:
        b24 = set((live or {}).get(room.beds24_room_id) or [])
        rows = []
        for kind, pid, url in _room_photos(room):
            ok = url in b24 or allowed(url)
            rows.append(dict(kind=kind, id=pid, url=url, beds24=ok,
                             source="Beds24" if url in b24 else source_label(url, gas_host)))
        bad = [r for r in rows if not r["beds24"]]
        shown = {r["url"] for r in rows}
        missing = [u for u in (live or {}).get(room.beds24_room_id, []) if u not in shown] if live else []
        report.append(dict(room=room, total=len(rows), beds24=len(rows) - len(bad), not_beds24=len(bad),
                           linked=bool(room.beds24_room_id),
                           beds24_available=len((live or {}).get(room.beds24_room_id, [])) if live else None,
                           missing=len(missing)))
        for r in bad:
            extras.append(dict(r, room_id=room.id, room_name=room.name))
        totals["rooms"] += 1
        totals["photos"] += len(rows)
        totals["not_beds24"] += len(bad)
        totals["beds24"] += len(rows) - len(bad)
        totals["rooms_with_extras"] += 1 if bad else 0
        totals["rooms_not_linked"] += 0 if room.beds24_room_id else 1
        totals["beds24_missing"] += len(missing)
    report.sort(key=lambda r: (-r["not_beds24"], r["room"].name.lower()))
    return report, extras, totals


def clean_to_beds24(rooms, live, db, RoomImage):
    """Make every linked room show exactly its Beds24 photos (cover first).
    Rooms Beds24 has no photos for, and rooms not linked to Beds24, lose
    their non-Beds24 photos. Returns counts."""
    removed = added = 0
    for room in rooms:
        b24 = list(dict.fromkeys(live.get(room.beds24_room_id) or [])) if room.beds24_room_id else []
        before = [u for _, _, u in _room_photos(room)]
        if b24:
            keep = b24
        else:
            keep = [u for u in before if allowed(u)]
        if keep == before:
            continue
        removed += len([u for u in before if u not in keep])
        added += len([u for u in keep if u not in before])
        room.image = keep[0] if keep else ""
        RoomImage.query.filter_by(room_id=room.id).delete()
        for url in keep[1:]:
            db.session.add(RoomImage(room_id=room.id, filename=url))
    db.session.commit()
    return {"removed": removed, "added": added}
