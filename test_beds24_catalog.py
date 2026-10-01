"""Regression tests for source catalog reconciliation; use an isolated DB."""
import copy
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch, Mock

_test_dir = tempfile.TemporaryDirectory(prefix="bookinriga-sync-tests-")
os.environ["DATABASE_URL"] = "sqlite:///" + _test_dir.name + "/test.db"
from app import app, db, Room, RoomAvailability, is_available
from beds24_catalog import apply_catalog, fetch_property, feature_codes
from beds24_sync import expand_calendar, sync_room

app.testing = True


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.context = app.app_context()
        self.context.push()
        db.drop_all()
        db.create_all()
        self.prop = {"id": 341384, "name": "BookinRiga", "city": "Riga",
            "roomTypes": [{"id": 705390, "name": "Old Riga Cozy One Bedroom Apartment",
                "qty": 1, "roomType": "apartment", "maxPeople": 4,
                "maxAdult": 3, "maxChildren": 1, "minStay": 2, "maxStay": 30,
                "roomSize": 45, "featureCodes": [["WIFI", "KITCHEN"], ["WIFI"]],
                "texts": [{"language": "en", "roomDescription": "<p>Source description</p>"}],
                "pictures": [{"url": "https://beds24.com/pic/room.jpg"}]}]}

    def tearDown(self):
        db.session.remove()
        self.context.pop()

    def test_remap_preserves_local_id_and_clears_old_rates(self):
        old = Room(name=self.prop["roomTypes"][0]["name"], room_type="Studio",
                   price=99, beds24_property_id=123, beds24_room_id=456,
                   specs_manual_override=True)
        other = Room(name="Unrelated property", room_type="Studio", price=88)
        db.session.add_all([old, other])
        db.session.flush()
        local_id = old.id
        db.session.add(RoomAvailability(room_id=old.id, date=date.today(), price=99, available=True))
        db.session.commit()
        result = apply_catalog(self.prop)
        self.assertEqual(result["created"], 0)
        self.assertEqual(old.id, local_id)
        self.assertEqual(old.beds24_room_id, 705390)
        self.assertEqual(old.price, 0)
        self.assertEqual(old.capacity, 4)
        self.assertEqual(old.description, "Source description")
        self.assertEqual(old.image, "https://beds24.com/pic/room.jpg")
        self.assertFalse(old.specs_manual_override)
        self.assertFalse(other.is_active)
        self.assertEqual(RoomAvailability.query.count(), 0)
        self.assertFalse(is_available(old.id, date.today(), date.today()+timedelta(days=1)))

    def test_rename_add_remove_and_photo_updates_are_idempotent(self):
        apply_catalog(self.prop)
        local_id = Room.query.first().id
        changed = copy.deepcopy(self.prop)
        changed["roomTypes"][0]["name"] = "Renamed in Beds24"
        changed["roomTypes"][0]["pictures"] = [{"url": "https://beds24.com/pic/new.jpg"}]
        changed["roomTypes"].append({"id": 705391, "name": "New source apartment", "qty": 1})
        apply_catalog(changed)
        apply_catalog(changed)
        self.assertEqual(Room.query.count(), 2)
        self.assertEqual(db.session.get(Room, local_id).name, "Renamed in Beds24")
        self.assertEqual(db.session.get(Room, local_id).image, "https://beds24.com/pic/new.jpg")
        apply_catalog(self.prop)
        self.assertEqual(Room.query.filter_by(is_active=True).count(), 1)

    def test_partial_or_failed_response_does_not_deactivate_rooms(self):
        apply_catalog(self.prop)
        with self.assertRaises(ValueError):
            apply_catalog({"id": 341384, "roomTypes": []})
        with patch("beds24_catalog.requests.get", return_value=Mock(status_code=403)):
            with self.assertRaises(RuntimeError):
                fetch_property("test-token")
        self.assertTrue(Room.query.first().is_active)

    def test_pagination_and_property_selection(self):
        responses = [Mock(status_code=200), Mock(status_code=200)]
        responses[0].json.return_value = {"data": [], "pages": {"nextPageExists": True}}
        responses[1].json.return_value = {"data": [self.prop], "pages": {"nextPageExists": False}}
        with patch("beds24_catalog.requests.get", side_effect=responses) as get:
            self.assertEqual(fetch_property("test-token"), self.prop)
            self.assertEqual(get.call_args.kwargs["params"]["page"], 2)
        self.assertEqual(feature_codes([["WIFI", "KITCHEN"], ["WIFI"]]), ["WIFI", "KITCHEN"])

    def test_missing_rate_or_availability_is_not_bookable(self):
        today = date.today()
        end = today + timedelta(days=1)
        ranges = [{"from": today.isoformat(), "to": end.isoformat(), "numAvail": 1}]
        self.assertEqual(expand_calendar(ranges, today, end)[today], (None, False))
        ranges[0]["price1"] = 100
        ranges[0].pop("numAvail")
        self.assertFalse(expand_calendar(ranges, today, end)[today][1])
        apply_catalog(self.prop)
        room = Room.query.first()
        room.price = 99
        db.session.commit()
        ranges[0]["numAvail"] = 1
        ranges[0].pop("price1")
        with patch("beds24_sync.fetch_calendar", return_value=ranges):
            sync_room("test-token", room)
        self.assertEqual(room.price, 0)
        self.assertFalse(is_available(room.id, today, end))

    def test_public_pages_show_catalog_without_fake_zero_euro_rates(self):
        apply_catalog(self.prop)
        client = app.test_client()
        for route in ("/", "/rooms", f"/room/{Room.query.first().id}"):
            response = client.get(route)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Contact for rates", response.data)
        self.assertEqual(client.get(f"/book/{Room.query.first().id}").status_code, 302)


if __name__ == "__main__":
    unittest.main()
