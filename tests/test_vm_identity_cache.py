import threading
import unittest
from unittest.mock import MagicMock, patch

from modules import status_cache


class VmCacheIdentityTests(unittest.TestCase):
    def setUp(self):
        status_cache._vm_cache.clear()
        status_cache._cache_invalidated_at.clear()
        status_cache.set_on_vm_update(None)

    def tearDown(self):
        status_cache._vm_cache.clear()
        status_cache._cache_invalidated_at.clear()
        status_cache.set_on_vm_update(None)

    def test_status_event_is_provider_scoped_and_outside_lock(self):
        events = []
        def observe(data):
            self.assertTrue(status_cache._cache_lock.acquire(blocking=False))
            status_cache._cache_lock.release()
            events.append(data)
        status_cache.set_on_vm_update(observe)
        status_cache.update_vm_status(7, 101, 'running', node='a')
        status_cache.update_vm_status(8, 101, 'stopped', node='a')
        self.assertEqual(['pve-7-vm-101', 'pve-8-vm-101'], [e['key'] for e in events])
        self.assertEqual([7, 8], [e['pve_server_id'] for e in events])
        self.assertEqual('running', status_cache.get_vm_status(7, 101)['status'])
        self.assertEqual('stopped', status_cache.get_vm_status(8, 101)['status'])

    def test_refresh_does_not_clobber_newer_action_or_revive_deleted_vm(self):
        queried = threading.Event()
        resume = threading.Event()
        provider = MagicMock()
        def old_status(node, vmid):
            queried.set()
            self.assertTrue(resume.wait(2))
            return {'status': 'stopped'}
        provider.get_vm_status.side_effect = old_status
        clusters = {'a': {'pve_server_id': 7,
                          'vms': {'client': {'pve_server_id': 7, 'vmid': 101, 'node': 'a'}}}}
        with (patch('modules.db.load_clusters', return_value=clusters),
              patch.object(status_cache, '_get_pve_server_configs', return_value={7: {}}),
              patch.object(status_cache, '_build_clients', return_value={7: provider})):
            thread = threading.Thread(target=status_cache._refresh_cache)
            thread.start()
            self.assertTrue(queried.wait(2))
            status_cache.update_vm_status(7, 101, 'running', node='a')
            resume.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual('running', status_cache.get_vm_status(7, 101)['status'])
            queried.clear();resume.clear()
            thread = threading.Thread(target=status_cache._refresh_cache)
            thread.start()
            self.assertTrue(queried.wait(2))
            status_cache.remove_vm_status(7, 101)
            resume.set();thread.join(3)
            self.assertEqual('unknown', status_cache.get_vm_status(7, 101)['status'])


if __name__ == '__main__':
    unittest.main()
