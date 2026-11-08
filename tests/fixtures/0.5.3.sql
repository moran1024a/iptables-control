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
  auto_update INTEGER
, kind TEXT NOT NULL DEFAULT 'forward' CHECK(kind='forward'), desired_mode TEXT NOT NULL DEFAULT 'active' CHECK(desired_mode IN ('active','passive')), reported_mode TEXT, desired_generation INTEGER NOT NULL DEFAULT 1, applied_generation INTEGER NOT NULL DEFAULT 0, manual_required INTEGER NOT NULL DEFAULT 0, protocol_version INTEGER NOT NULL DEFAULT 1, last_manual INTEGER, last_success INTEGER, installed_agent TEXT NOT NULL DEFAULT 'legacy', backend TEXT NOT NULL DEFAULT 'iptables', installation_id TEXT, boot_id TEXT, capabilities TEXT NOT NULL DEFAULT '[]', connection_state TEXT NOT NULL DEFAULT 'unknown', quality_port INTEGER NOT NULL DEFAULT 33445, wake_enabled INTEGER NOT NULL DEFAULT 0, wake_port INTEGER NOT NULL DEFAULT 0);
CREATE TABLE targets (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
 host TEXT NOT NULL, port INTEGER NOT NULL, params TEXT NOT NULL DEFAULT '{}',
 group_id INTEGER NOT NULL, sort INTEGER NOT NULL DEFAULT 0,
 CHECK(type IN ('endpoint','shadowsocks','vless-reality')),
 CHECK(port BETWEEN 1 AND 65535)
);
CREATE TABLE chains (
 id INTEGER PRIMARY KEY, remark TEXT NOT NULL, target_id INTEGER NOT NULL REFERENCES targets(id),
 route_id INTEGER REFERENCES routes(id), deleting INTEGER NOT NULL DEFAULT 0,
 sort INTEGER NOT NULL DEFAULT 0, group_id INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE hops (
  id INTEGER PRIMARY KEY,
  chain_id INTEGER NOT NULL REFERENCES chains(id),
  seq INTEGER NOT NULL,
  node_id INTEGER NOT NULL REFERENCES nodes(id),
  port INTEGER NOT NULL,
  UNIQUE (node_id, port)
);
CREATE TABLE chain_pending (
  chain_id INTEGER NOT NULL,
  node_id INTEGER NOT NULL,
  PRIMARY KEY (chain_id, node_id)
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
CREATE TABLE routes(id INTEGER PRIMARY KEY,name TEXT NOT NULL,node_ids TEXT NOT NULL,group_id INTEGER NOT NULL,sort INTEGER NOT NULL DEFAULT 0);
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
CREATE TABLE quality_samples (
 pair_id INTEGER NOT NULL, slot INTEGER NOT NULL, status TEXT NOT NULL,
 rtt REAL, error TEXT NOT NULL DEFAULT '', resolved_ip TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(pair_id,slot)
);
CREATE TABLE wake_jobs (
            node_id INTEGER PRIMARY KEY, generation INTEGER NOT NULL, token TEXT NOT NULL,
            started INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'waiting', error TEXT NOT NULL DEFAULT '');
CREATE TABLE pools(id INTEGER PRIMARY KEY, name TEXT NOT NULL, members TEXT NOT NULL DEFAULT '[]');
CREATE TABLE links(
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, chain_id INTEGER UNIQUE REFERENCES chains(id) ON DELETE SET NULL,
            plan TEXT NOT NULL, selected TEXT NOT NULL DEFAULT '{}', managed INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'draft', error TEXT NOT NULL DEFAULT '', job TEXT,
            failures TEXT NOT NULL DEFAULT '[]', retained TEXT NOT NULL DEFAULT '[]', revision INTEGER NOT NULL DEFAULT 1, group_id INTEGER NOT NULL DEFAULT 0, sort INTEGER NOT NULL DEFAULT 0, applied TEXT, failure_scope TEXT NOT NULL DEFAULT '', config_name TEXT NOT NULL DEFAULT '');
CREATE TABLE link_rules(
            id INTEGER PRIMARY KEY, link_id INTEGER NOT NULL REFERENCES links(id) ON DELETE CASCADE,
            phase TEXT NOT NULL, node_id INTEGER NOT NULL REFERENCES nodes(id), port INTEGER NOT NULL,
            host TEXT NOT NULL, tport INTEGER NOT NULL, UNIQUE(node_id,port));
CREATE TABLE link_retired(
            link_id INTEGER NOT NULL REFERENCES links(id) ON DELETE CASCADE,
            node_id INTEGER NOT NULL, port INTEGER NOT NULL, PRIMARY KEY(link_id,node_id,port));
CREATE TABLE link_events(
            id INTEGER PRIMARY KEY, link_id INTEGER NOT NULL REFERENCES links(id) ON DELETE CASCADE,
            ts INTEGER NOT NULL, message TEXT NOT NULL);
CREATE TABLE subscriptions (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, token TEXT NOT NULL UNIQUE,
 enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE subscription_links (
 subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
 link_id INTEGER NOT NULL REFERENCES links(id) ON DELETE CASCADE,
 PRIMARY KEY(subscription_id,link_id)
);
CREATE TABLE link_quality_samples (
 link_id INTEGER NOT NULL REFERENCES links(id) ON DELETE CASCADE,
 section TEXT NOT NULL, protocol TEXT NOT NULL, slot INTEGER NOT NULL,
 status TEXT NOT NULL, rtt REAL, error TEXT NOT NULL DEFAULT '',
 resolved_ip TEXT NOT NULL DEFAULT '', path_key TEXT NOT NULL, path_label TEXT NOT NULL,
 PRIMARY KEY(link_id,section,protocol,slot)
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
CREATE TABLE link_graphs(link_id INTEGER PRIMARY KEY REFERENCES links(id) ON DELETE CASCADE, draft TEXT, last_attempt INTEGER NOT NULL DEFAULT 0, blocked TEXT NOT NULL DEFAULT '{}');
CREATE TABLE node_addresses(node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE, addresses TEXT NOT NULL);
CREATE TABLE rule_routes(rule_id INTEGER PRIMARY KEY, route TEXT NOT NULL);
CREATE TABLE link_members(link_id INTEGER PRIMARY KEY REFERENCES links(id) ON DELETE CASCADE, root_id INTEGER NOT NULL REFERENCES links(id), entry TEXT NOT NULL, target TEXT NOT NULL, position INTEGER NOT NULL, UNIQUE(root_id,entry,target));
CREATE TABLE link_batches(root_id INTEGER PRIMARY KEY REFERENCES links(id) ON DELETE CASCADE, members TEXT NOT NULL, graph TEXT NOT NULL, path TEXT NOT NULL, removed TEXT NOT NULL DEFAULT '[]', phase TEXT NOT NULL DEFAULT 'prepare', kind TEXT NOT NULL DEFAULT 'publish');
CREATE TABLE topology_canvases(root_id INTEGER PRIMARY KEY REFERENCES links(id) ON DELETE CASCADE, graph TEXT NOT NULL, path TEXT NOT NULL, applied_graph TEXT, applied_path TEXT);
PRAGMA user_version=504;
