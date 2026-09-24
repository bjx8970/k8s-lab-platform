import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = tuple(ROOT / "templates" / name for name in ("index.html", "k8s.html", "student.html"))


class VmIdentityFrontendTests(unittest.TestCase):
    def test_templates_use_shared_identity_helper_for_all_vm_paths(self):
        for path in TEMPLATES:
            source = path.read_text(encoding="utf-8")
            with self.subTest(template=path.name):
                self.assertIn('/static/js/vm_identity.js', source)
                self.assertIn('VMIdentity.renderTag(', source)
                self.assertIn('VMIdentity.actionUrl(', source)
                self.assertIn('VMIdentity.refreshStatuses(', source)
                self.assertIn("stateSocket.on('vm_status_update'", source)
                self.assertNotIn('node + \'_\' + vmid', source)
                self.assertNotIn('|| c.pve_server_id', source)
                self.assertNotIn('?node=', source)

    def test_single_and_cluster_votes_send_provider_identity(self):
        for name in ("k8s.html", "student.html"):
            source = (ROOT / "templates" / name).read_text(encoding="utf-8")
            with self.subTest(template=name):
                marker = "stateSocket.emit('vm_shutdown_initiate'"
                vote_block = source[source.index(marker):source.index(marker) + 280]
                self.assertIn('pve_server_id: serverId', vote_block)
                self.assertIn('VMIdentity.clusterPayload(currentClusters,', source)

    def test_javascript_identity_and_dom_updates_execute_without_collision(self):
        script = r"""
const assert = require('node:assert/strict');
const VM = require('./static/js/vm_identity.js');
assert.equal(VM.actionUrl(7, 101, 'start'), '/api/pve/servers/7/vms/101/start');
for (const id of [0, null, undefined, true, '01']) {
    assert.throws(() => VM.identity({pve_server_id:id, vmid:101}));
}
let tags = {
  'pve-7-vm-101': {classList:{remove(){},add(v){this.last=v}},querySelector(){return {textContent:''}}},
  'pve-8-vm-101': {classList:{remove(){},add(v){this.last=v}},querySelector(){return {textContent:''}}}
};
let root = {querySelectorAll(selector){
  const match = selector.match(/data-vm-key="([^"]+)"/);
  return match && tags[match[1]] ? [tags[match[1]]] : [];
}};
let cache = {};
VM.updateStatus({pve_server_id:7,vmid:101,status:'running',key:'pve-7-vm-101'},cache,root);
assert.equal(tags['pve-7-vm-101'].classList.last, 'vm-running');
assert.equal(tags['pve-8-vm-101'].classList.last, undefined);
assert.throws(() => VM.updateStatus({pve_server_id:8,vmid:101,status:'stopped',key:'pve-7-vm-101'},cache,root));
assert.equal(tags['pve-8-vm-101'].classList.last, undefined);
"""
        result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
