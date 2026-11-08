CREATE TABLE item_groups (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  builtin INTEGER NOT NULL DEFAULT 0,
  sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE settings (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);
CREATE TABLE sessions (
  token_hash TEXT PRIMARY KEY,
  expires INTEGER NOT NULL
);
CREATE TABLE nodes (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  addr TEXT NOT NULL,
  key TEXT NOT NULL UNIQUE,
  deleting INTEGER NOT NULL DEFAULT 0,
  first_seen INTEGER,
  last_seen INTEGER,
  src_ip TEXT,
  reported_v TEXT,
  state TEXT,
  err TEXT,
  agent TEXT,
  skew INTEGER,
  health TEXT,
  sort INTEGER NOT NULL DEFAULT 0,
  ports TEXT NOT NULL DEFAULT '10000-65535',
  group_id INTEGER NOT NULL DEFAULT 0,
  auto_update INTEGER,
  kind TEXT NOT NULL DEFAULT 'forward' CHECK(kind='forward'),
  desired_mode TEXT NOT NULL DEFAULT 'active' CHECK(desired_mode IN ('active','passive')),
  reported_mode TEXT,
  desired_generation INTEGER NOT NULL DEFAULT 1,
  applied_generation INTEGER NOT NULL DEFAULT 0,
  manual_required INTEGER NOT NULL DEFAULT 0,
  protocol_version INTEGER NOT NULL DEFAULT 2,
  last_manual INTEGER,
  last_success INTEGER,
  installed_agent TEXT NOT NULL DEFAULT 'unknown',
  backend TEXT NOT NULL DEFAULT 'iptables',
  installation_id TEXT,
  boot_id TEXT,
  capabilities TEXT NOT NULL DEFAULT '[]',
  connection_state TEXT NOT NULL DEFAULT 'unknown',
  quality_port INTEGER NOT NULL DEFAULT 33445,
  wake_enabled INTEGER NOT NULL DEFAULT 0,
  wake_port INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE targets (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
 host TEXT NOT NULL, port INTEGER NOT NULL, params TEXT NOT NULL DEFAULT '{}',
 group_id INTEGER NOT NULL, sort INTEGER NOT NULL DEFAULT 0,
 CHECK(type IN ('endpoint','shadowsocks','vless-reality')),
 CHECK(port BETWEEN 1 AND 65535)
);
CREATE TABLE windows (
  node_id INTEGER NOT NULL,
  win INTEGER NOT NULL,
  level INTEGER NOT NULL, manual_count INTEGER NOT NULL DEFAULT 0, last_activity INTEGER,
  PRIMARY KEY (node_id, win)
);
CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  node_id INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  text TEXT NOT NULL
);
CREATE TABLE retired_ports(node_id INTEGER NOT NULL,port INTEGER NOT NULL,PRIMARY KEY(node_id,port));
CREATE TABLE active_windows (
        node_id INTEGER NOT NULL, win INTEGER NOT NULL, level INTEGER NOT NULL,
        PRIMARY KEY(node_id, win));
CREATE TABLE sync_ops (
 node_id INTEGER PRIMARY KEY, op TEXT NOT NULL, source TEXT NOT NULL,
 generation INTEGER NOT NULL, mode TEXT NOT NULL, hash TEXT NOT NULL,
 rules TEXT NOT NULL, expires INTEGER NOT NULL, result TEXT
);
CREATE TABLE deleted_receipts (
 key TEXT PRIMARY KEY, op TEXT NOT NULL, generation INTEGER NOT NULL, mode TEXT NOT NULL,
 hash TEXT NOT NULL, expires INTEGER NOT NULL
);
CREATE TABLE node_snapshots (
 node_id INTEGER PRIMARY KEY, inventory TEXT NOT NULL DEFAULT '{}', metrics TEXT NOT NULL DEFAULT '{}',
 ports TEXT NOT NULL DEFAULT '{}', inventory_at INTEGER, metrics_at INTEGER, ports_at INTEGER
);
CREATE TABLE traffic_epochs (
 node_id INTEGER NOT NULL, rule_id INTEGER NOT NULL, protocol TEXT NOT NULL, epoch TEXT NOT NULL,
 up TEXT NOT NULL, down TEXT NOT NULL, up_packets TEXT NOT NULL, down_packets TEXT NOT NULL,
 updated INTEGER NOT NULL, quality TEXT NOT NULL, PRIMARY KEY(node_id,rule_id,protocol,epoch)
);
CREATE TABLE metric_windows (
 node_id INTEGER NOT NULL, window INTEGER NOT NULL, data TEXT NOT NULL,
 PRIMARY KEY(node_id,window)
);
CREATE TABLE metric_hours (
 node_id INTEGER NOT NULL, window INTEGER NOT NULL, data TEXT NOT NULL,
 PRIMARY KEY(node_id,window)
);
CREATE TABLE archived_nodes (
 node_id INTEGER PRIMARY KEY, name TEXT NOT NULL, deleted_at INTEGER NOT NULL
);
CREATE TABLE agent_switches (
 node_id INTEGER PRIMARY KEY, op TEXT NOT NULL, target TEXT NOT NULL, generation INTEGER NOT NULL,
 hash TEXT NOT NULL, config TEXT NOT NULL, result TEXT, updated INTEGER NOT NULL
);
CREATE TABLE quality_pairs (
 id INTEGER PRIMARY KEY, source INTEGER NOT NULL, target INTEGER NOT NULL,
 source_addr TEXT NOT NULL, target_addr TEXT NOT NULL, port INTEGER NOT NULL,
 protocol TEXT NOT NULL DEFAULT 'udp-echo-v1',
 UNIQUE(source,target,source_addr,target_addr,port,protocol)
);
CREATE TABLE wake_jobs (
            node_id INTEGER PRIMARY KEY, generation INTEGER NOT NULL, token TEXT NOT NULL,
            started INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'waiting', error TEXT NOT NULL DEFAULT '');
CREATE TABLE subscriptions (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, token TEXT NOT NULL UNIQUE,
 enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE probe_tasks (
 id TEXT PRIMARY KEY, identity TEXT NOT NULL
);
CREATE TABLE probe_samples (
 task_id TEXT NOT NULL REFERENCES probe_tasks(id), slot INTEGER NOT NULL,
 status TEXT NOT NULL, rtt REAL, error TEXT NOT NULL DEFAULT '',
 resolved_ip TEXT NOT NULL DEFAULT '', epoch TEXT, previous_slot INTEGER, PRIMARY KEY(task_id, slot)
);
CREATE TABLE probe_bindings (
 id INTEGER PRIMARY KEY, scope TEXT NOT NULL, owner_id INTEGER NOT NULL,
 section TEXT NOT NULL, protocol TEXT NOT NULL, task_id TEXT REFERENCES probe_tasks(id),
 path_key TEXT NOT NULL, path_label TEXT NOT NULL,
 status TEXT NOT NULL, note TEXT NOT NULL, started INTEGER NOT NULL, ended INTEGER
);
CREATE TABLE node_addresses(node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE, addresses TEXT NOT NULL);

CREATE TABLE topologies (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, group_id INTEGER NOT NULL REFERENCES item_groups(id),
 sort INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1, graph TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE configurations (
 id INTEGER PRIMARY KEY AUTOINCREMENT, topology_id INTEGER NOT NULL REFERENCES topologies(id),
 entry TEXT NOT NULL, target TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', desired TEXT,
 applied TEXT, state TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '',
 revision INTEGER NOT NULL DEFAULT 1, deleted INTEGER NOT NULL DEFAULT 0,
 UNIQUE(topology_id,entry,target)
);
CREATE TABLE jobs (
 configuration_id INTEGER PRIMARY KEY REFERENCES configurations(id) ON DELETE CASCADE,
 phase TEXT NOT NULL, payload TEXT NOT NULL, started INTEGER NOT NULL
);
CREATE TABLE rules (
 id INTEGER PRIMARY KEY, configuration_id INTEGER NOT NULL REFERENCES configurations(id) ON DELETE CASCADE,
 phase TEXT NOT NULL, node_id INTEGER NOT NULL REFERENCES nodes(id), port INTEGER NOT NULL,
 host TEXT NOT NULL, tport INTEGER NOT NULL, route TEXT NOT NULL DEFAULT '', UNIQUE(node_id,port)
);
CREATE TABLE retirements (
 configuration_id INTEGER NOT NULL REFERENCES configurations(id) ON DELETE CASCADE,
 node_id INTEGER NOT NULL REFERENCES nodes(id), port INTEGER NOT NULL,
 PRIMARY KEY(configuration_id,node_id,port)
);
CREATE TABLE configuration_events (
 id INTEGER PRIMARY KEY, configuration_id INTEGER NOT NULL REFERENCES configurations(id) ON DELETE CASCADE,
 ts INTEGER NOT NULL, message TEXT NOT NULL
);
CREATE TABLE subscription_configurations (
 subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
 configuration_id INTEGER NOT NULL REFERENCES configurations(id) ON DELETE CASCADE,
 PRIMARY KEY(subscription_id,configuration_id)
);
CREATE TABLE migration_nodes (node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE);
CREATE INDEX rules_node ON rules(node_id);
CREATE INDEX configurations_topology ON configurations(topology_id);
CREATE INDEX probe_sample_age ON probe_samples(slot);
CREATE UNIQUE INDEX probe_binding_current ON probe_bindings(scope,owner_id,section,protocol) WHERE ended IS NULL;
CREATE INDEX probe_binding_history ON probe_bindings(scope,owner_id,section,protocol,started);
