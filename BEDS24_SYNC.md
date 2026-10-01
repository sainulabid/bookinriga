# Beds24 source catalog

The website catalog is sourced from **BookinRiga, property 341384**.
`beds24_catalog.py` reads that property's complete room list using API v2
`GET /properties` with `includeAllRooms`, `includeTexts`, `includeLanguages`
and `includePictures`. It does not send updates back to Beds24.

The sync updates names, descriptions, capacity, stay limits, size, amenities
and room-specific media when returned by the API. New room IDs create listings;
renames keep the same local room ID. The initial migration matches exact names
so existing booking references remain intact. Listings outside this catalog and
removed rooms are deactivated, never deleted. A failed/empty catalog response
cannot deactivate listings.

When IDs change from the former Homestate mapping, old calendar rows and prices
are cleared. A rate is displayed only after a successful calendar sync returns
a positive price and availability. Missing prices show “Contact for rates”;
unknown dates cannot be booked through the local booking endpoint.

`python beds24_sync.py` refreshes the catalog before its calendars. The existing
admin sync trigger remains available. A background loop starts on the first
web request and refreshes every 30 minutes while the service is running, retrying
failures after ten minutes. Render Free can suspend the process during inactivity;
on wake-up the loop starts again. This is eventual synchronization, not an instant
webhook. A continuously running worker or scheduled job is needed for updates
while the web service sleeps.

Required: an active Beds24 account and the existing `BEDS24_REFRESH_TOKEN` with
read access to property 341384 and inventory. No secrets belong in this document.
If the API omits room photos, the sync logs that limitation and preserves the
existing photos for the matching room. Photo synchronization must be verified
against the actual response; it must not be assumed from the parameter alone.

Verification on 2026-10-01 found 35 rooms in the BookinRiga control panel. The
account showed “Your credit has expired,” all rooms showed “No price found,” and
the public booking page returned “This account has been paused.” Those source
account conditions need correction in Beds24 before live booking can work.

Tests: `python -m unittest test_beds24_catalog -v` (always uses a temporary DB).
