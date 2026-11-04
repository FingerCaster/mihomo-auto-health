import contextlib
import copy
import http.server
import json
from pathlib import Path
import socketserver
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import auto_health as h
import flclash_health as f
import install_flclash as install
from test_auto_health import history


def fixture():
    return {'proxies': [{'name': '节点 (A)', 'type': 'ss'}, {'name': 'NO', 'type': 'ss'}],
        'proxy-groups': [{'name': 'Auto', 'type': 'url-test', 'proxies': ['节点 (A)', 'NO']},
                         {'name': 'Manual', 'type': 'select', 'proxies': ['节点 (A)', 'NO']}],
        'dns': {'enable': True}, 'rules': ['MATCH,Auto'], 'external-controller': '127.0.0.1:9090',
        'secret': 'fixture-only', f.OWNER_KEY: 'fixture-owner'}


class FlClashFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / f.HEALTH_DIR
        (self.root / 'scripts').mkdir()
        (self.root / 'profiles').mkdir()
        with sqlite3.connect(self.root / 'database.sqlite', factory=f.ClosingConnection) as conn:
            conn.executescript('''
                CREATE TABLE profiles (id INTEGER PRIMARY KEY, type TEXT NOT NULL,
                    extend_type TEXT NOT NULL, script_id INTEGER, label TEXT,
                    selected_map TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE scripts (id INTEGER PRIMARY KEY, label TEXT NOT NULL,
                    last_update_time INTEGER NOT NULL, url TEXT, "order" INTEGER);
                INSERT INTO profiles(id,type,extend_type,script_id,label)
                    VALUES (101, 'url', 'standard', NULL, 'Private profile');
                INSERT INTO profiles(id,type,extend_type,script_id,label)
                    VALUES (102, 'url', 'standard', NULL, 'Other profile');
            ''')
        self.config = {'currentProfileId': 101, 'themeProps': {'unrelated': True},
                       'patchClashConfig': {'secret': '', 'external-controller': '', 'mixed-port': 7890}}
        self.write_config(self.config)
        self.runtime = fixture()
        text = f.yaml_module().safe_dump(self.runtime, allow_unicode=True, sort_keys=False)
        (self.root / 'profiles/101.yaml').write_text(text, encoding='utf-8')
        (self.root / 'config.yaml').write_text(text, encoding='utf-8')

    def write_config(self, config):
        (self.root / 'shared_preferences.json').write_text(json.dumps({
            'flutter.config': json.dumps(config), 'flutter.unrelated': 'preserve'}), encoding='utf-8')

    def save_selections(self, selections):
        with f.database(self.root, writable=True) as conn:
            conn.execute('UPDATE profiles SET selected_map=? WHERE id=101',
                         (json.dumps(selections),))

    def install_fixture(self):
        installation, profile, files, _ = install.prepare(self.root)
        for path, text in files.items():
            h.atomic_write(path, text)
        with f.database(self.root, writable=True) as conn:
            install.install_binding(conn, installation, profile)
        self.installation = installation
        self.runtime[f.OWNER_KEY] = installation['owner']
        self.runtime['secret'] = installation['installed_controller']['secret']
        h.atomic_write(self.root / 'config.yaml', f.yaml_module().safe_dump(self.runtime, allow_unicode=True, sort_keys=False))
        return installation


class MetadataTests(FlClashFixture):
    def test_preflight_is_read_only_and_enables_local_controller_in_plan(self):
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        installation, profile, files, expected = install.prepare(self.root)
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(installation['profile_id'], 101)
        planned = json.loads(files[self.root / 'shared_preferences.json'])
        settings = json.loads(planned['flutter.config'])
        self.assertEqual(settings['themeProps'], self.config['themeProps'])
        self.assertEqual(settings['patchClashConfig']['external-controller'], '127.0.0.1:9090')
        self.assertGreater(len(settings['patchClashConfig']['secret']), 30)
        self.assertEqual(planned['flutter.unrelated'], 'preserve')

    def test_install_and_reinstall_preserve_state_and_do_not_double_wrap(self):
        installation = self.install_fixture()
        state = {'profile_id': 101, 'nodes': {'NO': {'quarantined': True}}}
        h.atomic_write(self.home / 'state.json', json.dumps(state))
        second, profile, files, _ = install.prepare(self.root)
        self.assertEqual(installation['script_id'], second['script_id'])
        self.assertEqual(installation['owner'], second['owner'])
        self.assertEqual(json.loads(files[self.home / 'state.json']), state)
        self.assertEqual(files[self.root / 'scripts' / f"{installation['script_id']}.js"].count(f.SCRIPT_HEADER), 1)
        self.assertEqual(f.profile_metadata(self.root, 102)['script_id'], None)
        self.assertTrue(f.binding_active(self.root, installation))
        self.config['currentProfileId'] = 102
        self.write_config(self.config)
        self.assertFalse(f.binding_active(self.root, installation))
        with self.assertRaisesRegex(RuntimeError, 'another directory/profile'):
            install.prepare(self.root)

    def test_existing_script_is_preserved(self):
        code = 'const main = async c => { c.mode = "rule"; return c; };'
        (self.root / 'scripts/77.js').write_text(code, encoding='utf-8')
        with f.database(self.root, writable=True) as conn:
            conn.execute("INSERT INTO scripts VALUES (77, 'Mine', 0, NULL, NULL)")
            conn.execute("UPDATE profiles SET extend_type='script',script_id=77 WHERE id=101")
        installation, _, files, _ = install.prepare(self.root)
        self.assertEqual(installation['previous_active_script_id'], 77)
        self.assertEqual((self.root / 'scripts/77.js').read_text(encoding='utf-8'), code)
        self.assertIn(code, files[self.root / 'scripts' / f"{installation['script_id']}.js"])

    def test_custom_provider_only_and_wrong_schema_are_rejected(self):
        with f.database(self.root, writable=True) as conn:
            conn.execute("UPDATE profiles SET type='custom' WHERE id=101")
        with self.assertRaisesRegex(RuntimeError, 'Custom profiles'):
            install.prepare(self.root)
        with f.database(self.root, writable=True) as conn:
            conn.execute("UPDATE profiles SET type='url' WHERE id=101")
        (self.root / 'profiles/101.yaml').write_text('proxy-providers: {}\n', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'Inline proxies'):
            install.prepare(self.root)
        with f.database(self.root, writable=True) as conn:
            conn.execute('ALTER TABLE profiles RENAME COLUMN extend_type TO overwrite_type')
        with self.assertRaisesRegex(RuntimeError, 'Unsupported FlClash database'):
            install.prepare(self.root)


class TransformTests(unittest.TestCase):
    def test_yaml_literals_and_non_group_content_are_preserved(self):
        text = ('# retained\nproxies:\n- name: NO\n  type: ss\n'
                'proxy-groups:\n- name: Auto\n  type: url-test\n  proxies: [NO]\n'
                'dns:\n  enable: true\nlan-allowed-ips:\n- ::/0\nlabel: 2026-01-01\nsecret: ":value"\n')
        config = f.load_yaml(text)
        self.assertEqual(config['proxies'][0]['name'], 'NO')
        self.assertEqual(config['label'], '2026-01-01')
        self.assertEqual(config['lan-allowed-ips'], ['::/0'])
        changed = f.patch_groups(text, h.transform(config, ['NO'], h.DEFAULTS)['proxy-groups'])
        self.assertTrue(changed.startswith(text.split('proxy-groups:')[0]))
        self.assertTrue(changed.endswith(text.split('dns:', 1)[1]))
        self.assertEqual(f.load_yaml(changed), h.transform(config, ['NO'], h.DEFAULTS))

    def test_script_composes_async_existing_main_and_tags_runtime(self):
        previous = 'const main = async c => { c.rules.unshift("DOMAIN,example.test,DIRECT"); return c; };'
        code = f.render_script(['节点 (A)'], h.DEFAULTS, 'owner', previous)
        runner = '''const vm=require('node:vm');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const ctx=vm.createContext({}); vm.runInContext(input.code,ctx); ctx.cfg=input.config;
Promise.resolve(vm.runInContext('main(cfg)',ctx)).then(x=>process.stdout.write(JSON.stringify(x)));'''
        result = json.loads(subprocess.check_output([h.node_binary(), '-e', runner],
            input=json.dumps({'code': code, 'config': fixture()}), text=True, encoding='utf-8'))
        self.assertEqual(result[f.OWNER_KEY], 'owner')
        self.assertEqual(result['rules'][0], 'DOMAIN,example.test,DIRECT')
        self.assertIn('节点', result['proxy-groups'][0]['exclude-filter'])
        self.assertEqual(result['proxy-groups'][1], fixture()['proxy-groups'][1])

    def test_shared_state_machine_isolates_recovers_and_preserves_manual_nodes(self):
        config = fixture()
        policy = dict(h.DEFAULTS, profile_id=101)
        data = {'Auto': {'all': ['NO', '节点 (A)']},
                'NO': {'extra': {policy['test_url']: {'history': history([False] * 3)}}},
                '节点 (A)': {'extra': {policy['test_url']: {'history': history([True] * 3)}}}}
        state = {'nodes': {}}
        excluded, due = h.evaluate_health(config, data, state, policy, lambda name: False)
        self.assertEqual(excluded, {'NO'})
        self.assertEqual(due, 1)
        for _ in range(3):
            excluded, _ = h.evaluate_health(config, data, state, policy, lambda name: True, True)
        self.assertEqual(excluded, set())
        self.assertEqual(state['nodes']['NO']['samples'], [])


class ControllerTests(unittest.TestCase):
    def test_only_authenticated_loopback_is_accepted(self):
        for address in ('0.0.0.0:9090', 'example.com:9090', 'localhost:9090',
                        '127.0.0.1:9090/x', '127.0.0.1:9090?x=1'):
            with self.subTest(address=address), self.assertRaises(RuntimeError):
                f.Controller(address, 'secret')
        f.Controller('[::1]:9090', 'secret')
        with self.assertRaises(RuntimeError):
            f.Controller('127.0.0.1:9090', '')

    def test_http_auth_unicode_payload_and_error_do_not_leak_credentials(self):
        calls = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                calls.append((self.path, self.headers.get('Authorization')))
                if self.path == '/denied':
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'private subscription secret')
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"version":"fixture","delay":21}')
            def do_PUT(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append(payload)
                self.send_response(204)
                self.end_headers()
        with socketserver.TCPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = f.Controller('127.0.0.1:' + str(server.server_address[1]), 'local-secret')
                self.assertEqual(client.api('/version')['version'], 'fixture')
                self.assertEqual(calls[0], ('/version', 'Bearer local-secret'))
                self.assertTrue(client.probe('节点 / A', h.DEFAULTS))
                self.assertIn('expected=204', calls[-1][0])
                self.assertIn('%E8%8A%82', calls[-1][0])
                client.api('/configs?force=true', 'PUT', {'payload': '节点: 日本'})
                self.assertEqual(calls[-1], {'payload': '节点: 日本'})
                with self.assertRaisesRegex(f.ControllerError, '^FlClash controller HTTP 401$'):
                    client.api('/denied')
            finally:
                server.shutdown()
                thread.join(timeout=2)

    def test_auth_failure_is_not_a_node_health_failure(self):
        client = f.Controller('127.0.0.1:9090', 'secret')
        with mock.patch.object(client, 'api', side_effect=f.ControllerError(401)):
            with self.assertRaises(f.ControllerError):
                client.probe('NO', h.DEFAULTS)
        with mock.patch.object(client, 'api', side_effect=f.ControllerError(504)):
            self.assertFalse(client.probe('NO', h.DEFAULTS))


class RuntimeTests(FlClashFixture):
    def setUp(self):
        super().setUp()
        self.install_fixture()
        self.policy = dict(h.DEFAULTS, profile_id=101)
        self.client = mock.Mock(spec=f.Controller)
        self.client.api.return_value = {'proxies': {}}
        self.before = (self.root / 'config.yaml').read_text(encoding='utf-8')

    def test_reload_rejection_preserves_runtime_and_script(self):
        script = self.root / 'scripts' / f"{self.installation['script_id']}.js"
        before_script = script.read_bytes()
        self.client.api.side_effect = [{'proxies': {}}, f.ControllerError(400)]
        with self.assertRaises(f.ControllerError):
            f.apply_rules(self.root, self.installation, self.policy, {'NO'}, self.client, self.before)
        self.assertEqual((self.root / 'config.yaml').read_text(encoding='utf-8'), self.before)
        self.assertEqual(script.read_bytes(), before_script)
        self.assertEqual(self.client.api.call_count, 2)

    def test_success_changes_only_groups_and_persists_script(self):
        self.assertTrue(f.apply_rules(self.root, self.installation, self.policy, {'NO'}, self.client, self.before))
        result = f.load_yaml((self.root / 'config.yaml').read_text(encoding='utf-8'))
        self.assertEqual(result, h.transform(self.runtime, {'NO'}, self.policy))
        self.assertEqual(self.client.api.call_args.args[:2], ('/configs?force=false', 'PUT'))
        self.assertIn('NO', (self.root / 'scripts' / f"{self.installation['script_id']}.js").read_text(encoding='utf-8'))

    def test_concurrent_profile_switch_prevents_reload(self):
        self.config['currentProfileId'] = 102
        self.write_config(self.config)
        self.assertFalse(f.apply_rules(self.root, self.installation, self.policy, {'NO'}, self.client, self.before))
        self.client.api.assert_not_called()

    def test_timeout_attempts_rollback(self):
        self.client.api.side_effect = [{'proxies': {}}, TimeoutError(), {}]
        with self.assertRaises(TimeoutError):
            f.apply_rules(self.root, self.installation, self.policy, {'NO'}, self.client, self.before)
        self.assertEqual(self.client.api.call_args.args[2], {'payload': self.before})

    def test_manual_selection_is_restored_after_reload(self):
        self.client.api.side_effect = [{'proxies': {
            'Manual / 手选': {'type': 'Selector', 'now': 'NO'},
            'Auto': {'type': 'URLTest', 'now': 'NO'}}}, {}, {'proxies': {
            'Manual / 手选': {'type': 'Selector', 'now': '节点 (A)', 'all': ['NO', '节点 (A)']},
            'Auto': {'type': 'URLTest', 'now': '节点 (A)', 'fixed': '', 'all': ['节点 (A)']}}}, {}]
        self.assertTrue(f.apply_rules(self.root, self.installation, self.policy, {'NO'}, self.client, self.before))
        self.assertEqual(self.client.api.call_args.args,
            ('/proxies/Manual%20%2F%20%E6%89%8B%E9%80%89', 'PUT', {'name': 'NO'}))

    def test_selection_restore_failure_rolls_back_accepted_config(self):
        reset = {'proxies': {'Manual': {'type': 'Selector', 'now': '节点 (A)', 'all': ['NO', '节点 (A)']}}}
        self.client.api.side_effect = [{'proxies': {'Manual': {'type': 'Selector', 'now': 'NO'}}},
                                       {}, reset, f.ControllerError(400), {}, reset, {}]
        with self.assertRaises(f.ControllerError):
            f.apply_rules(self.root, self.installation, self.policy, {'NO'}, self.client, self.before)
        self.assertEqual(self.client.api.call_args_list[4].args[2], {'payload': self.before})
        self.assertEqual((self.root / 'config.yaml').read_text(encoding='utf-8'), self.before)

    def test_runtime_fixed_is_restored_but_automatic_winner_is_not_pinned(self):
        self.client.api.side_effect = [{'proxies': {
            'Auto': {'type': 'URLTest', 'fixed': '节点 (A)', 'now': 'NO'},
            'Unpinned': {'type': 'URLTest', 'fixed': '', 'now': 'NO'},
            'Fallback': {'type': 'Fallback', 'fixed': '节点 (A)', 'now': 'NO'}}},
            {}, {'proxies': {
                name: {'type': kind, 'fixed': '', 'now': 'NO', 'all': ['节点 (A)', 'NO']}
                for name, kind in [('Auto', 'URLTest'), ('Unpinned', 'URLTest'), ('Fallback', 'Fallback')]}},
            {}, {}]
        self.assertTrue(f.apply_rules(self.root, self.installation, self.policy, {'NO'}, self.client, self.before))
        writes = [c.args for c in self.client.api.call_args_list if len(c.args) > 1 and c.args[0].startswith('/proxies/')]
        self.assertEqual(writes, [('/proxies/Auto', 'PUT', {'name': '节点 (A)'}),
                                  ('/proxies/Fallback', 'PUT', {'name': '节点 (A)'})])

    def test_durable_preference_survives_exclusion_and_returns_after_recovery(self):
        self.save_selections({'Auto': 'NO'})
        start = {'proxies': {'Auto': {'type': 'URLTest', 'fixed': 'NO', 'now': 'NO',
                                      'all': ['节点 (A)', 'NO']}}}
        excluded = {'proxies': {'Auto': {'type': 'URLTest', 'fixed': '', 'now': '节点 (A)',
                                         'all': ['节点 (A)']}}}
        recovered = {'proxies': {'Auto': {'type': 'URLTest', 'fixed': '', 'now': '节点 (A)',
                                          'all': ['节点 (A)', 'NO']}}}
        self.client.api.side_effect = [start, {}, excluded]
        self.assertTrue(f.apply_rules(self.root, self.installation, self.policy, {'NO'}, self.client, self.before))
        self.assertEqual(f.profile_selections(self.root, 101), {'Auto': 'NO'})
        self.assertFalse(any(c.args[0] == '/proxies/Auto' for c in self.client.api.call_args_list))
        before = (self.root / 'config.yaml').read_text(encoding='utf-8')
        self.client.api.reset_mock()
        self.client.api.side_effect = [excluded]
        self.assertTrue(f.apply_rules(self.root, self.installation, self.policy, {'NO'}, self.client, before))
        self.client.api.assert_called_once_with('/proxies')
        self.client.api.side_effect = [excluded, {}, recovered, {}]
        self.assertTrue(f.apply_rules(self.root, self.installation, self.policy, set(), self.client, before))
        self.assertEqual(self.client.api.call_args.args, ('/proxies/Auto', 'PUT', {'name': 'NO'}))
        self.assertEqual(f.profile_selections(self.root, 101), {'Auto': 'NO'})

    def test_lost_pin_is_repaired_without_reload(self):
        self.save_selections({'Auto': 'NO'})
        before = f.patch_groups(self.before, h.transform(self.runtime, set(), self.policy)['proxy-groups'])
        h.atomic_write(self.root / 'config.yaml', before)
        self.client.api.side_effect = [{'proxies': {
            'Auto': {'type': 'URLTest', 'fixed': '', 'now': '节点 (A)', 'all': ['节点 (A)', 'NO']}}}, {}]
        self.assertTrue(f.apply_rules(self.root, self.installation, self.policy, set(), self.client, before))
        self.assertEqual(self.client.api.call_args.args, ('/proxies/Auto', 'PUT', {'name': 'NO'}))
        self.assertFalse(any(c.args[0].startswith('/configs') for c in self.client.api.call_args_list))

    def test_user_unpin_does_not_restore_stale_runtime_pin(self):
        self.save_selections({'Auto': ''})
        self.client.api.side_effect = [{'proxies': {
            'Auto': {'type': 'URLTest', 'fixed': '节点 (A)', 'now': '节点 (A)'}}},
            {}, {'proxies': {
                'Auto': {'type': 'URLTest', 'fixed': '', 'now': '节点 (A)', 'all': ['节点 (A)']}}}]
        self.assertTrue(f.apply_rules(self.root, self.installation, self.policy, {'NO'}, self.client, self.before))
        self.assertFalse(any(c.args[0] == '/proxies/Auto' for c in self.client.api.call_args_list))

    def test_unpin_during_reload_is_respected(self):
        self.save_selections({'Auto': '节点 (A)'})
        def api(path, method='GET', body=None, **kwargs):
            if path.startswith('/configs'):
                self.save_selections({'Auto': ''})
                return {}
            self.assertEqual(path, '/proxies')
            return {'proxies': {'Auto': {'type': 'URLTest', 'fixed': '',
                                        'now': '节点 (A)', 'all': ['节点 (A)']}}}
        self.client.api.side_effect = api
        self.assertTrue(f.apply_rules(self.root, self.installation, self.policy, {'NO'}, self.client, self.before))

    def test_restore_skips_removed_member_or_changed_group_type(self):
        self.client.api.return_value = {'proxies': {
            'Auto': {'type': 'Selector', 'now': 'NO', 'all': ['NO', '节点 (A)']},
            'Manual': {'type': 'Selector', 'now': '节点 (A)', 'all': ['节点 (A)']}}}
        f.restore_selections(self.client, {'Auto': ('URLTest', 'NO'), 'Manual': ('Selector', 'NO'),
                                          'Removed': ('Fallback', 'NO')})
        self.client.api.assert_called_once_with('/proxies')

    def test_unpin_without_reload_uses_delete(self):
        self.save_selections({'Auto': ''})
        before = f.patch_groups(self.before, h.transform(self.runtime, set(), self.policy)['proxy-groups'])
        h.atomic_write(self.root / 'config.yaml', before)
        self.client.api.side_effect = [{'proxies': {
            'Auto': {'type': 'URLTest', 'fixed': 'NO', 'now': 'NO', 'all': ['节点 (A)', 'NO']}}}, {}]
        self.assertTrue(f.apply_rules(self.root, self.installation, self.policy, set(), self.client, before))
        self.assertEqual(self.client.api.call_args.args, ('/proxies/Auto', 'DELETE'))

    def test_daemon_pause_and_cycle_status(self):
        self.config['currentProfileId'] = 102
        self.write_config(self.config)
        with mock.patch.object(f, 'Controller') as controller, mock.patch.object(h, 'event'):
            f.run_once(self.root)
            controller.assert_not_called()
        status = json.loads((self.home / 'status.json').read_text(encoding='utf-8'))
        self.assertEqual(status['event'], 'inactive_profile')
        self.config['currentProfileId'] = 101
        self.write_config(self.config)
        self.client.api.side_effect = [{'version': 'fixture'}, {'proxies': {
            'Auto': {'all': ['NO', '节点 (A)']}, 'Manual': {}, 'NO': {}, '节点 (A)': {}}}, {'proxies': {}}, {}]
        with mock.patch.object(f, 'Controller', return_value=self.client), mock.patch.object(h, 'event'):
            f.run_once(self.root)
        status = json.loads((self.home / 'status.json').read_text(encoding='utf-8'))
        self.assertTrue(status['rules_applied'])
        self.assertEqual(status['quarantined_count'], 0)

    def test_missing_owner_marker_waits_for_flclash_to_apply(self):
        del self.runtime[f.OWNER_KEY]
        h.atomic_write(self.root / 'config.yaml', f.yaml_module().safe_dump(self.runtime))
        with mock.patch.object(f, 'Controller') as controller, mock.patch.object(h, 'event'):
            f.run_once(self.root)
            controller.assert_not_called()
        status = json.loads((self.home / 'status.json').read_text(encoding='utf-8'))
        self.assertEqual(status['event'], 'awaiting_profile_apply')


class TransactionTests(FlClashFixture):
    def test_failed_task_rolls_back_database_files_and_new_task(self):
        installation, previous, files, expected = install.prepare(self.root)
        calls = []
        def task(action, *args, **kwargs):
            calls.append(action)
            if action == 'Register':
                raise RuntimeError('registration denied')
            return ''
        with mock.patch.object(install, 'app_is_running', return_value=False), mock.patch.object(install, 'run_task', side_effect=task):
            with self.assertRaisesRegex(RuntimeError, 'rolled back'):
                install.transaction(self.root, installation, files, expected,
                    lambda conn: install.install_binding(conn, installation, previous))
        for path, old in expected.items():
            self.assertEqual(path.read_bytes() if path.exists() else None, old)
        self.assertEqual(f.profile_metadata(self.root, 101)['extend_type'], 'standard')
        with f.database(self.root) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM scripts').fetchone()[0], 0)
        self.assertEqual(calls, ['Export', 'Register', 'Remove'])

    def test_successful_install_and_uninstall_restores_controller_and_binding(self):
        installation, previous, files, expected = install.prepare(self.root)
        task_state = {'xml': ''}
        def task(action, *args, **kwargs):
            if action == 'Export':
                return task_state['xml']
            task_state['xml'] = '<Task/>' if action in ('Register', 'Restore') else ''
            return ''
        with mock.patch.object(install, 'app_is_running', return_value=False), mock.patch.object(install, 'run_task', side_effect=task):
            install.transaction(self.root, installation, files, expected,
                lambda conn: install.install_binding(conn, installation, previous))
            self.assertTrue(f.binding_active(self.root, installation))
            with contextlib.redirect_stdout(None):
                install.uninstall(self.root)
        self.assertEqual(f.profile_metadata(self.root, 101)['extend_type'], 'standard')
        self.assertEqual(f.read_preferences(self.root)[2], self.config)
        self.assertFalse((self.root / 'scripts' / f"{installation['script_id']}.js").exists())
        self.assertEqual(task_state['xml'], '')

    def test_concurrent_file_edit_is_not_overwritten(self):
        installation, previous, files, expected = install.prepare(self.root)
        new = b'{"changed": true}'
        (self.root / 'shared_preferences.json').write_bytes(new)
        with mock.patch.object(install, 'app_is_running', return_value=False), mock.patch.object(install, 'run_task', return_value=''):
            with self.assertRaisesRegex(RuntimeError, 'rolled back'):
                install.transaction(self.root, installation, files, expected,
                    lambda conn: install.install_binding(conn, installation, previous))
        self.assertEqual((self.root / 'shared_preferences.json').read_bytes(), new)
        self.assertEqual(f.profile_metadata(self.root, 101)['extend_type'], 'standard')

    def test_uninstall_preserves_later_user_settings_and_binding(self):
        installation = self.install_fixture()
        _, _, config = f.read_preferences(self.root)
        config['patchClashConfig']['secret'] = 'changed-by-user'
        self.write_config(config)
        with f.database(self.root, writable=True) as conn:
            conn.execute("UPDATE profiles SET extend_type='standard',script_id=NULL WHERE id=101")
        with mock.patch.object(install, 'app_is_running', return_value=False), mock.patch.object(install, 'run_task', return_value=''), contextlib.redirect_stdout(None):
            install.uninstall(self.root)
        self.assertEqual(f.read_preferences(self.root)[2]['patchClashConfig']['secret'], 'changed-by-user')
        self.assertEqual(f.profile_metadata(self.root, 101)['extend_type'], 'standard')

    def test_existing_task_is_restored_after_registration_failure(self):
        installation, previous, files, expected = install.prepare(self.root)
        calls = []
        def task(action, *args, **kwargs):
            calls.append(action)
            if action == 'Export':
                return '<Task>previous</Task>'
            if action == 'Register':
                raise RuntimeError('registration denied')
            if action == 'Restore':
                self.assertEqual(Path(kwargs['XmlFile']).read_text(encoding='utf-8'), '<Task>previous</Task>')
            return ''
        with mock.patch.object(install, 'app_is_running', return_value=False), mock.patch.object(install, 'run_task', side_effect=task):
            with self.assertRaisesRegex(RuntimeError, 'rolled back'):
                install.transaction(self.root, installation, files, expected,
                    lambda conn: install.install_binding(conn, installation, previous))
        self.assertEqual(calls, ['Export', 'Register', 'Restore'])
        self.assertEqual(f.profile_metadata(self.root, 101)['script_id'], None)


if __name__ == '__main__':
    unittest.main()
