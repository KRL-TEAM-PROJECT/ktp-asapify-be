"""Telegram alert groups and their outbox: linked groups, one job per group per published alert, claim and finish."""
from __future__ import annotations

import os
import secrets
from datetime import timedelta
from functools import lru_cache

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from .core import ApiProblem, as_time, iso, utc_now
from .notification_policy import group_job, key

GROUPS = 'telegram_groups'            # linked Telegram groups, keyed by key(chat id); every alert goes to each
GROUP_LINKS = 'telegram_group_links'  # one-time codes for linking a group, keyed by key(code)


class NotificationStore:
    def __init__(self, db):
        self.db = db

    def group_link(self, actor):
        code = secrets.token_urlsafe(24)
        # Hash only, 10 minutes, used once. Telegram hands it back as /start <code> in the group the operator picked.
        self.db.collection(GROUP_LINKS).document(key(code)).set(
            {'by': actor['email'], 'expires_at': iso(utc_now() + timedelta(minutes=10))})
        return code

    def link_group(self, code, chat, title):
        link_ref = self.db.collection(GROUP_LINKS).document(key(code))
        group_ref = self.db.collection(GROUPS).document(key(chat))

        @firestore.transactional
        def link(tx):
            snap = link_ref.get(transaction=tx)
            if not snap.exists or snap.to_dict()['expires_at'] <= iso(utc_now()):
                return False
            tx.delete(link_ref)
            tx.set(group_ref, {'id': group_ref.id, 'chat_id': chat, 'title': title[:128], 'invite_url': None,
                               'linked_by': snap.to_dict()['by'], 'linked_at': iso(utc_now())})
            return True
        return link(self.db.transaction())

    def migrate_group(self, old_chat, new_chat):
        """Telegram gives a group a new chat id when it becomes a supergroup; keep the link."""
        old_ref = self.db.collection(GROUPS).document(key(old_chat))
        snap = old_ref.get()
        if not snap.exists:
            return
        new_ref = self.db.collection(GROUPS).document(key(new_chat))
        batch = self.db.batch()
        batch.set(new_ref, {**snap.to_dict(), 'id': new_ref.id, 'chat_id': new_chat})
        batch.delete(old_ref)
        batch.commit()

    def group_linked(self, chat):
        return self.db.collection(GROUPS).document(key(chat)).get().exists

    def groups(self):
        rows = [s.to_dict() for s in self.db.collection(GROUPS).stream()]
        # The chat id stays on the server; the operator sees the group's title and its invite link.
        return sorted(({k: r.get(k) for k in ('id', 'title', 'invite_url', 'linked_by', 'linked_at')} for r in rows),
                      key=lambda g: g['linked_at'] or '')

    def join_links(self):
        """What the public join page shows: the title and invite link of every group that has one."""
        return [{'title': g['title'], 'invite_url': g['invite_url']} for g in self.groups() if g['invite_url']]

    def set_invite(self, group_id, invite_url):
        ref = self.db.collection(GROUPS).document(group_id)
        if not ref.get().exists:
            raise ApiProblem(404, 'NOT_FOUND', 'Telegram group not found.')
        ref.update({'invite_url': invite_url})
        return {k: v for k, v in ref.get().to_dict().items() if k != 'chat_id'}

    def unlink_group(self, group_id):
        ref = self.db.collection(GROUPS).document(group_id)
        if not ref.get().exists:
            raise ApiProblem(404, 'NOT_FOUND', 'Telegram group not found.')
        ref.delete()

    def fan_out(self, alert_id):
        """One job per linked group, after the decision commits.

        IDs are deterministic and documents are only created, so a resumed or concurrent run skips what exists and
        never resets a claimed job.
        """
        alert_ref = self.db.collection('alerts').document(alert_id)
        alert = alert_ref.get().to_dict()
        # `text` = the caption plus the coordinates, map link and slot (alert_text.py); alerts older than it: the caption
        payload = {'id': alert['id'], 'province': str(alert['province']), 'text': alert.get('text') or alert['caption'],
                   'purpose': alert['purpose'], 'expires_at': alert['expires_at']}
        groups = [s.id for s in self.db.collection(GROUPS).stream()]
        self._create_missing([('notification_jobs', group_job(payload, g, alert['published_at'])) for g in groups])
        summary = {'state': 'outbox_saved' if groups else 'no_groups', 'groups': len(groups), 'jobs': len(groups)}
        batch = self.db.batch()
        batch.update(alert_ref, {'fanout': 'done', 'notifications': summary})
        # The alert points at the verification document that holds the decision.
        batch.update(self.db.document(alert['source_path']), {'decision.notifications': summary})
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
                continue  # A concurrent run wrote part of this batch; read again.
        raise ApiProblem(503, 'FANOUT_CONFLICT', 'Outbox writes collided; the sweep will resume.')

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
                raise ApiProblem(503, 'NOT_DUE', 'The job is not due yet.')
            group = self.db.collection(GROUPS).document(job['group_id']).get(transaction=tx).to_dict()
            alert = self.db.collection('alerts').document(job['alert_id']).get(transaction=tx).to_dict() or {}
            reason = ('GROUP_UNLINKED' if not group else 'EXPIRED_OR_CLOSED'
                      if alert.get('status') != 'active' or as_time(job['expires_at']) <= as_time(now) else None)
            if reason:
                tx.update(ref, {'status': 'cancelled', 'error': reason, 'finished_at': now})
                return None
            tx.update(ref, {'status': 'sending', 'started_at': now, 'attempts': job['attempts'] + 1})
            return job, group
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
            item = {k: r.get(k) for k in ('id', 'channel', 'status', 'error', 'attempts', 'created_at', 'started_at', 'finished_at')}
            group = self.db.collection(GROUPS).document(r['group_id']).get().to_dict() if r.get('group_id') else None
            item['group'] = (group or {}).get('title')   # None once the group was unlinked
            result.append(item)
        return result


@lru_cache
def get_notification_store():
    project = os.getenv('GOOGLE_CLOUD_PROJECT')
    if not project:
        raise ApiProblem(503, 'NOTIFICATIONS_NOT_CONFIGURED', 'The notification project is not configured.')
    return NotificationStore(firestore.Client(project=project, database=os.getenv('FIRESTORE_DATABASE', 'asapify')))
