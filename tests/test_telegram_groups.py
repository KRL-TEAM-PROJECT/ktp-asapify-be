"""Telegram groups: an operator links a group once, then every published alert also goes to it."""
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from asapify_api.app import app
from asapify_api.auth import require_operator
from asapify_api.notification_delivery import send
from asapify_api.notification_policy import key
from asapify_api.notification_store import NotificationStore, get_notification_store
from test_notifications import SOURCE, FakeDB, count, fanout_db, live

CODE_RE = r'startgroup=([A-Za-z0-9_-]{32})$'
HOOK = {'X-Telegram-Bot-Api-Secret-Token': 'hook-secret'}


class Tx:
    def __init__(self, db):
        self.db = db

    def set(self, ref, data):
        self.db.docs[ref.path] = dict(data)

    def delete(self, ref):
        self.db.docs.pop(ref.path, None)

    def update(self, ref, fields):
        self.db.docs[ref.path].update(fields)


class GroupDB(FakeDB):
    def transaction(self):
        return Tx(self)


@pytest.fixture
def store(monkeypatch):
    from google.cloud import firestore
    monkeypatch.setattr(firestore, 'transactional', lambda f: f)
    monkeypatch.setenv('TELEGRAM_BOT_USERNAME', 'asapify_alert_bot')
    monkeypatch.setenv('TELEGRAM_WEBHOOK_SECRET', 'hook-secret')
    db = GroupDB()
    db.docs.update(fanout_db(0).docs)
    store = NotificationStore(db)
    app.dependency_overrides[get_notification_store] = lambda: store
    app.dependency_overrides[require_operator] = lambda: {'uid': 'op', 'email': 'operator@example.test'}
    yield store
    app.dependency_overrides.clear()


def link_code(client):
    import re
    r = client.post('/api/v1/operator/telegram-groups/link')
    assert r.status_code == 200 and r.json()['url'].startswith('https://t.me/asapify_alert_bot?startgroup=')
    return re.search(CODE_RE, r.json()['url']).group(1)


def group_message(text, chat_id=-1001, kind='supergroup', **extra):
    return {'message': {'chat': {'id': chat_id, 'type': kind, 'title': 'BPBD Kalteng'}, 'text': text, **extra}}


def test_operator_links_a_group_once_and_sees_its_title_only(store):
    c = TestClient(app, raise_server_exceptions=False)
    code = link_code(c)
    assert c.post('/api/v1/notifications/telegram-webhook', json=group_message(f'/start@asapify_alert_bot {code}'),
                  headers=HOOK).status_code == 200
    groups = c.get('/api/v1/operator/telegram-groups').json()
    assert [g['title'] for g in groups] == ['BPBD Kalteng'] and groups[0]['linked_by'] == 'operator@example.test'
    assert 'chat_id' not in groups[0]
    # the code is used up: another group cannot reuse it
    c.post('/api/v1/notifications/telegram-webhook', json=group_message(f'/start {code}', chat_id=-2002), headers=HOOK)
    assert len(c.get('/api/v1/operator/telegram-groups').json()) == 1
    assert c.delete(f"/api/v1/operator/telegram-groups/{groups[0]['id']}").json() == {'unlinked': True}
    assert c.get('/api/v1/operator/telegram-groups').json() == []


@pytest.mark.parametrize('text', ['/start ' + 'x' * 32, 'hello', '/start'])
def test_a_group_without_a_valid_code_is_not_linked(store, text):
    c = TestClient(app, raise_server_exceptions=False)
    c.post('/api/v1/notifications/telegram-webhook', json=group_message(text), headers=HOOK)
    assert store.groups() == []


def test_group_links_need_an_operator(store):
    app.dependency_overrides.pop(require_operator)
    c = TestClient(app, raise_server_exceptions=False)
    assert c.post('/api/v1/operator/telegram-groups/link').status_code == 401
    assert c.get('/api/v1/operator/telegram-groups').status_code == 401


def test_supergroup_migration_keeps_the_link(store):
    assert store.link_group(store.group_link({'email': 'op@example.test'}), '-1001', 'BPBD') is True
    c = TestClient(app, raise_server_exceptions=False)
    c.post('/api/v1/notifications/telegram-webhook', json=group_message('', chat_id=-1001, kind='group',
                                                                      migrate_to_chat_id=-1009), headers=HOOK)
    assert store.db.docs[f'telegram_groups/{key("-1009")}']['chat_id'] == '-1009'
    assert f'telegram_groups/{key("-1001")}' not in store.db.docs


def test_fan_out_queues_one_job_per_group_even_without_subscribers(store):
    store.link_group(store.group_link({'email': 'op@example.test'}), '-1001', 'BPBD')
    summary = store.fan_out('A-test')
    assert summary == {'state': 'outbox_saved', 'recipients': 0, 'groups': 1, 'jobs': 1}
    job = next(v for k, v in store.db.docs.items() if k.startswith('notification_jobs/'))
    assert job['channel'] == 'telegram_group' and job['inbox_id'] is None and job['text'] == '<b>WARNING</b>'
    assert store.db.docs[SOURCE]['decision']['notifications'] == summary
    assert count(store.db, 'notification_inbox') == 0


def test_group_message_goes_to_the_group_without_a_receipt_button(monkeypatch, store):
    live(monkeypatch)
    post = Mock(return_value=httpx.Response(200, json={'ok': True, 'result': {'message_id': 7}}))
    monkeypatch.setattr(httpx.Client, 'post', post)
    store.link_group(store.group_link({'email': 'op@example.test'}), '-1001', 'BPBD')
    store.fan_out('A-test')
    job = next(v for k, v in store.db.docs.items() if k.startswith('notification_jobs/'))
    group = store.db.docs[f'telegram_groups/{job["group_id"]}']
    assert send(job, job, group)['status'] == 'accepted'
    body = post.call_args.kwargs['json']
    assert body['chat_id'] == '-1001' and 'reply_markup' not in body and '<b>' not in body['text']


def test_unlinked_group_job_is_cancelled_at_claim(store):
    store.link_group(store.group_link({'email': 'op@example.test'}), '-1001', 'BPBD')
    store.fan_out('A-test')
    job_path, job = next((k, v) for k, v in store.db.docs.items() if k.startswith('notification_jobs/'))
    store.unlink_group(job['group_id'])
    assert store.claim(job['id']) is None
    assert store.db.docs[job_path]['status'] == 'cancelled' and store.db.docs[job_path]['error'] == 'GROUP_UNLINKED'
