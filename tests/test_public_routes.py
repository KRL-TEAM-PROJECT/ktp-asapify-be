"""Public read-only views: no sign-in, no operator names or notes, CDN-cacheable, no actions."""
import json

import pytest
from fastapi.testclient import TestClient

from asapify_api.app import app
from asapify_api.engine_routes import get_engine_repo
from asapify_engine.regions import load_regions
from test_engine_ingest import FakeRepo

REGION = load_regions()[0]
DECISION = {'action': 'publish', 'by': 'operator@example.test', 'at': '2026-10-15T01:00:00Z', 'reason': None,
            'caption': 'ALERT', 'alert_id': 'A-x', 'notifications': {'state': 'outbox_saved', 'groups': 1, 'jobs': 1}}


@pytest.fixture
def repo():
    repo = FakeRepo()
    repo.docs[REGION.id] = {'id': REGION.id, 'status': 'ALERT',
                            'verification': {'id': 'v1', 'validation_status': 'CONFIRMED', 'decision': 'publish'}}
    repo.verifications[REGION.id] = [{'id': '20261015T005000Z', 'trigger': 'manual', 'requested_by': 'operator@example.test',
                                      'validation_status': 'CONFIRMED', 'decision': DECISION}]
    repo.days['2026-10-14'] = {'date': '2026-10-14', 'month': '2026-10', 'regions': [REGION.id], 'alert': True,
                               'slots': {'0010': json.dumps({'at': '2026-10-13T17:10:00Z', 'status': 'A'})}}
    app.dependency_overrides[get_engine_repo] = lambda: repo
    yield repo
    app.dependency_overrides.clear()


def client():
    return TestClient(app, raise_server_exceptions=False)


def test_the_map_and_a_region_need_no_sign_in_and_name_no_operator(repo):
    c = client()
    regions = c.get('/api/v1/public/regions')
    assert regions.status_code == 200 and 's-maxage=120' in regions.headers['cache-control']
    assert next(r for r in regions.json() if r['id'] == REGION.id)['status'] == 'ALERT'
    detail = c.get(f'/api/v1/public/regions/{REGION.id}')
    assert detail.status_code == 200 and 'public' in detail.headers['cache-control']
    verification = detail.json()['verifications'][0]
    assert verification['decision'] == {'action': 'publish', 'at': '2026-10-15T01:00:00Z'}
    assert 'requested_by' not in verification and '@' not in detail.text


def test_an_automatic_publication_says_so_publicly(repo):
    repo.verifications[REGION.id][0]['decision'] = {**DECISION, 'by': 'automatic', 'auto': True}
    verification = client().get(f'/api/v1/public/regions/{REGION.id}').json()['verifications'][0]
    assert verification['decision'] == {'action': 'publish', 'at': '2026-10-15T01:00:00Z', 'auto': True}


def test_public_detail_is_limited_to_24_hours(repo):
    assert client().get(f'/api/v1/public/regions/{REGION.id}?hours=72').status_code == 400


def test_a_past_day_is_cached_for_a_day(repo):
    c = client()
    day = c.get('/api/v1/public/days/2026-10-14')
    assert day.status_code == 200 and 's-maxage=86400' in day.headers['cache-control']
    assert c.get('/api/v1/public/days?month=2026-10').json()['days'][0]['alert'] is True


def test_yesterday_is_final_only_once_its_last_slots_are_rated(monkeypatch):
    from asapify_api import public_routes
    from asapify_api.core import as_time
    monkeypatch.setattr(public_routes, 'utc_now', lambda: as_time('2026-10-14T17:20:00Z'))   # 00:20 WIB on the 15th
    assert not public_routes.past('2026-10-14')                    # its 23:50 slot may still be on its way
    monkeypatch.setattr(public_routes, 'utc_now', lambda: as_time('2026-10-14T18:05:00Z'))   # 01:05 WIB
    assert public_routes.past('2026-10-14') and not public_routes.past('2026-10-15')


@pytest.mark.parametrize('method,path', [
    ('post', f'/api/v1/public/regions/{REGION.id}/verify'), ('post', f'/api/v1/public/regions/{REGION.id}/decision'),
    ('get', '/api/v1/public/agent'),
])
def test_there_are_no_public_actions(repo, method, path):
    assert getattr(client(), method)(path).status_code in (404, 405)


def test_operator_endpoints_still_need_an_operator(repo):
    assert client().get('/api/v1/operator/regions').status_code == 401
