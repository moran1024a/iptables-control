"""Shared node labels; never part of forwarding or ordering."""

import re
import errors
import validation


def catalog(c):
    return [dict(r) for r in c.execute("SELECT * FROM tags ORDER BY id")]


def listing(c, nid):
    return [dict(r) for r in c.execute(
        "SELECT t.* FROM tags t JOIN node_tags n ON n.tag_id=t.id WHERE n.node_id=? ORDER BY t.id",
        (nid,),
    )]


def assign(c, nid, ids):
    if not isinstance(ids, list) or any(type(i) is not int for i in ids):
        raise errors.UserError("请选择有效标签", "tag_ids")
    ids = set(ids)
    if not ids <= {r[0] for r in c.execute("SELECT id FROM tags")}:
        raise errors.UserError("标签已删除，请刷新后重试", "tag_ids")
    c.execute("DELETE FROM node_tags WHERE node_id=?", (nid,))
    c.executemany("INSERT INTO node_tags VALUES (?,?)", [(nid, i) for i in sorted(ids)])


def save(c, body, ident=None):
    old = c.execute("SELECT * FROM tags WHERE id=?", (ident,)).fetchone() if ident is not None else None
    if ident is not None and not old:
        raise errors.UserError("标签不存在", status=404)
    name = validation.check_remark(body.get("name", old["name"] if old else None), "name", "标签名称")
    color = body.get("color", old["color"] if old else "#3b82f6")
    if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise errors.UserError("请选择有效颜色", "color")
    if c.execute("SELECT 1 FROM tags WHERE name=? COLLATE NOCASE AND id!=?", (name, ident or 0)).fetchone():
        raise errors.UserError("标签名称已存在", "name")
    if old:
        c.execute("UPDATE tags SET name=?,color=? WHERE id=?", (name, color.lower(), ident))
    else:
        ident = c.execute("INSERT INTO tags(name,color) VALUES (?,?)", (name, color.lower())).lastrowid
    return {"id": ident}
