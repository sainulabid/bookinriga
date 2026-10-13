import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

# Never use the production database.
os.environ['DATABASE_URL'] = 'sqlite:///' + tempfile.mktemp(suffix='.db')
from app import app, db, Room, RoomImage, RoomAvailability, is_available
import beds24_direct as bd


def props(single_pics=True):
    return [
        {'id': 25979, 'address': 'Teātra iela 4', 'city': 'Rīga',
         'pictures': [{'url': 'https://media.xmlcal.com/pic/p0002/5979/1.png'},
                      {'url': 'https://media.xmlcal.com/pic/p0002/5979/2.png'}] if single_pics else [],
         'roomTypes': [{'id': 61706, 'name': 'Old Riga Galleria Apartment', 'qty': 1, 'maxPeople': 4,
                        'minStay': 2, 'maxStay': 30, 'roomSize': 64, 'featureCodes': [['WIFI', 'KITCHEN']],
                        'texts': [{'language': 'en', 'roomDescription': '<p>Bright <b>flat</b></p><p>Near Old Town</p>'}]}]},
        {'id': 341384, 'address': 'Valdemāra iela 57', 'city': 'Rīga',
         'roomTypes': [{'id': 149472, 'name': 'Studio A', 'qty': 1, 'maxPeople': 2,
                        'pictures': ['https://media.xmlcal.com/pic/p0034/1384/9.png']},
                       {'id': 149473, 'name': 'Studio B', 'qty': 0, 'maxPeople': 2}]},
    ]


def calendars(start):
    d1, d2 = start.isoformat(), (start + timedelta(days=2)).isoformat()
    return {
        61706: [{'from': d1, 'to': d2, 'numAvail': 1, 'price1': 55, 'minStay': 2}],
        149472: [{'from': d1, 'to': d1, 'numAvail': 1, 'price1': 40},
                 {'from': (start + timedelta(days=1)).isoformat(), 'to': d2, 'numAvail': 0, 'price1': 40}],
        149473: [],
    }


class DirectSyncTests(unittest.TestCase):
    def setUp(self):
        app.testing = True
        self.context = app.app_context()
        self.context.push()
        db.drop_all()
        db.create_all()
        self.start = date.today()
        self.end = self.start + timedelta(days=9)

    def tearDown(self):
        db.session.remove()
        self.context.pop()

    def apply(self, p=None):
        return bd.apply_catalog(p or props(), calendars(self.start), self.start, self.end, probe_photos=False)

    def test_creates_rooms_with_texts_photos_prices(self):
        result = self.apply()
        self.assertEqual(result['rooms'], 3)
        r = Room.query.filter_by(beds24_room_id=61706).one()
        self.assertEqual(r.beds24_property_id, 25979)
        self.assertTrue(r.is_active and r.is_synced)
        self.assertEqual(r.description, 'Bright flat\nNear Old Town')
        self.assertEqual((r.capacity, r.min_stay, r.max_stay, r.size_sqm), (4, 2, 30, 64))
        self.assertEqual(r.location, 'Teātra iela 4, Rīga')
        self.assertIn('Wifi', r.amenities)
        # single-apartment property: property photos are the room photos
        self.assertTrue(r.image.endswith('/1.png'))
        self.assertEqual(len(r.photos), 1)
        self.assertEqual(r.price, 55)
        self.assertTrue(is_available(r.id, self.start, self.start + timedelta(days=2)))
        # unknown dates are never bookable
        self.assertFalse(is_available(r.id, self.start + timedelta(days=3), self.start + timedelta(days=5)))

    def test_multi_room_property_uses_only_room_photos(self):
        self.apply()
        a = Room.query.filter_by(beds24_room_id=149472).one()
        b = Room.query.filter_by(beds24_room_id=149473).one()
        self.assertTrue(a.image.endswith('/9.png'))
        self.assertEqual(b.image or '', '')
        self.assertFalse(b.is_active)  # qty 0
        self.assertEqual(a.price, 40)
        self.assertFalse(is_available(a.id, self.start + timedelta(days=1), self.start + timedelta(days=2)))

    def test_keeps_existing_room_bookings_and_photos_by_beds24_id(self):
        old = Room(name='Old GAS name', room_type='Apartment', price=99, beds24_room_id=149473,
                   reference_unit_id=2028, image='https://a0.muscache.com/original/x.jpg')
        gone = Room(name='Removed', room_type='Apartment', price=10, beds24_room_id=1, is_active=True)
        db.session.add_all([old, gone]); db.session.commit()
        old_id = old.id
        self.apply()
        r = db.session.get(Room, old_id)
        self.assertEqual(r.name, 'Studio B')
        self.assertEqual(r.image, 'https://a0.muscache.com/original/x.jpg')  # Beds24 Marketplace photo kept
        self.assertFalse(db.session.get(Room, gone.id).is_active)

    def test_only_old_site_apartments_are_published(self):
        import os
        os.environ['PUBLISHED_BEDS24_ROOM_IDS'] = '149472'
        try:
            result = self.apply()
        finally:
            os.environ.pop('PUBLISHED_BEDS24_ROOM_IDS')
        self.assertTrue(Room.query.filter_by(beds24_room_id=149472).one().is_active)
        self.assertFalse(Room.query.filter_by(beds24_room_id=61706).one().is_active)
        self.assertEqual(result['not_on_old_site'], 2)

    def test_manual_upload_removed_when_beds24_has_no_photo(self):
        old = Room(name='Studio B', room_type='Apartment', price=9, beds24_room_id=149473,
                   image='1782377816_upload.jpg')
        db.session.add(old); db.session.commit()
        self.apply()
        self.assertEqual(db.session.get(Room, old.id).image, '')

    def test_bed_count_from_feature_codes(self):
        from beds24_direct import bed_count
        self.assertEqual(bed_count({'featureCodes': [['BEDROOM', 'BED_DOUBLE'], ['BEDROOM', 'BED_SINGLE', 'BED_SINGLE'], ['LIVING_ROOM', 'SOFA_BED'], ['WIFI']]}), 4)
        self.assertEqual(bed_count({'featureCodes': [['WIFI', 'KITCHEN', 'BED_LINEN']]}), None)
        self.assertEqual(bed_count({'beds': 3}), 3)

    def test_admin_hidden_room_stays_hidden(self):
        self.apply()
        r = Room.query.filter_by(beds24_room_id=61706).one()
        r.website_hidden = True; r.is_active = False; db.session.commit()
        result = self.apply()
        self.assertFalse(db.session.get(Room, r.id).is_active)
        self.assertEqual(result['hidden_by_admin'], 1)

    def test_admin_spec_override_is_respected(self):
        self.apply()
        r = Room.query.filter_by(beds24_room_id=61706).one()
        r.capacity = 6; r.specs_manual_override = True; db.session.commit()
        self.apply()
        self.assertEqual(db.session.get(Room, r.id).capacity, 6)

    def test_api_error_keeps_previous_catalog(self):
        self.apply()
        before = Room.query.filter_by(is_active=True).count()
        bad = MagicMock(status_code=500)
        with patch.dict(os.environ, {'BEDS24_REFRESH_TOKEN': 'x'}), \
             patch.object(bd, 'access_token', return_value='t'), \
             patch.object(bd.requests, 'get', return_value=bad):
            with self.assertRaises(bd.Beds24Error):
                bd.fetch_catalog(days=10)
        self.assertEqual(Room.query.filter_by(is_active=True).count(), before)

    def test_missing_calendar_is_rejected(self):
        ok_props = MagicMock(status_code=200)
        ok_props.json.return_value = {'success': True, 'data': props(), 'pages': {}}
        empty_cal = MagicMock(status_code=200)
        empty_cal.json.return_value = {'success': True, 'data': [], 'pages': {}}
        with patch.object(bd, 'access_token', return_value='t'), \
             patch.object(bd.time, 'sleep'), \
             patch.object(bd.requests, 'get', side_effect=[ok_props, empty_cal]):
            with self.assertRaises(bd.Beds24Error):
                bd.fetch_catalog(days=10)

    def test_property_filter_requires_access(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {'success': True, 'data': props()[:1], 'pages': {}}
        with patch.dict(os.environ, {'BEDS24_PROPERTY_IDS': '25979, 341384'}), \
             patch.object(bd.requests, 'get', return_value=resp):
            with self.assertRaises(bd.Beds24Error):
                bd.fetch_properties('t')

    def test_property_report_suggests_previously_published(self):
        rep = bd.property_report(props(), {149472})
        self.assertEqual(rep['properties_seen'], 2)
        self.assertEqual(rep['rooms_seen'], 3)
        self.assertEqual(rep['suggested_BEDS24_PROPERTY_IDS'], '341384')
        self.assertEqual(rep['properties'][0]['property_id'], 341384)

    def test_expand_calendar_price_fallback(self):
        days = bd.expand_calendar([{'from': self.start.isoformat(), 'to': self.start.isoformat(),
                                    'numAvail': 2, 'price1': 0, 'price2': '48.5'}], self.start, self.end)
        self.assertEqual(days[self.start][:2], (48.5, True))


if __name__ == '__main__':
    unittest.main()
