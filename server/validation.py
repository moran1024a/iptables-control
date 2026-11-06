import ipaddress
import re
import secrets
import errors

HOST_RE = re.compile(
    "^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\\.)+[A-Za-z][A-Za-z0-9-]{0,62}$"
)


def check_host(s, field):
    s = str(s or "").strip()
    try:
        ipaddress.IPv4Address(s)
        return s
    except ValueError:
        pass
    if HOST_RE.match(s):
        return s.lower()
    raise errors.UserError("请填写有效的 IPv4 地址或域名", field)


def check_port(s, field):
    s = str(s or "").strip()
    if re.fullmatch("[1-9][0-9]{0,4}", s) and int(s) <= 65535:
        return int(s)
    raise errors.UserError("端口必须是 1-65535 的整数", field)


def check_remark(s, field="remark", label="别名"):
    s = str(s or "").strip()
    if not s:
        raise errors.UserError(f"{label}不能为空", field)
    if len(s) > 64 or "\n" in s:
        raise errors.UserError(f"{label}最长 64 个字符，且不能换行", field)
    return s


def parse_ports(s, field="ports"):
    """解析允许的端口：逗号分隔的端口或范围，如 10000-20000,30000。返回排序合并后的 [(起始, 结束)]。"""
    s = str(s or "").replace("，", ",").replace(" ", "")
    if len(s) > 300:
        raise errors.UserError("允许的端口最长 300 个字符", field)
    ranges = []
    for item in filter(None, s.split(",")):
        m = re.fullmatch("(\\d{1,5})(?:-(\\d{1,5}))?", item)
        if not m:
            raise errors.UserError(f"格式错误：{item}（应为 端口 或 起始-结束，多个用逗号分隔）", field)
        lo, hi = (int(m[1]), int(m[2] or m[1]))
        if not 1 <= lo <= hi <= 65535:
            raise errors.UserError(f"范围无效：{item}（端口为 1-65535，起始不能大于结束）", field)
        ranges.append((lo, hi))
    if not ranges:
        raise errors.UserError("请填写允许的端口", field)
    merged = []
    for lo, hi in sorted(ranges):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def format_ports(ranges):
    return ",".join((str(lo) if lo == hi else f"{lo}-{hi}" for lo, hi in ranges))


def in_ports(port, ranges):
    return any((lo <= port <= hi for lo, hi in ranges))


def pick_port(ranges, used):
    """在允许的端口中随机选一个未使用的端口；没有可用端口时返回 None。"""
    total = sum((hi - lo + 1 for lo, hi in ranges))
    if total <= sum((1 for p in used if in_ports(p, ranges))):
        return None
    for _ in range(64):
        k = secrets.randbelow(total)
        for lo, hi in ranges:
            if k <= hi - lo:
                port = lo + k
                break
            k -= hi - lo + 1
        if port not in used:
            return port
    return next((p for lo, hi in ranges for p in range(lo, hi + 1) if p not in used))


def check_note(s):
    s = str(s or "").strip().replace("\r\n", "\n")
    if len(s) > 500:
        raise errors.UserError("备注最长 500 个字符", "note")
    return s
