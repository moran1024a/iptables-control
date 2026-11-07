"""Node contact addresses, explicit forwarding bindings and sync-only discovery."""
import subprocess
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_server import Fixture, ROOT
import agent_v3
import api
import database
import errors
import node_addresses
import node_sync
import settings


class AddressTests(Fixture, unittest.TestCase):
    def addresses(self, nid):
        return node_addresses.listing(self.db, database.get_node(self.db, nid))

    def inventory(self, nid, *pairs):
        with self.db:
            agent_v3.save_snapshot(self.db, nid, 'inventory', dict(network_complete=True,
                interfaces=[dict(name=device, addresses=[ip]) for device, ip in pairs]))

    def test_minimal_node_and_contact_change_keep_identity(self):
        nid = self.node()
        before = dict(database.get_node(self.db, nid))
        address = self.addresses(nid)[0]
        self.assertTrue(address['primary'])
        self.assertEqual(address['host'], before['addr'])
        self.assertFalse(node_addresses.ready(address))
        self.call('api_node_edit', dict(addr='192.0.2.9'), nid)
        after = database.get_node(self.db, nid)
        self.assertEqual(after['key'], before['key'])
        self.assertEqual(self.addresses(nid)[0]['id'], address['id'])
        self.assertEqual(self.addresses(nid)[0]['host'], '192.0.2.9')

    def test_inventory_only_resolves_exact_unambiguous_local_addresses(self):
        nid = self.node()
        self.inventory(nid, ('lo', '127.0.0.1/8'), ('eth0', '192.0.2.1/24'), ('eth1', '10.0.0.1/24'), ('eth1', '2001:db8::1/64'))
        self.assertEqual(self.addresses(nid)[0]['device'], 'eth0')
        self.assertEqual(len(self.addresses(nid)), 1)  # Discovered addresses are candidates only.
        self.assertEqual(len(node_addresses.network(self.db, nid)['addresses']), 2)
        self.inventory(nid, ('ens3', '192.0.2.1/24'))
        self.assertEqual(self.addresses(nid)[0]['device'], 'eth0')  # Never silently rebind.
        values = self.addresses(nid) + [dict(id='secondary', host='10.0.0.2')]
        self.call('api_node_edit', dict(addresses=values), nid)
        self.inventory(nid, ('eth1', '10.0.0.2/24'), ('eth2', '10.0.0.2/24'))
        self.assertEqual(self.addresses(nid)[1]['device'], '')
        values = self.addresses(nid)
        values[1]['device'] = 'eth2'
        self.call('api_node_edit', dict(addresses=values), nid)
        self.assertTrue(node_addresses.ready(self.addresses(nid)[1]))

    def test_nat_and_domain_need_local_mapping_not_source_guess(self):
        nid = self.call('api_node_add', dict(name='NAT', addr='nat.example.com'))['id']
        self.inventory(nid, ('ens3', '10.0.0.2/24'))
        address = self.addresses(nid)[0]
        self.assertFalse(node_addresses.ready(address))
        address['local'] = '10.0.0.2'
        self.call('api_node_edit', dict(addresses=[address]), nid)
        self.assertEqual(self.addresses(nid)[0]['device'], 'ens3')
        self.assertEqual(database.get_node(self.db, nid)['addr'], 'nat.example.com')
        values = self.addresses(nid)
        values[0]['primary'] = False
        values.append(dict(id='local', primary=True, host='10.0.0.2'))
        self.call('api_node_edit', dict(addresses=values), nid)
        self.assertEqual(self.addresses(nid)[0]['host'], 'nat.example.com')
        self.assertEqual(database.get_node(self.db, nid)['addr'], '10.0.0.2')

    def test_rc1_migration_keeps_addresses_ids_and_does_not_rekey(self):
        nid = self.node()
        old_key = database.get_node(self.db, nid)['key']
        old = [dict(id='public', name='公网', host='192.0.2.1', local='10.0.0.1', device='eth0', gateway='10.0.0.254'),
               dict(id='private', name='内网', host='10.1.0.1', local='10.1.0.1', device='eth1', gateway='')]
        with self.db:
            node_addresses.write(self.db, nid, old)
            self.db.execute('PRAGMA user_version=503')
        node_addresses.migrate_primary(self.db)
        migrated = self.addresses(nid)
        self.assertEqual([{k: v for k, v in a.items() if k != 'primary'} for a in migrated], old)
        self.assertEqual([a['primary'] for a in migrated], [True, False])
        self.assertEqual(database.get_node(self.db, nid)['key'], old_key)
        node_addresses.migrate_primary(self.db)
        self.assertEqual(self.addresses(nid), migrated)

    def test_passive_manual_and_manager_sync_both_discover_network(self):
        nid = self.node()
        self.call('api_node_edit', dict(desired_mode='passive'), nid)
        fields = dict(t=str(int(time.time())), proto='2', source='manual', phase='begin', state='ok',
                      mode='passive', agent=settings.VERSION, op='a'*32, network_complete='1',
                      network_addresses='eth0,192.0.2.1/24;eth1,10.0.0.1/24')
        with self.db:
            node_sync.sync_report(self.db, database.get_node(self.db, nid), fields, '192.0.2.1')
        self.assertEqual(self.addresses(nid)[0]['device'], 'eth0')
        other = self.node('manager')
        artifact = Path(self.temp.name) / 'asset'
        artifact.write_text('test')
        with patch.object(agent_v3, 'artifact', return_value=(artifact, 'f'*64)), self.db:
            agent_v3.manage(self.db, database.get_node(self.db, other), dict(t=str(int(time.time())),
                manage='prepare', target='active', arch='amd64', network_complete='1', network_addresses='ens3,192.0.2.1/24'))
        self.assertEqual(self.addresses(other)[0]['device'], 'ens3')

    def test_primary_is_unique_and_mismatch_rolls_back(self):
        nid = self.node()
        original = self.addresses(nid)
        for values in ([dict(original[0], primary=False)], [original[0], dict(id='other', primary=True, host='192.0.2.2')]):
            with self.assertRaises(errors.UserError):
                self.call('api_node_edit', dict(addresses=values), nid)
            self.assertEqual(self.addresses(nid), original)
        with self.assertRaises(errors.UserError):
            self.call('api_node_edit', dict(addr='192.0.2.2', addresses=original), nid)
        self.assertEqual(database.get_node(self.db, nid)['addr'], '192.0.2.1')

    def test_incomplete_inventory_does_not_guess_unique_device(self):
        nid = self.node()
        with self.db:
            node_addresses.receive_network(self.db, nid, dict(network_complete='0', network_addresses='eth0,192.0.2.1/24'))
        self.assertFalse(node_addresses.ready(self.addresses(nid)[0]))
        self.assertFalse(node_addresses.network(self.db, nid)['complete'])

    def test_proxy_source_is_shared_and_validated(self):
        self.assertEqual(api.source_address('127.0.0.1', 'garbage, 8.8.8.8'), '8.8.8.8')
        self.assertEqual(api.source_address('8.8.4.4', '8.8.8.8'), '8.8.4.4')
        self.assertEqual(api.source_address('127.0.0.1', 'invalid'), '127.0.0.1')

    def test_bash_discovery_is_bounded_and_collectors_agree(self):
        output = []
        for script in ('agent/manage.sh', 'agent/passive.sh'):
            run = subprocess.run(['bash', '-c', '''
ICTL_LIBRARY=1 source "$1"
ip() {
 printf '1: lo inet 127.0.0.1/8 scope host lo\n'
 printf '2: eth0@if3 inet 192.0.2.1/24 scope global eth0\n'
 for ((i=1;i<=110;i++)); do printf '3: eth1 inet 10.0.0.%s/24 scope global eth1\n' "$i"; done
}
network_inventory
ip() { return 1; }
network_inventory
''', 'test', str(ROOT / script)], capture_output=True, text=True, check=True)
            fields = agent_v3.fields(run.stdout)
            self.assertEqual(fields['network_complete'], '0')
            self.assertEqual(len(fields['network_addresses'].split(';')), 96)
            self.assertTrue(fields['network_addresses'].startswith('eth0,192.0.2.1/24;'))
            output.append(run.stdout)
        self.assertEqual(*output)
