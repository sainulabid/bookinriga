import os
import tempfile
import unittest
from datetime import date, timedelta, datetime
from unittest.mock import Mock, patch

os.environ['DATABASE_URL'] = 'sqlite:///' + tempfile.mktemp(suffix='.db')
from app import app, db, User, Room, Booking
from beds24_booking import accept_payment, sync_booking, Client, SyncError, cents, process_queue
import beds24_booking


class BookingExportTests(unittest.TestCase):
    def setUp(self):
        app.testing = True
        self.context = app.app_context(); self.context.push()
        db.drop_all(); db.create_all()
        user = User(name='Test Guest', email='guest@example.test', phone='123')
        room = Room(name='Reference', price=50, room_type='Apartment', beds24_room_id=149472,
                    reference_unit_id=2015, capacity=4)
        db.session.add_all([user, room]); db.session.flush()
        self.bk = Booking(user_id=user.id, room_id=room.id, check_in=date.today()+timedelta(days=3),
            check_out=date.today()+timedelta(days=5), guests=2, total_price=100.01, stripe_session_id='cs_live_test')
        db.session.add(self.bk); db.session.commit()
        self.payment = {'id': 'cs_live_test', 'payment_status': 'paid', 'amount_total': 10001,
                        'currency': 'eur', 'livemode': True}

    def tearDown(self):
        db.session.remove(); self.context.pop()

    def ready(self):
        accept_payment(self.bk, self.payment)

    def remote(self):
        return {'id': 1234567, 'status': 'confirmed', 'roomId': 149472,
                'arrival': self.bk.check_in.isoformat(), 'departure': self.bk.check_out.isoformat(),
                'apiReference': self.bk.beds24_reference}

    def login(self, client):
        with client.session_transaction() as session:
            session['_user_id'] = str(self.bk.user_id); session['_fresh'] = True
            session['booking_csrf_token'] = 'test-csrf'

    def test_payment_verified_and_idempotent(self):
        self.ready(); reference = self.bk.beds24_reference
        self.bk.beds24_sync_state = 'working'; db.session.commit()
        accept_payment(self.bk, self.payment)
        self.assertEqual(self.bk.beds24_sync_state, 'working')
        self.assertEqual(self.bk.beds24_reference, reference)
        self.assertEqual(cents(0.29), 29)

    def test_invalid_payment_cannot_confirm(self):
        for change in ({'id':'other'}, {'payment_status':'unpaid'}, {'currency':'usd'}, {'amount_total':1}):
            with self.assertRaises(SyncError): accept_payment(self.bk, {**self.payment, **change})
        self.assertFalse(self.bk.payment_verified); self.assertEqual(self.bk.status, 'Pending')

    def test_test_payments_never_export(self):
        accept_payment(self.bk, {**self.payment, 'livemode':False})
        with patch('beds24_booking.Client') as client: sync_booking(self.bk.id); client.assert_not_called()
        self.assertEqual(self.bk.payment_status, 'Test payment')
        self.assertIsNone(self.bk.beds24_booking_id)

    def test_success_payload_and_no_duplicate(self):
        self.ready()
        with patch('beds24_booking.Client') as ctor:
            client=ctor.return_value; client.find.side_effect=[None,self.remote()]
            client.post.return_value={'success':True,'new':{'id':1234567}}
            sync_booking(self.bk.id); sync_booking(self.bk.id)
            self.assertEqual(client.post.call_count,1)
            payload=client.post.call_args.args[0]
            self.assertEqual(payload['roomId'],149472)
            self.assertNotEqual(payload['roomId'],2015)
            self.assertEqual(payload['price'],100.01)
            self.assertTrue(payload['actions']['checkAvailability'])
            self.assertEqual(payload['custom1'],self.bk.beds24_reference)
        self.assertEqual(self.bk.status,'Confirmed'); self.assertEqual(self.bk.beds24_booking_id,1234567)

    def test_lost_response_reconciles_without_another_create(self):
        self.ready()
        with patch('beds24_booking.Client') as ctor:
            c=ctor.return_value; c.find.return_value=None
            c.post.side_effect=SyncError('write_response_unknown',True)
            sync_booking(self.bk.id)
            self.assertEqual(self.bk.beds24_sync_state,'ambiguous')
            self.assertEqual(self.bk.status,'Pending')
            c.find.return_value=self.remote(); sync_booking(self.bk.id)
            self.assertEqual(c.post.call_count,1)
        self.assertEqual(self.bk.beds24_sync_state,'synced')

    def test_unknown_create_not_blindly_retried(self):
        self.ready(); self.bk.beds24_sync_state='ambiguous'; db.session.commit()
        with patch('beds24_booking.Client') as ctor:
            ctor.return_value.find.return_value=None
            sync_booking(self.bk.id); ctor.return_value.post.assert_not_called()
        self.assertEqual(self.bk.beds24_sync_state,'ambiguous')

    def test_auth_failure_retains_pending(self):
        self.ready()
        with patch('beds24_booking.Client',side_effect=SyncError('authentication_failed_401')):
            sync_booking(self.bk.id)
        self.assertEqual(self.bk.status,'Pending'); self.assertEqual(self.bk.beds24_sync_state,'error')

    def test_partial_post_failure_is_ambiguous(self):
        client=Client.__new__(Client); client.token='not-real'
        response=Mock(status_code=201); response.json.return_value=[{'success':False,'new':{'id':123}}]
        with patch('beds24_booking.requests.post',return_value=response):
            with self.assertRaises(SyncError) as error: client.post({'roomId':1})
            self.assertTrue(error.exception.ambiguous)

    def test_stale_worker_never_recreates_unknown_booking(self):
        self.ready(); self.bk.beds24_sync_state='working'
        self.bk.beds24_attempted_at=datetime.utcnow()-timedelta(minutes=10); db.session.commit()
        with patch('beds24_booking.Client') as ctor:
            ctor.return_value.find.return_value=None; sync_booking(self.bk.id)
            ctor.return_value.post.assert_not_called()
        self.assertEqual(self.bk.beds24_sync_state,'ambiguous')

    def test_recent_worker_not_claimed(self):
        self.ready(); self.bk.beds24_sync_state='working'; self.bk.beds24_attempted_at=datetime.utcnow(); db.session.commit()
        with patch('beds24_booking.Client') as ctor: sync_booking(self.bk.id); ctor.assert_not_called()

    def test_cancellation_updates_remote_then_local(self):
        self.ready(); self.bk.beds24_booking_id=1234567; self.bk.status='Confirmed'; self.bk.cancel_requested=True; db.session.commit()
        with patch('beds24_booking.Client') as ctor:
            ctor.return_value.find.side_effect=[self.remote(), {**self.remote(), 'status':'cancelled'}]
            sync_booking(self.bk.id)
            ctor.return_value.post.assert_called_once_with({'id':1234567,'status':'cancelled'})
        self.assertEqual(self.bk.status,'Cancelled')

    def test_cancellation_failure_does_not_free_local_inventory(self):
        self.ready(); self.bk.beds24_booking_id=1234567; self.bk.status='Confirmed'; self.bk.cancel_requested=True; db.session.commit()
        with patch('beds24_booking.Client') as ctor:
            ctor.return_value.find.return_value=self.remote()
            ctor.return_value.post.side_effect=SyncError('write_response_unknown',True)
            sync_booking(self.bk.id)
        self.assertEqual(self.bk.status,'Confirmed'); self.assertEqual(self.bk.beds24_sync_state,'ambiguous')

    def test_known_id_requires_remote_readback(self):
        self.ready(); self.bk.beds24_booking_id=1234567; db.session.commit()
        with patch('beds24_booking.Client') as ctor:
            ctor.return_value.find.return_value=None; sync_booking(self.bk.id)
            ctor.return_value.post.assert_not_called()
        self.assertEqual(self.bk.status,'Pending'); self.assertEqual(self.bk.beds24_sync_state,'ambiguous')

    def test_cancellation_ack_without_readback_stays_blocked(self):
        self.ready(); self.bk.beds24_booking_id=1234567; self.bk.status='Confirmed'; self.bk.cancel_requested=True; db.session.commit()
        with patch('beds24_booking.Client') as ctor:
            ctor.return_value.find.return_value=self.remote(); sync_booking(self.bk.id)
        self.assertEqual(self.bk.status,'Confirmed'); self.assertEqual(self.bk.beds24_sync_state,'ambiguous')

    def test_checkout_blocks_before_charge_if_api_unavailable(self):
        with app.test_client() as client:
            self.login(client)
            with patch('app.PAYMENTS_LIVE',True),patch('app.STRIPE_SECRET_KEY','sk_live_fake'),patch('app.stripe') as stripe,patch('beds24_booking.preflight',side_effect=SyncError('authentication_failed_401')):
                stripe.checkout.Session.retrieve.return_value={'status':'open','payment_status':'unpaid','url':'https://checkout.stripe.com/test'}
                response=client.get('/checkout/'+str(self.bk.id))
                self.assertEqual(response.status_code,302); stripe.checkout.Session.create.assert_not_called()

    def test_success_url_does_not_bypass_payment(self):
        with app.test_client() as client:
            self.login(client)
            with patch('app.PAYMENTS_LIVE',True),patch('app.stripe') as stripe:
                stripe.checkout.Session.retrieve.return_value={**self.payment,'payment_status':'unpaid'}
                response=client.get('/booking/'+str(self.bk.id)+'/success')
                self.assertEqual(response.status_code,302)
        self.assertEqual(self.bk.status,'Pending'); self.assertFalse(self.bk.payment_verified)

    def test_signed_webhook_queues_payment_without_export_in_request(self):
        event={'type':'checkout.session.completed','data':{'object':self.payment}}
        with app.test_client() as client,patch.dict(os.environ,{'STRIPE_WEBHOOK_SECRET':'fake'}),patch('app.stripe') as stripe,patch('beds24_booking.Client') as api:
            stripe.Webhook.construct_event.return_value=event
            self.assertEqual(client.post('/webhooks/stripe',data='test').status_code,200)
            api.assert_not_called()
            self.assertTrue(self.bk.payment_verified); self.assertEqual(self.bk.beds24_sync_state,'pending')
            stripe.Webhook.construct_event.side_effect=ValueError('invalid')
            self.assertEqual(client.post('/webhooks/stripe',data='test').status_code,400)

    def test_cancel_csrf_and_queue_loop(self):
        with app.test_client() as client:
            self.login(client)
            self.assertEqual(client.post('/booking/'+str(self.bk.id)+'/cancel-confirmed').status_code,400)
        self.ready()
        with patch('app.stripe',None),patch('beds24_booking.sync_booking') as sync:
            process_queue(); sync.assert_called_once_with(self.bk.id)

    def test_old_unverified_paid_records_not_exported(self):
        self.bk.payment_status='Paid'; self.bk.status='Confirmed'; db.session.commit()
        with patch('beds24_booking.Client') as api: sync_booking(self.bk.id); api.assert_not_called()

    def test_readback_fallback_checks_exact_reference(self):
        self.ready()
        c=Client.__new__(Client); c.token='fake'
        row=self.remote(); row.pop('apiReference'); row['custom1']=self.bk.beds24_reference
        with patch.object(c,'get',side_effect=[{'success':True,'data':[]},{'success':True,'data':[row]}]):
            self.assertEqual(c.find(self.bk)['id'],1234567)
        with patch.object(c,'get',return_value={'success':True,'data':[{**row,'roomId':999}]}):
            with self.assertRaises(SyncError):c.find(self.bk)


if __name__=='__main__':unittest.main()
