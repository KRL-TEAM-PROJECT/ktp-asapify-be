"""The alert outbox: one job per linked Telegram group, the worker's send, the queue and its recovery."""
import copy
import importlib
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from asapify_api import notification_tasks
from asapify_api.app import app
from asapify_api.core import ApiProblem
from asapify_api.notification_delivery import send
from asapify_api.notification_policy import group_job, key
from asapify_api.notification_store import NotificationStore, get_notification_store
from asapify_api.notification_tasks import CloudTasks, require_worker

NOW = '2026-10-08T23:00:00Z'
SOURCE = 'engine_regions/R-test/verifications/20261008T230000Z'
GROUP = {'id': 'g' * 64, 'chat_id': '-1001', 'title': 'ASAPify Notified'}


def alert(text='EXERCISE', purpose='exercise'):
    return {'id': 'A-test', 'province': '62', 'text': text, 'purpose': purpose, 'expires_at': '2026-10-09T00:00:00Z'}


def live(monkeypatch):
    monkeypatch.setenv('NOTIFICATION_MODE', 'live')
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'sensitive-token')
    monkeypatch.setenv('TELEGRAM_ENABLED', 'true')


@pytest.fixture(autouse=True)
def clean_app():
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


def test_dry_run_never_contacts_telegram(monkeypatch):
    monkeypatch.delenv('NOTIFICATION_MODE', raising=False)
    monkeypatch.setattr(httpx.Client, 'post', lambda *a, **k: pytest.fail('external call'))
    job = group_job(alert(), GROUP['id'], NOW)
    assert send(job, GROUP)['status'] == 'simulated'


def test_timeout_unknown_no_retry_or_secret_leak(monkeypatch):
    live(monkeypatch)
    post = Mock(side_effect=httpx.ReadTimeout('sensitive-token'))
    monkeypatch.setattr(httpx.Client, 'post', post)
    result = send(group_job(alert(), GROUP['id'], NOW), GROUP)
    assert result['status'] == 'unknown' and post.call_count == 1
    assert 'sensitive' not in str(result)


def test_telegram_acceptance_is_plain_text_to_the_group(monkeypatch):
    live(monkeypatch)
    post = Mock(return_value=httpx.Response(200, json={'ok': True, 'result': {'message_id': 42}}))
    monkeypatch.setattr(httpx.Client, 'post', post)
    assert send(group_job(alert('<b>WARNING</b> anomaly indication.'), GROUP['id'], NOW), GROUP)['status'] == 'accepted'
    body = post.call_args.kwargs['json']
    assert body['chat_id'] == '-1001' and body['text'].startswith('[EXERCISE]') and '<b>' not in body['text']
    assert 'photo' not in body and 'parse_mode' not in body and 'reply_markup' not in body


def test_worker_requires_auth(monkeypatch):
    monkeypatch.setenv('NOTIFICATION_WORKER_URL', 'https://worker.run.app')
    monkeypatch.setenv('NOTIFICATION_TASK_CALLER', 'task@test')
    c = TestClient(app, raise_server_exceptions=False)
    assert c.post('/_internal/notifications/dispatch', json={'job_id': 'a' * 64}).status_code == 401
    assert c.get('/api/v1/operator/alerts/x/deliveries').status_code == 401


@pytest.mark.parametrize('claims', [
    {'email': 'attacker@test'}, {'email': 'task@test', 'email_verified': False},
    {'email': 'task@test', 'email_verified': True, 'iss': 'wrong'},
])
def test_worker_rejects_wrong_identity(monkeypatch, claims):
    monkeypatch.setenv('NOTIFICATION_WORKER_URL', 'https://worker.run.app'); monkeypatch.setenv('NOTIFICATION_TASK_CALLER', 'task@test')
    monkeypatch.setattr(notification_tasks.id_token, 'verify_oauth2_token', lambda *a, **k: claims)
    with pytest.raises(ApiProblem) as e: require_worker('Bearer test')
    assert e.value.status == 403


def test_tasks_stable_name_oidc_without_chat_ids():
    task = CloudTasks.__new__(CloudTasks)
    task.parent, task.origin, task.caller = 'projects/test/locations/r/queues/q', 'https://worker.run.app', 'task@test'
    task.session = Mock(); task.session.post.return_value = httpx.Response(409)
    job = group_job(alert(), GROUP['id'], NOW); task.enqueue(job); task.enqueue(job)
    first, second = [c.kwargs['json'] for c in task.session.post.call_args_list]
    assert first == second
    assert first['task']['httpRequest']['oidcToken'] == {'serviceAccountEmail': 'task@test', 'audience': task.origin}
    assert '-1001' not in str(first) and 'EXERCISE' not in str(first)


def test_duplicate_dispatch_calls_send_once(monkeypatch):
    routes = importlib.import_module('asapify_api.notification_routes')
    job = group_job(alert(), GROUP['id'], NOW)
    store = Mock(); store.claim.side_effect = [(job, GROUP), None]
    sender = Mock(return_value={'status': 'accepted'}); monkeypatch.setattr(routes, 'send', sender)
    app.dependency_overrides[require_worker] = lambda: None
    app.dependency_overrides[get_notification_store] = lambda: store
    c = TestClient(app)
    for _ in range(2): assert c.post('/_internal/notifications/dispatch', json={'job_id': job['id']}).status_code == 200
    assert sender.call_count == store.finish.call_count == 1


def test_deploy_dry_run_private_worker_and_limited_queue(monkeypatch, capsys):
    from gcp.notifications import NotificationsDeploy
    monkeypatch.setattr('subprocess.run', lambda *a, **k: pytest.fail('unexpected execution'))
    d = NotificationsDeploy('asapify-test-project', 'asia-southeast2', False)
    d.bootstrap_notifications(); d.deploy_worker('notification-v1', 'https://test.web.app')
    plan = capsys.readouterr().out
    assert 'allUsers' not in plan and '--no-allow-unauthenticated' in plan
    assert 'NOTIFICATION_MODE=dry_run' in plan and '--max-concurrent-dispatches=2' in plan
    assert 'roles/owner' not in plan and 'roles/editor' not in plan


# ---------- an in-memory Firestore, just what the store uses ----------

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

    def set(self, data):
        self.db.docs[self.path] = copy.deepcopy(data)

    def update(self, fields):
        self.db.docs[self.path].update(copy.deepcopy(fields))

    def delete(self):
        self.db.docs.pop(self.path, None)


class FakeQuery:
    """Only what the store uses: where (==), order by, limit, stream."""
    def __init__(self, db, kind, filters=(), size=None):
        self.db, self.kind, self.filters, self.size = db, kind, filters, size

    def document(self, doc_id):
        return FakeRef(self.db, self.kind, doc_id)

    def where(self, filter):
        return FakeQuery(self.db, self.kind, self.filters + (filter,), self.size)

    def order_by(self, field):
        return self

    def limit(self, size):
        return FakeQuery(self.db, self.kind, self.filters, size)

    def stream(self):
        prefix = self.kind + '/'
        rows = sorted((p[len(prefix):], d) for p, d in self.db.docs.items() if p.startswith(prefix))
        for f in self.filters:
            rows = [(i, d) for i, d in rows if d.get(f.field_path) == f.value]
        return [FakeSnap(self.document(i), d) for i, d in rows[:self.size]]


class FakeBatch:
    def __init__(self, db):
        self.db, self.ops = db, []

    def create(self, ref, data):
        self.ops.append(('create', ref, data))

    def update(self, ref, fields):
        self.ops.append(('update', ref, fields))

    def set(self, ref, data):
        self.ops.append(('set', ref, data))

    def delete(self, ref):
        self.ops.append(('delete', ref, None))

    def commit(self):
        from google.api_core.exceptions import AlreadyExists
        if any(op == 'create' and ref.path in self.db.docs for op, ref, _ in self.ops):
            raise AlreadyExists('document exists')
        for op, ref, data in self.ops:
            if op in ('create', 'set'):
                self.db.docs[ref.path] = copy.deepcopy(data)
                continue
            if op == 'delete':
                self.db.docs.pop(ref.path, None)
                continue
            for field, value in data.items():
                target, *parts = self.db.docs[ref.path], *field.split('.')
                for part in parts[:-1]:
                    target = target.setdefault(part, {})
                target[parts[-1]] = value


class Tx:
    def __init__(self, db):
        self.db = db

    def set(self, ref, data):
        self.db.docs[ref.path] = dict(data)

    def delete(self, ref):
        self.db.docs.pop(ref.path, None)

    def update(self, ref, fields):
        self.db.docs[ref.path].update(fields)


class FakeDB:
    def __init__(self):
        self.docs, self.stale_reads = {}, 0

    def collection(self, kind):
        return FakeQuery(self, kind)

    def document(self, path):
        kind, doc_id = path.rsplit('/', 1)
        return FakeRef(self, kind, doc_id)

    def get_all(self, refs):
        if self.stale_reads:  # Simulates a concurrent run that wrote after this read.
            self.stale_reads -= 1
            return [FakeSnap(r, None) for r in refs]
        return [r.get() for r in refs]

    def batch(self):
        return FakeBatch(self)

    def transaction(self):
        return Tx(self)


def fanout_db(groups):
    """One published alert and `groups` linked Telegram groups."""
    db = FakeDB()
    db.docs['alerts/A-test'] = {'id': 'A-test', 'region_id': 'R-test', 'source_path': SOURCE, 'province': '62',
                                'caption': '<b>WARNING</b>', 'purpose': 'emergency', 'published_at': NOW,
                                'expires_at': '2026-10-09T00:00:00Z', 'status': 'active', 'fanout': 'pending'}
    db.docs[SOURCE] = {'id': '20261008T230000Z', 'decision': {'action': 'publish', 'notifications': {'state': 'fanout_pending'}}}
    for i in range(groups):
        gid = key(f'-100{i}')
        db.docs[f'telegram_groups/{gid}'] = {'id': gid, 'chat_id': f'-100{i}', 'title': f'Group {i}', 'invite_url': None,
                                             'linked_by': 'op@example.test', 'linked_at': NOW}
    return db


def count(db, kind):
    return sum(p.startswith(kind + '/') for p in db.docs)


def test_fanout_queues_one_job_per_group_and_records_the_summary():
    db = fanout_db(3)
    summary = NotificationStore(db).fan_out('A-test')
    assert summary == {'state': 'outbox_saved', 'groups': 3, 'jobs': 3}
    assert count(db, 'notification_jobs') == 3 and db.docs['alerts/A-test']['fanout'] == 'done'
    assert db.docs[SOURCE]['decision']['notifications'] == summary
    job = next(v for k, v in db.docs.items() if k.startswith('notification_jobs/'))
    assert job['channel'] == 'telegram_group' and job['text'] == '<b>WARNING</b>' and job['not_before'] == NOW


def test_resumed_fanout_skips_existing_and_never_resets_a_claimed_job():
    db = fanout_db(2)
    store = NotificationStore(db)
    store.fan_out('A-test')
    claimed = next(p for p in db.docs if p.startswith('notification_jobs/'))
    db.docs[claimed]['status'] = 'sending'
    db.docs['alerts/A-test']['fanout'] = 'pending'  # e.g. the API died before marking it done
    assert store.resume_fanouts() == 1
    assert db.docs[claimed]['status'] == 'sending'
    assert count(db, 'notification_jobs') == 2 and db.docs['alerts/A-test']['fanout'] == 'done'


def test_concurrent_fanout_conflict_is_retried_without_duplicates():
    db = fanout_db(3)
    store = NotificationStore(db)
    store.fan_out('A-test')
    db.stale_reads = 1  # first read misses documents another run already created
    assert store.fan_out('A-test')['groups'] == 3
    assert count(db, 'notification_jobs') == 3


def test_no_group_is_reported_not_failed():
    assert NotificationStore(fanout_db(0)).fan_out('A-test') == {'state': 'no_groups', 'groups': 0, 'jobs': 0}


@pytest.fixture
def tx_identity(monkeypatch):
    from google.cloud import firestore
    monkeypatch.setattr(firestore, 'transactional', lambda f: f)


def test_claim_reserves_once_and_cancels_a_closed_alert(tx_identity, monkeypatch):
    from asapify_api import notification_store
    monkeypatch.setattr(notification_store, 'utc_now', lambda: notification_store.as_time('2026-10-08T23:05:00Z'))
    db = fanout_db(1)
    store = NotificationStore(db)
    store.fan_out('A-test')
    path, job = next((k, v) for k, v in db.docs.items() if k.startswith('notification_jobs/'))
    claimed_job, group = store.claim(job['id'])
    assert group['chat_id'] == '-1000' and db.docs[path]['status'] == 'sending'
    assert store.claim(job['id']) is None   # already sending
    db.docs[path]['status'] = 'pending'
    db.docs['alerts/A-test']['status'] = 'closed'
    assert store.claim(job['id']) is None
    assert db.docs[path]['status'] == 'cancelled' and db.docs[path]['error'] == 'EXPIRED_OR_CLOSED'


def test_delivery_status_names_the_group_not_its_chat(tx_identity):
    db = fanout_db(1)
    store = NotificationStore(db)
    store.fan_out('A-test')
    rows = store.delivery_status('A-test')
    assert [r['group'] for r in rows] == ['Group 0'] and '-1000' not in str(rows)
