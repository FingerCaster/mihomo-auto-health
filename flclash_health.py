#!/usr/bin/env python3
"""FlClash desktop adapter. Windows installer: install_flclash_windows.ps1."""
import argparse
import datetime as dt
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import urllib.parse

import auto_health as h

HEALTH_DIR = 'adaptive-auto-selection-flclash'
OWNER_KEY = 'x-auto-health-owner'
SCRIPT_HEADER = '// Managed by Mihomo Auto Health (FlClash).\n'


def yaml_module():
    try:
        import yaml
        return yaml
    except ImportError as error:
        raise RuntimeError('Install dependencies: python -m pip install -r requirements-flclash.txt') from error


def load_yaml(text):
    yaml = yaml_module()
    # Mihomo uses YAML 1.2 booleans. Preserve node names such as ON, NO and dates.
    class Loader(yaml.SafeLoader):
        yaml_implicit_resolvers = {
            key: [(tag, pattern) for tag, pattern in entries
                  if tag not in ('tag:yaml.org,2002:bool', 'tag:yaml.org,2002:timestamp')]
            for key, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()}
    Loader.add_implicit_resolver('tag:yaml.org,2002:bool',
        re.compile(r'^(?:true|True|TRUE|false|False|FALSE)$'), list('tTfF'))
    try:
        value = yaml.load(text, Loader=Loader)
    except yaml.YAMLError as error:
        raise RuntimeError('Invalid YAML configuration; inspect it locally (contents omitted)') from error
    if not isinstance(value, dict):
        raise RuntimeError('Expected a YAML configuration object')
    return value


def patch_groups(text, groups):
    """Change only the group block, preserving credentials, comments and DNS."""
    yaml = yaml_module()
    try:
        root = yaml.compose(text)
    except yaml.YAMLError as error:
        raise RuntimeError('Invalid YAML configuration; inspect it locally (contents omitted)') from error
    if not isinstance(root, yaml.MappingNode):
        raise RuntimeError('Expected a YAML mapping')
    matches = [(i, key, value) for i, (key, value) in enumerate(root.value)
               if key.value == 'proxy-groups']
    if len(matches) != 1 or root.flow_style:
        raise RuntimeError('Expected one block-style proxy-groups section')
    index, key, _ = matches[0]
    end = root.value[index + 1][0].start_mark.index if index + 1 < len(root.value) else len(text)
    block = yaml.safe_dump({'proxy-groups': groups}, allow_unicode=True, sort_keys=False, width=100000)
    result = text[:key.start_mark.index] + block + text[end:]
    expected = load_yaml(text)
    expected['proxy-groups'] = groups
    if load_yaml(result) != expected:
        raise RuntimeError('Refusing a change outside proxy-groups (check YAML anchors)')
    return result


def read_preferences(data_dir):
    path = Path(data_dir) / 'shared_preferences.json'
    raw = path.read_bytes()
    preferences = json.loads(raw.decode('utf-8-sig'))
    encoded = preferences.get('flutter.config')
    if not isinstance(encoded, str):
        raise RuntimeError('Unsupported FlClash preferences: flutter.config is missing')
    config = json.loads(encoded)
    if not isinstance(config, dict):
        raise RuntimeError('Invalid FlClash preferences')
    return raw, preferences, config


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def database(data_dir, writable=False):
    path = (Path(data_dir) / 'database.sqlite').resolve()
    # mode=rw/ro avoids creating an empty database for a wrong data directory.
    connection = sqlite3.connect(path.as_uri() + ('?mode=rw' if writable else '?mode=ro'),
                                 uri=True, timeout=5, factory=ClosingConnection)
    connection.row_factory = sqlite3.Row
    return connection


def profile_metadata(data_dir, profile_id):
    with database(data_dir) as connection:
        try:
            row = connection.execute(
                'SELECT id, type, extend_type, script_id FROM profiles WHERE id=?',
                (profile_id,)).fetchone()
        except sqlite3.OperationalError as error:
            raise RuntimeError('Unsupported FlClash database; this adapter targets the v0.9.2 schema') from error
    if row is None:
        raise RuntimeError('Selected FlClash profile does not exist')
    return dict(row)


def profile_selections(data_dir, profile_id):
    """Read FlClash's durable choices without modifying its database."""
    with database(data_dir) as connection:
        try:
            row = connection.execute('SELECT selected_map FROM profiles WHERE id=?',
                                     (profile_id,)).fetchone()
        except sqlite3.OperationalError as error:
            raise RuntimeError('Unsupported FlClash database: selected_map is missing') from error
    if row is None:
        raise RuntimeError('Selected FlClash profile does not exist')
    try:
        selections = json.loads(row['selected_map'])
    except (TypeError, ValueError) as error:
        raise RuntimeError('Invalid FlClash selected_map (contents omitted)') from error
    if not isinstance(selections, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in selections.items()):
        raise RuntimeError('Invalid FlClash selected_map (contents omitted)')
    return selections


def selection_snapshot(live, persisted):
    selections = {}
    for name, proxy in live.items():
        kind = proxy.get('type')
        if kind == 'Selector':
            selected = proxy.get('now')
            if selected:
                selections[name] = (kind, selected)
        elif kind in ('URLTest', 'Fallback'):
            # `now` is only the current winner. Pinning it would disable normal
            # automatic choice. An explicit empty durable choice means unpin.
            selected = persisted.get(name, proxy.get('fixed', ''))
            selections[name] = (kind, selected)
    return selections


def restore_selections(controller, selections, live=None):
    if not selections:
        return
    if live is None:
        live = controller.api('/proxies')['proxies']
    for name, (kind, selected) in selections.items():
        proxy = live.get(name, {})
        if proxy.get('type') != kind:
            continue
        current = proxy.get('now' if kind == 'Selector' else 'fixed', '')
        if current == selected:
            continue
        path = '/proxies/' + urllib.parse.quote(name, safe='')
        if not selected:
            if kind in ('URLTest', 'Fallback'):
                controller.api(path, 'DELETE')
        elif selected in proxy.get('all', []):
            controller.api(path, 'PUT', {'name': selected})
        # An excluded/removed preferred node cannot be selected through HTTP.
        # Leave selected_map intact; a subsequent cycle restores it on return.


def choose_data_dir(explicit=None):
    candidates = [Path(explicit).expanduser()] if explicit else [
        Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')) / 'com.follow/clash',
        Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')) / 'com.follow/FlClash',
    ]
    found = {p.resolve() for p in candidates
             if (p / 'shared_preferences.json').is_file() and (p / 'database.sqlite').is_file()}
    if len(found) != 1:
        raise RuntimeError('Cannot uniquely find FlClash data. Pass --data-dir / -DataDir containing database.sqlite and shared_preferences.json')
    return found.pop()


def validate_config(config):
    proxies = config.get('proxies')
    groups = config.get('proxy-groups')
    if not isinstance(proxies, list) or not proxies:
        raise RuntimeError('Inline proxies are required; provider-only or overlay-only configurations are not supported')
    if not all(isinstance(p, dict) and isinstance(p.get('name'), str) for p in proxies):
        raise RuntimeError('Invalid inline proxy definitions')
    if not isinstance(groups, list) or not any(g.get('type') == 'url-test' for g in groups):
        raise RuntimeError('The profile must contain a url-test group')


def validate_policy(policy):
    for key in ('timeout_ms', 'interval_seconds', 'max_failed_times', 'window_size',
                'min_samples', 'failed_samples', 'consecutive_failures',
                'recovery_interval_seconds', 'recovery_successes', 'probe_workers'):
        if type(policy[key]) is not int or policy[key] <= 0:
            raise RuntimeError('Policy must contain a positive integer: ' + key)
    if not 1 <= policy['probe_workers'] <= 32:
        raise RuntimeError('probe_workers must be between 1 and 32')
    if not 0 <= policy['tolerance_ms'] or policy['timeout_ms'] > 60000:
        raise RuntimeError('Invalid tolerance_ms or timeout_ms')
    if max(policy['min_samples'], policy['consecutive_failures'], policy['failed_samples']) > policy['window_size']:
        raise RuntimeError('Health thresholds cannot exceed window_size')
    if urllib.parse.urlsplit(policy['test_url']).scheme not in ('http', 'https'):
        raise RuntimeError('test_url must be an HTTP(S) URL')


class ControllerError(RuntimeError):
    def __init__(self, status):
        self.status = status
        # Never echo core responses: they can contain subscription credentials.
        super().__init__('FlClash controller HTTP ' + str(status))


class Controller:
    def __init__(self, address, secret):
        parsed = urllib.parse.urlsplit('http://' + str(address))
        try:
            local = ipaddress.ip_address(parsed.hostname).is_loopback
            port = parsed.port
        except (TypeError, ValueError):
            local, port = False, None
        if not local or not port or parsed.path or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeError('Only a numeric loopback external-controller address is supported')
        if not isinstance(secret, str) or not secret or '\r' in secret or '\n' in secret:
            raise RuntimeError('Enable the external controller with a nonempty secret in FlClash')
        self.host, self.port, self.secret = parsed.hostname, port, secret

    def api(self, path, method='GET', body=None, timeout=10):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            payload = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
            connection.request(method, path, body=payload,
                headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.secret})
            response = connection.getresponse()
            content = response.read()
            if not 200 <= response.status < 300:
                raise ControllerError(response.status)
            return json.loads(content) if content else {}
        finally:
            connection.close()

    def probe(self, name, policy):
        query = urllib.parse.urlencode({'url': policy['test_url'], 'timeout': policy['timeout_ms'], 'expected': '204'})
        try:
            result = self.api('/proxies/' + urllib.parse.quote(name, safe='') + '/delay?' + query,
                              timeout=policy['timeout_ms'] / 1000 + 3)
            return result.get('delay', 0) > 0
        except ControllerError as error:
            if error.status not in (503, 504):
                raise
            return False
        except OSError:
            return False


def render_script(excluded, policy, owner, previous=''):
    # Separate lexical scopes preserve existing const/async main functions.
    previous = previous or 'function main(config) { return config; }'
    return (SCRIPT_HEADER + 'const __autoHealthPrevious = (() => {\n' + previous +
        '\n; return main;\n})();\nconst __autoHealthTransform = (() => {\n' +
        h.render_override(excluded, policy) + '\n; return main;\n})();\n' +
        'async function main(config) {\n' +
        '  const result = await __autoHealthPrevious(config);\n' +
        '  const updated = __autoHealthTransform(result == null ? config : result);\n' +
        '  updated[' + json.dumps(OWNER_KEY) + '] = ' + json.dumps(owner) + ';\n' +
        '  return updated;\n}\n')


def binding_active(data_dir, installation):
    _, _, config = read_preferences(data_dir)
    if config.get('currentProfileId') != installation['profile_id']:
        return False
    profile = profile_metadata(data_dir, installation['profile_id'])
    return profile['extend_type'] == 'script' and profile['script_id'] == installation['script_id']


def apply_rules(data_dir, installation, policy, excluded, controller, before_text):
    home = data_dir / HEALTH_DIR
    runtime_path = data_dir / 'config.yaml'
    script_path = data_dir / 'scripts' / (str(installation['script_id']) + '.js')
    previous = ''
    previous_id = installation.get('previous_active_script_id')
    if previous_id is not None:
        previous = (data_dir / 'scripts' / (str(previous_id) + '.js')).read_text(encoding='utf-8-sig')
    before_script = script_path.read_bytes()
    if not before_script.decode('utf-8-sig').startswith(SCRIPT_HEADER):
        raise RuntimeError('Managed script was edited or replaced; reinstall before proceeding')
    generated = render_script(excluded, policy, installation['owner'], previous)
    before = load_yaml(before_text)
    after = h.transform(before, excluded, policy)
    changed = after != before
    replacement = patch_groups(before_text, after['proxy-groups']) if changed else before_text

    def unchanged():
        return (binding_active(data_dir, installation) and
                runtime_path.read_text(encoding='utf-8-sig') == before_text and
                script_path.read_bytes() == before_script)

    if not unchanged():
        return False
    persisted = profile_selections(data_dir, installation['profile_id'])
    live = controller.api('/proxies')['proxies']
    selections = selection_snapshot(live, persisted)
    if not unchanged():
        return False

    reload_accepted = False
    def reload(payload):
        nonlocal reload_accepted
        # force=false replaces groups without starting listeners that the user
        # has stopped in FlClash. FlClash manages both manual choices and
        # automatic-group pins itself, with store-selected disabled.
        controller.api('/configs?force=false', 'PUT', {'payload': payload}, timeout=30)
        reload_accepted = True
        if not binding_active(data_dir, installation):
            return
        latest = profile_selections(data_dir, installation['profile_id'])
        restored = dict(selections)
        for name, (kind, selected) in restored.items():
            if latest.get(name) != persisted.get(name):
                # Respect a GUI choice/unpin made while the reload was running.
                restored[name] = (kind, latest.get(name, ''))
        restore_selections(controller, restored)

    # PUT /configs parses the complete candidate before applying it. FlClashCore
    # is an RPC host, not the standalone `mihomo -t` executable.
    if changed:
        h.atomic_write(home / 'candidate.yaml', replacement)
        try:
            reload(replacement)
        except Exception as error:
            # A rejected config was never applied. A lost response is ambiguous;
            # restore the previous payload only while the same binding is live.
            if (reload_accepted or not isinstance(error, ControllerError)) and unchanged():
                try:
                    reload(before_text)
                except Exception as rollback:
                    raise RuntimeError('Reload and rollback failed; reapply the profile in FlClash') from rollback
            raise
        if not unchanged():
            h.event('defer_concurrent_profile_update')
            return False
    else:
        # Also repair pins lost by an older agent without forcing a reload.
        restore_selections(controller, selections, live)
        if not unchanged():
            h.event('defer_concurrent_profile_update')
            return False
    try:
        h.atomic_write(script_path, generated)
        if changed:
            h.atomic_write(runtime_path, replacement)
    except Exception:
        h.atomic_write(script_path, before_script)
        if (changed and binding_active(data_dir, installation) and
                runtime_path.read_text(encoding='utf-8-sig') == before_text):
            reload(before_text)
        raise
    return True


def run_once(data_dir, force_probe=False):
    data_dir = Path(data_dir).resolve()
    home = data_dir / HEALTH_DIR
    installation = json.loads((home / 'installation.json').read_text(encoding='utf-8'))
    if installation.get('data_dir') != str(data_dir) or installation.get('uninstalled'):
        raise RuntimeError('Installation is inactive or belongs to another directory')
    policy = {**h.DEFAULTS, **json.loads((home / 'policy.json').read_text(encoding='utf-8'))}
    validate_policy(policy)
    if policy['profile_id'] != installation['profile_id']:
        raise RuntimeError('Policy belongs to a different profile')

    def status(event, **fields):
        snapshot = dict(updated_at=dt.datetime.now().isoformat(timespec='seconds'),
                        client='flclash', profile_id=installation['profile_id'],
                        event=event, rules_applied=False, **fields)
        h.atomic_write(home / 'status.json', json.dumps(snapshot, ensure_ascii=False, indent=2))
        h.event(event, **fields)

    if not binding_active(data_dir, installation):
        status('inactive_profile')
        return
    runtime_path = data_dir / 'config.yaml'
    before_text = runtime_path.read_text(encoding='utf-8-sig')
    runtime = load_yaml(before_text)
    if runtime.get(OWNER_KEY) != installation['owner']:
        status('awaiting_profile_apply')
        return
    validate_config(runtime)
    controller = Controller(runtime.get('external-controller'), runtime.get('secret'))
    try:
        controller.api('/version', timeout=3)
        proxies = controller.api('/proxies')['proxies']
    except OSError:
        status('core_not_running')
        return
    # A stale config file or another client listening on the same port must not
    # receive a reload. Authentication plus live node/group checks bind the core.
    if (any(p['name'] not in proxies for p in runtime['proxies']) or
        any(g['name'] not in proxies for g in runtime['proxy-groups'])):
        status('runtime_mismatch')
        return
    state_path = home / 'state.json'
    state = json.loads(state_path.read_text(encoding='utf-8'))
    if state.get('profile_id') != policy['profile_id']:
        raise RuntimeError('State belongs to a different profile')
    excluded, due = h.evaluate_health(runtime, proxies, state, policy,
        lambda name: controller.probe(name, policy), force_probe)
    h.atomic_write(state_path, json.dumps(state, ensure_ascii=False, indent=2))
    applied = apply_rules(data_dir, installation, policy, excluded, controller, before_text)
    snapshot = dict(updated_at=dt.datetime.now().isoformat(timespec='seconds'),
        client='flclash', profile_id=policy['profile_id'], event='cycle_complete',
        quarantined_count=len(excluded), recovery_probes=due, rules_applied=applied,
        recovering={n: e.get('recovery_streak', 0) for n, e in state['nodes'].items()
                    if n in excluded and e.get('recovery_streak', 0)})
    h.atomic_write(home / 'status.json', json.dumps(snapshot, ensure_ascii=False, indent=2))
    h.event('cycle_complete', **{k: v for k, v in snapshot.items() if k not in ('event', 'recovering')})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--force-probe', action='store_true')
    parser.add_argument('--log-file', type=Path)
    args = parser.parse_args()
    h.HOME_DIR = args.data_dir / HEALTH_DIR
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = args.log_file.open('a', encoding='utf-8', buffering=1)
    with h.process_lock() as acquired:
        if not acquired:
            return 0
        try:
            run_once(args.data_dir, args.force_probe)
        except Exception as error:
            h.event('cycle_error', error=type(error).__name__ + ': ' + str(error))
            h.atomic_write(h.HOME_DIR / 'status.json', json.dumps({
                'updated_at': dt.datetime.now().isoformat(timespec='seconds'),
                'event': 'cycle_error', 'rules_applied': False, 'error_type': type(error).__name__}))
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
