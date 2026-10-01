"""Mirror the public BookinRiga GAS catalog fed by Beds24 Marketplace.

Use the exact published unit IDs, not the separate Beds24 property 341384.
No credential is required for the same public endpoints the reference uses.
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from html import unescape
import requests
from beds24_catalog import RoomText

REFERENCE = 'https://bookinriga.sites.gas.travel'
API = 'https://admin.gas.travel/api/public'


def published_units(html):
    match = re.search(r'var gasRoomsMapData\s*=\s*(\[.*?\]);', html, re.S)
    units = json.loads(match.group(1)) if match else []
    ids = [u.get('id') for u in units]
    if not units or any(not isinstance(i, int) for i in ids) or len(ids) != len(set(ids)):
        raise RuntimeError('Reference catalog is empty or invalid; retained previous listings')
    return units


def get_json(url, params=None):
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()
    if data.get('success') is not True:
        raise RuntimeError('Reference API did not return successful data')
    return data


def localized(value):
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return value
        return localized(decoded)
    return str(value.get('en') or next(iter(value.values()), '')) if isinstance(value, dict) else ''


def plain_text(value):
    parser = RoomText()
    parser.feed(localized(value).replace('\\n', '\n'))
    return unescape(''.join(parser.text)).strip()


def positive(value):
    try:
        value = float(value)
        return value if value > 0 else None
    except (ValueError, TypeError):
        return None


def fetch_catalog():
    response = requests.get(REFERENCE + '/book-now/', timeout=30)
    response.raise_for_status()
    listing = published_units(response.text)
    home = requests.get(REFERENCE + '/', timeout=30)
    home.raise_for_status()
    featured = list(dict.fromkeys(int(i) for i in re.findall(r'data-room-id="(\d+)"', home.text)))
    if not featured or not set(featured).issubset({u['id'] for u in listing}):
        raise RuntimeError('Reference homepage selection is incomplete')
    today = date.today()
    end = today + timedelta(days=365)

    def fetch(item):
        uid = item['id']
        detail = get_json(f'{API}/unit/{uid}')
        unit = detail.get('unit', {})
        if unit.get('id') != uid or not unit.get('name') or unit.get('currency') != 'EUR':
            raise RuntimeError('Reference returned a different unit or unsupported currency')
        calendar = get_json(f'{API}/availability/{uid}', {'from': today.isoformat(), 'to': end.isoformat()})
        if str(calendar.get('unit_id')) != str(uid) or calendar.get('currency') != 'EUR':
            raise RuntimeError('Reference returned a different calendar')
        rows = calendar.get('calendar')
        if not isinstance(rows, list) or len(rows) != 365:
            raise RuntimeError('Reference calendar is incomplete; retained previous catalog')
        expected = {today + timedelta(days=i) for i in range(365)}
        normalized = []
        for row in rows:
            day = date.fromisoformat(row['date'])
            price = positive(row.get('price'))
            normalized.append((day, price, row.get('available') is True and price is not None))
        if {r[0] for r in normalized} != expected:
            raise RuntimeError('Reference calendar dates are incomplete')
        return detail, normalized

    pool = ThreadPoolExecutor(max_workers=4)
    pending = {pool.submit(fetch, item): index for index, item in enumerate(listing)}
    data = [None] * len(listing)
    try:
        for future in as_completed(pending):
            data[pending[future]] = future.result()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return data, featured


def apply_catalog(data, featured):
    from app import db, Room, RoomImage, RoomAvailability
    if not data:
        raise ValueError('Refusing an empty reference catalog')
    existing = Room.query.order_by(Room.id).all()
    selected = set()
    created = 0
    try:
        for order, (detail, calendar) in enumerate(data, 1):
            u = detail['unit']
            uid = u['id']
            room = next((r for r in existing if r.reference_unit_id == uid), None)
            if room is None:
                room = next((r for r in existing if r.id not in selected and not r.reference_unit_id
                             and r.name.strip().casefold() == u['name'].strip().casefold()), None)
            if room is None:
                room = Room(name=u['name'][:120], room_type='Apartment', price=0)
                db.session.add(room)
                db.session.flush()
                existing.append(room)
                created += 1
            selected.add(room.id)
            room.reference_unit_id = uid
            room.reference_order = order
            room.homepage_order = featured.index(uid) + 1 if uid in featured else 0
            room.name = localized(u.get('display_name'))[:120] or u['name'][:120]
            room.description = plain_text(u.get('full_description') or u.get('description') or u.get('short_description'))
            room.room_type = (u.get('unit_type') or 'Apartment').title()[:80]
            room.capacity = int(u.get('max_guests') or 0)
            room.max_adults = int(u.get('max_adults') or room.capacity)
            room.max_children = int(u.get('max_children') or 0)
            room.beds = int(u.get('beds_from_amenities') or u.get('beds') or 0)
            room.bathrooms = int(float(u.get('num_bathrooms') or u.get('bathroom_count') or 0))
            room.size_sqm = int(float(u.get('size_sqm') or 0))
            room.min_stay = int(u.get('min_stay') or 1)
            room.max_stay = int(u.get('max_stay') or 365)
            room.location = (u.get('property_address') or u.get('city') or 'Riga')[:200]
            room.is_active = not u.get('is_hidden', False)
            room.specs_manual_override = False
            # GAS unit IDs are distinct from Beds24 room IDs; never interchange them.
            room.beds24_room_id = int(u['cm_room_id']) if u.get('cm_source') == 'beds24-marketplace' and str(u.get('cm_room_id', '')).isdigit() else None
            room.beds24_property_id = None
            amenities = [localized(a.get('name')) for a in detail.get('amenities', [])]
            room.amenities = ', '.join(dict.fromkeys(a for a in amenities if a))[:255]
            grouped = {}
            for a in detail.get('amenities', []):
                grouped.setdefault(a.get('category') or 'amenities', []).append(localized(a.get('name')))
            room.features = json.dumps(grouped)
            urls = list(dict.fromkeys(i['url'] for i in detail.get('images', []) if isinstance(i.get('url'), str) and i['url'].startswith(('https://', 'http://')) and len(i['url']) <= 255))
            room.image = urls[0] if urls else ''
            RoomImage.query.filter_by(room_id=room.id).delete()
            for url in urls[1:]:
                db.session.add(RoomImage(room_id=room.id, filename=url))
            RoomAvailability.query.filter_by(room_id=room.id).delete()
            for day, price, available in calendar:
                db.session.add(RoomAvailability(room_id=room.id, date=day, price=price, available=available))
            room.price = positive(u.get('base_price')) or min((p for _, p, a in calendar if a), default=0)
        deactivated = 0
        for room in existing:
            if room.id not in selected and room.is_active:
                room.is_active = False
                deactivated += 1
        db.session.commit()
        return {'published_units': len(data), 'created': created, 'deactivated': deactivated}
    except Exception:
        db.session.rollback()
        raise


def main():
    from app import app
    data, featured = fetch_catalog()
    with app.app_context():
        result = apply_catalog(data, featured)
    print(f'[reference] BookinRiga GAS/Beds24 Marketplace sync: {result}')
