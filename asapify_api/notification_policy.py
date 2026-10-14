"""Telegram group jobs. No provider calls or delivery guarantees in this module."""
from __future__ import annotations

import hashlib


def key(*values: str) -> str:
    return hashlib.sha256('|'.join(values).encode()).hexdigest()


def group_job(alert: dict, group_id: str, now: str) -> dict:
    """One message to one linked Telegram group. Whoever reads the group reads the alert; the text travels in the job."""
    return {'id': key(alert['id'], 'telegram_group', group_id), 'alert_id': alert['id'], 'group_id': group_id,
            'channel': 'telegram_group', 'status': 'pending', 'error': None, 'text': alert['text'],
            'purpose': alert['purpose'], 'province': alert['province'], 'not_before': now,
            'expires_at': alert['expires_at'], 'created_at': now, 'provider_id': None, 'attempts': 0, 'generation': 0}
