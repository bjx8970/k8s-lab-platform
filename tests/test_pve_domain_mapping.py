import unittest

from modules.pve_domain_mapping import DomainMappingError, plan_pve_vm_mapping


class PveDomainMappingTests(unittest.TestCase):
    def setUp(self):
        self.bindings = {
            7: {"domain_id": "pve-domain-a", "connection_id": "connection-a"},
            8: {"domain_id": "pve-domain-a", "connection_id": "connection-b"},
            9: {"domain_id": "pve-domain-b", "connection_id": "connection-c"},
        }
        self.vm = {"pve_server_id": 7, "vmid": 101, "node": "node-a",
                   "cluster_id": 1, "resource_id": "resource-a"}

    def test_many_connections_share_one_domain_vm_resource(self):
        result = plan_pve_vm_mapping(self.bindings, [
            self.vm,
            {**self.vm, "pve_server_id": 8, "node": "node-b"},
            {**self.vm, "pve_server_id": 9, "cluster_id": 2,
             "resource_id": "resource-b", "node": "node-a"},
        ])
        self.assertEqual(2, len(result))
        self.assertEqual(("pve-domain-a", "pve.qemu/v1", "101"),
                         (result[0]["domain_id"], result[0]["driver_id"], result[0]["external_key"]))
        self.assertEqual(["connection-a", "connection-b"], result[0]["connections"])
        self.assertEqual([7, 8], result[0]["legacy_pve_server_ids"])
        self.assertEqual("resource-a", result[0]["resource_id"])
        self.assertEqual("pve-domain-b", result[1]["domain_id"])

    def test_ambiguous_domain_duplicate_must_be_resolved_manually(self):
        for changed in ({"resource_id": "other-resource"}, {"cluster_id": 2}):
            with self.subTest(changed=changed), self.assertRaisesRegex(DomainMappingError, "歧义"):
                plan_pve_vm_mapping(self.bindings, [
                    self.vm, {**self.vm, "pve_server_id": 8, **changed},
                ])

    def test_missing_mapping_identity_and_reused_connection_fail_closed(self):
        for vm in ({**self.vm, "pve_server_id": 10},
                   {**self.vm, "resource_id": None},
                   {**self.vm, "pve_server_id": 0}):
            with self.subTest(vm=vm), self.assertRaises(DomainMappingError):
                plan_pve_vm_mapping(self.bindings, [vm])
        bindings = {7: self.bindings[7], 9: {"domain_id": "other", "connection_id": "connection-a"}}
        with self.assertRaises(DomainMappingError):
            plan_pve_vm_mapping(bindings, [self.vm])


if __name__ == "__main__":
    unittest.main()
