# BookinRiga

Apartment booking website for short-term rentals in Riga, Latvia — live at
<https://bookinriga.onrender.com>.

Flask app with an English/Latvian site, apartment catalog synced from the
published BookinRiga reference site, Stripe checkout, email OTP / Google
sign-in, and verified export of paid bookings to Beds24.

## Run locally

```bash
python -m pip install -r requirements.txt
cp .env.example .env      # fill in only what you need
python app.py             # http://127.0.0.1:5000
```

Without keys the app runs in dev mode: OTP codes are shown on screen and
checkout is a demo (no real charge). Without `DATABASE_URL` it uses local
SQLite (`riganest.db`).

On a fresh database an admin `admin@riganest.com` is created with the password
from `ADMIN_PASSWORD`, or a random one-time password printed in the logs.

## Environment (Render)

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Long random string — required in production |
| `DATABASE_URL` | Render PostgreSQL URL |
| `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY` | Card payments (`sk_live_` for real bookings) |
| `STRIPE_WEBHOOK_SECRET` | Signing secret for `/webhooks/stripe` |
| `BEDS24_REFRESH_TOKEN` | Beds24 API v2 — booking export |
| `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` | Email OTP via Gmail SMTP |
| `RESEND_API_KEY`, `RESEND_FROM_EMAIL` | Email notifications via Resend (optional) |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | "Continue with Google" |
| `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET` | Persistent image uploads |
| `WHATSAPP_NUMBER` | Support number shown on the site |
| `ADMIN_PASSWORD` | Admin password on a fresh database only |
| `ENABLE_LEGACY_ADMIN_TOOLS` | Leave unset — enables old one-time Homestate migration routes |

Never commit `.env` — set secrets in Render → Environment.

## Catalog & Beds24

See [BEDS24_SYNC.md](BEDS24_SYNC.md) for the reference catalog sync, booking
export, reconciliation and Render configuration.

Admin pages: `/admin`, `/admin/bookings`, `/admin/booking-export/status`.

## Tests

```bash
python -m unittest test_beds24_catalog test_reference_sync test_beds24_booking
```

Tests use an isolated SQLite database and mocked APIs.
