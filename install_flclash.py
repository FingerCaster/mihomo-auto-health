#!/usr/bin/env python3
"""Preflight, install or uninstall FlClash Auto Health on Windows (v0.9.2 schema)."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import time

import auto_health as h
import flclash_health as f
from install_windows import run_task

HERE = Path(__file__).resolve().parent
LABEL = '自动选择健康隔离与恢复 (Auto Health)'


def app_is_running():
    command = ("$p = @(Get-Process -Name 'FlClash','FlClashCore' -ErrorAction SilentlyContinue); "
               "if ($p.Count -gt 0) { exit 9 }")
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                            capture_output=True, timeout=15, **h.child_options())
    if result.returncode not in (0, 9):
        raise RuntimeError('Cannot determine whether FlClash has exited')
    return result.returncode == 9


def dumps(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def prepare(data_dir):
    """Read only. No dependency download, file extraction or app setting changes."""
    data_dir = Path(data_dir).resolve()
    home = data_dir / f.HEALTH_DIR
    preferences_bytes, preferences, config = f.read_preferences(data_dir)
    profile_id = config.get('currentProfileId')
    if type(profile_id) is not int:
        raise RuntimeError('Select a profile in FlClash before installation')
    profile = f.profile_metadata(data_dir, profile_id)
    if profile['type'] not in ('file', 'url'):
        raise RuntimeError('Custom profiles are not supported; use an imported file or subscription')
    if profile['extend_type'] not in ('standard', 'script'):
        raise RuntimeError('Unsupported FlClash extension type')
    with f.database(data_dir) as connection:
        columns = {row['name'] for row in connection.execute('PRAGMA table_info(scripts)')}
        if not {'id', 'label', 'last_update_time', 'url'}.issubset(columns):
            raise RuntimeError('Unsupported FlClash scripts table')
    installation_path = home / 'installation.json'
    old = json.loads(installation_path.read_text(encoding='utf-8')) if installation_path.exists() else None
    if old and old.get('uninstalled'):
        old = None
    if old:
        if old.get('data_dir') != str(data_dir) or old['profile_id'] != profile_id:
            raise RuntimeError('This installation belongs to another directory/profile')
        if profile['script_id'] != old['script_id'] or profile['extend_type'] != 'script':
            raise RuntimeError('Binding changed. Uninstall the previous installation before reinstalling')
        installation = dict(old)
    else:
        script_id = int(time.time() * 1000)
        with f.database(data_dir) as connection:
            while connection.execute('SELECT 1 FROM scripts WHERE id=?', (script_id,)).fetchone() or (data_dir / 'scripts' / f'{script_id}.js').exists():
                script_id += 1
        patch = config.get('patchClashConfig', {})
        installation = dict(client='flclash', schema=1, data_dir=str(data_dir),
            profile_id=profile_id, script_id=script_id, owner=secrets.token_hex(24),
            previous_extend_type=profile['extend_type'], previous_script_id=profile['script_id'],
            previous_active_script_id=profile['script_id'] if profile['extend_type'] == 'script' else None,
            previous_controller={key: patch[key] for key in ('external-controller', 'secret') if key in patch},
            task_name='FlClash-AutoHealth-' + hashlib.sha256(str(data_dir).lower().encode()).hexdigest()[:10])
    script_path = data_dir / 'scripts' / f"{installation['script_id']}.js"
    previous = ''
    if installation['previous_active_script_id'] is not None:
        previous = (data_dir / 'scripts' / f"{installation['previous_active_script_id']}.js").read_text(encoding='utf-8-sig')
    if old and not script_path.read_text(encoding='utf-8-sig').startswith(f.SCRIPT_HEADER):
        raise RuntimeError('Managed script was replaced; refusing to overwrite it')
    # A full script build disables FlClash's base+overlay optimization. Before
    # first installation the source profile is authoritative for the no-script case.
    config_path = (data_dir / 'config.yaml' if profile['extend_type'] == 'script'
                   else data_dir / 'profiles' / f'{profile_id}.yaml')
    runtime = f.load_yaml(config_path.read_text(encoding='utf-8-sig'))
    f.validate_config(runtime)
    policy = {**h.DEFAULTS, 'profile_id': profile_id}
    state = {'profile_id': profile_id, 'nodes': {}}
    if old:
        policy.update(json.loads((home / 'policy.json').read_text(encoding='utf-8')))
        state = json.loads((home / 'state.json').read_text(encoding='utf-8'))
        if policy['profile_id'] != profile_id or state.get('profile_id') != profile_id:
            raise RuntimeError('Policy/state belongs to another profile')
    f.validate_policy(policy)
    excluded = {name for name, entry in state['nodes'].items() if entry.get('quarantined')}
    patch = config.setdefault('patchClashConfig', {})
    patch['external-controller'] = '127.0.0.1:9090'
    if not patch.get('secret'):
        patch['secret'] = secrets.token_urlsafe(32)
    f.Controller(patch['external-controller'], patch['secret'])
    installation['installed_controller'] = {key: patch[key] for key in ('external-controller', 'secret')}
    installation['installed_at'] = dt.datetime.now().astimezone().isoformat()
    preferences['flutter.config'] = json.dumps(config, ensure_ascii=False)
    files = {data_dir / 'shared_preferences.json': dumps(preferences),
             script_path: f.render_script(excluded, policy, installation['owner'], previous),
             home / 'policy.json': dumps(policy), home / 'state.json': dumps(state),
             home / 'installation.json': dumps(installation)}
    for name in ('auto_health.py', 'flclash_health.py', 'install_flclash.py', 'install_windows.py',
                 'task_windows.ps1', 'requirements-flclash.txt'):
        files[home / name] = (HERE / name).read_text(encoding='utf-8')
    expected = {path: path.read_bytes() if path.exists() else None for path in files}
    expected[data_dir / 'shared_preferences.json'] = preferences_bytes
    return installation, profile, files, expected


def install_binding(connection, installation, previous):
    row = connection.execute('SELECT extend_type, script_id FROM profiles WHERE id=?',
                             (installation['profile_id'],)).fetchone()
    if not row or dict(row) != {key: previous[key] for key in ('extend_type', 'script_id')}:
        raise RuntimeError('Profile changed during installation')
    script = connection.execute('SELECT label, url FROM scripts WHERE id=?', (installation['script_id'],)).fetchone()
    if script and (script['label'] != LABEL or script['url'] is not None):
        raise RuntimeError('Managed script ID conflicts with another script')
    connection.execute('INSERT INTO scripts (id,label,last_update_time,url) VALUES (?,?,?,NULL) '
                       'ON CONFLICT(id) DO UPDATE SET last_update_time=excluded.last_update_time',
                       (installation['script_id'], LABEL, int(time.time())))
    connection.execute("UPDATE profiles SET extend_type='script', script_id=? WHERE id=?",
                       (installation['script_id'], installation['profile_id']))


def transaction(data_dir, installation, files, expected, database_change, uninstall=False):
    """SQLite transaction + file/task rollback; retain complete local backups."""
    if app_is_running():
        raise RuntimeError('Exit FlClash completely from the system tray before changing its settings')
    task_name = installation['task_name']
    old_task = run_task('Export', task_name)
    backup = data_dir / f.HEALTH_DIR / 'backups' / dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup.mkdir(parents=True, mode=0o700)
    manifest = []
    for i, (path, content) in enumerate(expected.items()):
        saved = f'{i}.bin'
        if content is not None:
            (backup / saved).write_bytes(content)
        manifest.append({'path': str(path), 'backup': saved if content is not None else None})
    h.atomic_write(backup / 'files.json', dumps(manifest))
    if old_task:
        h.atomic_write(backup / 'task.xml', old_task)
    connection = f.database(data_dir, writable=True)
    applied = []
    task_attempted = False
    try:
        with sqlite3.connect(backup / 'database.sqlite', factory=f.ClosingConnection) as target:
            connection.backup(target)
        connection.execute('BEGIN IMMEDIATE')
        database_change(connection)
        for path, content in files.items():
            if (path.read_bytes() if path.exists() else None) != expected[path]:
                raise RuntimeError('File changed during installation: ' + path.name)
            applied.append(path)
            if content is None:
                path.unlink(missing_ok=True)
            else:
                h.atomic_write(path, content)
        if app_is_running():
            raise RuntimeError('FlClash started during installation; close it and retry')
        task_attempted = True
        if uninstall:
            run_task('Remove', task_name)
            if run_task('Export', task_name):
                raise RuntimeError('Task removal verification failed')
        else:
            home = data_dir / f.HEALTH_DIR
            run_task('Register', task_name, Pythonw=Path(sys.executable).with_name('pythonw.exe'),
                Daemon=home / 'flclash_health.py', DataDir=data_dir, LogFile=home / 'agent.log',
                Description='FlClash automatic node quarantine and recovery')
            if not run_task('Export', task_name):
                raise RuntimeError('Task registration verification failed')
        for path, content in files.items():
            actual = path.read_bytes() if path.exists() else None
            wanted = content.encode('utf-8') if isinstance(content, str) else content
            if actual != wanted:
                raise RuntimeError('File readback failed: ' + path.name)
        connection.commit()
    except Exception as error:
        connection.rollback()
        failures = []
        for path in reversed(applied):
            try:
                if expected[path] is None:
                    path.unlink(missing_ok=True)
                else:
                    h.atomic_write(path, expected[path])
            except OSError as failure:
                failures.append(type(failure).__name__)
        if task_attempted:
            try:
                if old_task:
                    run_task('Restore', task_name, XmlFile=backup / 'task.xml')
                else:
                    run_task('Remove', task_name)
            except RuntimeError as failure:
                failures.append(type(failure).__name__)
        if failures:
            raise RuntimeError('Rollback incomplete; inspect backup: ' + str(backup)) from error
        raise RuntimeError('Changes were rolled back: ' + str(error)) from error
    finally:
        connection.close()
    return backup


def uninstall(data_dir):
    home = data_dir / f.HEALTH_DIR
    installation = json.loads((home / 'installation.json').read_text(encoding='utf-8'))
    if installation.get('data_dir') != str(data_dir):
        raise RuntimeError('Installation belongs to another directory')
    if installation.get('uninstalled'):
        print(dumps({'uninstalled': True, 'already_removed': True}))
        return
    raw, preferences, config = f.read_preferences(data_dir)
    patch = config.setdefault('patchClashConfig', {})
    # Do not undo controller settings the user changed after installation.
    if all(patch.get(key) == value for key, value in installation['installed_controller'].items()):
        for key in ('external-controller', 'secret'):
            if key in installation['previous_controller']:
                patch[key] = installation['previous_controller'][key]
            else:
                patch.pop(key, None)
    preferences['flutter.config'] = json.dumps(config, ensure_ascii=False)
    script_path = data_dir / 'scripts' / f"{installation['script_id']}.js"
    if script_path.exists() and not script_path.read_text(encoding='utf-8-sig').startswith(f.SCRIPT_HEADER):
        raise RuntimeError('Managed script was replaced; refusing to delete user edits')
    installation['uninstalled'] = True
    files = {data_dir / 'shared_preferences.json': dumps(preferences),
             home / 'installation.json': dumps(installation), script_path: None}
    expected = {path: path.read_bytes() if path.exists() else None for path in files}
    expected[data_dir / 'shared_preferences.json'] = raw

    def remove_binding(connection):
        users = connection.execute('SELECT id FROM profiles WHERE script_id=? AND id<>?',
            (installation['script_id'], installation['profile_id'])).fetchall()
        if users:
            raise RuntimeError('Other profiles use the managed script; unbind them before uninstalling')
        connection.execute('UPDATE profiles SET extend_type=?, script_id=? WHERE id=? AND script_id=?',
            (installation['previous_extend_type'], installation['previous_script_id'],
             installation['profile_id'], installation['script_id']))
        connection.execute('DELETE FROM scripts WHERE id=?', (installation['script_id'],))

    backup = transaction(data_dir, installation, files, expected, remove_binding, uninstall=True)
    print(dumps({'uninstalled': True, 'backup': str(backup),
                'next': 'Restart FlClash and reapply your profile. Health history and backups are retained.'}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--install', action='store_true')
    action.add_argument('--uninstall', action='store_true')
    parser.add_argument('--data-dir', type=Path)
    args = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('The FlClash installer currently supports Windows only')
    data_dir = f.choose_data_dir(args.data_dir)
    if not args.install and not args.uninstall:
        installation, _, _, _ = prepare(data_dir)
        print(dumps({'check': 'passed', 'data_dir': str(data_dir),
            'profile_id': installation['profile_id'], 'task_name': installation['task_name'],
            'app_running': app_is_running(),
            'next': 'Exit FlClash, then run install_flclash_windows.ps1 -Install'}))
        return
    if not args.uninstall and not Path(sys.executable).with_name('pythonw.exe').is_file():
        raise RuntimeError('pythonw.exe is required; install standard Windows Python')
    if app_is_running():
        raise RuntimeError('Exit FlClash from the system tray before installing or uninstalling')
    h.HOME_DIR = data_dir / f.HEALTH_DIR
    with h.process_lock() as acquired:
        if not acquired:
            raise RuntimeError('Health check is running; retry in a moment')
        if args.uninstall:
            uninstall(data_dir)
        else:
            installation, previous, files, expected = prepare(data_dir)
            backup = transaction(data_dir, installation, files, expected,
                lambda connection: install_binding(connection, installation, previous))
            print(dumps({'installed': True, 'data_dir': str(data_dir), 'backup': str(backup),
                'task_name': installation['task_name'],
                'next': 'Start FlClash and apply the selected profile, then check status.json after 60-90 seconds'}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Avoid displaying database contents or generated config credentials.
        print('FlClash Auto Health: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
