from __future__ import annotations

import hmac
import os
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from .auth import require_operator, require_user
from .core import ApiProblem
from .notification_delivery import send
from .notification_policy import Preferences, capabilities
from .notification_store import get_notification_store
from .notification_tasks import enqueue_pending, get_tasks, require_worker

router = APIRouter()
HexID = Annotated[str, Path(pattern=r'^[a-f0-9]{64}$')]


def public_profile(profile):
    return {**{k: profile.get(k, [] if k in ('provinces', 'channels') else False)
               for k in ('provinces', 'channels', 'consent', 'exercises')},
            'telegram_linked': bool(profile.get('telegram_chat_id')),
            'device_registered': bool(profile.get('fcm_token'))}


@router.get('/api/v1/notifications/settings')
def settings(actor=Depends(require_user), store=Depends(get_notification_store)):
    return {'preferences': public_profile(store.profile(actor['uid'])), 'channels': capabilities(),
            'mode': os.getenv('NOTIFICATION_MODE', 'dry_run')}


@router.put('/api/v1/notifications/preferences')
def preferences(body: Preferences, actor=Depends(require_user), store=Depends(get_notification_store)):
    return public_profile(store.preferences(actor, body))


class Device(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    token: str = Field(min_length=20, max_length=4096, pattern=r'^[A-Za-z0-9_:\-\.]+$')


@router.post('/api/v1/notifications/device')
def device(body: Device, actor=Depends(require_user), store=Depends(get_notification_store)):
    store.device(actor['uid'], body.token)
    return {'registered': True}


@router.delete('/api/v1/notifications/device')
def unlink_device(actor=Depends(require_user), store=Depends(get_notification_store)):
    store.unlink_device(actor['uid'])
    return {'registered': False}


@router.post('/api/v1/notifications/telegram-link')
def telegram_link(actor=Depends(require_user), store=Depends(get_notification_store)):
    username = os.getenv('TELEGRAM_BOT_USERNAME', '')
    if not re.fullmatch(r'[A-Za-z0-9_]{5,32}', username):
        raise ApiProblem(503, 'TELEGRAM_NOT_CONFIGURED', 'Bot Telegram belum dikonfigurasi.')
    code = store.telegram_link(actor['uid'])
    return {'url': f'https://t.me/{username}?start={code}', 'expires_in_seconds': 600}


@router.post('/api/v1/notifications/telegram-webhook')
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None),
                           store=Depends(get_notification_store)):
    expected = os.getenv('TELEGRAM_WEBHOOK_SECRET')
    if not expected or not hmac.compare_digest(expected, x_telegram_bot_api_secret_token or ''):
        raise ApiProblem(401, 'UNAUTHENTICATED', 'Webhook tidak sah.')
    raw = await request.body()
    if len(raw) > 16384:
        raise ApiProblem(413, 'VALIDATION', 'Payload terlalu besar.')
    try:
        body = await request.json()
        message = body.get('message', {})
        chat = message.get('chat', {})
        text = message.get('text', '')
        # Linking never sends to a chat supplied by a normal API client.
        if chat.get('type') == 'private' and isinstance(chat.get('id'), int) and re.fullmatch(r'/start [A-Za-z0-9_-]{32}', text):
            store.link_telegram(text.split()[1], str(chat['id']))
    except (ValueError, TypeError, AttributeError):
        raise ApiProblem(400, 'VALIDATION', 'Payload webhook tidak sah.')
    return {'ok': True}  # No outgoing message or disclosure of binding state.


@router.get('/api/v1/notifications/inbox')
def inbox(actor=Depends(require_user), store=Depends(get_notification_store)):
    return store.inbox(actor['uid'])


@router.post('/api/v1/notifications/inbox/{inbox_id}/ack')
def acknowledge(inbox_id: HexID, actor=Depends(require_user), store=Depends(get_notification_store)):
    return store.acknowledge(actor['uid'], inbox_id)


@router.get('/api/v1/operator/alerts/{alert_id}/deliveries')
def deliveries(alert_id: str, actor=Depends(require_operator), store=Depends(get_notification_store)):
    return store.delivery_status(alert_id)


class Dispatch(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    job_id: str = Field(pattern=r'^[a-f0-9]{64}$')


@router.post('/_internal/notifications/dispatch', dependencies=[Depends(require_worker)])
def dispatch(body: Dispatch, store=Depends(get_notification_store)):
    claimed = store.claim(body.job_id)
    if not claimed:
        return {'processed': False}
    job, item, profile = claimed
    result = send(job, item, profile)
    store.finish(job, result)
    return {'processed': True, 'status': result['status']}


@router.post('/_internal/notifications/sweep', dependencies=[Depends(require_worker)])
def sweep(store=Depends(get_notification_store), tasks=Depends(get_tasks)):
    store.reconcile_uncertain()
    resumed = store.resume_fanouts()
    return {'resumed': resumed, 'enqueued': enqueue_pending(store, tasks)}
