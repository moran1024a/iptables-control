// Shapes of the panel API, transcribed from the server contract. Timestamps are epoch seconds unless noted.
/* eslint-disable */

export interface ApiErrorBody {
  error: string;            // Chinese, user-presentable; show as is
  field?: string | null;    // present (possibly null) on every UserError; absent on 401/403/404-route/429/content-type errors
  must_change?: true;       // only on the 403 above
}

export type LoginBody = { username: string; password: string };
export type LoginOk = { must_change: boolean };          // + Set-Cookie

export type Me = { username: string; version: string; must_change: boolean };

export type AccountBody = {
  current_password: string;
  username: string;      // required every time; /^[A-Za-z0-9_.@-]{1,32}$/ (trimmed)
  password?: string;     // 8–128 chars; ""/absent = keep current, but REQUIRED while must_change
};
export type AccountOk = { username: string };

export type Epoch = number;                         // seconds
export type Group = { id: number; name: string; builtin: 0 | 1 };      // builtin group cannot be deleted
export type Tag = { id: number; name: string; color: string };        // color "#rrggbb", lower-cased by server

/** Node address ("IP") — stored JSON; also the value type of address_book for node vertices. */
export interface NodeAddress {
  id: string;          // /^[A-Za-z0-9_-]{1,48}$/, unique per node; auto-created primary is "main"
  primary: boolean;    // exactly one true. [R] rows imported from 0.5.x may lack it until the node is saved once
  name: string;        // label, "" if none (<=64)
  host: string;        // advertised address peers connect to; primary may be a domain, others IPv4
  local: string;       // IPv4 on the machine ("" if unknown, e.g. domain primary)
  device: string;      // NIC name, "" until resolved from inventory
  gateway: string;     // optional IPv4 or ""
}
/** Target endpoint; id "main" is synthesized from target.host/port and always first. */
export interface TargetAddress { id: string; name: string; host: string; port: number }   // main: name "默认"

export type NodeStatus = 'new' | 'offline' | 'skew' | 'syncing' | 'synced' | 'error' | 'deleting';
// new: active node never connected · offline: active, no report for >30 s · skew: clock off by >60 s
// syncing: generation/rules not yet confirmed, or waiting for manual sync / wake · error: port conflict,
// failed/timed-out wake, runtime rule fault · deleting: waiting for the agent to clear rules

export interface NodesList { now: Epoch; kind: 'forward'; groups: Group[]; nodes: (NodeBrief & { bar: NodeBar })[]; tags: Tag[] }

export interface NodeBrief {
  id: number; kind: 'forward';
  name: string; note: string; weight: number;          // weight: path preference, -100000..100000
  tags: Tag[];
  addr: string;                                        // primary host (== primary address .host)
  addresses: NodeAddress[];
  ports: string;                                       // allowed listen ports, normalized e.g. "10000-10100,20000"
  group_id: number;
  status: NodeStatus;
  last_seen: Epoch | null;
  desired_mode: 'active' | 'passive';                  // active = websocket agent, passive = manual/bash sync
  reported_mode: string | null;
  manual_required: boolean;                            // mode switch pending a manual sync on the machine
  protocol_version: number;
  desired_generation: number; applied_generation: number;   // equal when the node has confirmed its config
  err: string;                                         // last node error, or current runtime-rule error, or ""
  installed_agent: 'unknown' | 'active' | 'passive';
  backend: string;                                     // 'iptables' | 'nftables'
  connection_state: 'unknown' | 'connected' | 'disconnected' | 'paused';
  quality_port: number;                                // UDP probe port, 0 = probing disabled (default 33445)
  wake_enabled: boolean; wake_port: number;            // remote wake, only meaningful for passive nodes
  wake: null | { state: 'waiting' | 'confirmed' | 'failed' | 'timeout'; error: string; started: Epoch; attempts: number; generation: number };
}

/** Last 60 windows, oldest first; levels.length === 60. start = epoch of first window. */
export type NodeBar =
  | { start: Epoch; window: 20; levels: ('ok' | 'fault' | 'lost' | 'none')[] }                                   // desired_mode active
  | { start: Epoch; window: 60; levels: ('ok' | 'white')[]; counts: number[]; times: (Epoch | null)[] };         // passive: manual-sync activity

export interface NodeCreate {
  name: string;                    // required, trimmed, 1..64 chars, no control chars
  addr: string;                    // required: IPv4 or domain (lower-cased)
  note?: string;                   // <=500 chars
  ports?: string;                  // default "10000-65535"; "a-b,c" (full-width comma ok), <=300 chars, 1..65535
  kind?: 'forward';
  group_id?: number | null;        // null/""/absent -> default group; must be a `nodes` group
  desired_mode?: 'active' | 'passive';   // default active
  quality_port?: number;           // default 33445; 0 disables; else 1..65535
  addresses?: NodeAddressInput[];  // default []: server creates primary "main" from addr
  weight?: number;                 // integer -100000..100000 (strings rejected)
  tag_ids?: number[];
  wake_enabled?: boolean;          // must be a real boolean; forced false unless passive
  wake_port?: number;              // 0 or 1..65535; required non-zero when enabled; must not collide with forwards
}
export interface NodeAddressInput {
  id: string;                      // required, /^[A-Za-z0-9_-]{1,48}$/, unique
  primary?: boolean;               // if ANY item carries `primary`, exactly one must be true and its host must equal `addr` (when addr is sent)
  name?: string; host: string;     // non-primary host must be a usable IPv4 (not 0.0.0.0/loopback/multicast)
  local?: string; device?: string; // device /^[A-Za-z0-9_.:-]{1,15}$/
  gateway?: string;
}
export type NodeCreated = { id: number };

export interface NodeDetail {
  now: Epoch;
  sync_path: string;            // secret agent path, for building install commands
  public_url: string;           // "" if unset
  version: string;              // server version
  forward_version: string;      // agent version expected for this node's desired_mode
  installer_url: string;
  telemetry: Telemetry;
  node: NodeBrief & {
    network: { addresses: { local: string; device: string }[]; sampled_at: Epoch | null; complete: boolean };  // IPv4s seen on the machine
    bar: NodeBar;
    key: string;                // 64-hex node secret (sensitive)
    first_seen: Epoch | null; src_ip: string | null; skew: number | null;   // skew: node clock - server clock, seconds
    agent: string | null;       // installed agent version
    reported_v: string | null; want_v: string;      // 12-hex rule-set hashes; equal when in sync
    state: 'init' | 'ok' | 'error' | null;          // raw agent state
    group: string;              // group name
    agent_update: 'uninstalled' | 'switch' | 'manual' | 'pending' | 'disabled' | 'newer' | 'same';
    // uninstalled: no agent yet · switch: mode change needs manual sync · manual: passive agent outdated ·
    // pending: active agent outdated, auto-update on · disabled: outdated, auto-update off · newer/same: vs expected version
  };
  rules: NodeRule[];
  configs: { id: number; name: string }[];          // one per rule (configuration id/name)
  events: { ts: Epoch; text: string }[];            // newest first, max 50
}
export interface NodeRule {
  id: number; configuration_id: number; configuration_name: string;
  phase: 'stage' | 'live';
  node_id: number; port: number;                    // listen port on this node
  host: string; tport: number;                      // next hop
  route: string;                                    // "v2,<inHost>,<srcLocal|->,<device|->,<gateway|->" or ""
}

export interface Telemetry {
  node_id?: number;                                  // absent when there is no snapshot row at all
  inventory: Partial<Inventory>; metrics: Partial<Metrics>; ports: Partial<PortSnapshot>;
  inventory_at?: Epoch | null; metrics_at?: Epoch | null; ports_at?: Epoch | null;
  traffic: TrafficTotal[];
  history: MetricWindow[];                           // 60-s windows, oldest first, max 120; time = window start
  hourly_history: MetricWindow[];                    // 3600-s windows, max 720
}
export interface Inventory {
  sampled_at: number /* ms */; version: string; os: string /* raw /etc/os-release text */; kernel: string; arch: string; cpus: number;
  boot_id: string; installation_id: string; backend: string; cgroup: string; nft_version?: string;
  interfaces: { name: string; mtu?: number; addresses: string[] /* CIDR, v4+v6 */; flags?: string }[];
  network_complete: boolean;
}
export interface Metrics {
  sampled_at: number /* ms */; scope: string;
  memory: Record<string, number>;                    // /proc/meminfo in bytes: MemTotal, MemAvailable, …
  agent_rss: number; load: string[]; uptime: string[];           // raw /proc/loadavg, /proc/uptime fields
  cpu_percent?: number; cpu_parts_percent?: Record<'user'|'nice'|'system'|'idle'|'iowait'|'irq'|'softirq'|'steal', number>;
  agent_cpu_percent?: number;
  disk?: { total: string; available: string; files: number; files_free: number };       // byte counts are strings
  interfaces: Record<string, { rx_bytes: string; rx_packets: string; rx_errors: string; rx_dropped: string;
                               tx_bytes: string; tx_packets: string; tx_errors: string; tx_dropped: string;
                               rx_bytes_per_second?: number; tx_bytes_per_second?: number }>;
  conntrack_count: string; conntrack_max: string; diskstats: string; error: string; traffic_quality: string;
}
export interface PortSnapshot { sampled_at: number /* ms */; rows: PortRow[]; errors: string[]; total: number; truncated: boolean }
export type PortRow =
  | { source: 'socket'; protocol: 'tcp' | 'udp'; address: string; port: number; detail: string }     // raw `ss` line
  | { source: 'ictl'; rule_id: number; protocol: string; port: number; target: string };             // "host:port"
export interface TrafficTotal { rule_id: number; protocol: 'tcp' | 'udp'; up: string; down: string; up_packets: string; down_packets: string;
                         updated: Epoch; quality: string /* "ok" or a Chinese gap note */ }
export interface MetricWindow { time: Epoch; data: { samples: number; first_at: Epoch; last_at: Epoch;
  values: Partial<Record<'cpu_percent' | 'agent_rss' | 'memory_used_percent', { count: number; sum: number; min: number; max: number; mean: number }>> } }

export type NodeEdit = Partial<Omit<NodeCreate, 'kind'>> & { kind?: 'forward' };   // only keys present are changed
export type NodeEdited = { key: null; upstream: string[] };   // key is always null; upstream = names of nodes forwarding to this one, only when addr changed

export type NodeDeleted = { deleted: boolean };   // true: row removed now (never connected, or forced). false: marked `deleting`, disappears once the agent confirms

export type QueryBody =
  | { kind: 'inventory' | 'ports' | 'port_detail' | 'network' | 'rules' | 'logs' }
  | { kind: 'connections' | 'diagnose'; rule_id: number };            // rule id from NodeDetail.rules
/** 200 body is the agent's websocket frame verbatim. */
export type QueryOk = { type: 'query_result'; id: string; session: string; data?: QueryData; error?: string };

export type QueryData =
  | Inventory                                                                                   // inventory (no `kind` key); also refreshes the stored snapshot
  | (PortSnapshot & { nft?: string; nft_error?: string; 'iptables-save'?: string; 'iptables-save_error'?: string })   // ports, port_detail (no `kind`)
  | { sampled_at: number; kind: 'network'; addresses: string; routes: string; dns: string }     // addresses/routes are JSON *text* of `ip -j`
  | { sampled_at: number; kind: 'rules'; rules: string }                                        // JSON text of `nft -j list table`
  | { sampled_at: number; kind: 'logs'; logs: string }                                          // last 200 journal lines
  | { sampled_at: number; kind: 'connections'; connections?: Partial<Record<'tcp' | 'udp', string>> }   // raw conntrack output
  | { sampled_at: number; kind: 'diagnose'; dns_ms?: number; addresses?: string[]; note?: string; resolved_ip?: string; tcp_connect_ms?: number; reachable?: true }
  | { sampled_at: number; error: string; truncated: true };                                     // result >900 KB

export interface TargetsList { targets: Target[]; groups: Group[] }
export interface Target {
  id: number; name: string;
  type: 'endpoint' | 'shadowsocks' | 'vless-enc';
  host: string; port: number;
  params: {} | { method: string; password: string; share_query: [string, string][] }      // shadowsocks
             | { uuid: string; share_query: [string, string][] };                         // vless-enc
  addresses: TargetAddress[];         // [0] is always {id:"main", name:"默认", host, port}
  group_id: number; sort: number;
  configs: { id: number; remark: string }[];   // configurations using it (remark = configuration name)
  uri: string;                        // endpoint: "host:port"; otherwise the share link renamed to `name` (contains secrets)
}

export type TargetSave =
  | { uri: string; name?: string; group_id?: number | null; addresses?: TargetAddressInput[] }      // SS / SS2022 / VLESS-ENC share link (<=8192 chars)
  | { host: string; port: number; name?: string; type?: 'endpoint'; group_id?: number | null; addresses?: TargetAddressInput[] };   // plain endpoint
export interface TargetAddressInput { id: string; name?: string; host: string; port: number }
// id /^[A-Za-z0-9_-]{1,64}$/, not "main", unique; (host,port) unique incl. main; max 32. Do NOT send the synthesized "main" entry back.

export type VertexId = string;                 // /^[A-Za-z0-9_-]{1,64}$/, unique in the graph; server may emit "applied-<cfgId>-<vid>" in view_graph
export type EdgeKey = `${VertexId}/${VertexId}`;
export const AUTO = '@auto';                   // "let the kernel pick the source address"

export interface Graph {                       // as returned
  version: 2; routing_version: 2;
  vertices: Vertex[];
  edges: [VertexId, VertexId][];        // directed a -> b
  bindings: Record<EdgeKey, { from: Record<VertexId, string | null>; to: Record<VertexId, string | null> }>;
  // from[a] = address id a sends from (or "@auto"); to[b] = address id of b that receives. A `direct` endpoint has no entry on its side ({}).
  // null only appears in layout_only previews of incomplete graphs.
  address_book: Record<VertexId, Record<string, NodeAddress | TargetAddress>>;   // server-generated snapshot per vertex; ignored on input
  ports: Record<EdgeKey, number>;       // key "<entryVid>/<targetVid>" -> entry listen port (server fills allocated ports on save)
}
export interface Vertex {
  id: VertexId;
  kind: 'entry' | 'node' | 'target' | 'direct';   // direct = weight-only modifier sitting on a wire (exactly 1 in, 1 out, never adjacent to another direct)
  disabled: false;                      // always false
  x: number; y: number;                 // canvas coords; default 40; |v| <= 1e6
  node_id?: number;                     // entry, node
  target_id?: number;                   // target
  weight?: number;                      // entry/node: copied from the node (read-only); direct: own weight (-100000..100000)
  address?: string | null;              // entry only: receiving address id (auto-selected when the node has exactly one)
  in_address?: string; out_address?: string;   // optional UI defaults, kept only if valid (out may be "@auto")
  layer?: number | null;                // set by arrange(): column index, null for direct / unplaced
  runtime_only?: true;                  // view_graph only: vertex removed from the design but still in an applied route
}

export interface TopologyResources {
  groups: Group[];                 // topology groups
  node_groups: Group[]; target_groups: Group[];
  nodes: PaletteNode[];            // ALL nodes, display order
  targets: PaletteTarget[];        // ALL targets
}
export interface PaletteNode {
  id: number; name: string; addr: string; weight: number; group_id: number; tags: Tag[];
  addresses: NodeAddress[];
  network: { addresses: { local: string; device: string }[]; sampled_at: Epoch | null; complete: boolean };
  mode: 'active' | 'passive'; status: NodeStatus;
}
export interface PaletteTarget {          // raw row: NOTE params is a JSON *string* here, and there is no uri/configs
  id: number; name: string; type: Target['type']; host: string; port: number;
  params: string; addresses: TargetAddress[]; group_id: number; sort: number;
}
export interface TopologiesList extends TopologyResources { topologies: Topology[] }   // includes topologies being deleted

export interface Topology {
  id: number; name: string; group_id: number; sort: number;
  revision: number;                // echo on every write
  deleted: 0 | 1; failover_enabled: 0 | 1;
  graph: Graph;                    // the design (editable)
  view_graph: Graph;               // design + still-applied vertices/edges that were removed from it (runtime_only)
  configurations: ConfigView[];    // includes ones being deleted
  state: TopologyState;            // 'deleting' if deleted; else first config in 'error', else first non-'active' config state, else 'active'; 'pending' if no configs
  error: string;                   // first non-empty configuration error
  events: { ts: Epoch; message: string }[];     // newest first, max 30 (key is `message`, node events use `text`)
  cleanup: { configuration_id: number; node_id: number; port: number }[];   // ports awaiting release on nodes
  sync: { retry: boolean; state: TopologyState };   // retry: show the "retry sync" action (state here ignores `deleted`)
  warnings: { vertex: VertexId; message: string }[];   // vertices not on any entry->target path
}
export type ConfigState = 'pending' | 'preparing' | 'switching' | 'waiting' | 'error' | 'active' | 'deleting';
// pending: queued · preparing: downstream rules being installed · switching: entry rule being swapped ·
// waiting: blocked on a node (offline, upgrading, manual sync…), `error` says why · error: needs user action / retry ·
// active: applied; reported as 'error' ("路径节点异常或离线") when any hop node is error/offline/skew
export type TopologyState = ConfigState;

export interface ConfigView {
  id: number;
  entry: VertexId; target: VertexId;   // vertex ids, not node/target ids
  name: string;                    // default "配置 0001"
  code: string;                    // "C0001"
  state: ConfigState; error: string;
  switching: boolean;              // an execution job is in flight
  active_path: VertexId[];         // applied route ([] if never applied)
  pending_path: VertexId[];        // desired route when state != 'active' ([] otherwise)
  active_graph: Graph | null;      // sub-graph of the applied path (snapshot at apply time)
  pending_graph: Graph | null;     // sub-graph of the desired path, when state != 'active'
  port: number | null;             // entry listen port (applied, else desired, else null)
  connection: Connection | null;   // how clients reach it; null until applied / while deleting
  target_address: TargetAddress | null;   // effective target endpoint
  failover: FailoverStatus;
}
export interface Connection {
  protocol: 'endpoint' | 'shadowsocks' | 'vless-enc';
  host: string; port: number;      // entry node host + entry port
  uri: string;                     // "host:port" for endpoint, else share link named after the configuration
  method?: string; password?: string; uuid?: string;
}
export type FailoverStatus = {} | {
  phase: 'pending' | 'retry' | 'unavailable' | 'paused' | 'done';
  message: string; at?: Epoch; next_try?: Epoch;
  attempts?: VertexId[][];         // candidate paths already tried
};

export interface PreviewBody {
  graph: Graph;
  layout_only?: boolean;     // lenient validation (incomplete graphs allowed) and auto-layout only
  arrange?: boolean;         // return the auto-arranged graph instead of the normalized one
  id?: number;               // existing topology id: keeps its current paths as "preferred"
  entry?: VertexId;          // restrict to one entry (defaults to prefix[0])
  prefix?: VertexId[];       // path chosen so far (interactive hop-by-hop selection)
  required?: VertexId[];     // vertices the path must pass through
}
export type PreviewResult =
  | { graph: Graph }                                                   // layout_only
  | {
      graph: Graph;                                                    // normalized: bindings/address_book filled in
      paths: { entry: VertexId; target: VertexId; path: VertexId[]; unchanged: boolean }[];   // best path per pair; unchanged vs current desired
      path: VertexId[];                                                // paths[0].path or []
      prefix: VertexId[];
      options: VertexId[];                                             // valid next vertices after prefix (empty if no prefix)
      complete: boolean;                                               // prefix given and nothing left to choose
      skipped: [];                                                     // always empty
      warnings: { vertex: VertexId; message: string }[];
    };

export interface TopologySave {
  name: string;                    // required (1..64) unless switch_only
  graph: Graph;                    // must be complete
  revision?: number;               // REQUIRED and must match on update
  group_id?: number | null;        // topology group; default: keep / default group
  failover_enabled?: boolean;      // default: keep / false. Must be a real boolean
  paths?: VertexId[][];            // explicit path per (entry,target) pair, <=64, one per pair; others get the best path
  switch_only?: boolean;           // update only: change paths without touching the design
}
export type TopologySaved =
  | { id: number; configurations: number; warnings: { vertex: VertexId; message: string }[] }
  | { id: number; changed: number; skipped: [] };          // switch_only: number of configurations re-pointed

export type TopologyAction =
  | { action: 'retry'; revision: number }                       // re-queue failed/waiting configurations and node syncs
  | { action: 'failover'; enabled: boolean; revision: number }; // toggle automatic failover (bumps revision)

export interface FailoverPreview {
  vertex: VertexId; node_id: number; revision: number;
  at: Epoch; offline_seconds: number;          // configured failover_offline_seconds
  message: string;                             // non-empty = preview not applicable (then paths is []), or "当前没有使用此节点的配置"
  graph: Graph;
  paths: {
    id: number; name: string; entry: VertexId; target: VertexId;      // configuration
    active_path: VertexId[];
    state: 'available' | 'unavailable' | 'blocked';
    path: VertexId[];                          // replacement path ([] unless available)
    message: string;                           // reason when blocked/unavailable
    switch_at?: VertexId;                      // last common real vertex (available only)
    weight?: number;                           // weight of the replacement path
    target_address?: TargetAddress;
  }[];
}

export interface ConfigsList {
  configs: (TopologyConfig | ExternalConfig)[];     // topology configs first (topology order, then id), then external (display order)
  groups: (Group & { source: 'topology' | 'external' })[];
}
export interface TopologyConfig extends ConfigView {       // deleted ones are excluded
  source: 'topology';
  topology_id: number; topology_name: string; group_id: number;     // the topology's group
  entry_name: string; entry_tags: Tag[]; target_name: string;
}
export interface ExternalConfig {
  source: 'external';
  id: number; name: string; uri: string;            // uri as imported (original fragment)
  group_id: number; sort: number; revision: number;
  code: string;                                     // "E0001"
  state: 'imported'; error: '';
  connection: { protocol: ExternalProtocol; host: string; port: number; uri: string };   // uri renamed to `name`
}
export type ExternalProtocol = 'shadowsocks' | 'vmess' | 'vless' | 'trojan' | 'hysteria2' | 'tuic' | 'anytls' | 'socks5' | 'http';

export interface ExternalSave {
  uri?: string;                 // required on create
  name?: string;                // default: link name (64 chars) on create, old name on edit
  group_id?: number | null;     // `external_configurations` group
  revision?: number;            // REQUIRED integer on PATCH
}

export interface SubscriptionsList {
  subscriptions: Subscription[];
  configs: SubscriptionCandidate[];                 // everything selectable (same order as GET configs)
  groups: (Group & { source: 'topology' | 'external' })[];
  public_url: string;                               // "" if unset -> fall back to location.origin
}
export interface Subscription {
  id: number; name: string; token: string;          // 64 hex
  enabled: boolean;
  configuration_ids: number[];                      // topology configuration ids
  external_configuration_ids: number[];
  path: string;                                     // "/sub/<token>" — at the server ROOT, not under panel_path
  counts: { v2ray: number; clash: number };         // members exportable in each format
}
export interface SubscriptionCandidate {
  id: number; source: 'topology' | 'external'; name: string; code: string; group_id: number;
  error: string;        // "" or why it cannot be exported as v2ray links ("配置尚未生效", "仅有地址和端口，缺少代理协议", …)
  clash_error: string;  // "" or why it cannot be exported for Clash/Mihomo
}

export interface SubscriptionSave {
  name?: string;                          // required on create
  enabled?: boolean;                      // real boolean; default true
  configuration_ids?: number[];           // replaces the set when present; unique ints, <=512, must exist
  external_configuration_ids?: number[];
  reset_token?: boolean;                  // PATCH: issue a new token (old URL dies)
}

export type Kind = 'nodes' | 'targets' | 'topologies' | 'external_configurations';

export interface Settings {
  failover_offline_seconds: number;      // 30..3600, default 60
  public_url: string;                    // "" or "http(s)://host[:port]"
  sync_path: string; panel_path: string; // secret prefixes (read-only)
  username: string; version: string;
  components: { api: number; supported_apis: number[]; server: string; active_agent: string; passive_agent: string;
                wake_agent: string; manager: string; standalone: string };
}

export type Range = 900 | 3600 | 86400 | 604800;            // seconds; bucket step 10 / 60 / 300 / 1800
export type QualityBody =
  | { scope: 'topologies'; id: number; range?: Range; configuration?: number }              // per-configuration sections
  | { scope: 'topologies'; id: number; range?: Range; source?: number; target?: number };   // node pair (mode chosen if either key is present)

export interface LinkQuality {
  name: string;                                     // topology name
  now: Epoch; interval: 10;                         // probe interval, seconds
  configurations: { id: number; name: string }[];   // selectable (non-deleted) configurations
  configuration: number | null;                     // the one shown (default: first); null if none
  sections: {
    id: string;                                     // "config:<cfgId>:full" | "config:<cfgId>:stage:<vertexId>"
    kind: '全段' | '子段';                            // whole path / one hop
    label: string;                                  // "relay-a → web"
    labels: { name: string; node_id?: number; tags?: Tag[]; target_id?: number; address?: TargetAddress | null }[];
    series: LinkSeries[];                           // always 3: tcp-connect-v1, udp-echo-v1, icmp-echo-v1
  }[];
}
export interface LinkSeries extends History {
  protocol: 'tcp-connect-v1' | 'udp-echo-v1' | 'icmp-echo-v1';
  state: 'collecting' | 'waiting' | 'unknown' | 'disabled' | 'unsupported' | 'offline' | 'stopped' | 'error';
  note: string;                                     // reason when state != collecting
  path_key: string;                                 // identity of the currently measured path (24 hex)
}

export type PairQuality =
  | { nodes: PairNode[]; pair: null; now: Epoch }                       // topology has < 2 nodes
  | { nodes: PairNode[]; pair: Pair; now: Epoch; interval: 10;
      adjacent: boolean;                                                // source,target consecutive in the topology's node list
      series: (History & { protocol: 'udp-echo-v1' | 'icmp-echo-v1'; pair: Pair; state: LinkSeries['state']; note: string })[] };   // always 2
export type PairNode = { id: number; name: string; mode: 'active' | 'passive' };   // nodes of this topology
export interface Pair { id: number; source: number; target: number; source_addr: string; target_addr: string; port: number; protocol: string }

export interface History {
  step: number;                    // bucket seconds
  points: Point[];                 // oldest first; ~range/step+1 (91 for 900 s); a bucket may split into several points at failures
  summary: Stats;                  // last 300 s
  latest: Sample | null;           // most recent raw row
}
export interface Stats { samples: number; lost: number; loss: number | null /* % */; mean: number | null; peak: number | null; p95: number | null; jitter: number | null }   // RTT in ms
export interface Point extends Stats {
  time: Epoch; end: Epoch;         // [time, end) covered by the point
  status: 'ok' | 'timeout' | 'error' | 'missing' | LinkSeries['state'];   // 'missing' = no data; a state name = not measurable in that interval
  error: string; failures: number;
  resolved_ip: string;
  first_slot: Epoch | null; last_slot: Epoch | null;
  gap: boolean;                    // contains a failure or discontinuity -> break the line
  endpoint_change: boolean;        // measured path/IP changed inside the point
  connect: boolean;                // continuous with the previous point -> draw a connecting line
  path_key: string | null; path_label: string;      // sections mode only (null/"" in pair mode)
  interval: number | null;         // seconds since the previous sample
}
export type Sample =
  | { task_id: string; slot: Epoch; status: 'ok' | 'timeout' | 'error'; rtt: number | null; error: string; resolved_ip: string;
      epoch: string | null; previous_slot: Epoch | null; path_key?: string; path_label?: string }
  | { slot: Epoch; last_slot: Epoch; count: number; status: string; rtt: null; error: string; resolved_ip: ''; path_key: string; path_label: string };   // synthesized "not measurable" interval

