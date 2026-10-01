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
prices or unknown dates cannot be booked. The catalog sync does not copy guest bookings
from Beds24. New local booking export is described below.

The app starts background refresh on its first request, repeats every 30
minutes, and retries failures after 10 minutes. Render Free sleeps when idle;
updates resume when it wakes. Logs expose the sync outcome and room count.

Existing direct Beds24 utilities remain available, but automatic and normal
admin sync use the reference source. Do not run legacy mapping/repair scripts
against the reference catalog.

## Website bookings → Beds24

`beds24_booking.py` exports **new, server-verified live Stripe payments** to
`POST /bookings`. Guest name/email/phone, actual Beds24 room ID, dates, occupancy,
total price and a durable website reference are sent. The GAS unit ID is never
used as a Beds24 room ID. `actions.checkAvailability=true` tells Beds24 not to
save a new reservation if the room has no availability.

The checkout preflight checks token scopes, access to the exact mapped room and
current offers before requesting payment. A booking remains Pending until its
remote ID and confirmed status are read back. A payment can succeed while the
room is sold through another channel; in that case the paid booking stays
Pending for staff resolution/refund, rather than falsely confirming it.

Successful redirects retrieve the saved Stripe Session on the server and verify
its ID, EUR amount and paid status. Signed Stripe webhooks also queue exports;
a background worker checks once a minute while Render is awake, and reconciles
recent Stripe sessions when the guest does not return. Read/auth errors retry
after five minutes. Lost/partial POST replies and interrupted workers reconcile
by remote ID or the durable `apiReference`/`custom1` marker and **never blindly
repeat a create**. Unresolved cases require staff review. Demo and Stripe test
payments do not create real reservations. Historical unverified records are not
backfilled automatically.

### Render configuration

- `BEDS24_REFRESH_TOKEN`: a valid refresh token for the account that can access
  the actual 50 mapped rooms, including authorized linked properties if needed.
- Required scopes: `read:bookings`, `write:bookings`,
  `read:bookings-personal`, `write:bookings-personal`,
  `write:bookings-financial`, `read:properties`, `read:inventory`.
  The matching `all:<category>` scope is accepted too. No delete permission is
  needed. Never paste credentials into source code or chat.
- Existing `STRIPE_SECRET_KEY` must be a **live** key for real exports.
- Recommended: configure a Stripe webhook for
  `https://bookinriga.onrender.com/webhooks/stripe`, subscribe to
  `checkout.session.completed` and `checkout.session.async_payment_succeeded`,
  and put its signing secret in Render `STRIPE_WEBHOOK_SECRET`. Signature
  verification is mandatory for this endpoint. Server-side return verification
  and the recovery worker also work without the webhook.

Read `/admin/booking-export/status` after signing in as an administrator for
safe connection/scopes/room-access status. `/admin/bookings` displays the remote
booking ID, export state and safe error codes, with a CSRF-protected check/retry
button. Startup logs print a credential-free connection summary.

Cancellation requests update the existing Beds24 booking by ID. Local inventory
stays blocked until the update succeeds. Cancellation does not automatically
refund Stripe payments. Requests for bookings created through the external Beds24
booking engine must be handled through their original booking channel.

Run `python -m unittest test_beds24_catalog test_reference_sync test_beds24_booking`.
Tests use an isolated SQLite database and mocked APIs: no real charge, guest
email or inventory-blocking test reservation is made.
