"""Provider acceptance != a member reading it. Timeouts are terminal/unknown, never blind retry."""
from __future__ import annotations

import os
from html.parser import HTMLParser

import httpx


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, value):
        self.parts.append(value)


def text_message(job):
    parser = PlainText()
    parser.feed(job['text'])
    parser.close()   # flushes text held back after a trailing '&' (a link like ...?q=1&z=10 would vanish without it)
    prefix = '[EXERCISE] ' if job['purpose'] == 'exercise' else ''
    body = prefix + ''.join(parser.parts).strip()
    return body[:1800] + '\nALERT is an indication, not a confirmed fire.'


def outcome(status, error=None, provider_id=None):
    return {'status': status, 'error': error, 'provider_id': str(provider_id) if provider_id is not None else None}


def send(job, group):
    """Post one alert in one linked Telegram group (`group` = its record)."""
    if job['channel'] != 'telegram_group':
        return outcome('blocked', 'ADAPTER_PENDING')
    if os.getenv('NOTIFICATION_MODE', 'dry_run') != 'live':
        return outcome('simulated', 'DRY_RUN_NO_SEND')
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    if os.getenv('TELEGRAM_ENABLED') != 'true' or not token or not group.get('chat_id'):
        return outcome('blocked', 'TELEGRAM_NOT_CONFIGURED')
    try:
        # Text only: no mandatory photo fetch on weak data networks; no HTML injection.
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            response = client.post(f'https://api.telegram.org/bot{token}/sendMessage', json={
                'chat_id': group['chat_id'], 'text': text_message(job), 'link_preview_options': {'is_disabled': True}})
        data = response.json()
        if response.is_success and data.get('ok') and data.get('result', {}).get('message_id'):
            return outcome('accepted', provider_id=data['result']['message_id'])
        if 400 <= response.status_code < 500:
            return outcome('failed', 'TELEGRAM_REJECTED')
        return outcome('unknown', 'DELIVERY_UNCERTAIN')
    except Exception:
        return outcome('unknown', 'DELIVERY_UNCERTAIN')
