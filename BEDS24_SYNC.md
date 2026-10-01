# BookinRiga reference sync

The required source is https://bookinriga.sites.gas.travel/book-now/.
Its current published catalog has 50 GAS unit IDs sourced through Beds24
Marketplace. This is different from the 35-room Beds24 property 341384.

`reference_sync.py` reads the unit selection from the published page and uses
its public GAS unit and availability endpoints. It copies names, full text,
photos, amenities, capacities, source order, homepage selections, and 365 days
of nightly prices/availability. GAS unit IDs are stored separately from Beds24
room IDs. It needs no Beds24 refresh token and follows the same public data
source as the reference website; timing depends on the upstream GAS sync.

The catalog updates only after every selected room and its calendar validate.
Any incomplete/API error leaves the previous catalog intact. Removed/unrelated
rooms become inactive; local IDs and bookings are retained. Missing calendar
prices or unknown dates cannot be booked. This does not copy guest bookings
from Beds24 and does not push new local bookings to Beds24/GAS.

The app starts background refresh on its first request, repeats every 30
minutes, and retries failures after 10 minutes. Render Free sleeps when idle;
updates resume when it wakes. Logs expose the sync outcome and room count.

Existing direct Beds24 utilities remain available, but automatic and normal
admin sync use the reference source. Do not run legacy mapping/repair scripts
against the reference catalog.
