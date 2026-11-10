"""Shared measurements and the intervals in which each view used them."""

import hashlib
import json



def fingerprint(identity):
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def measurement_identity(identity):
    if identity["protocol"] != "icmp-echo-v1" or identity["kind"] == "icmp":
        return identity
    destination = identity["target"][1] if identity["kind"] == "pair" else identity["endpoint"][0]
    return dict(
        kind="icmp",
        protocol="icmp-echo-v1",
        source=identity["source"],
        host=destination.lower().rstrip("."),
        **({"source_route": identity["source_route"]} if "source_route" in identity else {})
    )


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


def record(
    c, task_id, slot, status, rtt=None, error="", resolved_ip="", *, epoch=None, previous_slot=None
):
    c.execute(
        "INSERT OR IGNORE INTO probe_samples(task_id,slot,status,rtt,error,resolved_ip,epoch,previous_slot) VALUES (?,?,?,?,?,?,?,?)",
        (task_id, slot, status, rtt, str(error)[:300], str(resolved_ip)[:64], epoch, previous_slot),
    )


def pair_rows(c, pair, since):
    identity = dict(
        kind="pair",
        source=(pair["source"], pair["source_addr"]),
        target=(pair["target"], pair["target_addr"]),
        protocol=pair["protocol"],
        port=pair["port"],
    )
    key = fingerprint(measurement_identity(identity))
    return [
        dict(r)
        for r in c.execute(
            "SELECT * FROM probe_samples WHERE task_id=? AND slot>=? ORDER BY slot", (key, since)
        )
    ]


def link_rows(c, spec, since, now, interval=10, *, bucket=None):
    key = (spec["topology_id"], spec["section"], spec["protocol"])
    rows = []
    for b in c.execute(
        """SELECT * FROM probe_bindings WHERE scope='topologies' AND owner_id=?
        AND section=? AND protocol=? AND started<=? AND (ended IS NULL OR ended>?) ORDER BY started,id""",
        (*key, now, since),
    ):
        start, end = (
            max(b["started"], since),
            min(b["ended"] if b["ended"] is not None else now + 1, now + 1),
        )
        if start >= end:
            continue
        if b["status"]:
            # Unknown intervals contain no measurements. Keep their bounds/counts
            # per chart bucket instead of materializing every ten-second slot.
            samples = []
            cursor = start
            while cursor < end:
                stop = min(end, (cursor // bucket + 1) * bucket) if bucket else cursor + interval
                count = (stop - 1 - cursor) // interval + 1
                last = cursor + (count - 1) * interval
                samples.append(dict(slot=cursor, last_slot=last, count=count,
                                    status=b["status"], rtt=None, error=b["note"], resolved_ip=""))
                cursor = last + interval
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
    c.execute(
        "DELETE FROM quality_pairs WHERE source NOT IN (SELECT id FROM nodes) OR target NOT IN (SELECT id FROM nodes)"
    )
