from __future__ import annotations

import os
import secrets
from datetime import timedelta
from functools import lru_cache

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from .core import ApiProblem, as_time, iso, utc_now
from .notification_policy import (CONSENT_VERSION, MAX_RECIPIENTS, Preferences, alert_payload,
                                  dispatch_guard, key, plan_recipient)


class NotificationStore:
    def __init__(self, db):
        self.db = db

    def profile_ref(self, uid):
        return self.db.collection('notification_recipients').document(key(uid))

    def profile(self, uid):
        snap = self.profile_ref(uid).get()
        return snap.to_dict() if snap.exists else {'uid': uid, 'provinces': [], 'channels': [], 'consent': False, 'exercises': False}

    def preferences(self, actor, body: Preferences):
        data = {**body.model_dump(), 'uid': actor['uid'], 'email': actor['email'],
                'consent_version': CONSENT_VERSION, 'updated_at': iso(utc_now())}
        self.profile_ref(actor['uid']).set(data, merge=True)
        return self.profile(actor['uid'])

    def device(self, uid, token):
        ref = self.profile_ref(uid)
        owner = self.db.collection('notification_devices').document(key(token))

        @firestore.transactional
        def save(tx):
            old = ref.get(transaction=tx).to_dict() or {}
            registration = owner.get(transaction=tx)
            if registration.exists and registration.to_dict()['uid'] != uid:
                raise ApiProblem(409, 'DEVICE_ALREADY_LINKED', 'Putuskan perangkat dari akun sebelumnya terlebih dahulu.')
            if old.get('fcm_token') and old['fcm_token'] != token:
                tx.delete(self.db.collection('notification_devices').document(key(old['fcm_token'])))
            tx.set(owner, {'uid': uid})
            tx.set(ref, {'uid': uid, 'fcm_token': token}, merge=True)
        save(self.db.transaction())

    def unlink_device(self, uid):
        ref = self.profile_ref(uid)

        @firestore.transactional
        def remove(tx):
            profile = ref.get(transaction=tx).to_dict() or {}
            if profile.get('fcm_token'):
                tx.delete(self.db.collection('notification_devices').document(key(profile['fcm_token'])))
            tx.set(ref, {'fcm_token': firestore.DELETE_FIELD}, merge=True)
        remove(self.db.transaction())

    def telegram_link(self, uid):
        code = secrets.token_urlsafe(24)
        # A single active code per UID; hash only, expires in 10 min, previous code invalidated.
        self.profile_ref(uid).set({'uid': uid, 'telegram_link_hash': key(code),
                                 'telegram_link_expires': iso(utc_now() + timedelta(minutes=10))}, merge=True)
        return code

    def link_telegram(self, code, chat):
        matches = list(self.db.collection('notification_recipients').where(
            filter=FieldFilter('telegram_link_hash', '==', key(code))).limit(1).stream())
        if not matches:
            return False
        ref = matches[0].reference
        owner = self.db.collection('notification_telegram_chats').document(key(chat))

        @firestore.transactional
        def link(tx):
            profile = ref.get(transaction=tx).to_dict() or {}
            binding = owner.get(transaction=tx)
            if profile.get('telegram_link_hash') != key(code) or profile.get('telegram_link_expires', '') <= iso(utc_now()):
                return False
            if binding.exists and binding.to_dict()['uid'] != profile['uid']:
                return False
            if profile.get('telegram_chat_id') and profile['telegram_chat_id'] != chat:
                tx.delete(self.db.collection('notification_telegram_chats').document(key(profile['telegram_chat_id'])))
            tx.set(owner, {'uid': profile['uid']})
            tx.update(ref, {'telegram_chat_id': chat, 'telegram_link_hash': firestore.DELETE_FIELD,
                            'telegram_link_expires': firestore.DELETE_FIELD})
            return True
        return link(self.db.transaction())

    def prepare_publication(self, tx, cluster, caption, now):
        """All query reads happen before the caller's writes in the same decision transaction."""
        recipients = list(self.db.collection('notification_recipients').where(
            filter=FieldFilter('provinces', 'array_contains', str(cluster['province']))).where(filter=FieldFilter('consent', '==', True)).limit(MAX_RECIPIENTS + 1).stream(transaction=tx))
        if len(recipients) > MAX_RECIPIENTS:
            raise ApiProblem(503, 'FANOUT_LIMIT', 'Batas pilot 40 pelanggan per provinsi tercapai; perlu fanout bertahap.')
        alert = alert_payload(cluster, caption, now)
        inboxes, jobs = [], []
        for snapshot in recipients:
            inbox, planned = plan_recipient(alert, snapshot.to_dict(), now)
            if inbox:
                inboxes.append(inbox)
                jobs.extend(planned)
        return inboxes, jobs

    def write_publication(self, tx, inboxes, jobs):
        for inbox in inboxes:
            tx.create(self.db.collection('notification_inbox').document(inbox['id']), inbox)
        for job in jobs:
            tx.create(self.db.collection('notification_jobs').document(job['id']), job)

    def inbox(self, uid):
        # Composite index is checked in under gcp/firestore.indexes.json.
        items = list(self.db.collection('notification_inbox').where(
            filter=FieldFilter('recipient_id', '==', key(uid))).order_by('created_at', direction=firestore.Query.DESCENDING).limit(50).stream())
        result = []
        for snap in items:
            item = snap.to_dict()
            item.pop('recipient_id', None)
            result.append(item)
        return result

    def acknowledge(self, uid, inbox_id):
        ref = self.db.collection('notification_inbox').document(inbox_id)

        @firestore.transactional
        def ack(tx):
            snap = ref.get(transaction=tx)
            if not snap.exists or snap.to_dict()['recipient_id'] != key(uid):
                raise ApiProblem(404, 'NOT_FOUND', 'Notifikasi tidak ditemukan.')
            item = snap.to_dict()
            at = item.get('acknowledged_at') or iso(utc_now())
            if not item.get('acknowledged_at'):
                tx.update(ref, {'acknowledged_at': at})
            return {'acknowledged_at': at}
        return ack(self.db.transaction())

    def pending(self, limit=100):
        return [s.to_dict() for s in self.db.collection('notification_jobs').where(
            filter=FieldFilter('status', '==', 'pending')).order_by('not_before').limit(limit).stream()]

    def claim(self, job_id):
        ref = self.db.collection('notification_jobs').document(job_id)

        @firestore.transactional
        def reserve(tx):
            snap = ref.get(transaction=tx)
            if not snap.exists:
                return None
            job = snap.to_dict()
            if job['status'] != 'pending':
                return None
            now = iso(utc_now())
            if job['not_before'] > now:
                raise ApiProblem(503, 'NOT_DUE', 'Tugas belum waktunya dijalankan.')
            inbox = self.db.collection('notification_inbox').document(job['inbox_id']).get(transaction=tx).to_dict()
            profile = self.db.collection('notification_recipients').document(job['recipient_id']).get(transaction=tx).to_dict() or {}
            reason = dispatch_guard(job, inbox, profile, now)
            if reason:
                tx.update(ref, {'status': 'cancelled', 'error': reason, 'finished_at': now})
                return None
            tx.update(ref, {'status': 'sending', 'started_at': now, 'attempts': job['attempts'] + 1})
            return job, inbox, profile
        return reserve(self.db.transaction())

    def finish(self, job, outcome):
        ref = self.db.collection('notification_jobs').document(job['id'])

        @firestore.transactional
        def save(tx):
            current = ref.get(transaction=tx).to_dict()
            if current and current['status'] == 'sending':
                tx.update(ref, {**outcome, 'finished_at': iso(utc_now())})
        save(self.db.transaction())

    def reconcile_uncertain(self):
        """Crash after send may have delivered. Never resend expired reservations blindly."""
        for snap in self.db.collection('notification_jobs').where(filter=FieldFilter('status', '==', 'sending')).limit(100).stream():
            @firestore.transactional
            def mark(tx):
                current = snap.reference.get(transaction=tx).to_dict()
                if current and current['status'] == 'sending' and as_time(current['started_at']) < utc_now() - timedelta(minutes=5):
                    tx.update(snap.reference, {'status': 'unknown', 'error': 'PROCESS_INTERRUPTED', 'finished_at': iso(utc_now())})
            mark(self.db.transaction())

    def delivery_status(self, alert_id):
        rows = [s.to_dict() for s in self.db.collection('notification_jobs').where(
            filter=FieldFilter('alert_id', '==', alert_id)).stream()]
        result = []
        for r in rows:
            item = {k: r.get(k) for k in ('id', 'channel', 'stage', 'status', 'error', 'attempts', 'created_at', 'started_at', 'finished_at')}
            inbox = self.db.collection('notification_inbox').document(r['inbox_id']).get().to_dict() or {}
            item['acknowledged_at'] = inbox.get('acknowledged_at')
            result.append(item)
        return result


@lru_cache
def get_notification_store():
    project = os.getenv('GOOGLE_CLOUD_PROJECT')
    if not project:
        raise ApiProblem(503, 'NOTIFICATIONS_NOT_CONFIGURED', 'Project notifikasi belum dikonfigurasi.')
    return NotificationStore(firestore.Client(project=project, database=os.getenv('FIRESTORE_DATABASE', 'asapify')))
