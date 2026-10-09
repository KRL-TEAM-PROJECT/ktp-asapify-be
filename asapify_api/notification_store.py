from __future__ import annotations

import os
import secrets
from datetime import timedelta
from functools import lru_cache

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from .core import ApiProblem, as_time, iso, utc_now
from .notification_policy import CONSENT_VERSION, Preferences, dispatch_guard, key, plan_recipient

FANOUT_PAGE = 50  # <= 50 x (1 inbox + 7 jobs) = 400 writes, under the 500-write batch limit.


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

    def fan_out(self, alert_id):
        """Write inbox + channel jobs for every consenting recipient, page by page, after the decision commits.

        The subscriber count never blocks a decision. IDs are deterministic and documents are only
        created, so a resumed or concurrent run skips what exists and never resets a claimed job.
        """
        alert_ref = self.db.collection('alerts').document(alert_id)
        alert = alert_ref.get().to_dict()
        payload = {'id': alert['id'], 'cluster_id': alert['cluster_id'], 'province': str(alert['province']),
                   'text': alert['caption_id'], 'purpose': alert['purpose'], 'published_at': alert['published_at'],
                   'expires_at': alert['expires_at'], 'status': alert['status']}
        query = self.db.collection('notification_recipients').where(
            filter=FieldFilter('provinces', 'array_contains', payload['province'])).where(
            filter=FieldFilter('consent', '==', True)).order_by('__name__').limit(FANOUT_PAGE)
        recipients = jobs = 0
        last = None
        while True:
            page = list((query.start_after(last) if last else query).stream())
            docs = []
            for snapshot in page:
                inbox, planned = plan_recipient(payload, snapshot.to_dict(), payload['published_at'])
                if inbox:
                    recipients, jobs = recipients + 1, jobs + len(planned)
                    docs.append(('notification_inbox', inbox))
                    docs.extend(('notification_jobs', job) for job in planned)
            self._create_missing(docs)
            if len(page) < FANOUT_PAGE:
                break
            last = page[-1]
        summary = {'state': 'outbox_saved' if recipients else 'no_recipients', 'recipients': recipients, 'jobs': jobs}
        batch = self.db.batch()
        batch.update(alert_ref, {'fanout': 'done', 'notifications': summary})
        batch.update(self.db.collection('clusters').document(alert['cluster_id']), {'decision.notifications': summary})
        batch.commit()
        return summary

    def _create_missing(self, docs, attempts=3):
        for _ in range(attempts):
            refs = [self.db.collection(kind).document(doc['id']) for kind, doc in docs]
            existing = {s.reference.path for s in self.db.get_all(refs) if s.exists} if refs else set()
            missing = [(ref, doc) for ref, (_, doc) in zip(refs, docs) if ref.path not in existing]
            if not missing:
                return
            batch = self.db.batch()
            for ref, doc in missing:
                batch.create(ref, doc)
            try:
                batch.commit()
                return
            except AlreadyExists:
                continue  # A concurrent run wrote part of this page; read again.
        raise ApiProblem(503, 'FANOUT_CONFLICT', 'Penulisan inbox bertabrakan; sweep akan melanjutkan.')

    def resume_fanouts(self, limit=20):
        """Finish publications whose fan-out stopped after the decision was saved."""
        done = 0
        for snapshot in self.db.collection('alerts').where(filter=FieldFilter('fanout', '==', 'pending')).limit(limit).stream():
            try:
                self.fan_out(snapshot.id)
                done += 1
            except Exception:
                continue  # Retried on the next sweep; one bad alert must not block the others.
        return done

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
