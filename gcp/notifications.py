"""Notification infrastructure. Run: python -m gcp.notifications ... (prints plan unless --apply)."""
from __future__ import annotations

import argparse
from urllib.parse import urlparse

from gcp.manage import Deploy


class NotificationsDeploy(Deploy):
    def __init__(self, project, region, apply):
        super().__init__(project, region, apply)
        self.worker = f'asapify-notify@{project}.iam.gserviceaccount.com'
        self.caller = f'asapify-task@{project}.iam.gserviceaccount.com'

    def bootstrap_notifications(self):
        self.preflight()
        self.run('services', 'enable', 'cloudtasks.googleapis.com', 'cloudscheduler.googleapis.com')
        accounts = self.run('iam', 'service-accounts', 'list', read=True)
        existing = {a['email'] for a in accounts or []}
        for name, email in [('asapify-notify', self.worker), ('asapify-task', self.caller)]:
            if email not in existing:
                self.run('iam', 'service-accounts', 'create', name)
        self.run('projects', 'add-iam-policy-binding', self.project,
                 f'--member=serviceAccount:{self.worker}', '--role=roles/datastore.user', '--condition=None')
        for account in (self.api, self.worker):
            self.run('projects', 'add-iam-policy-binding', self.project,
                     f'--member=serviceAccount:{account}', '--role=roles/cloudtasks.enqueuer', '--condition=None')
            self.run('iam', 'service-accounts', 'add-iam-policy-binding', self.caller,
                     f'--member=serviceAccount:{account}', '--role=roles/iam.serviceAccountUser')
        queues = self.run('tasks', 'queues', 'list', f'--location={self.region}', read=True)
        verb = 'update' if any(q['name'].endswith('/asapify-notifications') for q in queues or []) else 'create'
        self.run('tasks', 'queues', verb, 'asapify-notifications', f'--location={self.region}',
                 '--max-dispatches-per-second=5', '--max-concurrent-dispatches=2',
                 '--max-attempts=8', '--min-backoff=10s', '--max-backoff=300s', '--max-retry-duration=3600s')
        # See docs/NOTIFICATIONS.md for index creation from checked-in config.

    def deploy_worker(self, version, public_origin):
        origin = urlparse(public_origin)
        if origin.scheme != 'https' or not origin.netloc or origin.username or origin.password or origin.path not in ('', '/') or origin.query or origin.fragment:
            raise ValueError('Public origin must be an HTTPS origin without path/query/credentials.')
        env = ','.join([f'GOOGLE_CLOUD_PROJECT={self.project}', 'FIRESTORE_DATABASE=asapify',
                        'NOTIFICATION_MODE=dry_run', 'TELEGRAM_ENABLED=false',
                        f'ASAPIFY_VERSION={version}', f'ASAPIFY_PUBLIC_ORIGIN={public_origin.rstrip("/")}',
                        f'NOTIFICATION_TASK_CALLER={self.caller}'])
        self.run('run', 'deploy', 'asapify-notify', f'--image={self.image(version)}', f'--region={self.region}',
                 f'--service-account={self.worker}', '--no-allow-unauthenticated', '--port=8080',
                 '--min=0', '--max=2', '--cpu=1', '--memory=512Mi', '--timeout=60', '--concurrency=4',
                 '--update-env-vars=' + env)
        service = self.run('run', 'services', 'describe', 'asapify-notify', f'--region={self.region}', read=True)
        url = service['status']['url'] if service else 'https://WORKER_URL.run.app'
        for name in ('asapify-api', 'asapify-notify'):
            self.run('run', 'services', 'update', name, f'--region={self.region}',
                     '--update-env-vars=' + env + f',NOTIFICATION_WORKER_URL={url}')
        self.run('run', 'services', 'add-iam-policy-binding', 'asapify-notify', f'--region={self.region}',
                 f'--member=serviceAccount:{self.caller}', '--role=roles/run.invoker')
        jobs = self.run('scheduler', 'jobs', 'list', f'--location={self.region}', read=True)
        verb = 'update' if any(j['name'].endswith('/asapify-notification-recovery') for j in jobs or []) else 'create'
        self.run('scheduler', 'jobs', verb, 'http', 'asapify-notification-recovery', f'--location={self.region}',
                 '--schedule=* * * * *', '--time-zone=Asia/Jakarta', '--http-method=POST',
                 f'--uri={url}/_internal/notifications/sweep', f'--oidc-service-account-email={self.caller}',
                 f'--oidc-token-audience={url}', '--attempt-deadline=60s')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['bootstrap', 'deploy'])
    p.add_argument('--project', required=True)
    p.add_argument('--region', default='asia-southeast2')
    p.add_argument('--version', default='notification-v1')
    p.add_argument('--public-origin', default='https://YOUR_PROJECT.web.app')
    p.add_argument('--apply', action='store_true')
    a = p.parse_args()
    d = NotificationsDeploy(a.project, a.region, a.apply)
    if a.action == 'bootstrap':
        d.bootstrap_notifications()
    else:
        d.deploy_worker(a.version, a.public_origin)


if __name__ == '__main__':
    main()
