"""Run once from the deployment directory as the data-directory owner."""
import os
from pathlib import Path
import secrets

root = Path(__file__).resolve().parent
secret_dir = root / 'secrets'
secret_dir.mkdir(mode=0o700, exist_ok=True)
secret_dir.chmod(0o700)
for name in ['admin_password', 'play_secret', 'metrics_token']:
    path = secret_dir / name
    if not path.exists():
        with path.open('x') as target:
            target.write(secrets.token_urlsafe(32) + '\n')
        path.chmod(0o600)
env = root / '.env'
if not env.exists():
    values = {'DATA_DIR': '/data', 'ADMIN_USER': 'admin',
              'ADMIN_PASSWORD_FILE': '/run/secrets/admin_password',
              'METRICS_TOKEN_FILE': '/run/secrets/metrics_token',
              'PLAY_SECRET_FILE': '/run/secrets/play_secret',
              'PLAY_SECRET': (secret_dir / 'play_secret').read_text().strip(),
              'COOKIE_SECURE': 'false', 'INGEST_BIND': '10.77.0.1',
              'WEB_BIND': '127.0.0.1', 'INGEST_URL': 'rtmp://10.77.0.1:1935/live',
              'STORAGE_BUDGET_BYTES': str(6 * 1024**4)}
    with env.open('x') as target:
        target.write(''.join(f'{k}={v}\n' for k, v in values.items()))
    env.chmod(0o600)
for directory in ['metadata', 'uploads', 'originals', 'dvr', 'exports', 'metadata-backups']:
    (root.parent / directory).mkdir(exist_ok=True, mode=0o750)
print('Initialized configuration. Admin password: secrets/admin_password (not printed).')
