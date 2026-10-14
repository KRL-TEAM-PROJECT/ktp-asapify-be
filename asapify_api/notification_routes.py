"""Telegram alert groups: operators link a group and set its invite link, the public join page reads the links, the
webhook links the group, and the worker posts each published alert in it."""
from __future__ import annotations

import hmac
import os
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from .auth import require_operator
from .core import ApiProblem
from .notification_delivery import send
from .notification_store import get_notification_store
from .notification_tasks import enqueue_pending, get_tasks, require_worker

router = APIRouter()
HexID = Annotated[str, Path(pattern=r'^[a-f0-9]{64}$')]
# A Telegram invite link (t.me/+code or t.me/joinchat/code) or a public group's t.me/<username>
INVITE = r'^https://t\.me/(\+[A-Za-z0-9_-]{8,64}|joinchat/[A-Za-z0-9_-]{8,64}|[A-Za-z][A-Za-z0-9_]{4,31})$'


def bot_username():
    username = os.getenv('TELEGRAM_BOT_USERNAME', '')
    if not re.fullmatch(r'[A-Za-z0-9_]{5,32}', username):
        raise ApiProblem(503, 'TELEGRAM_NOT_CONFIGURED', 'The Telegram bot is not configured.')
    return username


@router.get('/api/v1/telegram/join')
def join(store=Depends(get_notification_store)):
    """Public: the groups anyone may join to receive alerts. Nothing but titles and invite links."""
    return store.join_links()


@router.post('/api/v1/notifications/telegram-webhook')
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None),
                           store=Depends(get_notification_store)):
    expected = os.getenv('TELEGRAM_WEBHOOK_SECRET')
    if not expected or not hmac.compare_digest(expected, x_telegram_bot_api_secret_token or ''):
        raise ApiProblem(401, 'UNAUTHENTICATED', 'Invalid webhook.')
    raw = await request.body()
    if len(raw) > 16384:
        raise ApiProblem(413, 'VALIDATION', 'Payload too large.')
    try:
        body = await request.json()
        message = body.get('message', {})
        chat = message.get('chat', {})
        text = message.get('text', '')
        # Only groups are linked, and only with an operator's one-time code; private chats are ignored.
        if chat.get('type') in ('group', 'supergroup') and isinstance(chat.get('id'), int):
            # t.me/<bot>?startgroup=<code> adds the bot and posts /start@<bot> <code> in the group the operator picked
            group = re.fullmatch(r'/start(?:@[A-Za-z0-9_]{5,32})? ([A-Za-z0-9_-]{32})', text)
            if group:
                store.link_group(group.group(1), str(chat['id']), str(chat.get('title') or ''))
            elif isinstance(message.get('migrate_to_chat_id'), int):
                store.migrate_group(str(chat['id']), str(message['migrate_to_chat_id']))
    except (ValueError, TypeError, AttributeError):
        raise ApiProblem(400, 'VALIDATION', 'Invalid webhook payload.')
    return {'ok': True}  # No outgoing message or disclosure of binding state.


@router.get('/api/v1/operator/telegram-groups')
def telegram_groups(actor=Depends(require_operator), store=Depends(get_notification_store)):
    return store.groups()


@router.post('/api/v1/operator/telegram-groups/link')
def telegram_group_link(actor=Depends(require_operator), store=Depends(get_notification_store)):
    username = bot_username()
    return {'url': f'https://t.me/{username}?startgroup={store.group_link(actor)}', 'expires_in_seconds': 600}


class Invite(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    invite_url: str | None = Field(default=None, pattern=INVITE)   # null = not shown on the join page


@router.put('/api/v1/operator/telegram-groups/{group_id}')
def telegram_group_invite(group_id: HexID, body: Invite, actor=Depends(require_operator),
                          store=Depends(get_notification_store)):
    return store.set_invite(group_id, body.invite_url)


@router.delete('/api/v1/operator/telegram-groups/{group_id}')
def telegram_group_unlink(group_id: HexID, actor=Depends(require_operator), store=Depends(get_notification_store)):
    store.unlink_group(group_id)
    return {'unlinked': True}


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
    job, group = claimed
    result = send(job, group)
    store.finish(job, result)
    return {'processed': True, 'status': result['status']}


@router.post('/_internal/notifications/sweep', dependencies=[Depends(require_worker)])
def sweep(store=Depends(get_notification_store), tasks=Depends(get_tasks)):
    store.reconcile_uncertain()
    resumed = store.resume_fanouts()
    return {'resumed': resumed, 'enqueued': enqueue_pending(store, tasks)}
