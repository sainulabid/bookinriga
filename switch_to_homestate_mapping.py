"""
RigaNest x Beds24 — switch 20 confirmed rooms over to the REAL
Homestate property/room (instead of the BookinRiga duplicate we
created earlier).

The pairs below came from discover_homestate_mapping.py's "AMBIGUOUS"
section — each of these local rooms had an exact-name match in BOTH
the junk "BookinRiga" property AND a real Homestate property. This
script switches them to the real one.

Usage:
    python switch_to_homestate_mapping.py
"""

from app import app, db, Room

# local_room_id -> (homestate_property_id, homestate_room_id)
SWITCH_TO_HOMESTATE = {
    11: (64076, 149472),    # Old Riga Central Family Apartment
    12: (193334, 416243),   # One Bedroom Promenade Apartment
    14: (25979, 61706),     # Old Riga Galleria Apartment
    16: (58042, 134518),    # Riga Embassy center apartment with parking
    21: (115590, 260389),   # Design King Bed Studio Apartment in Old Town
    26: (191500, 415949),   # M7 Design Studio In City Center
    27: (193325, 416225),   # Spacious 2 bedroom Apartment on Vecpilsetas street
    28: (191770, 416237),   # Old Riga Smilsu street Quiet One Bedroom Apartment
    30: (191510, 416247),   # Riga Riverside Spacious One Bedroom Apartment
    31: (191847, 416249),   # Valdemara Design Studio Apartment in City Center
    33: (191861, 416427),   # Kalnina Street Modern Studio Apartment in Riga
    34: (191857, 416553),   # Valentina Design Studio Apartment
    35: (195518, 420486),   # Maria Studio in Riga centre
    36: (201485, 430912),   # Design Kalnina studio I Fast Wi-fi I Near Park
    38: (217748, 462948),   # Raina Boulevard Exclusive Design Apartment At Park
    39: (218050, 463425),   # Raina Boulevard Exclusive Studio Design Studio
    40: (233541, 494331),   # One bedroom Sunny Tallinn Street Apartment With Parking
    43: (262756, 551185),   # Designer Parkside Apartment I Fast wi-fi
    44: (72481, 167152),    # Old Riga Kaleju Studio Apartment With Terrace
    46: (204017, 435507),   # Central House Riverside Studio Apartment
}


def main():
    with app.app_context():
        updated = 0
        for local_id, (prop_id, room_id) in SWITCH_TO_HOMESTATE.items():
            room = Room.query.get(local_id)
            if not room:
                print(f"  Local room #{local_id} not found, skipping.")
                continue
            old = room.beds24_room_id
            room.beds24_room_id = room_id
            room.beds24_property_id = prop_id
            updated += 1
            print(f"  Room #{room.id} '{room.name}': room_id {old} -> {room_id}, property_id -> {prop_id}")
        db.session.commit()
        print(f"\nDone. {updated} room(s) switched to their real Homestate mapping.")


if __name__ == "__main__":
    main()
