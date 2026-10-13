"""Provider acceptance != phone delivery. Timeouts are terminal/unknown, never blind retry."""
from __future__ import annotations

import os
from html.parser import HTMLParser

import httpx
from firebase_admin import messaging

from .auth import firebase_app


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, value):
        self.parts.append(value)


def text_message(inbox):
    parser = PlainText()
    parser.feed(inbox['text'])
    prefix = '[EXERCISE] ' if inbox['purpose'] == 'exercise' else ''
    body = prefix + ''.join(parser.parts).strip()
    return body[:1800] + '\nWARNING is an indication, not a confirmed fire.'


def outcome(status, error=None, provider_id=None):
    return {'status': status, 'error': error, 'provider_id': str(provider_id) if provider_id is not None else None}


def send(job, inbox, profile):
    channel = job['channel']
    # Enforce at the last possible point as well as in planning/claiming.
    if channel in ('fcm', 'web_push') and inbox['purpose'] == 'emergency':
        return outcome('blocked', 'FCM_EMERGENCY_PROHIBITED')
    if channel not in ('telegram', 'fcm', 'web_push'):
        return outcome('blocked', 'ADAPTER_PENDING')
    if os.getenv('NOTIFICATION_MODE', 'dry_run') != 'live':
        return outcome('simulated', 'DRY_RUN_NO_SEND')
    origin = os.getenv('ASAPIFY_PUBLIC_ORIGIN', '').rstrip('/')
    if not origin.startswith('https://'):
        return outcome('blocked', 'PUBLIC_ORIGIN_NOT_CONFIGURED')
    link = origin + '/notifications?open=' + inbox['id']
    if channel == 'telegram':
        token = os.getenv('TELEGRAM_BOT_TOKEN')
        chat = profile.get('telegram_chat_id')
        if os.getenv('TELEGRAM_ENABLED') != 'true' or not token or not chat:
            return outcome('blocked', 'TELEGRAM_NOT_CONFIGURED')
        try:
            # Text only: no mandatory photo fetch on weak data networks; no HTML injection.
            with httpx.Client(timeout=15, follow_redirects=False) as client:
                response = client.post(f'https://api.telegram.org/bot{token}/sendMessage', json={
                    'chat_id': chat, 'text': text_message(inbox),
                    'link_preview_options': {'is_disabled': True},
                    'reply_markup': {'inline_keyboard': [[{'text': 'Open & confirm receipt', 'url': link}]]}})
            data = response.json()
            if response.is_success and data.get('ok') and data.get('result', {}).get('message_id'):
                return outcome('accepted', provider_id=data['result']['message_id'])
            if 400 <= response.status_code < 500:
                return outcome('failed', 'TELEGRAM_REJECTED')
            return outcome('unknown', 'DELIVERY_UNCERTAIN')
        except Exception:
            return outcome('unknown', 'DELIVERY_UNCERTAIN')
    if os.getenv('FCM_ENABLED') != 'true' or not profile.get('fcm_token'):
        return outcome('blocked', 'FCM_NOT_CONFIGURED')
    try:
        message_id = messaging.send(messaging.Message(
            token=profile['fcm_token'],
            notification=messaging.Notification(title='ASAPify · Exercise / information', body=text_message(inbox)[:220]),
            data={'inbox_id': inbox['id'], 'url': link, 'purpose': inbox['purpose']},
            android=messaging.AndroidConfig(priority='normal', ttl=300),
            webpush=messaging.WebpushConfig(headers={'TTL': '300', 'Urgency': 'normal'},
                                           fcm_options=messaging.WebpushFCMOptions(link=link)),
            apns=messaging.APNSConfig(headers={'apns-priority': '5'})), app=firebase_app())
        return outcome('accepted', provider_id=message_id)
    except (messaging.UnregisteredError, ValueError):
        return outcome('failed', 'DEVICE_INVALID')
    except Exception:
        return outcome('unknown', 'DELIVERY_UNCERTAIN')
