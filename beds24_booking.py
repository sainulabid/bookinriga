"""Durable, payment-verified Beds24 V2 booking export. Never export demo payments."""
import os
import time
import uuid
import threading
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import requests

API = 'https://beds24.com/api/v2'
_lock = threading.Lock()
_token = None
_token_until = 0
_REQUIRED = {'read:bookings', 'write:bookings', 'write:bookings-personal',
             'write:bookings-financial', 'read:bookings-personal', 'read:properties', 'read:inventory'}


class SyncError(Exception):
    def __init__(self, code, ambiguous=False):
        super().__init__(code)
        self.code, self.ambiguous = code, ambiguous


def cents(value):
    return int((Decimal(str(value)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


class Client:
    def __init__(self):
        global _token, _token_until
        refresh = os.environ.get('BEDS24_REFRESH_TOKEN', '').strip().strip('\"\'')
        if not refresh:
            raise SyncError('refresh_token_missing')
        with _lock:
            if not _token or time.monotonic() >= _token_until:
                try:
                    r = requests.get(API + '/authentication/token', headers={'refreshToken': refresh}, timeout=15)
                    if r.status_code != 200:
                        raise SyncError('authentication_failed_' + str(r.status_code))
                    data = r.json()
                    if not data.get('token'):
                        raise SyncError('authentication_invalid_response')
                    _token = data['token']
                    _token_until = time.monotonic() + max(1, int(data.get('expiresIn', 3600)) - 60)
                except requests.RequestException:
                    raise SyncError('authentication_unreachable') from None
                except (ValueError, TypeError):
                    raise SyncError('authentication_invalid_response') from None
            self.token = _token
        details = self.get('/authentication/details')
        scopes = set(details.get('token', {}).get('scopes', []))
        if details.get('validToken') is not True:
            raise SyncError('token_invalid')
        missing = [s for s in _REQUIRED if s not in scopes and 'all:' + s.split(':')[1] not in scopes]
        if missing:
            raise SyncError('missing_scopes_' + ','.join(sorted(missing)))

    def get(self, path, params=None):
        global _token_until
        try:
            r = requests.get(API + path, headers={'token': self.token, 'accept': 'application/json'}, params=params, timeout=15)
            if r.status_code != 200:
                if r.status_code == 401:
                    _token_until = 0
                raise SyncError('read_failed_' + str(r.status_code))
            data = r.json()
            if not isinstance(data, dict) or data.get('success') is False:
                raise SyncError('read_rejected')
            return data
        except requests.RequestException:
            raise SyncError('read_unreachable') from None
        except (ValueError, TypeError):
            raise SyncError('read_invalid_response') from None

    def post(self, payload):
        # A lost response may have created the booking. Never blindly retry a create.
        try:
            r = requests.post(API + '/bookings', headers={'token': self.token, 'accept': 'application/json'}, json=[payload], timeout=20)
        except requests.RequestException:
            raise SyncError('write_response_unknown', ambiguous=True) from None
        if r.status_code in (401, 403, 429):
            raise SyncError('write_rejected_' + str(r.status_code))
        if r.status_code not in (200, 201):
            raise SyncError('write_response_unknown', ambiguous=True)
        try:
            data = r.json()
            if not isinstance(data, list) or len(data) != 1:
                raise SyncError('write_response_unknown', ambiguous=True)
            result = data[0]
            if result.get('success') is not True:
                raise SyncError('write_item_rejected_manual_review', ambiguous=True)
            return result
        except (ValueError, TypeError, AttributeError):
            raise SyncError('write_response_unknown', ambiguous=True) from None

    def verify_room(self, room):
        if not room.beds24_room_id:
            raise SyncError('room_mapping_missing')
        data = self.get('/properties', {'roomId': room.beds24_room_id, 'includeAllRooms': 'true'})
        rooms = [r for p in data.get('data', []) for r in p.get('rooms', [])]
        if not any(str(r.get('id')) == str(room.beds24_room_id) for r in rooms):
            raise SyncError('room_not_authorized')

    def check_offer(self, bk):
        data = self.get('/inventory/rooms/offers', {'roomId': bk.room.beds24_room_id,
            'arrival': bk.check_in.isoformat(), 'departure': bk.check_out.isoformat(),
            'numAdults': bk.guests, 'numChildren': 0})
        offers = [o for r in data.get('data', []) if str(r.get('roomId')) == str(bk.room.beds24_room_id)
                  for o in r.get('offers', [])]
        if not any(float(o.get('price') or 0) > 0 and int(o.get('unitsAvailable') or 0) > 0 for o in offers):
            raise SyncError('dates_not_available')

    def find(self, bk):
        if not bk.beds24_reference:
            return None
        params = {'id': bk.beds24_booking_id} if bk.beds24_booking_id else {'apiReference': bk.beds24_reference}
        data = self.get('/bookings', params)
        if data.get('success') is not True or not isinstance(data.get('data'), list):
            raise SyncError('lookup_invalid_response')
        rows = [r for r in data['data'] if r.get('apiReference') == bk.beds24_reference or r.get('custom1') == bk.beds24_reference]
        if not rows and not bk.beds24_booking_id:
            # custom1 is writable in the published newBooking schema. It also
            # recovers a lost POST reply if apiReference was not persisted.
            data = self.get('/bookings', {'roomId': bk.room.beds24_room_id,
                'arrival': bk.check_in.isoformat(), 'departure': bk.check_out.isoformat()})
            if data.get('success') is not True or not isinstance(data.get('data'), list):
                raise SyncError('lookup_invalid_response')
            rows = [r for r in data['data'] if r.get('custom1') == bk.beds24_reference]
        if len(rows) > 1:
            raise SyncError('duplicate_reference_manual_review', ambiguous=True)
        if not rows:
            return None
        row = rows[0]
        if (str(row.get('roomId')) != str(bk.room.beds24_room_id) or
                row.get('arrival') != bk.check_in.isoformat() or row.get('departure') != bk.check_out.isoformat()):
            raise SyncError('reference_mismatch_manual_review', ambiguous=True)
        if not isinstance(row.get('id'), int) or row['id'] <= 0:
            raise SyncError('lookup_invalid_response')
        return row


def preflight(bk):
    client = Client()
    client.verify_room(bk.room)
    client.check_offer(bk)


def accept_payment(bk, checkout):
    """Only a server-retrieved or signature-verified Checkout Session may enter here."""
    from app import db
    if (checkout.get('id') != bk.stripe_session_id or checkout.get('payment_status') != 'paid'
            or checkout.get('currency') != 'eur' or checkout.get('amount_total') != cents(bk.total_price)):
        raise SyncError('payment_not_verified')
    if bk.payment_verified:
        return
    from app import Booking
    live = checkout.get('livemode') is True
    values = {Booking.payment_verified: True, Booking.payment_live: live,
              Booking.payment_status: 'Paid' if live else 'Test payment'}
    if live:
        values.update({Booking.beds24_reference: bk.beds24_reference or 'bookinriga-' + uuid.uuid4().hex,
            Booking.beds24_sync_state: 'pending' if bk.status != 'Cancelled' else 'manual_review',
            Booking.status: 'Pending' if bk.status != 'Cancelled' else 'Cancelled'})
    else:
        values.update({Booking.beds24_sync_state: 'demo', Booking.status: 'Confirmed'})
    Booking.query.filter_by(id=bk.id, payment_verified=False).update(values, synchronize_session=False)
    db.session.commit()
    db.session.expire_all()


def sync_booking(booking_id):
    from app import db, Booking
    bk = db.session.get(Booking, booking_id)
    if not bk or not bk.payment_verified or not bk.payment_live:
        return
    if bk.beds24_sync_state in ('synced', 'cancelled', 'demo', 'manual_review', 'not_requested'):
        return
    previous = bk.beds24_sync_state
    if previous == 'working':
        if bk.beds24_attempted_at and bk.beds24_attempted_at > datetime.utcnow() - timedelta(minutes=5):
            return
        previous = 'ambiguous'  # Worker may have died after POST reached Beds24.
    allowed = bk.beds24_sync_state
    claim = Booking.query.filter_by(id=bk.id, beds24_sync_state=allowed)
    if allowed == 'working':
        claim = claim.filter(Booking.beds24_attempted_at < datetime.utcnow() - timedelta(minutes=5))
    claimed = claim.update({
        Booking.beds24_sync_state: 'working', Booking.beds24_attempted_at: datetime.utcnow()}, synchronize_session=False)
    db.session.commit()
    if not claimed:
        return
    db.session.expire_all()
    bk = db.session.get(Booking, booking_id)
    try:
        client = Client()
        client.verify_room(bk.room)
        row = client.find(bk)
        if row:
            bk.beds24_booking_id = row['id']
            db.session.commit()  # Persist remote ID before any cancellation call.
        if bk.cancel_requested:
            if not bk.beds24_booking_id:
                if previous == 'ambiguous':
                    raise SyncError('creation_unknown_manual_review', ambiguous=True)
            else:
                client.post({'id': bk.beds24_booking_id, 'status': 'cancelled'})
            bk.status, bk.beds24_sync_state = 'Cancelled', 'cancelled'
        elif bk.beds24_booking_id:
            if row and row.get('status') == 'cancelled':
                raise SyncError('remote_cancelled_manual_review', ambiguous=True)
            bk.status, bk.beds24_sync_state = 'Confirmed', 'synced'
        else:
            if previous == 'ambiguous':
                raise SyncError('creation_unknown_manual_review', ambiguous=True)
            client.check_offer(bk)
            name = (bk.user.name or 'Guest').strip().split(' ', 1)
            result = client.post({'roomId': bk.room.beds24_room_id, 'status': 'confirmed',
                'arrival': bk.check_in.isoformat(), 'departure': bk.check_out.isoformat(),
                'numAdult': bk.guests, 'numChild': 0, 'firstName': name[0][:100],
                'lastName': name[1][:100] if len(name) > 1 else '', 'email': bk.user.email[:100],
                'phone': (bk.user.phone or '')[:100], 'price': float(bk.total_price),
                'apiReference': bk.beds24_reference, 'custom1': bk.beds24_reference,
                'referer': 'BookinRiga website', 'actions': {'checkAvailability': True},
                'notes': 'Payment verified by BookinRiga Stripe. Local booking #' + str(bk.id)})
            # V2 POST replies have dynamic "new" fields; read back by our reference.
            remote_id = result.get('new', {}).get('id') if isinstance(result.get('new'), dict) else None
            if isinstance(remote_id, int) and remote_id > 0:
                bk.beds24_booking_id = remote_id
            bk.beds24_sync_state = 'ambiguous'
            db.session.commit()
            row = client.find(bk)
            if not row or row.get('status') != 'confirmed':
                raise SyncError('creation_unknown_manual_review', ambiguous=True)
            bk.beds24_booking_id = row['id']
            bk.status, bk.beds24_sync_state = 'Confirmed', 'synced'
        # A cancellation can arrive while the create POST is in flight.
        requested = db.session.query(Booking.cancel_requested).filter_by(id=bk.id).scalar()
        if requested and bk.beds24_sync_state == 'synced':
            bk.beds24_sync_state = 'pending'
        bk.beds24_sync_error = ''
    except SyncError as e:
        # Preserve uncertainty even when the reconciliation read/token fails.
        uncertain = e.ambiguous or previous == 'ambiguous' or bk.beds24_sync_state == 'ambiguous'
        bk.beds24_sync_state = 'ambiguous' if uncertain else 'error'
        bk.beds24_sync_error = e.code[:255]
    except Exception:
        bk.beds24_sync_state = 'ambiguous'
        bk.beds24_sync_error = 'unexpected_error_manual_review'
    db.session.commit()


def process_queue():
    from app import db, Booking, stripe, app
    # Recover paid checkouts even if a guest closes the browser before returning.
    if stripe:
        waiting = Booking.query.filter(Booking.payment_verified.is_(False), Booking.stripe_session_id != '',
            Booking.created_at >= datetime.utcnow() - timedelta(days=7)).limit(20).all()
        for bk in waiting:
            try:
                checkout = stripe.checkout.Session.retrieve(bk.stripe_session_id)
                if checkout.get('payment_status') == 'paid':
                    accept_payment(bk, checkout)
            except Exception:
                db.session.rollback()
    cutoff = datetime.utcnow() - timedelta(minutes=5)
    queued = Booking.query.filter(Booking.payment_verified.is_(True), Booking.payment_live.is_(True),
        Booking.beds24_sync_state.in_(['pending', 'error', 'ambiguous', 'working']),
        db.or_(Booking.beds24_attempted_at.is_(None), Booking.beds24_attempted_at < cutoff)).limit(20).all()
    for bk in queued:
        sync_booking(bk.id)
        app.logger.warning('[booking-export] local_id=%s state=%s code=%s', bk.id, bk.beds24_sync_state, bk.beds24_sync_error)


def connection_status():
    from app import Room
    rooms = Room.query.filter_by(is_active=True).all()
    result = {'mapped_rooms': sum(bool(r.beds24_room_id) for r in rooms), 'published_rooms': len(rooms)}
    try:
        client = Client()
        data = client.get('/properties', {'includeAllRooms': 'true'})
        accessible = {str(r.get('id')) for p in data.get('data', []) for r in p.get('rooms', [])}
        result.update(status='authorized', accessible_rooms=sum(str(r.beds24_room_id) in accessible for r in rooms))
    except SyncError as e:
        result.update(status='blocked', code=e.code)
    return result
