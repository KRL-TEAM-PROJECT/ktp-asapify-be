"""Telegram groups: an operator links a group and sets its invite link; the public join page reads the links."""
import re

import pytest
from fastapi.testclient import TestClient

from asapify_api.app import app
from asapify_api.auth import require_operator
from asapify_api.notification_policy import key
from asapify_api.notification_store import NotificationStore, get_notification_store
from test_notifications import fanout_db

HOOK = {'X-Telegram-Bot-Api-Secret-Token': 'hook-secret'}
INVITE = 'https://t.me/+AbCdEf123456'


@pytest.fixture
def store(monkeypatch):
    from google.cloud import firestore
    monkeypatch.setattr(firestore, 'transactional', lambda f: f)
    monkeypatch.setenv('TELEGRAM_BOT_USERNAME', 'asapify_alert_bot')
    monkeypatch.setenv('TELEGRAM_WEBHOOK_SECRET', 'hook-secret')
    store = NotificationStore(fanout_db(0))
    app.dependency_overrides[get_notification_store] = lambda: store
    app.dependency_overrides[require_operator] = lambda: {'uid': 'op', 'email': 'operator@example.test'}
    yield store
    app.dependency_overrides.clear()


def client():
    return TestClient(app, raise_server_exceptions=False)


def link_code(c):
    r = c.post('/api/v1/operator/telegram-groups/link')
    assert r.status_code == 200 and r.json()['url'].startswith('https://t.me/asapify_alert_bot?startgroup=')
    return re.search(r'startgroup=([A-Za-z0-9_-]{32})$', r.json()['url']).group(1)


def group_message(text, chat_id=-1001, kind='supergroup', **extra):
    return {'message': {'chat': {'id': chat_id, 'type': kind, 'title': 'ASAPify Notified'}, 'text': text, **extra}}


def test_operator_links_a_group_once_and_sees_its_title_only(store):
    c = client()
    code = link_code(c)
    assert c.post('/api/v1/notifications/telegram-webhook', json=group_message(f'/start@asapify_alert_bot {code}'),
                  headers=HOOK).status_code == 200
    groups = c.get('/api/v1/operator/telegram-groups').json()
    assert [g['title'] for g in groups] == ['ASAPify Notified'] and groups[0]['linked_by'] == 'operator@example.test'
    assert 'chat_id' not in groups[0] and groups[0]['invite_url'] is None
    # the code is used up: another group cannot reuse it
    c.post('/api/v1/notifications/telegram-webhook', json=group_message(f'/start {code}', chat_id=-2002), headers=HOOK)
    assert len(c.get('/api/v1/operator/telegram-groups').json()) == 1
    assert c.delete(f"/api/v1/operator/telegram-groups/{groups[0]['id']}").json() == {'unlinked': True}
    assert c.get('/api/v1/operator/telegram-groups').json() == []


@pytest.mark.parametrize('message', [
    group_message('/start ' + 'x' * 32), group_message('hello'), group_message('/start'),
    group_message('/start ' + 'y' * 32, kind='private'),
])
def test_nothing_is_linked_without_a_valid_code_in_a_group(store, message):
    client().post('/api/v1/notifications/telegram-webhook', json=message, headers=HOOK)
    assert store.groups() == []


def test_the_join_page_lists_only_groups_with_an_invite_link(store):
    store.link_group(store.group_link({'email': 'op@example.test'}), '-1001', 'ASAPify Notified')
    c = client()
    assert c.get('/api/v1/telegram/join').json() == []
    gid = c.get('/api/v1/operator/telegram-groups').json()[0]['id']
    r = c.put(f'/api/v1/operator/telegram-groups/{gid}', json={'invite_url': INVITE})
    assert r.status_code == 200 and r.json()['invite_url'] == INVITE and 'chat_id' not in r.json()
    app.dependency_overrides.pop(require_operator)   # the join page is public
    assert client().get('/api/v1/telegram/join').json() == [{'title': 'ASAPify Notified', 'invite_url': INVITE}]


@pytest.mark.parametrize('url', ['http://t.me/+AbCdEf123456', 'https://evil.example/+AbCdEf123456',
                                 'https://t.me/+short', 'javascript:alert(1)'])
def test_only_telegram_invite_links_are_accepted(store, url):
    store.link_group(store.group_link({'email': 'op@example.test'}), '-1001', 'ASAPify Notified')
    gid = store.groups()[0]['id']
    assert client().put(f'/api/v1/operator/telegram-groups/{gid}', json={'invite_url': url}).status_code == 400


def test_group_management_needs_an_operator(store):
    app.dependency_overrides.pop(require_operator)
    c = client()
    assert c.post('/api/v1/operator/telegram-groups/link').status_code == 401
    assert c.get('/api/v1/operator/telegram-groups').status_code == 401
    assert c.put('/api/v1/operator/telegram-groups/' + 'a' * 64, json={'invite_url': INVITE}).status_code == 401


def test_supergroup_migration_keeps_the_link(store):
    assert store.link_group(store.group_link({'email': 'op@example.test'}), '-1001', 'ASAPify Notified') is True
    client().post('/api/v1/notifications/telegram-webhook',
                  json=group_message('', chat_id=-1001, kind='group', migrate_to_chat_id=-1009), headers=HOOK)
    assert store.db.docs[f'telegram_groups/{key("-1009")}']['chat_id'] == '-1009'
    assert f'telegram_groups/{key("-1001")}' not in store.db.docs


def test_an_unlinked_group_job_is_cancelled_at_claim(store):
    store.link_group(store.group_link({'email': 'op@example.test'}), '-1001', 'ASAPify Notified')
    store.fan_out('A-test')
    path, job = next((k, v) for k, v in store.db.docs.items() if k.startswith('notification_jobs/'))
    store.unlink_group(job['group_id'])
    assert store.claim(job['id']) is None
    assert store.db.docs[path]['status'] == 'cancelled' and store.db.docs[path]['error'] == 'GROUP_UNLINKED'


def hook(message):
    return client().post('/api/v1/notifications/telegram-webhook', json=message, headers=HOOK).json()


def test_the_bot_answers_start_in_the_group_with_the_link_result(store, capsys):
    code = link_code(client())
    linked = hook(group_message(f'/start@asapify_alert_bot {code}'))
    assert linked['method'] == 'sendMessage' and linked['chat_id'] == -1001 and linked['text'].startswith('Linked.')
    assert hook(group_message('/start'))['text'].startswith('This group is linked')
    assert hook(group_message(f'/start {code}', chat_id=-2002))['text'].startswith('This code is not valid')
    assert hook(group_message('/start', chat_id=-2002))['text'].startswith('To link this group')
    assert hook(group_message('hello')) == {'ok': True}   # anything else gets no answer
    logged = capsys.readouterr().out
    assert '"outcome": "linked"' in logged and code not in logged and '-1001' not in logged


def test_a_pasted_startgroup_link_also_links(store):
    code = link_code(client())
    answer = hook(group_message(f'/start https://t.me/asapify_alert_bot?startgroup={code}'))
    assert answer['text'].startswith('Linked.') and [g['title'] for g in store.groups()] == ['ASAPify Notified']


def test_a_private_chat_is_pointed_to_the_join_page(store, monkeypatch):
    monkeypatch.setenv('ASAPIFY_PUBLIC_ORIGIN', 'https://asapify.example')
    answer = hook(group_message('halo', chat_id=777, kind='private'))
    assert answer['chat_id'] == 777 and answer['text'].endswith('https://asapify.example/join')
    assert store.groups() == []
