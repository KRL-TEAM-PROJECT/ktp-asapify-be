from __future__ import annotations

import base64
import json
import os
from functools import lru_cache

import google.auth
from google.auth.transport.requests import AuthorizedSession, Request as GoogleRequest
from google.oauth2 import id_token
from fastapi import Header

from .core import ApiProblem


def require_worker(authorization: str | None = Header(default=None)):
    expected = os.getenv('NOTIFICATION_TASK_CALLER')
    audience = os.getenv('NOTIFICATION_WORKER_URL', '').rstrip('/')
    if not expected or not audience:
        raise ApiProblem(503, 'WORKER_NOT_CONFIGURED', 'The worker identity is not configured.')
    if not authorization or not authorization.startswith('Bearer '):
        raise ApiProblem(401, 'UNAUTHENTICATED', 'A worker token is required.')
    try:
        claims = id_token.verify_oauth2_token(authorization[7:], GoogleRequest(), audience=audience)
    except Exception:
        raise ApiProblem(401, 'UNAUTHENTICATED', 'Invalid worker token.')
    if claims.get('email') != expected or claims.get('email_verified') is not True or claims.get('iss') not in ('accounts.google.com', 'https://accounts.google.com'):
        raise ApiProblem(403, 'FORBIDDEN', 'The worker caller identity does not match.')


class CloudTasks:
    def __init__(self):
        project = os.getenv('GOOGLE_CLOUD_PROJECT')
        self.origin = os.getenv('NOTIFICATION_WORKER_URL', '').rstrip('/')
        self.caller = os.getenv('NOTIFICATION_TASK_CALLER')
        region = os.getenv('NOTIFICATION_TASK_REGION', 'asia-southeast2')
        queue = os.getenv('NOTIFICATION_TASK_QUEUE', 'asapify-notifications')
        if not project or not self.origin.startswith('https://') or not self.caller:
            raise ApiProblem(503, 'QUEUE_NOT_CONFIGURED', 'The notification queue is not configured.')
        self.parent = f'projects/{project}/locations/{region}/queues/{queue}'
        creds, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
        self.session = AuthorizedSession(creds)

    def enqueue(self, job):
        # Hashed, deterministic name; no PII/tokens in task names or bodies.
        name = self.parent + '/tasks/n-' + job['id'] + '-' + str(job.get('generation', 0))
        response = self.session.post('https://cloudtasks.googleapis.com/v2/' + self.parent + '/tasks', json={
            'task': {'name': name, 'scheduleTime': job['not_before'],
                     'httpRequest': {'httpMethod': 'POST', 'url': self.origin + '/_internal/notifications/dispatch',
                                     'headers': {'Content-Type': 'application/json'},
                                     'body': base64.b64encode(json.dumps({'job_id': job['id']}).encode()).decode(),
                                     'oidcToken': {'serviceAccountEmail': self.caller, 'audience': self.origin}}}}, timeout=15)
        if response.status_code not in (200, 201, 409):
            raise ApiProblem(503, 'QUEUE_UNAVAILABLE', 'The queue did not accept the job; the outbox is saved.')


@lru_cache
def get_tasks():
    return CloudTasks()


def enqueue_pending(store, tasks, limit=100):
    count = 0
    for job in store.pending(limit):
        tasks.enqueue(job)
        count += 1
    return count
