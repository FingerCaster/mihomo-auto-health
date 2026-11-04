"""Optional real FlClashCore check: set FLCLASH_TEST_CORE to its executable.

Uses a temporary profile, no TUN/system proxy and loopback listeners only.
The bridge speaks the v0.9.2 length-prefixed IPC protocol solely in tests.
"""
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import unittest

import auto_health as h
import flclash_health as f


BRIDGE = r'''
const net = require('node:net'), cp = require('node:child_process');
const input = JSON.parse(process.argv[1]);
const address = '\\\\.\\pipe\\auto-health-test-' + process.pid;
let core, socket, pending = Buffer.alloc(0), serial = 0;
const calls = new Map();
function rpc(method, arguments) {
  const id = String(++serial);
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('RPC timeout: ' + method)), 10000);
    calls.set(id, value => { clearTimeout(timeout); value.error ? reject(new Error(JSON.stringify(value.error))) : resolve(value.result); });
    const data = Buffer.from(JSON.stringify({id, method, arguments}));
    const header = Buffer.alloc(4); header.writeUInt32LE(data.length);
    socket.write(Buffer.concat([header, data]));
  });
}
const server = net.createServer(async connection => {
  socket = connection;
  connection.on('data', data => {
    pending = Buffer.concat([pending, data]);
    while (pending.length >= 4 && pending.length >= 4 + pending.readUInt32LE(0)) {
      const size = pending.readUInt32LE(0);
      const message = JSON.parse(pending.subarray(4, size + 4).toString('utf8'));
      pending = pending.subarray(size + 4);
      if (calls.has(message.id)) { calls.get(message.id)(message); calls.delete(message.id); }
    }
  });
  try {
    if (!await rpc('initClash', {'home-dir': input.home, version: 1})) throw new Error('init failed');
    const result = await rpc('setupConfig', {'selected-map': {}, 'test-url': input.testUrl});
    if (result) throw new Error(result);
    process.stdout.write('READY\n');
  } catch (error) { process.stderr.write(String(error)); cleanup(1); }
});
function cleanup(code = 0) { if (core) core.kill(); if (socket) socket.destroy(); server.close(); process.exit(code); }
server.listen(address, () => {
  core = cp.spawn(input.core, [address], {windowsHide: true, stdio: ['ignore', 'ignore', 'pipe']});
  core.stderr.on('data', data => process.stderr.write(data));
  core.on('error', error => { process.stderr.write(String(error)); cleanup(1); });
  core.on('exit', code => cleanup(code || 0));
});
process.stdin.resume();
process.stdin.on('data', () => cleanup());
process.stdin.on('end', () => cleanup());
setTimeout(() => cleanup(2), 40000).unref();
'''


@unittest.skipUnless(os.name == 'nt' and os.environ.get('FLCLASH_TEST_CORE'),
                     'Set FLCLASH_TEST_CORE for the optional real Windows core test')
class RealFlClashCoreTest(unittest.TestCase):
    def test_reload_excludes_recovers_and_keeps_manual_and_preferred_choices(self):
        with tempfile.TemporaryDirectory(prefix='flclash-core-test-') as directory:
            root = Path(directory)
            with socket.socket() as reserved:
                reserved.bind(('127.0.0.1', 0))
                port = reserved.getsockname()[1]
            with socket.socket() as reserved:
                reserved.bind(('127.0.0.1', 0))
                mixed_port = reserved.getsockname()[1]
            config = {'external-controller': '127.0.0.1:' + str(port), 'secret': 'temporary-test-secret',
                'mixed-port': mixed_port, 'bind-address': '127.0.0.1', 'allow-lan': False, 'tun': {'enable': False},
                'dns': {'enable': False}, 'profile': {'store-selected': False},
                'proxies': [{'name': 'Node A', 'type': 'direct'}, {'name': 'Node B', 'type': 'direct'}],
                'proxy-groups': [
                    {'name': 'Auto', 'type': 'url-test', 'proxies': ['Node A', 'Node B'],
                     'url': 'http://127.0.0.1:1/test', 'lazy': True},
                    {'name': 'Manual', 'type': 'select', 'proxies': ['Node A', 'Node B']}],
                'rules': ['MATCH,DIRECT'], f.OWNER_KEY: 'test'}
            (root / 'config.yaml').write_text(f.yaml_module().safe_dump(config), encoding='utf-8')
            process = subprocess.Popen([h.node_binary(), '-e', BRIDGE, json.dumps({
                'core': str(Path(os.environ['FLCLASH_TEST_CORE']).resolve()), 'home': str(root),
                'testUrl': 'http://127.0.0.1:1/test'})],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', **h.child_options())
            try:
                ready = process.stdout.readline()
                if ready.strip() != 'READY':
                    _, errors = process.communicate(timeout=10)
                    self.fail('Core did not initialize: ' + errors[-2000:])
                controller = f.Controller(config['external-controller'], config['secret'])
                self.assertIn('version', controller.api('/version'))
                policy = dict(h.DEFAULTS, profile_id=101, test_url='http://127.0.0.1:1/test')
                installation = dict(profile_id=101, script_id=201, owner='test')
                preferences = {'flutter.config': json.dumps({'currentProfileId': 101})}
                h.atomic_write(root / 'shared_preferences.json', json.dumps(preferences))
                with sqlite3.connect(root / 'database.sqlite', factory=f.ClosingConnection) as connection:
                    connection.executescript("CREATE TABLE profiles(id INTEGER, type TEXT, extend_type TEXT, script_id INTEGER, selected_map TEXT); "
                        """INSERT INTO profiles VALUES(101, 'file', 'script', 201, '{"Auto":"Node B"}');""")
                h.atomic_write(root / 'scripts/201.js', f.render_script([], policy, 'test'))
                controller.api('/proxies/Manual', 'PUT', {'name': 'Node B'})
                controller.api('/proxies/Auto', 'PUT', {'name': 'Node B'})
                self.assertEqual(controller.api('/proxies')['proxies']['Auto']['fixed'], 'Node B')
                before = (root / 'config.yaml').read_text(encoding='utf-8')
                self.assertTrue(f.apply_rules(root, installation, policy, {'Node A'}, controller, before))
                live = controller.api('/proxies')['proxies']
                self.assertEqual(live['Auto']['all'], ['Node B'])
                self.assertEqual(live['Manual']['all'], ['Node A', 'Node B'])
                self.assertEqual(live['Manual']['now'], 'Node B')
                self.assertEqual(live['Auto']['fixed'], 'Node B')
                before = (root / 'config.yaml').read_text(encoding='utf-8')
                self.assertTrue(f.apply_rules(root, installation, policy, {'Node A', 'Node B'}, controller, before))
                self.assertEqual(controller.api('/proxies')['proxies']['Auto']['all'], ['REJECT'])
                self.assertEqual(f.profile_selections(root, 101), {'Auto': 'Node B'})
                before = (root / 'config.yaml').read_text(encoding='utf-8')
                self.assertTrue(f.apply_rules(root, installation, policy, set(), controller, before))
                self.assertEqual(controller.api('/proxies')['proxies']['Auto']['all'], ['Node A', 'Node B'])
                self.assertEqual(controller.api('/proxies')['proxies']['Manual']['now'], 'Node B')
                self.assertEqual(controller.api('/proxies')['proxies']['Auto']['fixed'], 'Node B')
                # Clearing in FlClash must not resurrect an earlier pin.
                with f.database(root, writable=True) as connection:
                    connection.execute("UPDATE profiles SET selected_map=? WHERE id=101", ('{"Auto":""}',))
                before = (root / 'config.yaml').read_text(encoding='utf-8')
                self.assertTrue(f.apply_rules(root, installation, policy, set(), controller, before))
                self.assertEqual(controller.api('/proxies')['proxies']['Auto']['fixed'], '')
                with socket.socket() as check:
                    check.settimeout(1)
                    self.assertNotEqual(check.connect_ex(('127.0.0.1', mixed_port)), 0,
                                        'Background reload unexpectedly started a stopped listener')
                with self.assertRaises(f.ControllerError):
                    controller.api('/configs', 'PUT', {'payload': 'proxy-groups: broken'})
                self.assertEqual(controller.api('/proxies')['proxies']['Auto']['all'], ['Node A', 'Node B'])
            finally:
                try:
                    process.communicate('stop\n', timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
