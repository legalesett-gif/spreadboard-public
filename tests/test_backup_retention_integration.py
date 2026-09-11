"""Exercise retention against real isolated restic snapshots, not argv alone."""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from scripts import backup_spreadboard as backup


def test_rotating_stage_paths_share_retention_without_touching_other_backups(tmp_path, monkeypatch):
    restic = shutil.which('restic')
    if restic is None:
        pytest.skip('real restic binary required for repository integration')
    password = tmp_path / 'fixture-password'
    password.write_text('disposable-local-test-repository')
    environment = {
        'PATH': os.environ.get('PATH', ''),
        'RESTIC_REPOSITORY': str(tmp_path / 'repository'),
        'RESTIC_PASSWORD_FILE': str(password),
        'RESTIC_CACHE_DIR': str(tmp_path / 'cache'),
    }

    def run(command, **kwargs):
        kwargs.update(env=environment, capture_output=True, text=True)
        kwargs.setdefault('timeout', 45)
        return subprocess.run(command, check=kwargs.pop('check', False), **kwargs)

    def command(*args):
        return run([restic, *args], check=True).stdout

    command('init')
    # Five historical runs of one service, each with a distinct staging path.
    for day in range(1, 6):
        stage = tmp_path / f'old-stage-{day}'
        stage.mkdir()
        (stage / 'payload.txt').write_text(f'old-{day}')
        command('backup', '--host', 'spreadboard-prod', '--tag', 'spreadboard',
                '--time', f'2020-01-{day:02d} 12:00:00', str(stage))
    for host, tag in [('another-host', 'spreadboard'), ('spreadboard-prod', 'another-backup')]:
        for day in range(1, 4):
            command('backup', '--host', host, '--tag', tag,
                    '--time', f'2020-01-{day:02d} 13:00:00', str(stage))
    before = json.loads(command('snapshots', '--json'))
    unrelated = {s['id'] for s in before if s['hostname'] != 'spreadboard-prod' or s['tags'] != ['spreadboard']}
    source = tmp_path / 'runtime'
    source.mkdir()
    (source / 'state.json').write_text('{"verified":true}')
    monkeypatch.setattr(backup, 'RUNTIME_DIR', source)
    monkeypatch.setattr(backup, 'RESTIC', restic)
    monkeypatch.setattr(backup, 'RETENTION_DAILY', '2')
    monkeypatch.setattr(backup, 'RETENTION_WEEKLY', '0')
    monkeypatch.setattr(backup, 'RETENTION_MONTHLY', '0')
    monkeypatch.setattr(backup, '_run_restic', run)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    backup.run_backup()
    after = json.loads(command('snapshots', '--json'))
    owned = [s for s in after if s['hostname'] == 'spreadboard-prod' and s['tags'] == ['spreadboard']]
    assert len(owned) == 2, 'random temporary paths must not preserve every old run'
    assert unrelated <= {s['id'] for s in after}, 'retention must preserve other hosts and backup tags'
    assert any(s['time'].startswith('2020-01-05') for s in owned)
    latest = max(owned, key=lambda s: s['time'])
    restored = tmp_path / 'restored'
    command('restore', latest['id'], '--target', str(restored))
    state_files = list(restored.rglob('state.json'))
    assert len(state_files) == 1
    assert json.loads(state_files[0].read_text()) == {'verified': True}
