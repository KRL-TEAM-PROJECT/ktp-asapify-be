"""Deterministic routing. No provider calls or delivery guarantees in this module."""
from __future__ import annotations

import hashlib
import os
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .core import PROVINCES, as_time, iso

Channel = Literal['telegram', 'fcm', 'web_push', 'whatsapp', 'sms', 'gmail', 'lora', 'bluetooth_mesh']
CHANNELS = ('telegram', 'fcm', 'web_push', 'whatsapp', 'sms', 'gmail', 'lora', 'bluetooth_mesh')
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
             'enabled': live and (os.getenv('TELEGRAM_ENABLED') == 'true' if c == 'telegram'
                                 else os.getenv('FCM_ENABLED') == 'true' if c in ('fcm', 'web_push') else False),
             'restriction': 'NON_EMERGENCY_ONLY' if c in ('fcm', 'web_push') else
                            'HARDWARE_REQUIRED' if c in ('lora', 'bluetooth_mesh') else
                            'ADAPTER_PENDING' if c in FUTURE else None}
            for c in CHANNELS]


def alert_payload(cluster: dict, caption: str, now: str) -> dict:
    exercise = cluster.get('replay') is True
    return {'id': 'A-' + cluster['id'].removeprefix('C-'), 'cluster_id': cluster['id'],
            'province': str(cluster['province']), 'text': caption,
            'purpose': 'exercise' if exercise else 'emergency', 'published_at': now,
            'expires_at': iso(as_time(now) + timedelta(hours=1)), 'status': 'active'}


def plan_recipient(alert: dict, recipient: dict, now: str, escalation_seconds: int = 120):
    """Inbox is authoritative; provider acceptance never serves as an acknowledgement.

    Production AWAS is always emergency, regardless of urgency or edited caption.
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
    selected = recipient.get('channels', [])
    push_chosen = 'web_push' if 'web_push' in selected else 'fcm'
    for channel in selected:
        if channel in ('fcm', 'web_push') and channel != push_chosen:
            continue  # Same token/transport: never send twice.
        reason = None
        if channel in ('fcm', 'web_push') and alert['purpose'] == 'emergency':
            reason = 'FCM_EMERGENCY_PROHIBITED'
        elif channel in FUTURE:
            reason = 'HARDWARE_REQUIRED' if channel in ('lora', 'bluetooth_mesh') else 'ADAPTER_PENDING'
        elif channel == 'telegram' and not recipient.get('telegram_chat_id'):
            reason = 'TELEGRAM_NOT_LINKED'
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
