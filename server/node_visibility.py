"""Project private forwarding addresses into labels for a subaccount's panel."""


def address_labels(rows):
    ordered = sorted(rows, key=lambda a: (not a.get("primary"), a["id"]))
    return [dict(id=a["id"], name=f"地址 {i}", primary=bool(a.get("primary")),
                 host="", local="", device="", gateway="")
            for i, a in enumerate(ordered, 1)]


def graph(value, visible):
    if not value or visible is None:
        return value
    private_vertices = {v["id"] for v in value["vertices"] if "node_id" in v and v["node_id"] not in visible}
    if not private_vertices:
        return value
    ports = {key: port for key, port in value.get("ports", {}).items() if key.split("/", 1)[0] not in private_vertices}
    return dict(value, ports=ports, address_book={
        vid: ({row["id"]: row for row in address_labels(book.values())}
              if vid in private_vertices else book)
        for vid, book in value["address_book"].items()
    })


def history(value):
    # Samples may describe a previously applied path, even when the current
    # path no longer contains the hidden node.
    for row in value["points"] + ([value["latest"]] if value["latest"] else []):
        row["resolved_ip"] = ""
        if row.get("error"):
            row["error"] = "探测异常"
    return value
