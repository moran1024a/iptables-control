"""Shared measurements and the intervals in which each view used them."""

import hashlib
import json

import database

SCHEMA = """
CREATE TABLE IF NOT EXISTS probe_tasks (
 id TEXT PRIMARY KEY, identity TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS probe_samples (
 task_id TEXT NOT NULL REFERENCES probe_tasks(id), slot INTEGER NOT NULL,
 status TEXT NOT NULL, rtt REAL, error TEXT NOT NULL DEFAULT '',
 resolved_ip TEXT NOT NULL DEFAULT '', PRIMARY KEY(task_id, slot)
);
CREATE INDEX IF NOT EXISTS probe_sample_age ON probe_samples(slot);
CREATE TABLE IF NOT EXISTS probe_bindings (
 id INTEGER PRIMARY KEY, scope TEXT NOT NULL, owner_id INTEGER NOT NULL,
 section TEXT NOT NULL, protocol TEXT NOT NULL, task_id TEXT REFERENCES probe_tasks(id),
 path_key TEXT NOT NULL, path_label TEXT NOT NULL,
 status TEXT NOT NULL, note TEXT NOT NULL, started INTEGER NOT NULL, ended INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS probe_binding_current
 ON probe_bindings(scope, owner_id, section, protocol) WHERE ended IS NULL;
CREATE INDEX IF NOT EXISTS probe_binding_history
 ON probe_bindings(scope, owner_id, section, protocol, started);
"""


def install_schema(c):
    for statement in SCHEMA.split(";"):
        if statement.strip():
            c.execute(statement)
    # Resource IDs can be reused. Remove their bindings, never the shared samples.
    for table, scope in (("links", "links"), ("routes", "routes"), ("chains", "configs")):
        c.execute(f"""CREATE TRIGGER IF NOT EXISTS probe_delete_{table}
            AFTER DELETE ON {table} BEGIN
              DELETE FROM probe_bindings WHERE scope='{scope}' AND owner_id=OLD.id;
            END""")


def fingerprint(identity):
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def measurement_identity(identity):
    if identity["protocol"] != "icmp-echo-v1" or identity["kind"] == "icmp":
        return identity
    destination = identity["target"][1] if identity["kind"] == "pair" else identity["endpoint"][0]
    return dict(kind="icmp", protocol="icmp-echo-v1", source=identity["source"],
                host=destination.lower().rstrip("."))


def pair_identity(nodes, a, b, protocol):
    return dict(
        kind="pair",
        source=(a, nodes[a]["addr"]),
        target=(b, nodes[b]["addr"]),
        protocol=protocol,
        port=nodes[b]["quality_port"] if protocol == "udp-echo-v1" else 0,
    )


def task(c, identity):
    identity = measurement_identity(identity)
    key = fingerprint(identity)
    c.execute(
        "INSERT OR IGNORE INTO probe_tasks VALUES (?,?)",
        (key, json.dumps(identity, sort_keys=True, separators=(",", ":"))),
    )
    return key


def bind(c, demands, now):
    """Replace only changed intervals. A task may have many independent consumers."""
    current = {
        (r["scope"], r["owner_id"], r["section"], r["protocol"]): dict(r)
        for r in c.execute("SELECT * FROM probe_bindings WHERE ended IS NULL")
    }
    fields = ("task_id", "path_key", "path_label", "status", "note")
    for demand in demands:
        key = (demand["scope"], demand["owner_id"], demand["section"], demand["protocol"])
        demand["task_id"] = task(c, demand["identity"]) if demand.get("identity") else None
        reason = demand.get("reason")
        demand["status"], demand["note"] = reason or ("", "")
        previous = current.pop(key, None)
        if previous and all(previous[f] == demand[f] for f in fields):
            continue
        if previous:
            c.execute("UPDATE probe_bindings SET ended=? WHERE id=?", (now, previous["id"]))
        c.execute(
            """INSERT INTO probe_bindings
          (scope, owner_id, section, protocol, task_id, path_key, path_label, status, note, started)
          VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (*key, *(demand[f] for f in fields), now),
        )
    for previous in current.values():
        c.execute("UPDATE probe_bindings SET ended=? WHERE id=?", (now, previous["id"]))


def record(c, task_id, slot, status, rtt=None, error="", resolved_ip="", *, epoch=None, previous_slot=None):
    c.execute(
        "INSERT OR IGNORE INTO probe_samples(task_id,slot,status,rtt,error,resolved_ip,epoch,previous_slot) VALUES (?,?,?,?,?,?,?,?)",
        (task_id, slot, status, rtt, str(error)[:300], str(resolved_ip)[:64], epoch, previous_slot),
    )


def cutoff(c):
    return int(database.setting(c, "probe_cutover", "0"))


def pair_rows(c, pair, since):
    cut = cutoff(c)
    rows = [
        dict(r)
        for r in c.execute(
            "SELECT * FROM quality_samples WHERE pair_id=? AND slot>=? AND slot<?", (pair["id"], since, cut)
        )
    ]
    identity = dict(
        kind="pair",
        source=(pair["source"], pair["source_addr"]),
        target=(pair["target"], pair["target_addr"]),
        protocol=pair["protocol"],
        port=pair["port"],
    )
    old_key, key = fingerprint(identity), fingerprint(measurement_identity(identity))
    identity_cut = int(database.setting(c, "probe_identity_v2", "0"))
    rows.extend(
        dict(r)
        for r in c.execute(
            """SELECT * FROM probe_samples WHERE slot>=? AND
            (task_id=? OR (task_id=? AND slot<?))""",
            (max(cut, since), key, old_key, identity_cut),
        )
    )
    return sorted(rows, key=lambda r: r["slot"])


def link_rows(c, spec, since, now, interval=10):
    cut = cutoff(c)
    key = (spec["link_id"], spec["section"], spec["protocol"])
    rows = [
        dict(r)
        for r in c.execute(
            """SELECT * FROM link_quality_samples
        WHERE link_id=? AND section=? AND protocol=? AND slot>=? AND slot<?""",
            (*key, since, cut),
        )
    ]
    for b in c.execute(
        """SELECT * FROM probe_bindings WHERE scope='links' AND owner_id=?
        AND section=? AND protocol=? AND started<=? AND (ended IS NULL OR ended>?) ORDER BY started,id""",
        (*key, now, max(cut, since)),
    ):
        start, end = (
            max(b["started"], cut, since),
            min(b["ended"] if b["ended"] is not None else now + 1, now + 1),
        )
        if start >= end:
            continue
        if b["status"]:
            samples = [
                dict(slot=t, status=b["status"], rtt=None, error=b["note"], resolved_ip="")
                for t in range(start, end, interval)
            ]
        else:
            samples = [
                dict(r)
                for r in c.execute(
                    """SELECT * FROM probe_samples
                WHERE task_id=? AND slot>=? AND slot<?""",
                    (b["task_id"], start, end),
                )
            ]
        rows.extend(dict(r, path_key=b["path_key"], path_label=b["path_label"]) for r in samples)
    return sorted(rows, key=lambda r: r["slot"])


def cleanup(c, before):
    c.execute("DELETE FROM probe_samples WHERE slot<?", (before,))
    c.execute("DELETE FROM probe_bindings WHERE ended IS NOT NULL AND ended<=?", (before,))
    c.execute("""DELETE FROM probe_tasks WHERE id NOT IN (SELECT task_id FROM probe_samples)
        AND id NOT IN (SELECT task_id FROM probe_bindings WHERE task_id IS NOT NULL)""")
    # These compatibility tables receive no new measurements after the cutover.
    c.execute("DELETE FROM quality_samples WHERE slot<?", (before,))
    c.execute("DELETE FROM link_quality_samples WHERE slot<?", (before,))
    c.execute("""DELETE FROM quality_pairs WHERE id NOT IN (SELECT DISTINCT pair_id FROM quality_samples)
        AND (source NOT IN (SELECT id FROM nodes) OR target NOT IN (SELECT id FROM nodes))""")
