"""
RigaNest — Premium Hotel Booking
Full Flask app: user accounts, Gmail email OTP login, Stripe payments,
calendar booking, admin panel, image uploads, homepage slider.

Run:  python app.py   ->  http://127.0.0.1:5000

Config (optional) via environment variables or a .env file:
  STRIPE_SECRET_KEY, STRIPE_PUBLISHABLE_KEY   -> real card payments
  RESEND_API_KEY, RESEND_FROM_EMAIL           -> real email OTP via Resend API (recommended,
                                                  works even on hosts that block outbound SMTP)
  GMAIL_ADDRESS, GMAIL_APP_PASSWORD           -> real Gmail email OTP (fallback, needs SMTP access)
If none of these are set, the app runs in DEV MODE:
  - payments are auto-approved (no real charge)
  - the OTP code is printed to the terminal AND shown on screen
"""

import os
import json
import cloudinary
import cloudinary.uploader
import random
import smtplib
import requests
from email.mime.text import MIMEText
from datetime import datetime, date, timedelta
import secrets
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, abort, session
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from authlib.integrations.flask_client import OAuth

# Load a .env file if python-dotenv is available (optional)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
SLIDES_FOLDER = os.path.join(BASE_DIR, "static", "slides")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "onboarding@resend.dev")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY", "")
CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "")
CLOUDINARY_ENABLED = bool(CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET)

PAYMENTS_LIVE = bool(STRIPE_SECRET_KEY)
RESEND_ENABLED = bool(RESEND_API_KEY)
EMAIL_LIVE = bool(RESEND_ENABLED or (GMAIL_ADDRESS and GMAIL_APP_PASSWORD))
GOOGLE_LOGIN_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

# WhatsApp contact number shown as a floating button site-wide and on the
# room detail page. Stored as digits-only (wa.me format) + a display form.
WHATSAPP_NUMBER = os.environ.get("WHATSAPP_NUMBER", "+371 28 458 050")
WHATSAPP_NUMBER_DIGITS = "".join(ch for ch in WHATSAPP_NUMBER if ch.isdigit())

app = Flask(__name__)

# Configure Cloudinary if keys are available
if CLOUDINARY_ENABLED:
    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET,
        secure=True
    )

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-key")
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "riganest.db"))
# Render.com gives postgres:// but SQLAlchemy needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(SLIDES_FOLDER, exist_ok=True)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

# Stripe (only import/configure if a key is present)
stripe = None
if PAYMENTS_LIVE:
    try:
        import stripe as _stripe
        _stripe.api_key = STRIPE_SECRET_KEY
        stripe = _stripe
    except Exception as e:
        print("Stripe not available:", e)
        PAYMENTS_LIVE = False

# Google OAuth ("Continue with Google") — only registered if keys are present
oauth = OAuth(app)
if GOOGLE_LOGIN_ENABLED:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


# ----------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    phone = db.Column(db.String(30), default="")
    password_hash = db.Column(db.String(255), nullable=True)
    is_admin = db.Column(db.Boolean, default=False)
    google_id = db.Column(db.String(120), default="")
    otp_code = db.Column(db.String(6), default="")
    otp_expiry = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    bookings = db.relationship("Booking", backref="user", cascade="all, delete-orphan")

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, pw)


FEATURE_CATALOG = {
    "amenities": [
        ("heating", "Heating", "Apkure"),
        ("air_conditioning", "Air conditioning", "Gaisa kondicionieris"),
        ("iron_board", "Iron board", "Gludināmais dēlis"),
        ("hair_dryer", "Hair dryer", "Fēns"),
        ("linens", "Linens", "Gultas veļa"),
        ("towels", "Towels", "Dvieļi"),
        ("smoke_detector", "Smoke detector", "Dūmu detektors"),
        ("daily_housekeeping", "Daily housekeeping", "Ikdienas uzkopšana"),
        ("reception_247", "24/7 reception", "Reģistratūra 24/7"),
        ("elevator", "Elevator", "Lifts"),
        ("river_view", "River view", "Skats uz upi"),
        ("balcony", "Balcony", "Balkons"),
        ("parking", "Parking", "Autostāvvieta"),
        ("washer", "Washer", "Veļas mašīna"),
    ],
    "entertainment": [
        ("smart_tv", "Smart TV with streaming", "Viedā TV ar straumēšanu"),
        ("streaming", "Streaming services", "Straumēšanas pakalpojumi"),
        ("board_games", "Board games", "Galda spēles"),
        ("books", "Books", "Grāmatas"),
        ("sound_system", "Sound system", "Skaņas sistēma"),
    ],
    "internet": [
        ("free_wifi", "Free high-speed WiFi", "Bezmaksas ātrgaitas WiFi"),
        ("wired_internet", "Wired internet", "Vadu internets"),
        ("workspace", "Dedicated workspace", "Darba vieta"),
    ],
    "kitchen": [
        ("full_kitchen", "Full kitchen", "Pilna virtuve"),
        ("kitchenette", "Kitchenette", "Neliela virtuve"),
        ("refrigerator", "Refrigerator", "Ledusskapis"),
        ("microwave", "Microwave", "Mikroviļņu krāsns"),
        ("dishwasher", "Dishwasher", "Trauku mazgājamā mašīna"),
        ("kettle_coffee", "Kettle & coffee maker", "Tējkanna un kafijas automāts"),
        ("minibar", "Minibar", "Minibārs"),
        ("breakfast", "Breakfast available", "Pieejamas brokastis"),
        ("water", "Complimentary water", "Bezmaksas ūdens"),
    ],
    "pets": [
        ("pets_allowed", "Pets allowed", "Mājdzīvnieki atļauti"),
        ("pet_bowl", "Pet bowl provided", "Bļoda mājdzīvniekiem"),
        ("pet_walk_area", "Nearby pet walking area", "Tuvumā pastaigu vieta mājdzīvniekiem"),
    ],
    "suitability": [
        ("non_smoking", "Non-smoking", "Nesmēķētāju telpa"),
        ("families_welcome", "Families welcome", "Ģimenes gaidītas"),
        ("child_friendly", "Suitable for children", "Piemērots bērniem"),
        ("wheelchair_accessible", "Wheelchair accessible", "Piemērots ratiņkrēslu lietotājiem"),
        ("no_parties", "Parties not allowed", "Ballītes nav atļautas"),
        ("long_stays", "Long-term stays welcome", "Iespējama ilgtermiņa uzturēšanās"),
    ],
}

FEATURE_CATEGORY_LABELS = {
    "amenities": ("Amenities", "Ērtības"),
    "entertainment": ("Entertainment", "Izklaide"),
    "internet": ("Internet", "Internets"),
    "kitchen": ("Kitchen", "Virtuve"),
    "pets": ("Pets", "Mājdzīvnieki"),
    "suitability": ("Suitability", "Piemērotība"),
}

app.jinja_env.globals.update(
    FEATURE_CATALOG=FEATURE_CATALOG,
    FEATURE_CATEGORY_LABELS=FEATURE_CATEGORY_LABELS,
)


class Room(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    room_type = db.Column(db.String(80), nullable=False)
    description = db.Column(db.Text, default="")
    price = db.Column(db.Float, nullable=False)
    capacity = db.Column(db.Integer, default=2)
    beds = db.Column(db.Integer, default=1)
    size_sqm = db.Column(db.Integer, default=30)
    bathrooms = db.Column(db.Integer, default=1)
    rating = db.Column(db.Float, default=4.8)
    amenities = db.Column(db.String(255), default="WiFi, AC, Breakfast")
    features = db.Column(db.Text, default="{}")  # JSON: {"amenities": ["heating", ...], "kitchen": [...], ...}
    image = db.Column(db.String(255), default="")
    location = db.Column(db.String(200), default="")
    beds24_room_id = db.Column(db.Integer, nullable=True)
    beds24_property_id = db.Column(db.Integer, nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    bookings = db.relationship("Booking", backref="room", cascade="all, delete-orphan")
    photos = db.relationship("RoomImage", backref="room", cascade="all, delete-orphan")

    def image_url(self):
        if not self.image:
            return None
        if self.image.startswith("http"):
            return self.image
        return url_for("static", filename="uploads/" + self.image)

    def amenity_list(self):
        return [a.strip() for a in self.amenities.split(",") if a.strip()]

    def features_dict(self):
        """Return the saved feature tags as {category: [tag_key, ...]}."""
        try:
            data = json.loads(self.features or "{}")
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

    def feature_tags(self, category):
        """List of selected tag keys for one category (e.g. 'kitchen')."""
        return self.features_dict().get(category, [])

    def has_feature(self, category, key):
        return key in self.feature_tags(category)

    def photo_urls(self):
        """Extra photos for the slideshow only. Cover shown separately."""
        urls = []
        for p in self.photos:
            if p.filename.startswith("http"):
                urls.append(p.filename)
            else:
                urls.append(url_for("static", filename=p.filename))
        if not urls:
            cover = self.image_url()
            if cover:
                urls.append(cover)
        return urls


class Booking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    room_id = db.Column(db.Integer, db.ForeignKey("room.id"), nullable=False)
    check_in = db.Column(db.Date, nullable=False)
    check_out = db.Column(db.Date, nullable=False)
    guests = db.Column(db.Integer, default=1)
    total_price = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(40), default="Pending")
    payment_status = db.Column(db.String(40), default="Unpaid")
    stripe_session_id = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    beds24_booking_id = db.Column(db.Integer, nullable=True)

    def nights(self):
        return (self.check_out - self.check_in).days


class RoomAvailability(db.Model):
    """Per-date price/availability synced from Beds24. A row here
    overrides Room.price and adds an extra availability check for
    that specific room + date."""
    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey("room.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    price = db.Column(db.Float, nullable=True)
    available = db.Column(db.Boolean, default=True)


class RoomImage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey("room.id"), nullable=False)
    filename = db.Column(db.String(255), nullable=False)


class Attraction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    summary = db.Column(db.Text, default="")
    image = db.Column(db.String(255), default="")
    location = db.Column(db.String(200), default="")
    allow_children = db.Column(db.Boolean, default=True)
    allow_smoking = db.Column(db.Boolean, default=False)
    allow_pets = db.Column(db.Boolean, default=False)
    has_parking = db.Column(db.Boolean, default=True)
    has_handicap = db.Column(db.Boolean, default=True)
    opening_time = db.Column(db.String(50), default="All Day")
    closing_time = db.Column(db.String(50), default="All Day")
    adult_price = db.Column(db.String(40), default="Free")
    children_price = db.Column(db.String(40), default="Free")
    oap_price = db.Column(db.String(40), default="Free")
    booking_url = db.Column(db.String(255), default="")
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    photos = db.relationship("AttractionImage", backref="attraction", cascade="all, delete-orphan")

    def image_url(self):
        if not self.image:
            return None
        if self.image.startswith("http"):
            return self.image
        if "/" in self.image:
            return url_for("static", filename=self.image)
        return url_for("static", filename="uploads/" + self.image)

    def photo_urls(self):
        """All photos for the slideshow: extra photos + cover, fallback to cover only."""
        urls = []
        for p in self.photos:
            if p.filename.startswith("http"):
                urls.append(p.filename)
            else:
                urls.append(url_for("static", filename=p.filename))
        cover = self.image_url()
        if cover and cover not in urls:
            urls.insert(0, cover)
        return urls


class AttractionImage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    attraction_id = db.Column(db.Integer, db.ForeignKey("attraction.id"), nullable=False)
    filename = db.Column(db.String(255), nullable=False)


class ContactMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    subject = db.Column(db.String(200), default="")
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)




class SiteAbout(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), default="About Us")
    hero_subtitle = db.Column(db.String(200), default="Our Story")
    content = db.Column(db.Text, default="")
    image = db.Column(db.String(255), default="")
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    def image_url(self):
        if not self.image:
            return None
        if self.image.startswith("http"):
            return self.image
        return url_for("static", filename="uploads/" + self.image)



class SitePage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), unique=True, nullable=False)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, default="")
    image = db.Column(db.String(255), default="")
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
    def image_url(self):
        if not self.image:
            return None
        if self.image.startswith("http"):
            return self.image
        return url_for("static", filename="uploads/" + self.image)

@login_manager.user_loader
def load_user(uid):
    return db.session.get(User, int(uid))


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def allowed_file(fn):
    return "." in fn and fn.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_banner_images():
    if not os.path.isdir(SLIDES_FOLDER):
        return []
    files = [f for f in os.listdir(SLIDES_FOLDER) if allowed_file(f)]
    files.sort()
    return files


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*a, **k):
        if not current_user.is_admin:
            abort(403)
        return view(*a, **k)
    return wrapped


def parse_date(v):
    try:
        return datetime.strptime(v, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def is_available(room_id, ci, co, exclude=None):
    q = Booking.query.filter(
        Booking.room_id == room_id,
        Booking.status.in_(["Confirmed", "Pending"]),
        Booking.check_in < co, Booking.check_out > ci,
    )
    if exclude:
        q = q.filter(Booking.id != exclude)
    if q.first() is not None:
        return False

    blocked = RoomAvailability.query.filter(
        RoomAvailability.room_id == room_id,
        RoomAvailability.date >= ci,
        RoomAvailability.date < co,
        RoomAvailability.available.is_(False),
    ).first()
    return blocked is None


def calc_total_price(room, ci, co):
    """Use Beds24-synced per-night prices if we have them for every
    night of the stay; otherwise fall back to the flat room price."""
    rows = {
        r.date: r.price
        for r in RoomAvailability.query.filter(
            RoomAvailability.room_id == room.id,
            RoomAvailability.date >= ci,
            RoomAvailability.date < co,
            RoomAvailability.price.isnot(None),
        ).all()
    }
    nights = (co - ci).days
    if not rows:
        return nights * room.price
    total = 0.0
    d = ci
    while d < co:
        total += rows.get(d, room.price)
        d += timedelta(days=1)
    return total


def send_otp(user):
    code = f"{random.randint(0, 999999):06d}"
    user.otp_code = code
    user.otp_expiry = datetime.utcnow() + timedelta(minutes=5)
    db.session.commit()

    subject = "Your RigaNest verification code"
    body = (f"Hi {user.name},\n\n"
            f"Your RigaNest verification code is: {code}\n"
            f"This code expires in 5 minutes.\n\n"
            f"If you didn't request this, you can ignore this email.")
    html_body = (f"<p>Hi {user.name},</p>"
                 f"<p>Your RigaNest verification code is: <strong>{code}</strong></p>"
                 f"<p>This code expires in 5 minutes.</p>"
                 f"<p>If you didn't request this, you can ignore this email.</p>")

    # 1) Try Resend first (HTTP API, works reliably even on hosts that
    #    block outbound SMTP ports, e.g. Render's free tier).
    if RESEND_ENABLED and user.email:
        try:
            resp = requests.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": f"RigaNest <{RESEND_FROM_EMAIL}>",
                    "to": [user.email],
                    "subject": subject,
                    "html": html_body,
                },
                timeout=10,
            )
            if resp.status_code in (200, 201):
                return None
            print("Resend send failed:", resp.status_code, resp.text)
        except Exception as e:
            print("Resend send error:", e)

    # 2) Fallback: Gmail SMTP (works on hosts that allow outbound SMTP,
    #    e.g. paid Render plans, VPS, shared hosting).
    if GMAIL_ADDRESS and GMAIL_APP_PASSWORD and user.email:
        try:
            msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"] = GMAIL_ADDRESS
            msg["To"] = user.email
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
                server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
                server.sendmail(GMAIL_ADDRESS, [user.email], msg.as_string())
            return None
        except Exception as e:
            print("Gmail send failed:", e)

    print(f"\n*** [DEV OTP] {user.email} -> code: {code} ***\n")
    return code


# ── FIXED: generic upload_image — works for both Room and Attraction ──
def upload_image(obj, folder="riganest/rooms"):
    f = request.files.get("image")
    if f and f.filename and allowed_file(f.filename):
        if CLOUDINARY_ENABLED:
            result = cloudinary.uploader.upload(f, folder=folder)
            obj.image = result["secure_url"]
        else:
            fn = f"{int(datetime.utcnow().timestamp())}_{secure_filename(f.filename)}"
            f.save(os.path.join(app.config["UPLOAD_FOLDER"], fn))
            obj.image = fn


def save_room_features(room, form):
    """Read the per-category checkbox groups from the room form
    (fields named '<category>_tags', e.g. 'kitchen_tags') and store
    them as JSON on room.features. Valid tag keys only (ignore junk)."""
    data = {}
    for category, tags in FEATURE_CATALOG.items():
        valid_keys = {key for key, _, _ in tags}
        selected = [k for k in form.getlist(f"{category}_tags") if k in valid_keys]
        if selected:
            data[category] = selected
    room.features = json.dumps(data)


def upload_extra_photos(room):
    files = request.files.getlist("photos")
    for i, f in enumerate(files):
        if f and f.filename and allowed_file(f.filename):
            if CLOUDINARY_ENABLED:
                result = cloudinary.uploader.upload(f, folder="riganest/rooms")
                db.session.add(RoomImage(room_id=room.id, filename=result["secure_url"]))
            else:
                fn = f"{int(datetime.utcnow().timestamp())}_{i}_{secure_filename(f.filename)}"
                f.save(os.path.join(app.config["UPLOAD_FOLDER"], fn))
                db.session.add(RoomImage(room_id=room.id, filename="uploads/" + fn))


def upload_attraction_photos(attraction):
    files = request.files.getlist("photos")
    for i, f in enumerate(files):
        if f and f.filename and allowed_file(f.filename):
            if CLOUDINARY_ENABLED:
                result = cloudinary.uploader.upload(f, folder="riganest/attractions")
                db.session.add(AttractionImage(attraction_id=attraction.id, filename=result["secure_url"]))
            else:
                fn = f"{int(datetime.utcnow().timestamp())}_{i}_{secure_filename(f.filename)}"
                f.save(os.path.join(app.config["UPLOAD_FOLDER"], fn))
                db.session.add(AttractionImage(attraction_id=attraction.id, filename="uploads/" + fn))


# ----------------------------------------------------------------------
# Public pages
# ----------------------------------------------------------------------
@app.route("/")
def index():
    rooms = Room.query.filter_by(is_active=True).order_by(Room.rating.desc()).limit(8).all()
    slides = [url_for("static", filename=f"slides/{fn}") for fn in get_banner_images()]
    gallery = Attraction.query.filter_by(is_active=True).order_by(Attraction.created_at.desc()).limit(8).all()
    today_str = date.today().isoformat()
    tomorrow_str = (date.today() + timedelta(days=1)).isoformat()
    return render_template("index.html", rooms=rooms, slides=slides, gallery=gallery,
                           today=today_str, tomorrow=tomorrow_str)


@app.route("/rooms")
def rooms():
    q = request.args.get("q", "").strip()
    max_price = request.args.get("max_price", type=float)
    guests = request.args.get("guests", type=int)
    check_in_str = request.args.get("check_in", "").strip()
    check_out_str = request.args.get("check_out", "").strip()

    check_in = check_out = None
    date_error = None
    if check_in_str and check_out_str:
        try:
            check_in = datetime.strptime(check_in_str, "%Y-%m-%d").date()
            check_out = datetime.strptime(check_out_str, "%Y-%m-%d").date()
            if check_out <= check_in:
                date_error = "Check-out date must be after the check-in date."
                check_in = check_out = None
        except ValueError:
            date_error = "Please enter valid dates."

    query = Room.query.filter_by(is_active=True)
    if q:
        query = query.filter(Room.name.ilike(f"%{q}%") | Room.room_type.ilike(f"%{q}%"))
    if max_price:
        query = query.filter(Room.price <= max_price)
    if guests:
        query = query.filter(Room.capacity >= guests)
    listings = query.order_by(Room.price.asc()).all()

    if check_in and check_out:
        available = []
        for r in listings:
            has_conflict = any(
                b.status in ("Confirmed", "Pending")
                and check_in < b.check_out and check_out > b.check_in
                for b in r.bookings
            )
            if not has_conflict:
                available.append(r)
        listings = available

    return render_template("rooms.html", rooms=listings, q=q,
                           max_price=max_price, guests=guests,
                           check_in=check_in_str, check_out=check_out_str,
                           date_error=date_error)


@app.route("/room/<int:room_id>")
def room_detail(room_id):
    room = Room.query.get_or_404(room_id)
    booked = [{"from": b.check_in.isoformat(), "to": b.check_out.isoformat()}
              for b in room.bookings if b.status in ("Confirmed", "Pending")]

    # Also block out dates Beds24 says are unavailable (synced via beds24_sync.py
    # into RoomAvailability), so the calendar reflects real-world bookings made
    # through other channels (Airbnb, Booking.com, etc.), not just our own site.
    blocked_dates = [
        r.date for r in RoomAvailability.query.filter(
            RoomAvailability.room_id == room.id,
            RoomAvailability.available.is_(False),
        ).order_by(RoomAvailability.date).all()
    ]
    if blocked_dates:
        start = prev = blocked_dates[0]
        for d in blocked_dates[1:]:
            if (d - prev).days == 1:
                prev = d
                continue
            booked.append({"from": start.isoformat(), "to": (prev + timedelta(days=1)).isoformat()})
            start = prev = d
        booked.append({"from": start.isoformat(), "to": (prev + timedelta(days=1)).isoformat()})

    photos = room.photo_urls()

    related = Room.query.filter(
        Room.id != room.id,
        Room.is_active.is_(True),
        Room.room_type == room.room_type,
    ).order_by(Room.rating.desc()).limit(3).all()

    if len(related) < 3:
        exclude_ids = [room.id] + [r.id for r in related]
        related += Room.query.filter(
            Room.id.notin_(exclude_ids),
            Room.is_active.is_(True),
        ).order_by(Room.rating.desc()).limit(3 - len(related)).all()

    return render_template("room_detail.html", room=room,
                           booked_ranges=booked, photos=photos,
                           related_rooms=related)


@app.route("/attractions")
def attractions():
    listings = Attraction.query.filter_by(is_active=True).order_by(Attraction.created_at.desc()).all()
    return render_template("attractions.html", attractions=listings)


@app.route("/attraction/<int:attraction_id>")
def attraction_detail(attraction_id):
    a = Attraction.query.get_or_404(attraction_id)
    photos = a.photo_urls()
    related = Attraction.query.filter(
        Attraction.id != a.id, Attraction.is_active.is_(True)
    ).order_by(Attraction.created_at.desc()).limit(3).all()
    return render_template("attraction_detail.html", attraction=a, photos=photos,
                           related_attractions=related)


@app.route("/about")
def about():
    about = SiteAbout.query.first()
    return render_template("about.html", about=about)


@app.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        subject = request.form.get("subject", "").strip()
        message = request.form.get("message", "").strip()
        if name and email and message:
            db.session.add(ContactMessage(name=name, email=email,
                                          subject=subject, message=message))
            db.session.commit()
            flash("Thanks! Your message has been sent.", "success")
            return redirect(url_for("contact"))
        flash("Please fill in your name, email and message.", "error")
    return render_template("contact.html")


# ----------------------------------------------------------------------
# Auth + Email OTP (Gmail)
# ----------------------------------------------------------------------
@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        name = f"{first_name} {last_name}".strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        pw = request.form.get("password", "")
        pw_confirm = request.form.get("password_confirm", "")
        accept_terms = request.form.get("accept_terms")

        if not all([first_name, last_name, email, pw, pw_confirm]):
            flash("Please fill in every required field.", "error")
        elif len(pw) < 8:
            flash("Password must be at least 8 characters.", "error")
        elif pw != pw_confirm:
            flash("Passwords do not match.", "error")
        elif not accept_terms:
            flash("Please accept the Terms & Conditions and Privacy Policy.", "error")
        elif User.query.filter_by(email=email).first():
            flash("That email is already registered.", "error")
        else:
            u = User(name=name, email=email, phone=phone)
            u.set_password(pw)
            db.session.add(u)
            db.session.commit()
            session["pending_user"] = u.id
            session["new_registration"] = True
            dev_code = send_otp(u)
            session["dev_otp"] = dev_code
            flash("Account created! Enter the code we emailed you to finish signing in.", "success")
            return redirect(url_for("verify_otp"))
    return render_template("register.html", google_login_enabled=GOOGLE_LOGIN_ENABLED)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        u = User.query.filter_by(email=email).first()
        if u and u.check_password(pw):
            login_user(u)
            flash("Logged in successfully.", "success")
            nxt = session.pop("next_url", None)
            return redirect(nxt or url_for("dashboard"))
        flash("Email or password is incorrect.", "error")
    return render_template("login.html", google_login_enabled=GOOGLE_LOGIN_ENABLED)


@app.route("/verify-otp", methods=["GET", "POST"])
def verify_otp():
    uid = session.get("pending_user")
    if not uid:
        return redirect(url_for("login"))
    u = db.session.get(User, uid)
    if not u:
        session.pop("pending_user", None)
        return redirect(url_for("login"))

    if request.method == "POST":
        entered = request.form.get("otp", "").strip()
        if u.otp_code and entered == u.otp_code and u.otp_expiry and datetime.utcnow() < u.otp_expiry:
            u.otp_code = ""
            db.session.commit()
            is_new_account = session.get("new_registration", False)
            login_user(u)
            session.pop("pending_user", None)
            session.pop("dev_otp", None)
            session.pop("new_registration", None)
            if is_new_account:
                flash("Welcome to RigaNest! Your account is ready.", "success")
            else:
                flash("Logged in successfully.", "success")
            nxt = session.pop("next_url", None)
            return redirect(nxt or url_for("dashboard"))
        flash("Invalid or expired code. Please try again.", "error")

    masked = (u.email[:2] + "•" * max(0, len(u.email.split('@')[0]) - 2) + "@" + u.email.split('@')[1]) if u.email and '@' in u.email else "your email"
    return render_template("verify_otp.html", email=masked,
                           dev_otp=session.get("dev_otp"), email_live=EMAIL_LIVE)


@app.route("/resend-otp")
def resend_otp():
    uid = session.get("pending_user")
    if not uid:
        return redirect(url_for("login"))
    u = db.session.get(User, uid)
    if u:
        session["dev_otp"] = send_otp(u)
        flash("A new code has been sent.", "info")
    return redirect(url_for("verify_otp"))


# ----------------------------------------------------------------------
# "Continue with Google" sign-in
# ----------------------------------------------------------------------
@app.route("/login/google")
def google_login():
    if not GOOGLE_LOGIN_ENABLED:
        flash("Google sign-in is not configured yet.", "error")
        return redirect(url_for("login"))
    redirect_uri = url_for("google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/login/google/callback")
def google_callback():
    if not GOOGLE_LOGIN_ENABLED:
        return redirect(url_for("login"))
    try:
        token = oauth.google.authorize_access_token()
        info = token.get("userinfo") or oauth.google.parse_id_token(token)
    except Exception as e:
        flash(f"Google sign-in failed: {e}", "error")
        return redirect(url_for("login"))

    google_id = info.get("sub")
    email = (info.get("email") or "").strip().lower()
    name = info.get("name") or (email.split("@")[0] if email else "Google User")
    if not email:
        flash("Could not get an email address from Google.", "error")
        return redirect(url_for("login"))

    u = User.query.filter_by(email=email).first()
    if u:
        if not u.google_id:
            u.google_id = google_id
            db.session.commit()
    else:
        u = User(name=name, email=email, google_id=google_id)
        db.session.add(u)
        db.session.commit()

    login_user(u)
    nxt = session.pop("next_url", None)
    flash("Logged in with Google.", "success")
    return redirect(nxt or url_for("dashboard"))


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been signed out.", "success")
    return redirect(url_for("index"))


# ----------------------------------------------------------------------
# Booking + Stripe payment
# ----------------------------------------------------------------------
@app.route("/book/<int:room_id>", methods=["GET", "POST"])
def booking(room_id):
    room = Room.query.get_or_404(room_id)
    if not current_user.is_authenticated:
        session["next_url"] = url_for("booking", room_id=room_id)
        flash("Please sign in to book.", "info")
        return redirect(url_for("login"))

    if request.method == "POST":
        ci = parse_date(request.form.get("check_in"))
        co = parse_date(request.form.get("check_out"))
        guests = request.form.get("guests", 1, type=int)
        if not ci or not co:
            flash("Choose valid dates.", "error")
        elif ci < date.today():
            flash("Check-in cannot be in the past.", "error")
        elif co <= ci:
            flash("Check-out must be after check-in.", "error")
        elif guests > room.capacity:
            flash(f"This room holds up to {room.capacity} guests.", "error")
        elif not is_available(room_id, ci, co):
            flash("Those dates are already booked.", "error")
        else:
            nights = (co - ci).days
            bk = Booking(user_id=current_user.id, room_id=room.id, check_in=ci,
                         check_out=co, guests=guests,
                         total_price=calc_total_price(room, ci, co))
            db.session.add(bk)
            db.session.commit()
            return redirect(url_for("checkout", booking_id=bk.id))
    return render_template("booking.html", room=room)


@app.route("/checkout/<int:booking_id>")
@login_required
def checkout(booking_id):
    bk = Booking.query.get_or_404(booking_id)
    if bk.user_id != current_user.id:
        abort(403)

    if PAYMENTS_LIVE and stripe:
        try:
            sess = stripe.checkout.Session.create(
                mode="payment",
                line_items=[{
                    "price_data": {
                        "currency": "eur",
                        "product_data": {"name": f"{bk.room.name} ({bk.nights()} nights)"},
                        "unit_amount": int(bk.total_price * 100),
                    },
                    "quantity": 1,
                }],
                success_url=url_for("booking_success", booking_id=bk.id, _external=True)
                + "?session_id={CHECKOUT_SESSION_ID}",
                cancel_url=url_for("booking_cancel", booking_id=bk.id, _external=True),
            )
            bk.stripe_session_id = sess.id
            db.session.commit()
            return redirect(sess.url, code=303)
        except Exception as e:
            flash(f"Payment error: {e}", "error")
            return redirect(url_for("dashboard"))

    return render_template("checkout_demo.html", booking=bk,
                           publishable_key=STRIPE_PUBLISHABLE_KEY)


@app.route("/beds24-return")
def beds24_return():
    """
    Beds24's booking engine redirects the guest here after a booking is
    made (SETTINGS > BOOKING ENGINE > PROPERTY BOOKING PAGE > BEHAVIOUR
    > Booking Return URL must be set to this route's full URL).

    Beds24 appends the booking details as query parameters. We use them
    to: 1) record the booking in our own database so it shows on the
    guest's dashboard and in /admin/bookings, and 2) immediately mark
    those dates unavailable in RoomAvailability so our own site's
    calendar reflects it right away, without waiting for the next
    scheduled beds24_sync.py run.
    """
    bookid = request.args.get("bookid")
    roomid = request.args.get("roomid", type=int)

    if not bookid or not roomid:
        flash("Booking confirmation link is missing some details. If you completed a payment, contact us to confirm.", "info")
        return redirect(url_for("index"))

    # Avoid creating a duplicate local booking if this page is reloaded/revisited
    existing = Booking.query.filter_by(beds24_booking_id=bookid).first()
    if existing:
        return redirect(url_for("booking_success", booking_id=existing.id))

    room = Room.query.filter_by(beds24_room_id=roomid).first()
    if not room:
        flash("Your Beds24 booking is confirmed, but we couldn't automatically match it to a room on our site. Contact us if anything looks wrong.", "info")
        return redirect(url_for("index"))

    ci = parse_date(request.args.get("firstnight"))
    co = parse_date(request.args.get("checkout"))
    if not co:
        last = parse_date(request.args.get("lastnight"))
        if last:
            co = last + timedelta(days=1)
    if not ci or not co:
        flash("Your Beds24 booking is confirmed! We couldn't read the exact dates here, but your booking stands.", "success")
        return redirect(url_for("index"))

    guests = request.args.get("numadult", 1, type=int)
    try:
        price = float(request.args.get("price", 0) or 0)
    except ValueError:
        price = 0.0
    email = (request.args.get("guestemail") or "").strip()
    first_name = (request.args.get("guestfirstname") or "Guest").strip()
    last_name = (request.args.get("guestname") or "").strip()

    user = current_user._get_current_object() if current_user.is_authenticated else None
    if not user and email:
        user = User.query.filter_by(email=email).first()
        if not user:
            user = User(name=(first_name + " " + last_name).strip() or "Guest",
                        email=email, phone="",
                        password_hash=generate_password_hash(secrets.token_hex(16)))
            db.session.add(user)
            db.session.commit()

    if not user:
        flash("Your booking on Beds24 is confirmed! Sign in or contact us to link it to your RigaNest account.", "success")
        return redirect(url_for("index"))

    bk = Booking(user_id=user.id, room_id=room.id, check_in=ci, check_out=co,
                 guests=guests, total_price=price, status="Confirmed",
                 payment_status="Paid", beds24_booking_id=bookid)
    db.session.add(bk)
    db.session.commit()

    d = ci
    while d < co:
        row = RoomAvailability.query.filter_by(room_id=room.id, date=d).first()
        if not row:
            row = RoomAvailability(room_id=room.id, date=d)
            db.session.add(row)
        row.available = False
        d += timedelta(days=1)
    db.session.commit()

    if not current_user.is_authenticated:
        login_user(user)

    flash("Your booking is confirmed!", "success")
    return redirect(url_for("booking_success", booking_id=bk.id))


@app.route("/booking/<int:booking_id>/success")
@login_required
def booking_success(booking_id):
    bk = Booking.query.get_or_404(booking_id)
    if bk.user_id != current_user.id:
        abort(403)
    bk.status = "Confirmed"
    bk.payment_status = "Paid"
    db.session.commit()

    if not bk.beds24_booking_id:
        try:
            from beds24_booking import push_booking
            new_id = push_booking(bk)
            if new_id:
                bk.beds24_booking_id = new_id
                db.session.commit()
        except Exception as e:
            print(f"[beds24] Could not push booking #{bk.id}: {e}")

    return render_template("booking_success.html", booking=bk)


@app.route("/booking/<int:booking_id>/cancel")
@login_required
def booking_cancel(booking_id):
    bk = Booking.query.get_or_404(booking_id)
    if bk.user_id != current_user.id:
        abort(403)
    if bk.payment_status == "Unpaid":
        db.session.delete(bk)
        db.session.commit()
        flash("Payment cancelled — booking not saved.", "info")
    return redirect(url_for("room_detail", room_id=bk.room_id))


@app.route("/dashboard")
@login_required
def dashboard():
    bks = Booking.query.filter_by(user_id=current_user.id).order_by(Booking.check_in.desc()).all()
    return render_template("dashboard.html", bookings=bks, today=date.today())


@app.route("/booking/<int:booking_id>/cancel-confirmed", methods=["POST"])
@login_required
def cancel_confirmed(booking_id):
    bk = Booking.query.get_or_404(booking_id)
    if bk.user_id != current_user.id and not current_user.is_admin:
        abort(403)
    bk.status = "Cancelled"
    db.session.commit()

    if bk.beds24_booking_id:
        try:
            from beds24_booking import cancel_booking
            cancel_booking(bk)
        except Exception as e:
            print(f"[beds24] Could not cancel booking #{bk.id}: {e}")

    flash("Booking cancelled.", "success")
    return redirect(request.referrer or url_for("dashboard"))


# ----------------------------------------------------------------------
# Admin
# ----------------------------------------------------------------------
@app.route("/admin/beds24-debug")
@admin_required
def admin_beds24_debug():
    """
    Read-only debug page (no Render Shell needed on the free tier).
    Shows exactly what the LIVE production database currently has
    stored for each room's beds24_room_id, so we can compare it
    against Beds24's room list and find/remove duplicates.
    """
    rooms = Room.query.order_by(Room.id).all()
    return {
        "total_rooms": len(rooms),
        "mapped": sum(1 for r in rooms if r.beds24_room_id),
        "rooms": [
            {"local_id": r.id, "name": r.name, "beds24_room_id": r.beds24_room_id}
            for r in rooms
        ],
    }


@app.route("/admin/beds24-price-debug")
@admin_required
def admin_beds24_price_debug():
    """
    Shows exactly what the LIVE production database currently has for
    Room.price and the next 7 days of RoomAvailability price/available
    rows, per room. Use this to check whether prices actually made it
    into the production DB (as opposed to only a local copy), and
    whether the values look right.
    """
    today = date.today()
    result = []
    for r in Room.query.order_by(Room.id).all():
        rows = (
            RoomAvailability.query.filter(
                RoomAvailability.room_id == r.id,
                RoomAvailability.date >= today,
            )
            .order_by(RoomAvailability.date)
            .limit(7)
            .all()
        )
        result.append({
            "room_id": r.id,
            "name": r.name,
            "beds24_room_id": r.beds24_room_id,
            "room_price_field": r.price,
            "next_7_days": [
                {"date": row.date.isoformat(), "price": row.price, "available": row.available}
                for row in rows
            ],
            "total_availability_rows": RoomAvailability.query.filter_by(room_id=r.id).count(),
        })
    return {"today": today.isoformat(), "rooms": result}


_beds24_sync_state = {"running": False, "status": "never_run", "log": [], "started_at": None, "finished_at": None}
_beds24_sync_lock = None


def _get_sync_lock():
    global _beds24_sync_lock
    if _beds24_sync_lock is None:
        import threading
        _beds24_sync_lock = threading.Lock()
    return _beds24_sync_lock


def _run_beds24_sync_background():
    import io
    import contextlib
    from beds24_sync import main as sync_main

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            sync_main()
        _beds24_sync_state["status"] = "success"
    except Exception as e:
        buf.write(f"\nERROR: {e}")
        _beds24_sync_state["status"] = "error"
    finally:
        _beds24_sync_state["log"] = buf.getvalue().splitlines()
        _beds24_sync_state["finished_at"] = datetime.utcnow().isoformat()
        _beds24_sync_state["running"] = False


@app.route("/admin/run-beds24-sync")
@admin_required
def admin_run_beds24_sync():
    """
    TEMPORARY debug/trigger route — lets us run beds24_sync.py from the
    browser on Render plans where the Shell tab isn't available.

    IMPORTANT: this used to run the sync directly inside the web
    request, which could take longer than gunicorn's worker timeout
    (120s) on accounts with many rooms / a large BEDS24_SYNC_DAYS
    window. When the worker got killed mid-request, it could leave the
    shared DB connection in a broken state and take the WHOLE SITE
    down (not just this route) until the service was restarted.

    Fix: the sync now runs in a background thread. This request just
    starts it and returns immediately. Check progress/result with
    /admin/beds24-sync-status.

    Once syncing is confirmed working and a proper scheduled job
    (Render Cron Job) is set up to run beds24_sync.py automatically,
    this route can be removed.
    """
    import threading

    lock = _get_sync_lock()
    if not lock.acquire(blocking=False):
        return {"status": "already_running", "message": "A sync is already in progress. Check /admin/beds24-sync-status."}

    def _target():
        try:
            _run_beds24_sync_background()
        finally:
            lock.release()

    _beds24_sync_state["running"] = True
    _beds24_sync_state["status"] = "running"
    _beds24_sync_state["log"] = []
    _beds24_sync_state["started_at"] = datetime.utcnow().isoformat()
    _beds24_sync_state["finished_at"] = None

    threading.Thread(target=_target, daemon=True).start()

    return {
        "status": "started",
        "message": "Sync started in the background. Poll /admin/beds24-sync-status for progress/result.",
    }


@app.route("/admin/beds24-sync-status")
@admin_required
def admin_beds24_sync_status():
    """Check on the background sync started by /admin/run-beds24-sync."""
    return dict(_beds24_sync_state)


@app.route("/admin/fix-beds24-room-id-mismatches")
@admin_required
def admin_fix_beds24_room_id_mismatches():
    """
    ONE-TIME FIX. Comparing our DB's beds24_room_id values against
    Beds24's actual BookinRiga (property 341384) room list turned up 3
    rooms pointing at the wrong Beds24 room id (stale from an earlier
    mapping/cleanup pass) — which is why syncing them returned no price
    data. This corrects those 3 by exact room name match. Safe to
    re-run; it's a no-op once applied. Remove this route afterward.
    """
    fixes = {
        "Kalnina Quiet Apartment in city center": 705436,
        "Old Riga Smilsu street Quiet One Bedroom Apartment": 705446,
        "Kalnina Street Modern Studio Apartment in Riga": 705450,
    }
    results = []
    for name, correct_id in fixes.items():
        room = Room.query.filter_by(name=name).first()
        if not room:
            results.append({"name": name, "status": "not_found_in_db"})
            continue
        old_id = room.beds24_room_id
        if old_id == correct_id:
            results.append({"name": name, "status": "already_correct", "beds24_room_id": correct_id})
            continue
        room.beds24_room_id = correct_id
        results.append({"name": name, "status": "fixed", "old_beds24_room_id": old_id, "new_beds24_room_id": correct_id})

    db.session.commit()
    return {"results": results}


@app.route("/admin/migrate-to-homestate-rooms")
@admin_required
def admin_migrate_to_homestate_rooms():
    """
    ONE-TIME MIGRATION. Switches the room catalog over from the
    BookinRiga (property 341384) Beds24 listing — which turned out to
    have no rate plans configured for many rooms — to the Homestate
    account's individual per-apartment properties, which DO have real
    rates. Source data: data/homestate_rooms.csv (80 rows), built
    earlier from a Beds24 property/room discovery pass.

    What this does, in order:
      1. Deactivates the 13 local rooms that only ever existed under
         BookinRiga (341384) and have no equivalent in the Homestate
         list at all (is_active = False; not deleted, so existing
         bookings referencing them stay intact).
      2. For every row in the CSV: if a Room with that exact name
         already exists, updates its beds24_room_id/property_id,
         price, capacity, size, image, and reactivates it. If no Room
         with that name exists, creates a new one.

    Safe to re-run (idempotent) — matches by room name each time.
    Remove this route once the migration is confirmed correct.
    """
    import csv as csv_module

    csv_path = os.path.join(BASE_DIR, "data", "homestate_rooms.csv")
    if not os.path.exists(csv_path):
        return {"error": f"CSV not found at {csv_path}. Make sure data/homestate_rooms.csv was committed and deployed."}

    # 1) Deactivate the 13 rooms with no Homestate equivalent at all.
    no_homestate_match = [
        "Riga City Center Apartment",
        "Old Riga Cozy One Bedroom Apartment",
        "Old Riga Terrace Apartment",
        "Kalnina Quiet Apartment in city center",
        "Old Riga Palasta Loft Apartment with river view",
        "Kr. Barona iela 24/26 Residential Barona One Bedroom Apartment",
        "Kr. Barona iela 24/26 One Bedroom Apartment",
        "Modern Design Studio Apartment In Riga Center",
        "Brīvības Yard Apartment With Parking In City Center",
        "Riga Riverside Design One Bedroom Apartment",
        "Kungu iela 25 Old Riga Ridzenes Residence Studio Apartment",
        "Old Riga Kaleju Cozy apartment2",
        "Old Riga 2 Bedroom Vecpilsetas street Apartment",
    ]
    deactivated = []
    for name in no_homestate_match:
        room = Room.query.filter(db.func.lower(Room.name) == name.strip().lower()).first()
        if room:
            room.is_active = False
            deactivated.append(name)

    # 2) Sync every Homestate CSV row into the Room table.
    updated, created, skipped = [], [], []
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv_module.DictReader(f)
        for row in reader:
            room_name = (row.get("room_name") or "").strip()
            if not room_name:
                continue

            def _num(key, default=0):
                try:
                    return float(row.get(key) or default)
                except ValueError:
                    return default

            beds24_room_id = int(row["room_id"]) if row.get("room_id") else None
            beds24_property_id = int(row["property_id"]) if row.get("property_id") else None
            price = _num("rack_rate") or _num("min_price") or 50
            capacity = int(_num("max_people", 2)) or 2
            size_sqm = int(_num("room_size_m2", 30)) or 30
            location = ", ".join(x for x in [row.get("address", "").strip(), row.get("city", "").strip()] if x)
            image_urls = [u.strip() for u in (row.get("image_url") or "").split(";") if u.strip()]
            cover_image = image_urls[0] if image_urls else ""

            existing = Room.query.filter(db.func.lower(Room.name) == room_name.lower()).first()
            if existing:
                existing.beds24_room_id = beds24_room_id
                existing.beds24_property_id = beds24_property_id
                existing.price = price
                existing.capacity = capacity
                existing.size_sqm = size_sqm
                if location:
                    existing.location = location
                if cover_image and not existing.image:
                    existing.image = cover_image
                existing.is_active = True
                updated.append(room_name)
            else:
                new_room = Room(
                    name=room_name,
                    room_type="Standard",
                    description=(row.get("description") or "").strip(),
                    price=price,
                    capacity=capacity,
                    beds=1,
                    size_sqm=size_sqm,
                    location=location,
                    beds24_room_id=beds24_room_id,
                    beds24_property_id=beds24_property_id,
                    image=cover_image,
                    is_active=True,
                )
                db.session.add(new_room)
                db.session.flush()  # get new_room.id for RoomImage rows
                for extra_url in image_urls[1:]:
                    db.session.add(RoomImage(room_id=new_room.id, filename=extra_url))
                created.append(room_name)

    db.session.commit()

    return {
        "deactivated_no_homestate_match": deactivated,
        "updated_count": len(updated),
        "updated": updated,
        "created_count": len(created),
        "created": created,
        "total_active_rooms_now": Room.query.filter_by(is_active=True).count(),
    }


@app.route("/admin/cleanup-homestate-duplicates")
@admin_required
def admin_cleanup_homestate_duplicates():
    """
    ONE-TIME CLEANUP, follow-up to /admin/migrate-to-homestate-rooms.

    That migration deactivated 13 rooms it couldn't exact-match by name
    against the Homestate CSV. But 8 of those 13 already had a correct,
    working beds24_room_id set by an earlier (pre-this-conversation)
    mapping pass — the CSV loop just didn't touch them since the name
    didn't match, so they stayed deactivated even though their pricing
    was fine. Meanwhile the CSV loop's own row for that same physical
    Beds24 room created a SEPARATE new Room with a different name,
    pointing at the identical beds24_room_id — a duplicate listing.

    This fixes both sides in one pass, using room names as keys:
      1. Reactivates the 8 originals that turned out to have real
         pricing all along.
      2. Deactivates their newly-created duplicate counterparts.

    The remaining 5 of the original 13 (Riga City Center Apartment,
    Kalnina Quiet Apartment in city center, both Kr. Barona iela 24/26
    apartments, Kungu iela 25 ...) genuinely have no rate plan in
    Beds24 at all — those stay deactivated until fixed in Beds24
    directly. Safe to re-run. Remove this route once confirmed.
    """
    reactivate_names = [
        "Old Riga Cozy One Bedroom Apartment",
        "Old Riga Terrace Apartment",
        "Old Riga Palasta Loft Apartment with river view",
        "Brīvības Yard Apartment With Parking In City Center",
        "Riga Riverside Design One Bedroom Apartment",
        "Old Riga Kaleju Cozy apartment2",
        "Old Riga 2 Bedroom Vecpilsetas street Apartment",
    ]
    # (Modern Design Studio Apartment In Riga Center was already
    # reactivated correctly by the CSV loop itself, no action needed.)

    duplicate_names_to_deactivate = [
        "Kaleju iela 57-5",                                             # dup of Old Riga Cozy One Bedroom Apartment
        "Teātra iela 4-19",                                             # dup of Old Riga Terrace Apartment
        "Palasta iela 9 - 21",                                          # dup of Old Riga Palasta Loft Apartment with river view
        "Brīvības iela 60",                                             # dup of Brīvības Yard Apartment With Parking In City Center
        "Riverside Design Apartment With Underground Private Parking",  # dup of Riga Riverside Design One Bedroom Apartment
        "Old Riga Kaleju Cozy apartment",                               # dup of Old Riga Kaleju Cozy apartment2
        "Vecpilsētas iela 3k1 - 1",                                     # dup of Old Riga 2 Bedroom Vecpilsetas street Apartment
        "Kalnina Design Studio in Centre",                              # dup of A. Kalnina iela 1-57
        "City Retreat Design Apartment",                                # dup of Marijas iela 4-24
    ]

    reactivated, dup_deactivated, not_found = [], [], []

    for name in reactivate_names:
        room = Room.query.filter(db.func.lower(Room.name) == name.strip().lower()).first()
        if room:
            room.is_active = True
            reactivated.append(name)
        else:
            not_found.append(name)

    for name in duplicate_names_to_deactivate:
        room = Room.query.filter(db.func.lower(Room.name) == name.strip().lower()).first()
        if room:
            room.is_active = False
            dup_deactivated.append(name)
        else:
            not_found.append(name)

    db.session.commit()

    return {
        "reactivated": reactivated,
        "duplicates_deactivated": dup_deactivated,
        "not_found": not_found,
        "total_active_rooms_now": Room.query.filter_by(is_active=True).count(),
        "still_deactivated_no_price_in_beds24": [
            "Riga City Center Apartment",
            "Kalnina Quiet Apartment in city center",
            "Kr. Barona iela 24/26 Residential Barona One Bedroom Apartment",
            "Kr. Barona iela 24/26 One Bedroom Apartment",
            "Kungu iela 25 Old Riga Ridzenes Residence Studio Apartment",
        ],
    }


@app.route("/admin")
@admin_required
def admin_dashboard():
    stats = {
        "rooms": Room.query.count(),
        "attractions": Attraction.query.count(),
        "users": User.query.count(),
        "bookings": Booking.query.filter_by(status="Confirmed").count(),
        "revenue": db.session.query(db.func.sum(Booking.total_price))
        .filter(Booking.payment_status == "Paid").scalar() or 0,
    }
    recent = Booking.query.order_by(Booking.created_at.desc()).limit(8).all()
    return render_template("admin/dashboard.html", stats=stats, recent=recent)


@app.route("/admin/rooms")
@admin_required
def admin_rooms():
    return render_template("admin/rooms.html",
                           rooms=Room.query.order_by(Room.created_at.desc()).all())


@app.route("/admin/rooms/new", methods=["GET", "POST"])
@admin_required
def admin_add_room():
    if request.method == "POST":
        r = Room(
            name=request.form.get("name", "").strip(),
            room_type=request.form.get("room_type", "").strip(),
            description=request.form.get("description", "").strip(),
            price=request.form.get("price", 0, type=float),
            capacity=request.form.get("capacity", 2, type=int),
            beds=request.form.get("beds", 1, type=int),
            size_sqm=request.form.get("size_sqm", 30, type=int),
            bathrooms=request.form.get("bathrooms", 1, type=int),
            amenities=request.form.get("amenities", "").strip(),
            location=request.form.get("location", "").strip(),
            is_active=bool(request.form.get("is_active")),
        )
        save_room_features(r, request.form)
        upload_image(r, folder="riganest/rooms")
        db.session.add(r)
        db.session.commit()
        upload_extra_photos(r)
        db.session.commit()
        flash("Room added.", "success")
        return redirect(url_for("admin_rooms"))
    return render_template("admin/room_form.html", room=None)


@app.route("/admin/rooms/<int:room_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_room(room_id):
    r = Room.query.get_or_404(room_id)
    if request.method == "POST":
        r.name = request.form.get("name", "").strip()
        r.room_type = request.form.get("room_type", "").strip()
        r.description = request.form.get("description", "").strip()
        r.price = request.form.get("price", 0, type=float)
        r.capacity = request.form.get("capacity", 2, type=int)
        r.beds = request.form.get("beds", 1, type=int)
        r.size_sqm = request.form.get("size_sqm", 30, type=int)
        r.bathrooms = request.form.get("bathrooms", 1, type=int)
        r.amenities = request.form.get("amenities", "").strip()
        r.location = request.form.get("location", "").strip()
        r.is_active = bool(request.form.get("is_active"))
        save_room_features(r, request.form)
        upload_image(r, folder="riganest/rooms")
        upload_extra_photos(r)
        db.session.commit()
        flash("Room updated.", "success")
        return redirect(url_for("admin_rooms"))
    return render_template("admin/room_form.html", room=r)


@app.route("/admin/rooms/<int:room_id>/delete", methods=["POST"])
@admin_required
def admin_delete_room(room_id):
    r = Room.query.get_or_404(room_id)
    db.session.delete(r)
    db.session.commit()
    flash("Room deleted.", "success")
    return redirect(url_for("admin_rooms"))


@app.route("/admin/photo/<int:photo_id>/delete", methods=["POST"])
@admin_required
def admin_delete_photo(photo_id):
    p = RoomImage.query.get_or_404(photo_id)
    rid = p.room_id
    db.session.delete(p)
    db.session.commit()
    flash("Photo removed.", "success")
    return redirect(url_for("admin_edit_room", room_id=rid))


@app.route("/admin/attractions")
@admin_required
def admin_attractions():
    return render_template("admin/attractions.html",
                           attractions=Attraction.query.order_by(Attraction.created_at.desc()).all())


def _attraction_fields_from_form():
    return dict(
        name=request.form.get("name", "").strip(),
        summary=request.form.get("summary", "").strip(),
        location=request.form.get("location", "").strip(),
        allow_children=bool(request.form.get("allow_children")),
        allow_smoking=bool(request.form.get("allow_smoking")),
        allow_pets=bool(request.form.get("allow_pets")),
        has_parking=bool(request.form.get("has_parking")),
        has_handicap=bool(request.form.get("has_handicap")),
        opening_time=request.form.get("opening_time", "").strip() or "All Day",
        closing_time=request.form.get("closing_time", "").strip() or "All Day",
        adult_price=request.form.get("adult_price", "").strip() or "Free",
        children_price=request.form.get("children_price", "").strip() or "Free",
        oap_price=request.form.get("oap_price", "").strip() or "Free",
        booking_url=request.form.get("booking_url", "").strip(),
        is_active=bool(request.form.get("is_active")),
    )


@app.route("/admin/attractions/new", methods=["GET", "POST"])
@admin_required
def admin_add_attraction():
    if request.method == "POST":
        a = Attraction(**_attraction_fields_from_form())
        upload_image(a, folder="riganest/attractions")
        db.session.add(a)
        db.session.commit()
        upload_attraction_photos(a)
        db.session.commit()
        flash("Attraction added.", "success")
        return redirect(url_for("admin_attractions"))
    return render_template("admin/attraction_form.html", attraction=None)


@app.route("/admin/attractions/<int:attraction_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_attraction(attraction_id):
    a = Attraction.query.get_or_404(attraction_id)
    if request.method == "POST":
        for k, v in _attraction_fields_from_form().items():
            setattr(a, k, v)
        upload_image(a, folder="riganest/attractions")
        upload_attraction_photos(a)
        db.session.commit()
        flash("Attraction updated.", "success")
        return redirect(url_for("admin_attractions"))
    return render_template("admin/attraction_form.html", attraction=a)


@app.route("/admin/attractions/<int:attraction_id>/delete", methods=["POST"])
@admin_required
def admin_delete_attraction(attraction_id):
    a = Attraction.query.get_or_404(attraction_id)
    db.session.delete(a)
    db.session.commit()
    flash("Attraction deleted.", "success")
    return redirect(url_for("admin_attractions"))


@app.route("/admin/attraction-photo/<int:photo_id>/delete", methods=["POST"])
@admin_required
def admin_delete_attraction_photo(photo_id):
    p = AttractionImage.query.get_or_404(photo_id)
    aid = p.attraction_id
    db.session.delete(p)
    db.session.commit()
    flash("Photo removed.", "success")
    return redirect(url_for("admin_edit_attraction", attraction_id=aid))


@app.route("/admin/messages")
@admin_required
def admin_messages():
    msgs = ContactMessage.query.order_by(ContactMessage.created_at.desc()).all()
    return render_template("admin/messages.html", messages=msgs)


@app.route("/admin/bookings")
@admin_required
def admin_bookings():
    return render_template("admin/bookings.html",
                           bookings=Booking.query.order_by(Booking.created_at.desc()).all())


@app.route("/admin/users")
@admin_required
def admin_users():
    return render_template("admin/users.html",
                           users=User.query.order_by(User.created_at.desc()).all())




@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    u = User.query.get_or_404(user_id)
    if u.is_admin:
        flash("Cannot delete admin users.", "danger")
        return redirect(url_for("admin_users"))
    db.session.delete(u)
    db.session.commit()
    flash("User deleted.", "success")
    return redirect(url_for("admin_users"))



@app.route("/careers")
def careers():
    page = SitePage.query.filter_by(slug="careers").first()
    return render_template("static_page.html", page=page, slug="careers", default_title="Careers")

@app.route("/press")
def press():
    page = SitePage.query.filter_by(slug="press").first()
    return render_template("static_page.html", page=page, slug="press", default_title="Press")

@app.route("/blog")
def blog():
    page = SitePage.query.filter_by(slug="blog").first()
    return render_template("static_page.html", page=page, slug="blog", default_title="Blog")

@app.route("/help")
def help_centre():
    page = SitePage.query.filter_by(slug="help").first()
    return render_template("static_page.html", page=page, slug="help", default_title="Help Centre")

@app.route("/cancellation-policy")
def cancellation_policy():
    page = SitePage.query.filter_by(slug="cancellation-policy").first()
    return render_template("static_page.html", page=page, slug="cancellation-policy", default_title="Cancellation Policy")

@app.route("/privacy-policy")
def privacy_policy():
    page = SitePage.query.filter_by(slug="privacy-policy").first()
    return render_template("static_page.html", page=page, slug="privacy-policy", default_title="Privacy Policy")

@app.route("/terms-of-service")
def terms_of_service():
    page = SitePage.query.filter_by(slug="terms-of-service").first()
    return render_template("static_page.html", page=page, slug="terms-of-service", default_title="Terms of Service")

@app.route("/safety")
def safety():
    page = SitePage.query.filter_by(slug="safety").first()
    return render_template("static_page.html", page=page, slug="safety", default_title="Safety")


@app.route("/admin/pages")
@admin_required
def admin_pages():
    pages_config = [
        {"slug": "careers", "title": "Careers", "endpoint": "careers"},
        {"slug": "press", "title": "Press", "endpoint": "press"},
        {"slug": "blog", "title": "Blog", "endpoint": "blog"},
        {"slug": "help", "title": "Help Centre", "endpoint": "help_centre"},
        {"slug": "cancellation-policy", "title": "Cancellation Policy", "endpoint": "cancellation_policy"},
        {"slug": "privacy-policy", "title": "Privacy Policy", "endpoint": "privacy_policy"},
        {"slug": "terms-of-service", "title": "Terms of Service", "endpoint": "terms_of_service"},
        {"slug": "safety", "title": "Safety", "endpoint": "safety"},
    ]
    pages = {p.slug: p for p in SitePage.query.all()}
    return render_template("admin/pages.html", pages_config=pages_config, pages=pages)

@app.route("/admin/pages/<slug>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_page(slug):
    page = SitePage.query.filter_by(slug=slug).first()
    if not page:
        page = SitePage(slug=slug, title=slug.replace("-", " ").title())
        db.session.add(page)
        db.session.commit()
    if request.method == "POST":
        page.title = request.form.get("title", "").strip()
        page.content = request.form.get("content", "").strip()
        img = request.files.get("image")
        if img and img.filename and allowed_file(img.filename):
            if CLOUDINARY_ENABLED:
                result = cloudinary.uploader.upload(img, folder="riganest/pages")
                page.image = result["secure_url"]
            else:
                fn = f"page_{slug}_{int(datetime.utcnow().timestamp())}_{secure_filename(img.filename)}"
                img.save(os.path.join(app.config["UPLOAD_FOLDER"], fn))
                page.image = fn
        page.updated_at = datetime.utcnow()
        db.session.commit()
        flash("Page updated.", "success")
        return redirect(url_for("admin_pages"))
    return render_template("admin/page_form.html", page=page)

@app.route("/admin/about", methods=["GET", "POST"])
@admin_required
def admin_about():
    about = SiteAbout.query.first()
    if not about:
        about = SiteAbout()
        db.session.add(about)
        db.session.commit()
    if request.method == "POST":
        about.title = request.form.get("title", "About Us").strip()
        about.hero_subtitle = request.form.get("hero_subtitle", "Our Story").strip()
        about.content = request.form.get("content", "").strip()
        about.updated_at = datetime.utcnow()
        # Handle image upload
        img = request.files.get("image")
        if img and img.filename and allowed_file(img.filename):
            fn = f"about_{int(datetime.utcnow().timestamp())}_{secure_filename(img.filename)}"
            img.save(os.path.join(app.config["UPLOAD_FOLDER"], fn))
            about.image = fn
        db.session.commit()
        flash("About page updated.", "success")
        return redirect(url_for("admin_about"))
    return render_template("admin/about_form.html", about=about)

@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    logo_path = os.path.join(BASE_DIR, "static", "logo.png")
    logo_exists = os.path.exists(logo_path)

    if request.method == "POST":
        action = request.form.get("action")

        if action == "upload":
            file = request.files.get("logo")
            if not file or file.filename == "":
                flash("No file selected.", "warning")
            else:
                ext = os.path.splitext(file.filename)[1].lower()
                if ext not in (".png", ".jpg", ".jpeg", ".svg", ".webp"):
                    flash("Allowed formats: PNG, JPG, SVG, WEBP.", "danger")
                else:
                    save_name = "logo" + ext
                    save_path = os.path.join(BASE_DIR, "static", save_name)
                    for old_ext in (".png", ".jpg", ".jpeg", ".svg", ".webp"):
                        old_file = os.path.join(BASE_DIR, "static", "logo" + old_ext)
                        if os.path.exists(old_file):
                            os.remove(old_file)
                    file.save(save_path)
                    flash("✅ Logo uploaded successfully!", "success")
                    return redirect(url_for("admin_settings"))

        elif action == "remove":
            for ext in (".png", ".jpg", ".jpeg", ".svg", ".webp"):
                f = os.path.join(BASE_DIR, "static", "logo" + ext)
                if os.path.exists(f):
                    os.remove(f)
            flash("Logo removed. Site name text will be shown.", "info")
            return redirect(url_for("admin_settings"))

        elif action == "upload_banner":
            files = request.files.getlist("banner_images")
            files = [f for f in files if f and f.filename]
            if not files:
                flash("No banner image selected.", "warning")
            else:
                saved = 0
                for f in files:
                    if allowed_file(f.filename):
                        fn = f"banner_{int(datetime.utcnow().timestamp()*1000)}_{secure_filename(f.filename)}"
                        f.save(os.path.join(SLIDES_FOLDER, fn))
                        saved += 1
                if saved:
                    flash(f"✅ {saved} banner image{'s' if saved > 1 else ''} uploaded!", "success")
                else:
                    flash("Allowed formats: PNG, JPG, GIF, WEBP.", "danger")
            return redirect(url_for("admin_settings"))

        elif action == "remove_banner":
            fn = secure_filename(request.form.get("filename", ""))
            f = os.path.join(SLIDES_FOLDER, fn)
            if fn and os.path.exists(f):
                os.remove(f)
                flash("Banner image removed.", "info")
            else:
                flash("Banner image not found.", "warning")
            return redirect(url_for("admin_settings"))

    current_logo = None
    for ext in (".png", ".jpg", ".jpeg", ".svg", ".webp"):
        if os.path.exists(os.path.join(BASE_DIR, "static", "logo" + ext)):
            current_logo = "logo" + ext
            break

    return render_template("admin/settings.html", logo_exists=logo_exists,
                           current_logo=current_logo, banner_images=get_banner_images())


# ----------------------------------------------------------------------
# Template globals
# ----------------------------------------------------------------------
@app.route("/set_lang/<lang>")
def set_lang(lang):
    if lang in ("en", "lv"):
        session["lang"] = lang
    return redirect(request.referrer or url_for("index"))


@app.context_processor
def inject_globals():
    logo = any(os.path.exists(os.path.join(BASE_DIR, "static", "logo" + ext))
               for ext in (".png", ".jpg", ".jpeg", ".svg", ".webp"))
    lang = session.get("lang", "en")
    logo_file = None
    for ext in (".png", ".jpg", ".jpeg", ".svg", ".webp"):
        if os.path.exists(os.path.join(BASE_DIR, "static", "logo" + ext)):
            logo_file = "logo" + ext
            break
    return {"current_year": datetime.utcnow().year, "today": date.today(),
            "logo_exists": logo, "logo_file": logo_file,
            "payments_live": PAYMENTS_LIVE, "email_live": EMAIL_LIVE,
            "lang": lang,
            "whatsapp_number": WHATSAPP_NUMBER,
            "whatsapp_digits": WHATSAPP_NUMBER_DIGITS}


# ----------------------------------------------------------------------
# Auto-migration: add any missing columns automatically on startup
# ----------------------------------------------------------------------
def auto_migrate():
    """
    Compares the SQLAlchemy models against the live database schema and
    adds any missing columns with ALTER TABLE. This avoids needing shell
    access on Render every time a new column is added to a model.
    """
    from sqlalchemy import inspect, text

    type_map = {
        "INTEGER": "INTEGER",
        "VARCHAR": "VARCHAR(255)",
        "TEXT": "TEXT",
        "FLOAT": "FLOAT",
        "BOOLEAN": "BOOLEAN",
        "DATETIME": "TIMESTAMP",
        "DATE": "DATE",
    }

    inspector = inspect(db.engine)
    existing_tables = inspector.get_table_names()

    for table_name, table in db.metadata.tables.items():
        if table_name not in existing_tables:
            continue  # brand-new table, db.create_all() already handles this
        existing_columns = {c["name"] for c in inspector.get_columns(table_name)}
        for column in table.columns:
            if column.name in existing_columns:
                continue
            col_type = str(column.type).split("(")[0].upper()
            sql_type = type_map.get(col_type, "TEXT")
            default_clause = ""
            if column.default is not None and getattr(column.default, "is_scalar", False):
                val = column.default.arg
                if isinstance(val, bool):
                    default_clause = f" DEFAULT {'TRUE' if val else 'FALSE'}"
                elif isinstance(val, (int, float)):
                    default_clause = f" DEFAULT {val}"
                elif isinstance(val, str):
                    default_clause = f" DEFAULT '{val}'"
            try:
                db.session.execute(text(
                    f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}" {sql_type}{default_clause}'
                ))
                db.session.commit()
                print(f"[auto_migrate] Added column {table_name}.{column.name}")
            except Exception as e:
                db.session.rollback()
                print(f"[auto_migrate] Could not add {table_name}.{column.name}: {e}")


# ----------------------------------------------------------------------
# First-run seed
# ----------------------------------------------------------------------
def seed():
    db.create_all()
    auto_migrate()
    if not User.query.filter_by(email="admin@riganest.com").first():
        a = User(name="RigaNest Admin", email="admin@riganest.com",
                 phone="+10000000000", is_admin=True)
        a.set_password("admin123")
        db.session.add(a)
    if Room.query.count() == 0:
        r1 = Room(name="Vecrīga Loft", room_type="Suite", price=245, capacity=3,
                  beds=1, size_sqm=48, rating=4.9,
                  amenities="Old Town View, WiFi, Kitchenette, Smart TV, Washer",
                  location="Riga Old Town, Latvia",
                  description="Tucked above a cobblestone lane in the heart of Vecrīga, this loft pairs "
                               "exposed wooden beams with a crisp, modern fit-out. A small kitchenette and "
                               "a comfortable lounge area make it equally suited to a romantic weekend or a "
                               "few quiet nights of solo exploring, with the Dome Cathedral and the best "
                               "cafés in town just a short stroll away.")
        r2 = Room(name="Vērmanes Park Suite", room_type="Deluxe", price=195, capacity=2,
                  beds=1, size_sqm=40, rating=4.8,
                  amenities="Park View, WiFi, Dedicated Workspace, Air Conditioning, Breakfast Kit",
                  location="Centre District, Riga, Latvia",
                  description="A calm, light-filled suite looking out over the green canopy of Vērmanes "
                               "Garden. The dedicated desk and reliable WiFi make it a favourite with remote "
                               "workers, while the tram stop right outside puts both the Old Town and the "
                               "Central Station within easy reach.")
        r3 = Room(name="Dome Square Studio", room_type="Standard", price=120, capacity=2,
                  beds=1, size_sqm=30, rating=4.6,
                  amenities="WiFi, Kitchenette, Smart TV, Washer",
                  location="Riga Old Town, Latvia",
                  description="A snug, efficiently laid-out studio just steps from Dome Square. Everything "
                               "you need for a short stay is within arm's reach — a compact kitchenette, a "
                               "comfortable sofa bed and fast WiFi — making it an easy, budget-friendly base "
                               "for exploring the Old Town on foot.")
        r4 = Room(name="Daugava Riverside Apartment", room_type="Family", price=210, capacity=4,
                  beds=2, size_sqm=62, rating=4.7,
                  amenities="River View, WiFi, Full Kitchen, Balcony, Washer",
                  location="Āgenskalns, Riga, Latvia",
                  description="A spacious two-bedroom apartment on the quieter left bank of the Daugava, "
                               "with a private balcony looking out toward the river and the Old Town skyline "
                               "beyond. The full kitchen and generous living area make it a comfortable home "
                               "base for families or small groups staying a few extra days.")
        r5 = Room(name="Art Nouveau Quarter Apartment", room_type="Premium", price=230, capacity=4,
                  beds=2, size_sqm=55, rating=4.9,
                  amenities="WiFi, Designer Interior, Full Kitchen, Dishwasher, Smart TV",
                  location="Alberta iela, Riga, Latvia",
                  description="Set on one of Riga's celebrated Art Nouveau streets, this apartment combines "
                               "high ceilings and tall windows with a thoughtfully furnished, design-forward "
                               "interior. A fully equipped kitchen and two comfortable bedrooms make it ideal "
                               "for guests who want a bit more space without straying from the city centre.")
        r6 = Room(name="Central Market Cozy Studio", room_type="Standard", price=95, capacity=2,
                  beds=1, size_sqm=26, rating=4.5,
                  amenities="WiFi, Compact Kitchen, Smart TV",
                  location="Riga Centre, Latvia",
                  description="A no-fuss, well-priced studio a few minutes' walk from the historic pavilions "
                               "of Riga Central Market. Compact but comfortable, it suits guests who plan to "
                               "spend most of their time out exploring and just need a reliable, central place "
                               "to rest.")
        db.session.add_all([r1, r2, r3, r4, r5, r6])
        db.session.commit()
        photo_sets = {r1.id: ["g1.jpg", "g3.jpg", "slide1.jpg"],
                      r2.id: ["g2.jpg", "g7.jpg", "slide2.jpg"],
                      r3.id: ["g4.jpg", "g8.jpg", "slide3.jpg"],
                      r4.id: ["g5.jpg", "g1.jpg", "g6.jpg"],
                      r5.id: ["g6.jpg", "g3.jpg", "g2.jpg"],
                      r6.id: ["g7.jpg", "g5.jpg", "g8.jpg"]}
        for rid, files in photo_sets.items():
            for fn in files:
                src = "slides/" + fn if fn.startswith("slide") else "gallery/" + fn
                db.session.add(RoomImage(room_id=rid, filename=src))
    if Attraction.query.count() == 0:
        attraction_data = [
            dict(name="Freedom Monument", image="gallery/g1.jpg", location="Riga, Latvia",
                 summary="A landmark in central Riga that honours Latvia's independence, unveiled in 1935. "
                         "The column is topped by a figure holding three stars, each representing one of "
                         "the country's historical regions. Visitors can also explore the surrounding park, "
                         "home to several other monuments."),
            dict(name="Riga Old Town", image="gallery/g2.jpg", location="Vecriga, Riga, Latvia",
                 summary="A UNESCO World Heritage medieval quarter packed with cobblestone streets, "
                         "Art Nouveau facades and centuries-old churches. It's the heart of Riga's "
                         "history and a favourite spot for an evening stroll."),
            dict(name="House of the Blackheads", image="gallery/g3.jpg", location="Riga, Latvia",
                 summary="A striking Gothic building dating back to the 14th century, rebuilt after "
                         "wartime damage. It once served as a guild hall for unmarried merchants and "
                         "now hosts exhibitions and civic events."),
            dict(name="St. Peter's Church", image="gallery/g4.jpg", location="Riga, Latvia",
                 summary="One of Riga's oldest churches, known for its tall spire and an observation "
                         "deck with panoramic views over the rooftops of the Old Town."),
            dict(name="Riga Central Market", image="gallery/g5.jpg", location="Riga, Latvia",
                 summary="A lively covered market set in five enormous zeppelin-hangar pavilions, "
                         "offering fresh produce, local food and souvenirs."),
            dict(name="Latvian National Opera", image="gallery/g6.jpg", location="Riga, Latvia",
                 summary="A neoclassical theatre on the edge of Bastejkalns Park, home to opera and "
                         "ballet performances year-round."),
            dict(name="Bastejkalns Park", image="gallery/g7.jpg", location="Riga, Latvia",
                 summary="A scenic canal-side park bordering the Old Town, popular for boat rides, "
                         "picnics and quiet walks beneath the trees."),
            dict(name="Riga Cathedral", image="gallery/g8.jpg", location="Riga, Latvia",
                 summary="A grand Lutheran cathedral overlooking Dome Square, with one of the largest "
                         "pipe organs in the world."),
        ]
        for d in attraction_data:
            img = d.pop("image")
            db.session.add(Attraction(image=img, **d))
    db.session.commit()


with app.app_context():
    seed()


if __name__ == "__main__":
    print("=" * 60)
    print(f"  Payments: {'STRIPE (live keys set)' if PAYMENTS_LIVE else 'DEV MODE (demo, no real charge)'}")
    print(f"  Email OTP : {'GMAIL (live)' if EMAIL_LIVE else 'DEV MODE (code shown on screen + terminal)'}")
    print("  Admin   : admin@riganest.com / admin123")
    print("=" * 60)
    app.run(debug=True)
@app.route("/debug-cloudinary")
def debug_cloudinary():
    return {
        "CLOUDINARY_CLOUD_NAME": bool(CLOUDINARY_CLOUD_NAME),
        "CLOUDINARY_API_KEY": bool(CLOUDINARY_API_KEY),
        "CLOUDINARY_API_SECRET": bool(CLOUDINARY_API_SECRET),
        "CLOUDINARY_ENABLED": CLOUDINARY_ENABLED,
    }
