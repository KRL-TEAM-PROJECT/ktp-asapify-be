import copy
import importlib
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from asapify_api.app import app
from asapify_api.auth import require_user
from asapify_api.core import ApiProblem
from asapify_api.notification_policy import Preferences, alert_payload, plan_recipient, dispatch_guard
from asapify_api.notification_delivery import send
from asapify_api.notification_store import get_notification_store
from asapify_api.notification_tasks import CloudTasks, require_worker
from asapify_api import notification_tasks

NOW = '2026-10-08T23:00:00Z'
PROFILE = {'uid': 'test-user', 'provinces': ['62'], 'consent': True, 'exercises': True,
           'channels': ['telegram', 'fcm', 'web_push', 'whatsapp', 'sms', 'gmail', 'lora', 'bluetooth_mesh'],
           'telegram_chat_id': '123', 'fcm_token': 'test-device-token-123456789'}
CLUSTER = {'id': 'C-test', 'province': '62', 'replay': True}


def planned(purpose='exercise'):
    alert = alert_payload({**CLUSTER, 'replay': purpose == 'exercise'}, '<b>WARNING</b> anomaly indication.', NOW)
    return plan_recipient(alert, copy.deepcopy(PROFILE), NOW)


def test_emergency_excludes_fcm_and_does_not_delay_sms_whatsapp():
    inbox, jobs = planned('emergency')
    assert inbox['purpose'] == 'emergency'
    assert next(j for j in jobs if j['channel'] == 'web_push')['error'] == 'FCM_EMERGENCY_PROHIBITED'
    assert all(j['not_before'] == NOW for j in jobs if j['channel'] in ('sms', 'whatsapp'))
    assert next(j for j in jobs if j['channel'] == 'telegram')['status'] == 'pending'


def test_one_push_transport_and_stable_nonduplicated_job_ids():
    inbox, jobs = planned()
    assert [j['channel'] for j in jobs].count('web_push') == 1
    assert all(j['channel'] != 'fcm' for j in jobs)
    assert planned() == (inbox, jobs)
    assert len({j['id'] for j in jobs}) == len(jobs)


@pytest.mark.parametrize('change', [{'consent': False}, {'provinces': ['11']}, {'exercises': False}])
def test_consent_region_and_exercise_opt_out(change):
    assert plan_recipient(alert_payload(CLUSTER, 'EXERCISE', NOW), {**PROFILE, **change}, NOW) == (None, [])


def test_future_adapters_blocked_and_escalation_due_time():
    _, jobs = planned()
    for j in jobs:
        if j['channel'] in ('sms', 'whatsapp', 'gmail', 'lora', 'bluetooth_mesh'):
            assert j['status'] == 'blocked'
        if j['channel'] in ('sms', 'whatsapp'):
            assert j['not_before'] == '2026-10-08T23:02:00Z' and j['stage'] == 'escalation'


@pytest.mark.parametrize('change,profile,reason', [
    ({'acknowledged_at': NOW}, {}, 'ACKNOWLEDGED'),
    ({'status': 'closed'}, {}, 'EXPIRED_OR_CLOSED'),
    ({}, {'consent': False}, 'CONSENT_REVOKED'),
    ({}, {'channels': []}, 'CONSENT_REVOKED'),
    ({}, {'provinces': []}, 'CONSENT_REVOKED'),
])
def test_last_moment_guard(change, profile, reason):
    inbox, jobs = planned()
    assert dispatch_guard(jobs[0], {**inbox, **change}, {**PROFILE, **profile}, NOW) == reason


def test_expired_message_cancelled():
    inbox, jobs = planned()
    assert dispatch_guard(jobs[0], inbox, PROFILE, '2026-10-09T00:00:00Z') == 'EXPIRED_OR_CLOSED'


@pytest.mark.parametrize('body', [
    {'consent': True, 'provinces': ['62'], 'telegram_chat_id': 'victim'},
    {'consent': True, 'provinces': ['62'], 'channels': ['sms', 'sms']},
    {'consent': True, 'provinces': ['99']},
])
def test_preferences_reject_arbitrary_endpoints_or_invalid_choices(body):
    with pytest.raises(ValidationError): Preferences.model_validate(body)


def test_dry_run_never_contacts_provider(monkeypatch):
    monkeypatch.delenv('NOTIFICATION_MODE', raising=False)
    monkeypatch.setattr(httpx.Client, 'post', lambda *a, **k: pytest.fail('external call'))
    inbox, jobs = planned()
    assert send(jobs[0], inbox, PROFILE)['status'] == 'simulated'


def test_emergency_guard_precedes_live_provider(monkeypatch):
    monkeypatch.setenv('NOTIFICATION_MODE', 'live'); monkeypatch.setenv('FCM_ENABLED', 'true')
    inbox, jobs = planned('emergency')
    assert send(next(j for j in jobs if j['channel'] == 'web_push'), inbox, PROFILE)['error'] == 'FCM_EMERGENCY_PROHIBITED'


def live(monkeypatch):
    monkeypatch.setenv('NOTIFICATION_MODE', 'live')
    monkeypatch.setenv('ASAPIFY_PUBLIC_ORIGIN', 'https://asapify.example')
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'sensitive-token')
    monkeypatch.setenv('TELEGRAM_ENABLED', 'true')


def test_timeout_unknown_no_retry_or_secret_leak(monkeypatch):
    live(monkeypatch)
    post = Mock(side_effect=httpx.ReadTimeout('sensitive-token'))
    monkeypatch.setattr(httpx.Client, 'post', post)
    inbox, jobs = planned(); result = send(jobs[0], inbox, PROFILE)
    assert result['status'] == 'unknown' and post.call_count == 1
    assert 'sensitive' not in str(result)


def test_telegram_acceptance_is_not_delivery_and_uses_lightweight_text(monkeypatch):
    live(monkeypatch)
    post = Mock(return_value=httpx.Response(200, json={'ok': True, 'result': {'message_id': 42}}))
    monkeypatch.setattr(httpx.Client, 'post', post)
    inbox, jobs = planned()
    assert send(jobs[0], inbox, PROFILE)['status'] == 'accepted'
    body = post.call_args.kwargs['json']
    assert body['text'].startswith('[EXERCISE]') and '<b>' not in body['text']
    assert 'photo' not in body and 'parse_mode' not in body and inbox['acknowledged_at'] is None


def test_fcm_adapter_for_exercises(monkeypatch):
    module = importlib.import_module('asapify_api.notification_delivery')
    live(monkeypatch); monkeypatch.setenv('FCM_ENABLED', 'true')
    monkeypatch.setattr(module, 'firebase_app', lambda: None)
    sender = Mock(return_value='projects/test/messages/123'); monkeypatch.setattr(module.messaging, 'send', sender)
    inbox, jobs = planned()
    assert send(next(j for j in jobs if j['channel'] == 'web_push'), inbox, PROFILE)['status'] == 'accepted'
    assert sender.call_args.args[0].android.priority == 'normal'


@pytest.fixture(autouse=True)
def clean_app():
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


def test_inbox_and_worker_require_auth(monkeypatch):
    monkeypatch.setenv('NOTIFICATION_WORKER_URL', 'https://worker.run.app')
    monkeypatch.setenv('NOTIFICATION_TASK_CALLER', 'task@test')
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get('/api/v1/notifications/inbox').status_code == 401
    assert c.post('/_internal/notifications/dispatch', json={'job_id': 'a'*64}).status_code == 401


@pytest.mark.parametrize('claims', [
    {'email': 'attacker@test'}, {'email': 'task@test', 'email_verified': False},
    {'email': 'task@test', 'email_verified': True, 'iss': 'wrong'},
])
def test_worker_rejects_wrong_identity(monkeypatch, claims):
    monkeypatch.setenv('NOTIFICATION_WORKER_URL', 'https://worker.run.app'); monkeypatch.setenv('NOTIFICATION_TASK_CALLER', 'task@test')
    monkeypatch.setattr(notification_tasks.id_token, 'verify_oauth2_token', lambda *a, **k: claims)
    with pytest.raises(ApiProblem) as e: require_worker('Bearer test')
    assert e.value.status == 403


def test_tasks_stable_name_oidc_without_pii():
    task = CloudTasks.__new__(CloudTasks)
    task.parent, task.origin, task.caller = 'projects/test/locations/r/queues/q', 'https://worker.run.app', 'task@test'
    task.session = Mock(); task.session.post.return_value = httpx.Response(409)
    _, jobs = planned(); task.enqueue(jobs[0]); task.enqueue(jobs[0])
    first, second = [c.kwargs['json'] for c in task.session.post.call_args_list]
    assert first == second
    assert first['task']['httpRequest']['oidcToken'] == {'serviceAccountEmail': 'task@test', 'audience': task.origin}
    assert 'test-user' not in str(first) and 'test-device-token' not in str(first)


def test_duplicate_dispatch_calls_send_once(monkeypatch):
    routes = importlib.import_module('asapify_api.notification_routes'); inbox, jobs = planned()
    store = Mock(); store.claim.side_effect = [(jobs[0], inbox, PROFILE), None]
    sender = Mock(return_value={'status': 'accepted'}); monkeypatch.setattr(routes, 'send', sender)
    app.dependency_overrides[require_worker] = lambda: None
    app.dependency_overrides[get_notification_store] = lambda: store
    c = TestClient(app)
    for _ in range(2): assert c.post('/_internal/notifications/dispatch', json={'job_id': jobs[0]['id']}).status_code == 200
    assert sender.call_count == store.finish.call_count == 1


def test_profile_redacts_endpoints():
    from asapify_api.notification_routes import public_profile
    result = public_profile(PROFILE)
    assert 'fcm_token' not in result and 'telegram_chat_id' not in result


def test_receiver_inbox_without_operator_role():
    app.dependency_overrides[require_user] = lambda: {'uid': 'u', 'email': 'u@example.test'}
    store = Mock(); store.inbox.return_value = []; app.dependency_overrides[get_notification_store] = lambda: store
    c = TestClient(app)
    assert c.get('/api/v1/notifications/inbox').status_code == 200
    store.inbox.assert_called_once_with('u')
    assert c.get('/api/v1/operator/alerts/x/deliveries').status_code == 401


def test_deploy_dry_run_private_worker_and_limited_queue(monkeypatch, capsys):
    from gcp.notifications import NotificationsDeploy
    monkeypatch.setattr('subprocess.run', lambda *a, **k: pytest.fail('unexpected execution'))
    d = NotificationsDeploy('asapify-test-project', 'asia-southeast2', False)
    d.bootstrap_notifications(); d.deploy_worker('notification-v1', 'https://test.web.app')
    plan = capsys.readouterr().out
    assert 'allUsers' not in plan and '--no-allow-unauthenticated' in plan
    assert 'NOTIFICATION_MODE=dry_run' in plan and '--max-concurrent-dispatches=2' in plan
    assert 'roles/owner' not in plan and 'roles/editor' not in plan


def test_ack_is_owner_scoped_and_idempotent(monkeypatch):
    from asapify_api.notification_store import NotificationStore
    from asapify_api.notification_policy import key
    from google.cloud import firestore
    monkeypatch.setattr(firestore, 'transactional', lambda f: f)
    ref, db, tx = Mock(), Mock(), Mock()
    db.collection.return_value.document.return_value = ref
    db.transaction.return_value = tx
    snapshot = Mock(exists=True)
    snapshot.to_dict.return_value = {'recipient_id': key('owner'), 'acknowledged_at': '2026-10-08T23:15:00Z'}
    ref.get.return_value = snapshot
    store = NotificationStore(db)
    with pytest.raises(ApiProblem) as exc: store.acknowledge('attacker', 'a'*64)
    assert exc.value.status == 404 and tx.update.call_count == 0
    assert store.acknowledge('owner', 'a'*64)['acknowledged_at'] == '2026-10-08T23:15:00Z'
    assert tx.update.call_count == 0


def test_unlinked_telegram_does_not_turn_into_arbitrary_target():
    profile = {**PROFILE, 'telegram_chat_id': None}
    _, jobs = plan_recipient(alert_payload(CLUSTER, 'EXERCISE', NOW), profile, NOW)
    assert next(j for j in jobs if j['channel'] == 'telegram')['error'] == 'TELEGRAM_NOT_LINKED'


def test_claim_transaction_cancels_after_ack_and_reserves_once(monkeypatch):
    from asapify_api.notification_store import NotificationStore
    from google.cloud import firestore
    from datetime import timedelta
    from asapify_api.core import iso, utc_now
    monkeypatch.setattr(firestore, 'transactional', lambda f: f)
    inbox, jobs = planned()
    job = {**jobs[0], 'expires_at': iso(utc_now() + timedelta(hours=1)), 'not_before': iso(utc_now()-timedelta(seconds=1))}
    values = {'notification_jobs': job, 'notification_inbox': inbox, 'notification_recipients': PROFILE}
    db, tx = Mock(), Mock()
    def collection(name):
        doc = Mock(); doc.get.side_effect = lambda **kw: Mock(exists=True, to_dict=lambda: copy.deepcopy(values[name]))
        doc.kind = name
        c = Mock(); c.document.return_value = doc; return c
    db.collection.side_effect = collection; db.transaction.return_value = tx
    tx.update.side_effect = lambda ref, fields: values[ref.kind].update(fields)
    store = NotificationStore(db)
    assert store.claim(job['id']) is not None
    assert store.claim(job['id']) is None
    job['status'] = 'pending'; inbox['acknowledged_at'] = NOW
    assert store.claim(job['id']) is None
    assert job['status'] == 'cancelled' and job['error'] == 'ACKNOWLEDGED'


class FakeSnap:
    def __init__(self, ref, data):
        self.reference, self.id, self._data = ref, ref.id, data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return copy.deepcopy(self._data)


class FakeRef:
    def __init__(self, db, kind, doc_id):
        self.db, self.id, self.path = db, doc_id, f'{kind}/{doc_id}'

    def get(self, **kw):
        return FakeSnap(self, self.db.docs.get(self.path))


class FakeQuery:
    """Only what fan-out uses: where (== / array_contains), order by id, limit, start_after, stream."""
    def __init__(self, db, kind, filters=(), size=None, after=None):
        self.db, self.kind, self.filters, self.size, self.after = db, kind, filters, size, after

    def document(self, doc_id):
        return FakeRef(self.db, self.kind, doc_id)

    def where(self, filter):
        return FakeQuery(self.db, self.kind, self.filters + (filter,), self.size, self.after)

    def order_by(self, field):
        return self

    def limit(self, size):
        return FakeQuery(self.db, self.kind, self.filters, size, self.after)

    def start_after(self, snapshot):
        return FakeQuery(self.db, self.kind, self.filters, self.size, snapshot.id)

    def stream(self):
        prefix = self.kind + '/'
        rows = sorted((p[len(prefix):], d) for p, d in self.db.docs.items() if p.startswith(prefix))
        for f in self.filters:
            match = (lambda d, f=f: f.value in d.get(f.field_path, [])) if f.op_string == 'array_contains' \
                else (lambda d, f=f: d.get(f.field_path) == f.value)
            rows = [(i, d) for i, d in rows if match(d)]
        rows = [(i, d) for i, d in rows if self.after is None or i > self.after][:self.size]
        return [FakeSnap(self.document(i), d) for i, d in rows]


class FakeBatch:
    def __init__(self, db):
        self.db, self.ops = db, []

    def create(self, ref, data):
        self.ops.append(('create', ref, data))

    def update(self, ref, fields):
        self.ops.append(('update', ref, fields))

    def commit(self):
        from google.api_core.exceptions import AlreadyExists
        if any(op == 'create' and ref.path in self.db.docs for op, ref, _ in self.ops):
            raise AlreadyExists('document exists')
        for op, ref, data in self.ops:
            if op == 'create':
                self.db.docs[ref.path] = copy.deepcopy(data)
                continue
            for field, value in data.items():
                target, *parts = self.db.docs[ref.path], *field.split('.')
                for part in parts[:-1]:
                    target = target.setdefault(part, {})
                target[parts[-1]] = value


class FakeDB:
    def __init__(self):
        self.docs, self.stale_reads = {}, 0

    def collection(self, kind):
        return FakeQuery(self, kind)

    def get_all(self, refs):
        if self.stale_reads:  # Simulates a concurrent run that wrote after this read.
            self.stale_reads -= 1
            return [FakeSnap(r, None) for r in refs]
        return [r.get() for r in refs]

    def batch(self):
        return FakeBatch(self)


def fanout_db(subscribers):
    db = FakeDB()
    db.docs['alerts/A-test'] = {'id': 'A-test', 'cluster_id': 'C-test', 'province': '62', 'caption': '<b>WARNING</b>',
                                'purpose': 'emergency', 'published_at': NOW, 'expires_at': '2026-10-09T00:00:00Z',
                                'status': 'active', 'fanout': 'pending'}
    db.docs['clusters/C-test'] = {'id': 'C-test', 'decision': {'action': 'publish', 'notifications': {'state': 'fanout_pending'}}}
    subscriber = {'provinces': ['62'], 'consent': True, 'channels': ['telegram']}
    for i in range(subscribers):
        db.docs[f'notification_recipients/r{i:03}'] = {**subscriber, 'uid': f'u{i}', 'telegram_chat_id': str(i)}
    db.docs['notification_recipients/z-other'] = {**subscriber, 'uid': 'other', 'provinces': ['11']}
    db.docs['notification_recipients/z-off'] = {**subscriber, 'uid': 'off', 'consent': False}
    return db


def count(db, kind):
    return sum(p.startswith(kind + '/') for p in db.docs)


def test_fanout_has_no_subscriber_cap_and_reaches_everyone_in_pages():
    from asapify_api.notification_store import NotificationStore
    db = fanout_db(120)
    summary = NotificationStore(db).fan_out('A-test')
    assert summary == {'state': 'outbox_saved', 'recipients': 120, 'jobs': 120}
    assert count(db, 'notification_inbox') == 120 and count(db, 'notification_jobs') == 120
    assert db.docs['alerts/A-test']['fanout'] == 'done'
    assert db.docs['clusters/C-test']['decision']['notifications'] == summary


def test_resumed_fanout_skips_existing_and_never_resets_a_claimed_job():
    from asapify_api.notification_store import NotificationStore
    db = fanout_db(60)
    store = NotificationStore(db)
    store.fan_out('A-test')
    claimed = next(p for p in db.docs if p.startswith('notification_jobs/'))
    db.docs[claimed]['status'] = 'sending'
    db.docs['alerts/A-test']['fanout'] = 'pending'  # e.g. the API died before marking it done
    assert store.resume_fanouts() == 1
    assert db.docs[claimed]['status'] == 'sending'
    assert count(db, 'notification_jobs') == 60 and db.docs['alerts/A-test']['fanout'] == 'done'


def test_concurrent_fanout_conflict_is_retried_without_duplicates():
    from asapify_api.notification_store import NotificationStore
    db = fanout_db(3)
    store = NotificationStore(db)
    store.fan_out('A-test')
    db.stale_reads = 1  # first read misses documents another run already created
    assert store.fan_out('A-test')['recipients'] == 3
    assert count(db, 'notification_inbox') == 3


def test_no_subscribers_is_reported_not_failed():
    from asapify_api.notification_store import NotificationStore
    assert NotificationStore(fanout_db(0)).fan_out('A-test') == {'state': 'no_recipients', 'recipients': 0, 'jobs': 0}
