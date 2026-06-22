# LuxeStay — Premium Hotel Booking 🏨

LuxeStay design-ൽ (royal blue + gold) ഉണ്ടാക്കിയ ഒരു **complete hotel booking website**.
User login + **SMS OTP**, **Stripe payment**, calendar booking, home page **slider**,
admin panel — ഒരു hotel booking site-ന് വേണ്ട എല്ലാ features-ഉം.

A full hotel booking web app: SMS-verified login, Stripe card payments,
calendar availability, a homepage image slider, and a complete admin panel.

---

## ✨ Features

- 🎨 **LuxeStay design** — royal blue + gold, Inter font
- 🖼️ **Homepage image slider** (auto-rotating)
- 👤 **User accounts** — register with mobile number
- 📱 **SMS OTP login** — password + a 6-digit code sent by SMS (Twilio)
- 💳 **Stripe payment gateway** — secure card checkout
- 📅 **Calendar booking** — pick dates, double-booking blocked
- 🛠️ **Admin panel** — rooms (add/edit/delete + photo upload), bookings, users, revenue
- 🔍 Room search & filters (price, guests)

> 💡 **Important:** the app works **immediately in DEV MODE** without any keys —
> the OTP code is shown on screen + terminal, and payment is a demo (no real charge).
> Add Stripe/Twilio keys (below) to go live.

---

## 🚀 How to run (എങ്ങനെ run ചെയ്യാം)

**1. Dependencies install ചെയ്യുക:**
```bash
cd luxestay
python -m pip install -r requirements.txt
```

**2. App run ചെയ്യുക:**
```bash
python app.py
```

**3. Browser-ൽ തുറക്കുക:** `http://127.0.0.1:5000`

That's it! 🎉 (SQLite database automatic ആയി ഉണ്ടാകും, 3 sample rooms-ഉം.)

---

## 🔑 Admin login

| | |
|--------|------------------------|
| Email  | `admin@luxestay.com`   |
| Password | `admin123`           |

Login ചെയ്യുമ്പോൾ OTP code terminal-ലും screen-ലും കാണാം (dev mode).
കയറിയ ശേഷം **Admin** menu-ൽ rooms add ചെയ്യാം, photo upload ചെയ്യാം, bookings/users നോക്കാം.

---

## 📱 SMS OTP — Twilio വെക്കാൻ (real SMS-ന്)

Dev mode-ൽ OTP screen-ൽ കാണിക്കും. Real SMS വേണമെങ്കിൽ:

1. [twilio.com](https://www.twilio.com/console)-ൽ free account → SID, Auth Token, ഒരു phone number കിട്ടും.
2. `.env.example` → `.env` ആയി rename ചെയ്യുക.
3. ഈ വരികൾ fill ചെയ്യുക:
   ```
   TWILIO_ACCOUNT_SID=ACxxxxxxxx
   TWILIO_AUTH_TOKEN=xxxxxxxx
   TWILIO_FROM_NUMBER=+1xxxxxxxxxx
   ```
4. `python app.py` restart ചെയ്യുക → ഇനി OTP user-ന്റെ phone-ലേക്ക് SMS ആയി പോകും.

---

## 💳 Stripe payment വെക്കാൻ (real card payment-ന്)

Dev mode-ൽ demo checkout ആണ്. Real Stripe payment വേണമെങ്കിൽ:

1. [dashboard.stripe.com/test/apikeys](https://dashboard.stripe.com/test/apikeys)-ൽ **free test keys** എടുക്കുക.
2. `.env`-ൽ:
   ```
   STRIPE_SECRET_KEY=sk_test_xxxxxxxx
   STRIPE_PUBLISHABLE_KEY=pk_test_xxxxxxxx
   ```
3. restart ചെയ്യുക → "Reserve" → dates → Stripe-ന്റെ secure checkout page-ലേക്ക് പോകും.
4. Test card: `4242 4242 4242 4242`, any future expiry, any CVC.

---

## 🏷️ Logo

`static/` folder-ൽ `logo.png` ഇട്ടാൽ header-ൽ "LuxeStay" text-ന് പകരം logo വരും.

---

## 📁 Structure

```
luxestay/
├── app.py                 # backend: auth, OTP, Stripe, bookings, admin
├── requirements.txt
├── .env.example           # -> rename to .env and add keys
├── static/
│   ├── css/style.css      # LuxeStay design system
│   └── uploads/           # room photos
└── templates/
    ├── base.html, index.html (slider), rooms.html, room_detail.html
    ├── booking.html (calendar), checkout_demo.html, booking_success.html
    ├── login.html, verify_otp.html, register.html, dashboard.html
    └── admin/ (dashboard, rooms, room_form, bookings, users)
```

---

## 🛠️ Tech

Flask · Flask-SQLAlchemy · Flask-Login · Stripe · Twilio · flatpickr (calendar) · SQLite

> To start fresh: delete `luxestay.db` and run again.

Enjoy! 🌟
