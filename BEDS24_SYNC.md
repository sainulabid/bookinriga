# BookinRiga ↔ Beds24 sync

## Catalog: Beds24 → website (default)

`beds24_direct.py` reads the catalog straight from the Beds24 account behind
`BEDS24_REFRESH_TOKEN` (API v2). Every 30 minutes it mirrors, for each room:
name, description, photos, amenities (feature codes), capacity, stay limits,
size, address, and 365 days of nightly prices and availability. Whatever you
change in Beds24 appears on bookinriga.onrender.com at the next sync.

- Publishes only the 50 apartments that were on the old site
  (bookinriga.sites.gas.travel), listed by Beds24 room ID in `old_site.py`.
  Every other Beds24 room (other properties, duplicate listings) stays
  hidden. To publish a different set, set
  `PUBLISHED_BEDS24_ROOM_IDS=123712,61706,...` in Render.
- Rooms are matched by Beds24 room ID, so existing apartments, their
  bookings and homepage order are kept.
- Photos: room pictures from the API; for single-apartment properties the
  property pictures (Beds24 media.xmlcal.com). If Beds24 returns none, the
  room keeps its Beds24 / Beds24 Marketplace photos, and apartments with no
  photo at all get the photos the old site showed for them. Manual uploads
  are removed. Check with `/admin/photo-audit`.
- The catalog changes only after every property and calendar validated; any
  API error keeps the previous catalog. Removed rooms are hidden, not deleted.
- Unknown dates or dates without a price can never be booked.
- An apartment unticked "Visible to guests" in admin stays hidden after syncs.
- Room specs edited in admin (specs override) are not overwritten.
- Required token scopes for the catalog: `read:properties`, `read:inventory`.

Run a sync now: `/admin/run-beds24-sync`; check it: `/admin/beds24-sync-status`.

### Retiring bookinriga.sites.gas.travel

Some Riga and attraction photos were hosted on the gas.travel CDN. The app
copies them into its own database (`CachedImage`, served from `/media/<id>`)
in the background. Before deleting the gas.travel site, open
`/admin/beds24-sync-status` and confirm `"safe_to_remove_gas_travel_site": true`
(Beds24 sync succeeded and no photo still points at gas.travel).

The old reference sync (`reference_sync.py`) remains available with
`CATALOG_SOURCE=reference`, but it stops working once the site is removed.

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

Run `python -m unittest test_beds24_direct test_beds24_catalog test_reference_sync test_beds24_booking`.
Tests use an isolated SQLite database and mocked APIs: no real charge, guest
email or inventory-blocking test reservation is made.
