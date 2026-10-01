import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

# Never use the production database.
os.environ['DATABASE_URL'] = 'sqlite:///' + tempfile.mktemp(suffix='.db')
from app import app, db, Room, RoomAvailability, is_available
from reference_sync import apply_catalog, published_units, localized


class ReferenceSyncTests(unittest.TestCase):
    def setUp(self):
        app.testing = True
        self.context = app.app_context()
        self.context.push()
        db.drop_all()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        self.context.pop()

    def data(self, name='Reference Apartment'):
        return [({'unit': {'id': 2015, 'name': name, 'display_name': {'en': name},
                          'max_guests': 6, 'max_adults': 6, 'base_price': '59',
                          'num_bathrooms': '2', 'beds_from_amenities': 3,
                          'full_description': '{"en":"<p>Source text</p>"}',
                          'cm_source': 'beds24-marketplace', 'cm_room_id': '149472'},
                  'images': [{'url': 'https://example.com/room.jpg'}],
                  'amenities': [{'name': {'en': 'Air Conditioning'}, 'category': 'amenities'}]},
                 [(date.today(), 72.0, True), (date.today() + timedelta(days=1), None, False)])]

    def test_migration_rename_removal_and_dates(self):
        old = Room(name='Reference Apartment', room_type='Apartment', price=100)
        unrelated = Room(name='Unrelated', room_type='Apartment', price=80)
        db.session.add_all([old, unrelated]); db.session.commit()
        old_id = old.id
        result = apply_catalog(self.data(), [2015])
        self.assertEqual(result['created'], 0)
        self.assertEqual(old.id, old_id)
        self.assertEqual(old.reference_unit_id, 2015)
        self.assertEqual(old.beds24_room_id, 149472)
        self.assertIsNone(old.beds24_property_id)
        self.assertEqual(old.description, 'Source text')
        self.assertEqual(old.image, 'https://example.com/room.jpg')
        self.assertIn('Air Conditioning', old.amenity_list())
        self.assertFalse(unrelated.is_active)
        self.assertTrue(is_available(old.id, date.today(), date.today()+timedelta(days=1)))
        self.assertFalse(is_available(old.id, date.today(), date.today()+timedelta(days=2)))
        apply_catalog(self.data('Renamed Apartment'), [2015])
        self.assertEqual(Room.query.count(), 2)
        self.assertEqual(old.name, 'Renamed Apartment')
        self.assertEqual(RoomAvailability.query.count(), 2)
        with app.test_client() as client:
            self.assertEqual(client.get('/').status_code, 200)
            self.assertEqual(client.get('/rooms').status_code, 200)
            self.assertEqual(client.get('/room/'+str(old_id)).status_code, 200)

    def test_invalid_catalog_and_localization(self):
        with self.assertRaises(RuntimeError): published_units('no units')
        with self.assertRaises(RuntimeError): published_units('var gasRoomsMapData = [{"id":1},{"id":1}];')
        self.assertEqual(localized('{"en":"Name"}'), 'Name')
        with self.assertRaises(ValueError): apply_catalog([], [])


if __name__ == '__main__': unittest.main()
