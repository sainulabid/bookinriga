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


@app.route("/booking/<int:booking_id>/success")
@login_required
def booking_success(booking_id):
    bk = Booking.query.get_or_404(booking_id)
    if bk.user_id != current_user.id:
        abort(403)
    bk.status = "Confirmed"
    bk.payment_status = "Paid"
    db.session.commit()

    # Push to Beds24 so the room is blocked there too (prevents double-booking
    # with Airbnb/Booking.com/etc). If this fails, the local booking still
    # stands — check server logs and push manually if needed.
    try:
        from beds24_push_booking import push_booking
        ok, detail = push_booking(bk)
        if not ok:
            app.logger.warning(f"Beds24 push failed for booking {bk.id}: {detail}")
    except Exception as e:
        app.logger.warning(f"Beds24 push crashed for booking {bk.id}: {e}")

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
    flash("Booking cancelled.", "success")
    return redirect(request.referrer or url_for("dashboard"))


# ----------------------------------------------------------------------
# Admin
# ----------------------------------------------------------------------
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
