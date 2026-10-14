"""Deterministic routing. No provider calls or delivery guarantees in this module."""
from __future__ import annotations

import hashlib
import os
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .core import PROVINCES, as_time, iso

# Subscriber channels. Telegram is not one of them: alerts go to the linked Telegram groups (group_job below).
Channel = Literal['fcm', 'web_push', 'whatsapp', 'sms', 'gmail', 'lora', 'bluetooth_mesh']
CHANNELS = ('fcm', 'web_push', 'whatsapp', 'sms', 'gmail', 'lora', 'bluetooth_mesh')
FUTURE = {'whatsapp', 'sms', 'gmail', 'lora', 'bluetooth_mesh'}
CONSENT_VERSION = 'notification-v1-2026-10-09'


def key(*values: str) -> str:
    return hashlib.sha256('|'.join(values).encode()).hexdigest()


class Preferences(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    provinces: list[str] = Field(default_factory=list, max_length=15)
    channels: list[Channel] = Field(default_factory=list, max_length=8)
    consent: bool
    exercises: bool = False

    @model_validator(mode='after')
    def valid(self):
        if len(set(self.channels)) != len(self.channels) or len(set(self.provinces)) != len(self.provinces):
            raise ValueError('Duplicate choice')
        if any(v not in {str(p) for p in PROVINCES} for v in self.provinces):
            raise ValueError('Invalid province')
        if self.consent and not self.provinces:
            raise ValueError('Choose at least one province')
        return self


def capabilities() -> list[dict]:
    live = os.getenv('NOTIFICATION_MODE', 'dry_run') == 'live'
    return [{'channel': c, 'implemented': c not in FUTURE,
             'enabled': live and os.getenv('FCM_ENABLED') == 'true' and c in ('fcm', 'web_push'),
             'restriction': 'NON_EMERGENCY_ONLY' if c in ('fcm', 'web_push') else
                            'HARDWARE_REQUIRED' if c in ('lora', 'bluetooth_mesh') else
                            'ADAPTER_PENDING' if c in FUTURE else None}
            for c in CHANNELS]


def plan_recipient(alert: dict, recipient: dict, now: str, escalation_seconds: int = 120):
    """Inbox is authoritative; provider acceptance never serves as an acknowledgement.

    Production ALERT is always emergency, regardless of urgency or edited caption.
    FCM, including FCM-backed web push, is excluded from that route.
    """
    if not recipient.get('consent') or alert['province'] not in recipient.get('provinces', []):
        return None, []
    if alert['purpose'] == 'exercise' and not recipient.get('exercises'):
        return None, []
    rid = key(recipient['uid'])
    inbox_id = key(alert['id'], rid)
    inbox = {**alert, 'id': inbox_id, 'alert_id': alert['id'], 'recipient_id': rid,
             'acknowledged_at': None, 'created_at': now}
    jobs = []
    selected = [c for c in recipient.get('channels', []) if c in CHANNELS]   # older profiles may still say telegram
    push_chosen = 'web_push' if 'web_push' in selected else 'fcm'
    for channel in selected:
        if channel in ('fcm', 'web_push') and channel != push_chosen:
            continue  # Same token/transport: never send twice.
        reason = None
        if channel in ('fcm', 'web_push') and alert['purpose'] == 'emergency':
            reason = 'FCM_EMERGENCY_PROHIBITED'
        elif channel in FUTURE:
            reason = 'HARDWARE_REQUIRED' if channel in ('lora', 'bluetooth_mesh') else 'ADAPTER_PENDING'
        elif channel in ('fcm', 'web_push') and not recipient.get('fcm_token'):
            reason = 'DEVICE_NOT_REGISTERED'
        due = as_time(now)
        stage = 'initial'
        if channel in ('whatsapp', 'sms') and alert['purpose'] != 'emergency':
            due += timedelta(seconds=escalation_seconds)
            stage = 'escalation'
        jobs.append({'id': key(inbox_id, channel), 'alert_id': alert['id'], 'inbox_id': inbox_id,
                     'recipient_id': rid, 'channel': channel, 'stage': stage,
                     'status': 'blocked' if reason else 'pending', 'error': reason,
                     'not_before': iso(due), 'expires_at': alert['expires_at'], 'created_at': now,
                     'provider_id': None, 'attempts': 0, 'generation': 0})
    return inbox, jobs


def group_job(alert: dict, group_id: str, now: str) -> dict:
    """One message to one linked Telegram group. A group has no inbox or consent record: whoever reads the group
    reads the alert. The text travels in the job because there is no inbox entry to read it from."""
    return {'id': key(alert['id'], 'telegram_group', group_id), 'alert_id': alert['id'], 'inbox_id': None,
            'recipient_id': None, 'group_id': group_id, 'channel': 'telegram_group', 'stage': 'initial',
            'status': 'pending', 'error': None, 'text': alert['text'], 'purpose': alert['purpose'],
            'province': alert['province'], 'not_before': now, 'expires_at': alert['expires_at'], 'created_at': now,
            'provider_id': None, 'attempts': 0, 'generation': 0}


def dispatch_guard(job: dict, inbox: dict, profile: dict, now: str) -> str | None:
    if inbox.get('acknowledged_at'):
        return 'ACKNOWLEDGED'
    if inbox.get('status') != 'active' or as_time(job['expires_at']) <= as_time(now):
        return 'EXPIRED_OR_CLOSED'
    if not profile.get('consent') or job['channel'] not in profile.get('channels', []) or inbox['province'] not in profile.get('provinces', []):
        return 'CONSENT_REVOKED'
    if inbox['purpose'] == 'exercise' and not profile.get('exercises'):
        return 'CONSENT_REVOKED'
    if job['channel'] in ('fcm', 'web_push') and inbox['purpose'] == 'emergency':
        return 'FCM_EMERGENCY_PROHIBITED'
    return None
