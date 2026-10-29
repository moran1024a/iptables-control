#!/usr/bin/env bash
# IPv4 转发管理；运行时配置和备份保存在 /root 下。
set -u
set -o pipefail

VERSION=3.1
SELF_PATH="$(readlink -f -- "$0")"

error() { printf '错误：%s\n' "$*" >&2; }
confirm() {
  local answer
  read -rp "$1 [y/N]: " answer || return 1
  [[ "$answer" == y || "$answer" == Y ]]
}
prompt() {
  local var="$1" label="$2" default="${3:-}" value
  read -rp "$label${default:+ [$default]}: " value || return 1
  [[ "$value" == q ]] && return 1
  value="${value:-$default}"
  [[ -n "$value" ]] || { error '不能为空。'; return 1; }
  printf -v "$var" '%s' "$value"
}

check_dependencies() {
  local tool manager='' package missing=() packages=()
  for tool in python3 iptables iptables-save iptables-restore ss; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
  done
  if ((${#missing[@]})); then
    printf '缺少依赖：%s\n正在自动安装所需软件包。\n' "${missing[*]}"
    for tool in apt-get dnf yum pacman zypper; do
      if command -v "$tool" >/dev/null 2>&1; then manager="$tool"; break; fi
    done
    [[ -n "$manager" ]] || { error '未识别到支持的包管理器，无法自动安装依赖。'; return 1; }
    for tool in "${missing[@]}"; do
      case "$tool" in
        python3) package=python3; [[ "$manager" == pacman ]] && package=python ;;
        iptables*) package=iptables ;;
        ss) package=iproute2; [[ "$manager" == dnf || "$manager" == yum ]] && package=iproute ;;
      esac
      [[ " ${packages[*]} " == *" $package "* ]] || packages+=("$package")
    done
    case "$manager" in
      apt-get)
        DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}" || {
          apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}"
        } || return 1 ;;
      dnf|yum) "$manager" install -y "${packages[@]}" || return 1 ;;
      pacman) pacman -S --needed --noconfirm "${packages[@]}" || return 1 ;;
      zypper) zypper --non-interactive install "${packages[@]}" || return 1 ;;
    esac
  fi
  for tool in python3 iptables iptables-save iptables-restore ss systemctl; do
    command -v "$tool" >/dev/null 2>&1 || { error "依赖仍不可用：$tool"; return 1; }
  done
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || { error '需要 Python 3.8 或更新版本。'; return 1; }
}

backend() {
  python3 - "$SELF_PATH" "$@" <<'IPT_PYTHON'
import contextlib
from collections import Counter
import datetime
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import uuid

CONF = Path('/root/iptables-forward-conf/managed-forwards.conf')
BACKUPS = Path('/root/iptables-backup')
PERSIST = CONF.parent / 'rules.v4'
LEGACY_PERSIST = Path('/etc/iptables/rules.v4')
AGENT_CONF = Path('/var/lib/ictl/config')
AGENT_LOCK = Path('/run/ictl.lock')
SYSCTL = Path('/etc/sysctl.d/99-ipt-forward-manager.conf')
IP_FORWARD = Path('/proc/sys/net/ipv4/ip_forward')
LOCK = Path('/run/ipt-forward-manager.lock')
LOG = Path('/var/log/ipt-forward-manager.log')
UNIT_DIR = Path('/etc/systemd/system')
SERVICE = 'ipt-forward-selfcheck.service'
TIMER = 'ipt-forward-selfcheck.timer'
PREFIX = 'iptfm:'

class Failure(Exception):
    pass

def command(args, data=None, check=True, timeout=40):
    try:
        result = subprocess.run(args, input=data, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Failure(f'命令执行失败：{shlex.join(args)}：{exc}') from exc
    if check and result.returncode:
        raise Failure(f'{shlex.join(args)}：{result.stderr.strip() or result.stdout.strip() or "执行失败"}')
    return result

def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def log(message):
    try:
        with LOG.open('a') as stream:
            stream.write(f'[{datetime.datetime.now():%F %T}] {message}\n')
    except OSError as exc:
        print(f'日志写入失败：{exc}', file=sys.stderr)

@contextlib.contextmanager
def locked():
    # The Agent only takes AGENT_LOCK; this fixed order cannot form a lock cycle.
    with contextlib.ExitStack() as stack:
        deadline = time.monotonic() + 30
        for path in (LOCK, AGENT_LOCK):
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = stack.enter_context(path.open('a'))
            while True:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise Failure('其他实例或 Agent 正在操作转发，请稍后重试。')
                    time.sleep(0.1)
        yield

def ipv4(value):
    try:
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError:
        return None

def validate(values):
    if len(values) != 5:
        raise Failure('配置格式：入站端口 目标IP或域名 目标端口 tcp/udp/both 备注')
    incoming, host, target, proto, comment = values
    for label, port in [('入站', incoming), ('目标', target)]:
        if not re.fullmatch(r'[1-9][0-9]{0,4}', port) or int(port) > 65535:
            raise Failure(f'{label}端口必须为 1–65535：{port}')
    if not ipv4(host):
        labels = host.split('.')
        if (len(host) > 253 or len(labels) < 2 or not re.match(r'^[A-Za-z]', labels[-1])
                or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', x) for x in labels)):
            raise Failure(f'无效的 IPv4 地址或域名：{host}')
    proto = proto.lower()
    if proto not in ('tcp', 'udp', 'both'):
        raise Failure(f'协议必须是 tcp、udp 或 both：{proto}')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,199}', comment):
        raise Failure('备注须以字母或数字开头，限字母、数字、._:-，最多 200 字符。')
    return dict(incoming=incoming, host=host, target=target, proto=proto, comment=comment)

def protocols(row):
    return ('tcp', 'udp') if row['proto'] == 'both' else (row['proto'],)

def option(spec, *names):
    for index, value in enumerate(spec[:-1]):
        if value in names:
            return spec[index + 1], index > 0 and spec[index - 1] == '!'
    return None, False

def normalize(spec):
    # iptables-save 会重新排列 -s/-d/-p 等参数，不能按原命令的词序比较。
    arguments = {'-p', '-s', '-d', '-m', '-j', '--dport', '--sport', '--comment',
                 '--ctstate', '--ctorigdstport', '--ctdir', '--to-destination', '--dst-type'}
    result = []
    index = 0
    while index < len(spec):
        negated = spec[index] == '!'
        if negated:
            index += 1
        if index >= len(spec):
            result.append(('raw', '!', ''))
            break
        flag = spec[index]
        if flag not in arguments or index + 1 >= len(spec):
            result.append(('raw', '!' if negated else '', flag))
            index += 1
            continue
        value = spec[index + 1]
        if flag == '-m' and value in ('tcp', 'udp') and not negated:
            index += 2
            continue
        if flag in ('-s', '-d') and value.endswith('/32'):
            value = value[:-3]
        if flag == '-p':
            value = {'6': 'tcp', '17': 'udp'}.get(value, value)
        result.append(('!' if negated else '', flag, value))
        index += 2
    return tuple(sorted(result))

def snapshot(text=None):
    live = text is None
    if text is None:
        text = command(['iptables-save']).stdout
    table = None
    rules = []
    counts = {}
    chains = set()
    for line in text.splitlines():
        if line.startswith('*'):
            table = line[1:]
        elif line == 'COMMIT':
            table = None
        elif line.startswith(':') and table:
            chains.add((table, line[1:].split()[0]))
        elif line.startswith('-A ') and table:
            words = shlex.split(line)
            chain = words[1]
            key = (table, chain)
            counts[key] = counts.get(key, 0) + 1
            rules.append(dict(table=table, chain=chain, spec=words[2:], pos=counts[key]))
    if ('nat', 'PREROUTING') not in chains or ('filter', 'FORWARD') not in chains:
        if live:
            # 空白机器可能尚未加载 NAT/filter 表；显式查询后重新读取一次。
            for name in ('nat', 'filter'):
                command(['iptables', '-w', '30', '-t', name, '-S'])
            return snapshot(command(['iptables-save']).stdout)
        raise Failure('无法读取完整的 IPv4 NAT/FORWARD 表，已停止操作。')
    return dict(text=text, rules=rules, chains=chains)

def rule_key(rule):
    return rule['table'], rule['chain'], normalize(rule['spec'])

def make_rules(rows, legacy=False):
    result = []
    for row in rows:
        if not row['ip']:
            continue
        for proto in protocols(row):
            ip, target, incoming = row['ip'], row['target'], row['incoming']
            comment = row['comment'] if legacy else PREFIX + row['id']
            mark = ['-m', 'comment', '--comment', comment]
            pre = ['-p', proto, '--dport', incoming]
            post = ['-p', proto, '-d', ip, '--dport', target]
            if legacy:
                specs = [('nat', 'PREROUTING', pre + mark + ['-j', 'DNAT', '--to-destination', ip + ':' + target]),
                         ('nat', 'POSTROUTING', post + mark + ['-j', 'MASQUERADE']),
                         ('filter', 'FORWARD', post + mark + ['-j', 'ACCEPT'])]
            else:
                # 原目的端口使每条转发独立；LOCAL 限制为发往本机的流量。
                original = ['-m', 'conntrack', '--ctstate', 'DNAT', '--ctorigdstport', incoming, '--ctdir', 'ORIGINAL']
                reply = ['-p', proto, '-s', ip, '--sport', target, '-m', 'conntrack', '--ctstate', 'DNAT',
                         '--ctorigdstport', incoming, '--ctdir', 'REPLY']
                specs = [('nat', 'PREROUTING', pre + ['-m', 'addrtype', '--dst-type', 'LOCAL'] + mark + ['-j', 'DNAT', '--to-destination', ip + ':' + target]),
                         ('nat', 'POSTROUTING', post + original + mark + ['-j', 'MASQUERADE']),
                         ('filter', 'FORWARD', post + original + mark + ['-j', 'ACCEPT']),
                         ('filter', 'FORWARD', reply + mark + ['-j', 'ACCEPT'])]
            result.extend(dict(table=t, chain=c, spec=s) for t, c, s in specs)
    return result

def load_config(snap, text=None):
    if text is None:
        text = CONF.read_text() if CONF.exists() else ''
    modern = text.startswith('# ipt-forward-manager v3\n')
    rows = []
    old_groups = {}
    for n, line in enumerate(text.splitlines(), 1):
        fields = line.split('#', 1)[0].split()
        if not fields:
            continue
        try:
            if modern:
                if len(fields) != 7 or not re.fullmatch(r'[0-9a-f]{32}', fields[0]) or not ipv4(fields[6]):
                    raise Failure('托管配置格式错误')
                row = validate(fields[1:6])
                row.update(id=fields[0], ip=fields[6], legacy=False)
                rows.append(row)
            else:
                row = validate(fields)
                if row['proto'] == 'both':
                    raise Failure('旧托管配置必须按 tcp/udp 分行保存')
                # 只接管与旧脚本模板完全一致的规则，不丢弃外部规则的限制条件。
                matches = []
                for rule in snap['rules']:
                    spec = rule['spec']
                    dest, _ = option(spec, '--to-destination')
                    if (rule['table'] == 'nat' and rule['chain'] == 'PREROUTING'
                        and option(spec, '--comment')[0] == row['comment']
                        and option(spec, '-p')[0] == row['proto']
                        and option(spec, '--dport')[0] == row['incoming']
                        and dest and dest.endswith(':' + row['target'])):
                        ip = dest.rsplit(':', 1)[0]
                        candidate = dict(row, id='', ip=ip)
                        if not ipv4(ip) or normalize(spec) != normalize(make_rules([candidate], True)[0]['spec']):
                            raise Failure('旧规则包含无法安全迁移的条件，请先人工核对：' + shlex.join(spec))
                        matches.append(ip)
                if len(set(matches)) > 1:
                    raise Failure('同一旧配置对应多个目标 IP，无法安全迁移')
                ip = matches[0] if matches else (ipv4(row['host']) or '')
                key = (row['incoming'], row['host'], row['target'], row['comment'], ip)
                if key in old_groups:
                    existing = old_groups[key]
                    if existing['proto'] == row['proto'] or existing['proto'] == 'both':
                        raise Failure('旧托管配置重复')
                    existing['proto'] = 'both'
                else:
                    row.update(id=uuid.uuid5(uuid.NAMESPACE_URL, repr(key)).hex, ip=ip, legacy=True)
                    old_groups[key] = row
                    rows.append(row)
        except Failure as exc:
            raise Failure(f'托管配置第 {n} 行：{exc}') from exc
    if len({r['id'] for r in rows}) != len(rows):
        raise Failure('托管配置存在重复标识，已停止操作。')
    unique_ports(rows)
    return rows

def serialize(rows):
    lines = ['# ipt-forward-manager v3']
    for r in rows:
        lines.append(' '.join(r[x] for x in ('id', 'incoming', 'host', 'target', 'proto', 'comment', 'ip')))
    return '\n'.join(lines) + '\n'

def fingerprint(row):
    values = [row[k] for k in ('id', 'incoming', 'host', 'target', 'proto', 'comment')]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()[:16]

def owned_rules(snap, rows):
    expected = {rule_key(r) for r in make_rules([r for r in rows if not r['legacy']])}
    old_expected = {rule_key(r) for r in make_rules([r for r in rows if r['legacy']], True)}
    result = []
    for rule in snap['rules']:
        comment, _ = option(rule['spec'], '--comment')
        key = rule_key(rule)
        if comment and comment.startswith(PREFIX):
            if key not in expected:
                raise Failure('发现未知或被外部修改的托管规则，已停止操作：' + shlex.join(rule['spec']))
            result.append(rule)
        elif key in old_expected:
            result.append(rule)
    return result

def unique_ports(rows):
    seen = {}
    for row in rows:
        for proto in protocols(row):
            key = (proto, row['incoming'])
            if key in seen:
                raise Failure(f'配置冲突：{proto.upper()} {row["incoming"]} 同时用于 [{seen[key]}] 和 [{row["comment"]}]。')
            seen[key] = row['comment']

def port_matches(value, port, proto):
    for part in value.split(','):
        bounds = part.split(':')
        if len(bounds) > 2:
            raise Failure(f'无法判断现有端口条件：{value}')
        def number(x, default):
            if not x:
                return default
            if x.isdigit():
                return int(x)
            try:
                return socket.getservbyname(x, proto)
            except OSError as exc:
                raise Failure(f'无法判断现有端口条件：{value}') from exc
        if len(bounds) == 1:
            low = high = number(bounds[0], 0)
        else:
            low, high = number(bounds[0], 0), number(bounds[1], 65535)
        if low <= int(port) <= high:
            return True
    return False

def may_match(spec, proto, port):
    value, negate = option(spec, '-p', '--protocol')
    if value is not None:
        match = value in (proto, {'tcp': '6', 'udp': '17'}[proto], 'all', '0')
        if match == negate:
            return False
    for names in [('--dport', '--destination-port'), ('--dports', '--destination-ports')]:
        value, negate = option(spec, *names)
        if value is not None and port_matches(value, port, proto) == negate:
            return False
    # --ports 可以匹配源端口，未知扩展也可能限制流量；无法证明不重叠时保守拒绝。
    return True

def rule_conflict(snap, proto, port, owned):
    ignored = {rule_key(r) for r in owned}
    by_chain = {}
    for rule in snap['rules']:
        by_chain.setdefault((rule['table'], rule['chain']), []).append(rule)
    def visit(table, chain, seen):
        if chain in seen:
            return None
        for rule in by_chain.get((table, chain), []):
            if rule_key(rule) in ignored or not may_match(rule['spec'], proto, port):
                continue
            jump, _ = option(rule['spec'], '-j', '--jump', '-g', '--goto')
            if jump in ('DNAT', 'REDIRECT', 'TPROXY'):
                return rule
            if (table, jump) in snap['chains']:
                found = visit(table, jump, seen | {chain})
                if found:
                    return found
        return None
    return visit('nat', 'PREROUTING', set()) or visit('mangle', 'PREROUTING', set())

def sockets(proto):
    flags = '-lntp' if proto == 'tcp' else '-aunp'
    output = command(['ss', '-H', flags]).stdout
    result = {}
    for line in output.splitlines():
        fields = line.split()
        offset = 1 if fields and fields[0] in ('tcp', 'udp') else 0
        if len(fields) < 5 + offset:
            raise Failure('无法解析本机端口状态，已停止操作：' + line)
        address, port = fields[3 + offset].rsplit(':', 1)
        address = address.strip('[]').split('%', 1)[0]
        if ':' in address:
            ip6 = ipaddress.IPv6Address(address)
            # 明确绑定到 IPv6 专用地址的套接字不占用 IPv4；:: 可能双栈，保守拒绝。
            if not ip6.is_unspecified and ip6.ipv4_mapped is None:
                continue
        if not port.isdigit():
            raise Failure('无法判断本机端口占用，已停止操作：' + line)
        result.setdefault(port, []).append(line)
    return result

def check_conflicts(check_rows, all_rows, snap, owned):
    unique_ports(all_rows)
    if not check_rows:
        return  # Removing our own entries must remain possible when the peer is damaged.
    reserved = agent_ports()
    used = {p: sockets(p) for p in {p for r in check_rows for p in protocols(r)}}
    for row in check_rows:
        for proto in protocols(row):
            port = row['incoming']
            if (proto, port) in reserved:
                raise Failure(f'端口冲突：{proto.upper()} {port} 已被 Agent 保存的配置占用。')
            if port in used[proto]:
                raise Failure(f'端口冲突：{proto.upper()} {port} 已被本机进程占用：\n' + '\n'.join(used[proto][port]))
            rule = rule_conflict(snap, proto, port, owned)
            if rule:
                raise Failure(f'端口冲突：现有规则可能覆盖 {proto.upper()} {port}：\n'
                              f'{rule["table"]}/{rule["chain"]} ' + shlex.join(rule['spec']))

def agent_ports():
    if not AGENT_CONF.exists() and not AGENT_CONF.is_symlink():
        return set()
    try:
        lines = AGENT_CONF.read_text().splitlines()
        if not lines or not re.fullmatch(r'v=[0-9a-f]{1,64}', lines[0]):
            raise ValueError('版本头无效')
        result = set()
        for line in lines[1:]:
            if not line.strip():
                continue
            fields = line.split()
            if (len(fields) != 5 or not fields[0].isdigit() or fields[1] not in ('tcp', 'udp', 'both')
                    or not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', fields[3])
                    or any(not p.isdigit() or not 1 <= int(p) <= 65535 for p in (fields[2], fields[4]))):
                raise ValueError('规则格式无效')
            for proto in ('tcp', 'udp') if fields[1] == 'both' else (fields[1],):
                result.add((proto, str(int(fields[2]))))
        return result
    except (OSError, ValueError) as exc:
        raise Failure(f'无法校验 Agent 端口占用：{exc}') from exc

def owned_snapshot(snap, rows):
    rules = owned_rules(snap, rows)
    lines = []
    for table, chains in [('nat', ('PREROUTING', 'POSTROUTING')), ('filter', ('FORWARD',))]:
        lines += ['*' + table] + [':' + c + ' - [0:0]' for c in chains]
        lines += [shlex.join(['-A', r['chain']] + r['spec']) for r in rules if r['table'] == table]
        lines.append('COMMIT')
    return '\n'.join(lines) + '\n'

def clean_legacy_persist(rows, saved):
    if not LEGACY_PERSIST.exists():
        return
    text = LEGACY_PERSIST.read_text()
    legacy = {rule_key(r) for r in make_rules(rows, True)}
    table = None
    kept = []
    for line in text.splitlines(keepends=True):
        if line.startswith('*'):
            table = line[1:].strip()
        elif line == 'COMMIT\n':
            table = None
        if table and line.startswith('-A '):
            words = shlex.split(line)
            comment, _ = option(words[2:], '--comment')
            rule = dict(table=table, chain=words[1], spec=words[2:])
            if (comment and re.fullmatch(r'iptfm:[0-9a-f]{32}', comment)) or rule_key(rule) in legacy:
                continue
        kept.append(line)
    if ''.join(kept) != text:
        if LEGACY_PERSIST.is_symlink():
            raise Failure('旧全局规则文件是软链接，请人工迁移其中的单机脚本规则。')
        atomic_write(saved / 'previous-system-rules.v4', text)
        atomic_write(LEGACY_PERSIST, ''.join(kept))
        return text

def resolve(rows):
    cache = {}
    for row in rows:
        host = row['host']
        if ipv4(host):
            row['ip'] = host
            continue
        if host not in cache:
            code = 'import socket,sys,json; print(json.dumps(list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(sys.argv[1],None,socket.AF_INET,socket.SOCK_STREAM)))))'
            try:
                addresses = json.loads(command([sys.executable, '-c', code, host], timeout=10).stdout)
            except (Failure, ValueError) as exc:
                raise Failure(f'域名解析失败：{host}：{exc}') from exc
            if not addresses or any(not ipv4(ip) for ip in addresses):
                raise Failure(f'没有可用的 IPv4 地址：{host}')
            cache[host] = addresses
        row['ip'] = row['ip'] if row['ip'] in cache[host] else cache[host][0]

def backup(snap):
    BACKUPS.mkdir(parents=True, exist_ok=True)
    name = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-')
    path = Path(tempfile.mkdtemp(prefix=name, dir=BACKUPS))
    try:
        atomic_write(path / 'rules.v4', owned_snapshot(snap, load_config(snap)))
        atomic_write(path / 'managed.conf', CONF.read_text() if CONF.exists() else '')
        atomic_write(path / 'complete', '1\n')
    except Exception:
        # 未完成的目录不会出现在恢复列表中。
        raise
    return path

def ipt(rule, op, pos=None):
    args = ['iptables', '-w', '30', '-t', rule['table'], op, rule['chain']]
    if pos is not None:
        args.append(str(pos))
    return command(args + rule['spec'])

def rollback_rules(owned, desired):
    # 根据实际规则撤销，兼顾命令已经提交、但在返回前超时或收到信号的情况。
    current = snapshot()
    expected = Counter(rule_key(r) for r in owned)
    relevant = set(expected) | {rule_key(r) for r in desired}
    actual = Counter(rule_key(r) for r in current['rules'] if rule_key(r) in relevant)
    for rule in reversed(current['rules']):
        key = rule_key(rule)
        if key in relevant and actual[key] > expected[key]:
            ipt(rule, '-D')
            actual[key] -= 1
    for rule in sorted(owned, key=lambda r: (r['table'], r['chain'], r['pos'])):
        key = rule_key(rule)
        if actual[key] < expected[key]:
            latest = snapshot()
            length = sum(r['table'] == rule['table'] and r['chain'] == rule['chain'] for r in latest['rules'])
            ipt(rule, '-I', min(rule['pos'], length + 1))
            actual[key] += 1

def restore_file(path, old):
    if old is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write(path, old)

def service_text(script):
    if any(c in script for c in '\n\r\0') or not Path(script).is_file():
        raise Failure('无法定位脚本文件，不能配置开机恢复。')
    escaped = script.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$')
    return ('[Unit]\nDescription=IPv4 forwarding restore and DDNS refresh\nAfter=network-online.target\nWants=network-online.target\n'
            '[Service]\nType=oneshot\nExecStart=/bin/bash "' + escaped + '" --self-check\nTimeoutStartSec=300\n'
            '[Install]\nWantedBy=multi-user.target\n')

def scheduler(rows, script):
    if not Path('/run/systemd/system').is_dir():
        raise Failure('当前版本需要运行中的 systemd 来提供开机恢复和域名定时刷新。')
    atomic_write(UNIT_DIR / SERVICE, service_text(script))
    atomic_write(UNIT_DIR / TIMER, '[Unit]\nDescription=IPv4 forwarding periodic check\n[Timer]\nOnBootSec=1min\nOnUnitActiveSec=5min\nUnit=' + SERVICE + '\n[Install]\nWantedBy=timers.target\n')
    command(['systemctl', 'daemon-reload'])
    command(['systemctl', 'enable', SERVICE])
    if any(not ipv4(r['host']) for r in rows):
        command(['systemctl', 'enable', '--now', TIMER])
    else:
        command(['systemctl', 'disable', '--now', TIMER])

def scheduler_state():
    return {SERVICE: command(['systemctl', 'is-enabled', '--quiet', SERVICE], check=False).returncode == 0,
            TIMER: command(['systemctl', 'is-enabled', '--quiet', TIMER], check=False).returncode == 0,
            'active': command(['systemctl', 'is-active', '--quiet', TIMER], check=False).returncode == 0}

def restore_scheduler(state):
    command(['systemctl', 'daemon-reload'])
    for unit in (SERVICE, TIMER):
        command(['systemctl', 'enable' if state[unit] else 'disable', unit], check=state[unit])
    command(['systemctl', 'start' if state['active'] else 'stop', TIMER], check=state['active'])

def transact(old_rows, rows, snap, script, refresh=False):
    owned = owned_rules(snap, old_rows)
    desired = make_rules(rows)
    desired_keys = {rule_key(r) for r in desired}
    live_keys = {rule_key(r) for r in snap['rules']}
    additions = [r for r in desired if rule_key(r) not in live_keys]
    kept = set()
    deletions = []
    for rule in owned:
        key = rule_key(rule)
        if key in desired_keys and key not in kept:
            kept.add(key)
        else:
            deletions.append(rule)
    # 回程/出站先准备好，DNAT 最后添加；新规则完整存在后才删除旧规则。
    additions.sort(key=lambda r: r['chain'] == 'PREROUTING')
    deletions.sort(key=lambda r: r['chain'] != 'PREROUTING')
    config_text = serialize(rows)
    old_conf = CONF.read_text() if CONF.exists() else None
    if refresh and not additions and not deletions and old_conf == config_text and IP_FORWARD.read_text().strip() == '1':
        return
    saved = backup(snap)
    files = {p: p.read_text() if p.exists() else None for p in (CONF, PERSIST, SYSCTL, UNIT_DIR / SERVICE, UNIT_DIR / TIMER)}
    legacy_before = None
    ip_forward = IP_FORWARD.read_text()
    old_scheduler = scheduler_state() if not refresh else None
    signals = {}
    import signal
    def interrupted(signum, frame):
        raise Failure(f'操作被信号 {signum} 中断')
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signals[sig] = signal.signal(sig, interrupted)
    try:
        legacy_before = clean_legacy_persist(old_rows, saved)
        if rows:
            atomic_write(SYSCTL, 'net.ipv4.ip_forward=1\n')
            if IP_FORWARD.read_text().strip() != '1':
                IP_FORWARD.write_text('1\n')
            if IP_FORWARD.read_text().strip() != '1':
                raise Failure('无法开启 IPv4 转发。')
        for rule in additions:
            ipt(rule, '-I', 1) if rule['chain'] == 'FORWARD' else ipt(rule, '-A')
        for rule in deletions:
            ipt(rule, '-D')
        atomic_write(CONF, config_text)
        atomic_write(PERSIST, owned_snapshot(snapshot(), rows))
        if not refresh:
            scheduler(rows, script)
        log(f'change committed: forwards={len(rows)} backup={saved}')
    except Exception as exc:
        for sig in signals:
            signal.signal(sig, signal.SIG_IGN)
        errors = []
        if legacy_before is not None:
            try:
                restore_file(LEGACY_PERSIST, legacy_before)
            except Exception as rollback_error:
                errors.append(str(rollback_error))
        try:
            rollback_rules(owned, desired)
        except Exception as rollback_error:
            errors.append(str(rollback_error))
        for path, value in files.items():
            try:
                restore_file(path, value)
            except Exception as rollback_error:
                errors.append(str(rollback_error))
        try:
            if IP_FORWARD.read_text() != ip_forward:
                IP_FORWARD.write_text(ip_forward)
        except OSError as rollback_error:
            errors.append(str(rollback_error))
        if not refresh:
            try:
                restore_scheduler(old_scheduler)
            except Exception as rollback_error:
                errors.append(str(rollback_error))
        log(f'change failed: {exc}; rollback_errors={errors}; backup={saved}')
        if errors:
            raise Failure(f'{exc}\n恢复未全部成功：{"; ".join(errors)}\n完整备份：{saved}') from exc
        raise Failure(f'{exc}\n已撤销本次规则与配置修改。备份：{saved}') from exc
    finally:
        for sig, handler in signals.items():
            signal.signal(sig, handler)
    if not refresh:
        print(f'操作成功，规则已保存。备份：{saved}')

def import_rows(path):
    rows = []
    for n, line in enumerate(Path(path).read_text().splitlines(), 1):
        fields = line.split('#', 1)[0].split()
        if not fields:
            continue
        if len(fields) == 3:
            fields.append('both')
        if len(fields) == 4:
            fields.append(f'xjj-forward-{fields[0]}-to-{fields[1]}-{fields[2]}'[:200])
        try:
            row = validate(fields)
        except Failure as exc:
            raise Failure(f'导入文件第 {n} 行：{exc}；整个文件未导入。') from exc
        row.update(id=uuid.uuid4().hex, ip='', legacy=False)
        rows.append(row)
    if not rows:
        raise Failure('文件中没有可导入的配置。')
    unique_ports(rows)
    return rows

def complete_backups():
    if not BACKUPS.exists():
        return []
    return sorted((p for p in BACKUPS.iterdir() if p.is_dir() and (p / 'complete').is_file()
                   and (p / 'rules.v4').is_file() and (p / 'managed.conf').is_file()), reverse=True)

def restore_backup(name, script):
    matches = [p for p in complete_backups() if p.name == name]
    if len(matches) != 1:
        raise Failure('备份不存在或缺少配套配置，不能恢复。')
    chosen = matches[0]
    rules_text = (chosen / 'rules.v4').read_text()
    conf_text = (chosen / 'managed.conf').read_text()
    rows = load_config(snapshot(rules_text), conf_text)
    before = snapshot()
    old_rows = load_config(before)
    check_conflicts(rows, rows, before, owned_rules(before, old_rows))
    resolve([r for r in rows if not r['ip']])
    for row in rows:
        row['legacy'] = False
    transact(old_rows, rows, before, script)
    log(f'backup restored: {chosen}')
    print('已恢复单机脚本的规则及配置，其他防火墙规则保持不变。')

def dispatch(args, script):
    action = args[0]
    if action == 'backups':
        for path in complete_backups():
            print(path.name)
        return
    if action == 'restore':
        restore_backup(args[1], script)
        return
    if action == 'preview-import':
        for row in import_rows(args[1]):
            print(f'{row["comment"]}: {row["proto"].upper()} {row["incoming"]} → {row["host"]}:{row["target"]}')
        return
    snap = snapshot()
    old_rows = load_config(snap)
    if action == 'list':
        for row in old_rows:
            print('\t'.join([row['id'], fingerprint(row)] + [row[k] for k in ('incoming', 'host', 'target', 'proto', 'comment', 'ip')]))
        return
    if action == 'export':
        if not old_rows:
            raise Failure('没有可导出的托管转发。')
        output = '# 入站端口 目标IP或域名 目标端口 协议 备注\n'
        output += ''.join(' '.join(row[k] for k in ('incoming', 'host', 'target', 'proto', 'comment')) + '\n' for row in old_rows)
        destination = Path(args[1]).resolve()
        if (destination in {CONF.resolve(), PERSIST.resolve(), LEGACY_PERSIST.resolve(), AGENT_CONF.resolve(),
                            SYSCTL.resolve(), Path(script).resolve(), Path('/usr/local/bin/ictl'),
                            UNIT_DIR / 'ictl-agent.service'}
                or Path('/var/lib/ictl') in destination.parents or Path('/etc/ictl') in destination.parents):
            raise Failure('不能覆盖脚本或运行配置文件。')
        atomic_write(destination, output)
        print(f'已导出 {len(old_rows)} 条转发：{destination}')
        return
    owned = owned_rules(snap, old_rows)
    rows = [dict(r) for r in old_rows]
    check_rows = []
    if action in ('delete', 'update'):
        selected = next((r for r in rows if r['id'] == args[1]), None)
        if selected is None or fingerprint(selected) != args[2]:
            raise Failure('所选转发已被修改或删除，请重新选择。')
        rows.remove(selected)
        if action == 'update':
            row = validate(args[3:])
            row.update(id=selected['id'], ip=selected['ip'] if row['host'] == selected['host'] else '', legacy=False)
            rows.append(row)
            check_rows.append(row)
    elif action == 'add':
        row = validate(args[1:])
        row.update(id=uuid.uuid4().hex, ip='', legacy=False)
        rows.append(row)
        check_rows.append(row)
    elif action == 'import':
        check_rows = import_rows(args[1])
        rows.extend(check_rows)
    elif action == 'refresh':
        if not rows:
            return
        check_rows = rows
    else:
        raise Failure('未知操作：' + action)
    check_conflicts(check_rows, rows, snap, owned)
    # 删除不依赖其他域名当前能否解析；仅新建、修改、刷新和缺失 IP 的迁移需要解析。
    resolution_error = None
    try:
        resolve([r for r in rows if r in check_rows or not r['ip']])
    except Failure as exc:
        if action != 'refresh' or any(not ipv4(r['ip']) for r in old_rows):
            raise
        # 开机时 DNS 暂不可用，也能用上次成功的地址恢复已有转发。
        rows = [dict(r) for r in old_rows]
        resolution_error = exc
    for row in rows:
        row['legacy'] = False
    # 在开始写入前再读一次，检测预检查期间其他管理器对规则的修改。
    latest = snapshot()
    if [rule_key(r) for r in latest['rules']] != [rule_key(r) for r in snap['rules']]:
        raise Failure('检查期间防火墙规则发生变化，未执行操作，请重试。')
    check_conflicts(check_rows, rows, latest, owned)
    transact(old_rows, rows, latest, script, action == 'refresh')
    if resolution_error:
        raise Failure(f'{resolution_error}；已保留或恢复上次成功的 IPv4 地址，等待下次刷新。')

def main():
    script, args = sys.argv[1], sys.argv[2:]
    import signal
    def interrupted(signum, frame):
        raise Failure(f'操作被信号 {signum} 中断')
    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        if not args:
            raise Failure('缺少操作参数。')
        with locked():
            dispatch(args, script)
        return 0
    except Exception as exc:
        print(f'错误：{exc}', file=sys.stderr)
        log(f'operation failed: {exc}')
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

if __name__ == '__main__':
    sys.exit(main())
IPT_PYTHON
}

ROWS=()
SELECTED_ID='' SELECTED_TOKEN='' SELECTED_IN='' SELECTED_HOST=''
SELECTED_TARGET='' SELECTED_PROTO='' SELECTED_COMMENT=''

list_forwards() {
  local output line id token incoming host target proto comment ip n=0
  output="$(backend list)" || return 1
  ROWS=()
  if [[ -z "$output" ]]; then
    printf '没有托管的转发配置。\n'
    return 0
  fi
  mapfile -t ROWS <<< "$output"
  for line in "${ROWS[@]}"; do
    IFS=$'\t' read -r id token incoming host target proto comment ip <<< "$line"
    n=$((n+1))
    printf '%d. %s — %s %s → %s:%s\n' "$n" "$comment" "${proto^^}" "$incoming" "$host" "$target"
    [[ "$host" != "$ip" ]] && printf '   当前 IPv4：%s\n' "${ip:-尚未解析}"
  done
  return 0
}

select_forward() {
  local index
  list_forwards || return 1
  ((${#ROWS[@]})) || return 1
  prompt index '选择编号（q 取消）' || return 1
  if [[ ! "$index" =~ ^[1-9][0-9]{0,8}$ ]] || ((index > ${#ROWS[@]})); then
    error '编号无效，请使用列表中的数字，不要添加前导零。'
    return 1
  fi
  IFS=$'\t' read -r SELECTED_ID SELECTED_TOKEN SELECTED_IN SELECTED_HOST SELECTED_TARGET SELECTED_PROTO SELECTED_COMMENT _ <<< "${ROWS[index-1]}"
}

edit_forward() {
  local operation="$1" incoming='' host='' target='' proto=tcp comment=''
  if [[ "$operation" == update ]]; then
    select_forward || return 1
    incoming="$SELECTED_IN" host="$SELECTED_HOST" target="$SELECTED_TARGET"
    proto="$SELECTED_PROTO" comment="$SELECTED_COMMENT"
  fi
  printf '输入 q 取消。协议可选 tcp、udp、both。\n'
  prompt incoming '入站端口' "$incoming" || return 1
  prompt host '目标 IPv4/域名' "$host" || return 1
  prompt target '目标端口' "${target:-$incoming}" || return 1
  prompt proto '协议' "$proto" || return 1
  prompt comment '备注（字母、数字、._:-）' "${comment:-xjj-forward-$incoming}" || return 1
  printf '%s：%s %s → %s:%s\n' "$comment" "$proto" "$incoming" "$host" "$target"
  confirm '确认提交？提交前将检查规则和端口冲突' || return 0
  if [[ "$operation" == update ]]; then
    backend update "$SELECTED_ID" "$SELECTED_TOKEN" "$incoming" "$host" "$target" "$proto" "$comment"
  else
    backend add "$incoming" "$host" "$target" "$proto" "$comment"
  fi
}

delete_forward() {
  select_forward || return 1
  confirm "确认删除 [$SELECTED_COMMENT] $SELECTED_PROTO $SELECTED_IN → $SELECTED_HOST:$SELECTED_TARGET？" || return 0
  backend delete "$SELECTED_ID" "$SELECTED_TOKEN"
}

export_config() {
  local file
  prompt file '导出路径' "/root/forwards-$(date +%Y%m%d-%H%M%S).conf" || return 1
  if [[ -e "$file" ]]; then confirm '文件已存在，确认覆盖？' || return 0; fi
  backend export "$file"
}

import_config() {
  local file
  prompt file '导入文件路径（q 取消）' || return 1
  backend preview-import "$file" || return 1
  confirm '确认导入？任一配置有错误或冲突时，整次导入失败' || return 0
  backend import "$file"
}

restore_config() {
  local output index n=0 item backups=()
  output="$(backend backups)" || return 1
  [[ -n "$output" ]] || { printf '没有包含规则及托管配置的完整备份。\n'; return 0; }
  mapfile -t backups <<< "$output"
  for item in "${backups[@]}"; do n=$((n+1)); printf '%d. %s\n' "$n" "$item"; done
  prompt index '选择备份编号（q 取消）' || return 1
  if [[ ! "$index" =~ ^[1-9][0-9]{0,8}$ ]] || ((index > ${#backups[@]})); then error '编号无效。'; return 1; fi
  confirm '这会检查端口冲突并恢复单机脚本自身的规则和配置，确认继续？' || return 0
  backend restore "${backups[index-1]}"
}

main() {
  [[ "$(id -u)" == 0 ]] || { error "请使用 sudo bash $SELF_PATH 运行。"; return 1; }
  [[ $# == 0 || ( $# == 1 && "$1" == --self-check ) ]] || { error '仅支持交互运行或 --self-check。'; return 1; }
  umask 077
  check_dependencies || { error '依赖准备失败，未进入转发操作。'; return 1; }
  if [[ "${1:-}" == --self-check ]]; then backend refresh; return $?; fi
  local choice
  while true; do
    printf '\nIPv4 转发管理 v%s\n' "$VERSION"
    printf '1. 查看转发\n2. 添加转发\n3. 修改转发\n4. 删除转发\n5. 导出配置\n6. 导入配置\n7. 恢复备份\n0. 退出\n'
    read -rp '请选择：' choice || return 0
    case "$choice" in
      1) list_forwards ;;
      2) edit_forward add ;;
      3) edit_forward update ;;
      4) delete_forward ;;
      5) export_config ;;
      6) import_config ;;
      7) restore_config ;;
      0|q) return 0 ;;
      *) error '无效选项。' ;;
    esac
  done
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
