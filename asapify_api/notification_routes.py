"""Telegram alert groups: operators link a group and set its invite link, the public join page reads the links, the
webhook links the group, and the worker posts each published alert in it."""
from __future__ import annotations

import hmac
import json
import os
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from .auth import require_admin, require_operator
from .core import ApiProblem
from .notification_delivery import send
from .notification_store import get_notification_store
from .notification_tasks import enqueue_pending, get_tasks, require_worker

router = APIRouter()
HexID = Annotated[str, Path(pattern=r'^[a-f0-9]{64}$')]
# A Telegram invite link (t.me/+code or t.me/joinchat/code) or a public group's t.me/<username>
INVITE = r'^https://t\.me/(\+[A-Za-z0-9_-]{8,64}|joinchat/[A-Za-z0-9_-]{8,64}|[A-Za-z][A-Za-z0-9_]{4,31})$'
# The team's alert group (one group for everyone, team decision of 15 October 2026; its link since 17 October).
# TELEGRAM_JOIN_URL replaces the link, and an empty value takes the group off the join page.
TEAM_GROUP = {'title': 'ASAPify alert group', 'invite_url': 'https://t.me/+dWeL0KTSkbYxOTE9'}


def bot_username():
    username = os.getenv('TELEGRAM_BOT_USERNAME', '')
    if not re.fullmatch(r'[A-Za-z0-9_]{5,32}', username):
        raise ApiProblem(503, 'TELEGRAM_NOT_CONFIGURED', 'The Telegram bot is not configured.')
    return username


def team_group():
    """The team's group for the join page, or None when TELEGRAM_JOIN_URL is empty or not a Telegram invite link."""
    url = os.getenv('TELEGRAM_JOIN_URL', TEAM_GROUP['invite_url']).strip()
    return {**TEAM_GROUP, 'invite_url': url} if re.fullmatch(INVITE, url) else None


@router.get('/api/v1/telegram/join')
def join(store=Depends(get_notification_store)):
    """Public: the groups anyone may join to receive alerts. Nothing but titles and invite links.

    The team's group comes first even before an operator saves its invite link on the Admin page; a linked group saved
    with the same link takes its place, under its own title. It needs no database, so it stays listed when the saved
    links cannot be read."""
    team = team_group()
    try:
        saved = store.join_links()
    except Exception:
        if team is None:
            raise
        saved = []
    return ([team] if team and all(g['invite_url'] != team['invite_url'] for g in saved) else []) + saved


LINKED = 'Linked. ASAPify will post its peat-fire alerts in this group.'
ALREADY = 'This group is linked. ASAPify posts its peat-fire alerts here.'
BAD_CODE = ('This code is not valid or has expired. In ASAPify open Admin, Telegram groups, "Add the bot to a group", '
            'and send the new message here within 10 minutes.')
NO_CODE = ('To link this group, send the message ASAPify shows under Admin, Telegram groups, "Add the bot to a group". '
           'It looks like /start@<bot> <code>.')
PRIVATE = 'I only post ASAPify peat-fire alerts in the alert group.'


def reply(chat_id: int, text: str) -> dict:
    """Telegram sends this as the bot's answer to the update, so the API needs no bot token."""
    return {'method': 'sendMessage', 'chat_id': chat_id, 'text': text, 'link_preview_options': {'is_disabled': True}}


def log(outcome: str, chat_type) -> None:
    # Structured line for Cloud Logging; never the code, chat id or message text.
    print(json.dumps({'severity': 'INFO', 'message': 'telegram webhook', 'outcome': outcome, 'chat_type': chat_type}),
          flush=True)


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
        kind, chat_id = chat.get('type'), chat.get('id')
        if not isinstance(chat_id, int):
            return {'ok': True}
        if kind == 'private':
            # Nobody subscribes in a private chat: point to the public join page.
            origin = os.getenv('ASAPIFY_PUBLIC_ORIGIN', '').rstrip('/')
            log('private', kind)
            return reply(chat_id, PRIVATE + (f' Join it from {origin}/join' if origin.startswith('https://') else ''))
        if kind not in ('group', 'supergroup'):
            return {'ok': True}
        if isinstance(message.get('migrate_to_chat_id'), int):
            store.migrate_group(str(chat_id), str(message['migrate_to_chat_id']))
            log('migrated', kind)
            return {'ok': True}
        # Only an operator's one-time code links a group: /start@<bot> <code>, which t.me/<bot>?startgroup=<code>
        # posts when Telegram adds the bot, or which an operator sends by hand. Any other message gets no answer.
        start = re.fullmatch(r'/start(?:@[A-Za-z0-9_]{5,32})?(?:\s+(\S+))?', text.strip())
        if not start:
            return {'ok': True}
        argument = start.group(1) or ''
        code = argument.split('startgroup=', 1)[-1]   # a pasted t.me link works too
        if not argument:
            outcome, answer = ('already', ALREADY) if store.group_linked(str(chat_id)) else ('no_code', NO_CODE)
        elif re.fullmatch(r'[A-Za-z0-9_-]{32}', code) and store.link_group(code, str(chat_id), str(chat.get('title') or '')):
            outcome, answer = 'linked', LINKED
        else:
            outcome, answer = 'bad_code', BAD_CODE
        log(outcome, kind)
        return reply(chat_id, answer)
    except (ValueError, TypeError, AttributeError):
        raise ApiProblem(400, 'VALIDATION', 'Invalid webhook payload.')


@router.get('/api/v1/operator/telegram-groups')
def telegram_groups(actor=Depends(require_admin), store=Depends(get_notification_store)):
    return store.groups()


@router.post('/api/v1/operator/telegram-groups/link')
def telegram_group_link(actor=Depends(require_admin), store=Depends(get_notification_store)):
    username = bot_username()
    return {'url': f'https://t.me/{username}?startgroup={store.group_link(actor)}', 'expires_in_seconds': 600}


class Invite(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    invite_url: str | None = Field(default=None, pattern=INVITE)   # null = not shown on the join page


@router.put('/api/v1/operator/telegram-groups/{group_id}')
def telegram_group_invite(group_id: HexID, body: Invite, actor=Depends(require_admin),
                          store=Depends(get_notification_store)):
    return store.set_invite(group_id, body.invite_url)


@router.delete('/api/v1/operator/telegram-groups/{group_id}')
def telegram_group_unlink(group_id: HexID, actor=Depends(require_admin), store=Depends(get_notification_store)):
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
