"""The authoritative owned-rule snapshot for each node."""

import hashlib
import json


def node_rule_rows(c, node):
    if node["deleting"]:
        return []
    return [
        dict(r)
        for r in c.execute(
            """SELECT r.*,c.name AS configuration_name
        FROM rules r JOIN configurations c ON c.id=r.configuration_id WHERE r.node_id=? ORDER BY r.id""",
            (node["id"],),
        )
    ]


def node_rules(c, node):
    return [
        f"{r['id']} both {r['port']} {r['host']} {r['tport']}"
        + (" " + r["route"] if r["route"] else "")
        for r in node_rule_rows(c, node)
    ]


def rules_version(lines):
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:12]


def reserved(c, nid, exclude_configuration=None, *, allow_owned_retired=False):
    """Only exempt this configuration's ingress, never another owner's port."""
    ingress_ids = set()
    jobs = list(c.execute("SELECT configuration_id,payload FROM jobs"))
    if exclude_configuration is not None:
        row = c.execute(
            "SELECT applied FROM configurations WHERE id=?", (exclude_configuration,)
        ).fetchone()
        if row and row[0]:
            ingress_ids.add(json.loads(row[0])["rules"][0]["id"])
        for job in jobs:
            if job["configuration_id"] == exclude_configuration:
                ingress_ids.add(json.loads(job["payload"])["new"][0]["id"])
    result = {
        r["port"]
        for r in c.execute("SELECT * FROM rules WHERE node_id=?", (nid,))
        if r["configuration_id"] != exclude_configuration or r["id"] not in ingress_ids
    }
    for row in c.execute("SELECT port FROM retired_ports WHERE node_id=?", (nid,)):
        owners = {
            r[0]
            for r in c.execute(
                "SELECT configuration_id FROM retirements WHERE node_id=? AND port=?",
                (nid, row[0]),
            )
        }
        if not (allow_owned_retired and owners == {exclude_configuration}):
            result.add(row[0])

    for row in c.execute(
        "SELECT id,desired FROM configurations WHERE deleted=0 AND desired IS NOT NULL"
    ):
        plan = json.loads(row["desired"])
        if row["id"] != exclude_configuration and plan["entry"] == nid and plan.get("port"):
            result.add(plan["port"])
    for row in jobs:
        rules = json.loads(row["payload"])["new"]
        if row["configuration_id"] == exclude_configuration:
            rules = rules[1:]
        result.update(x["port"] for x in rules if x["node_id"] == nid)
    return result
