"""User-owned client profiles, independent of topology execution."""
import database
import errors
import proxy_formats
import validation


def get(c, ident):
    row = c.execute("SELECT * FROM external_configurations WHERE id=?", (ident,)).fetchone()
    if row is None:
        raise errors.UserError("外部配置不存在", status=404)
    return row


def parse(uri):
    try:
        return proxy_formats.parse(uri)
    except ValueError as exc:
        raise errors.UserError(str(exc), "uri") from None


def preview(body):
    node = parse(body.get("uri"))
    return {k:node[k] for k in ("name", "protocol", "host", "port")}


def save(c, body, ident=None):
    old = get(c, ident) if ident is not None else None
    if old and (type(body.get("revision")) is not int or body["revision"] != old["revision"]):
        raise errors.UserError("配置已变化，请重新打开编辑", status=409)
    node = parse(body.get("uri", old["uri"] if old else None))
    name = validation.check_remark(body.get("name") or (old["name"] if old else node["name"][:64]), "name", "名称")
    try:
        proxy_formats.clean(name)
    except ValueError:
        raise errors.UserError("名称不能含有控制字符", "name") from None
    group = database.check_group(c, "external_configurations", body.get("group_id", old["group_id"] if old else None))
    if old:
        c.execute("UPDATE external_configurations SET name=?,uri=?,revision=revision+1 WHERE id=?", (name,node["uri"],ident))
        if group != old["group_id"]:
            database.move_group(c, "external_configurations", ident, group)
    else:
        ident = c.execute("INSERT INTO external_configurations(name,uri,group_id,sort) VALUES (?,?,?,(SELECT COALESCE(MAX(sort),0)+1 FROM external_configurations))",
                          (name,node["uri"],group)).lastrowid
    return dict(id=ident, code=f"E{ident:04d}")


def delete(c, ident):
    get(c, ident)
    c.execute("DELETE FROM external_configurations WHERE id=?", (ident,))
    return dict(deleted=True)


def listing(c):
    result = []
    for row in c.execute(database.ordered("external_configurations")):
        node = proxy_formats.parse(row["uri"])
        result.append(dict(row, source="external", code=f"E{row['id']:04d}", state="imported", error="",
                           connection=dict(protocol=node["protocol"], host=node["host"], port=node["port"],
                                           uri=proxy_formats.share(node,row["name"]))))
    return result
