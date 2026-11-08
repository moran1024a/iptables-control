package main

import (
	"fmt"
	"net"
	"os"
	"os/exec"
	"strconv"
	"strings"
)

func portIn(value any, port int, sets map[string]any) bool {
	switch x := value.(type) {
	case string:
		if strings.HasPrefix(x, "@") {
			v, ok := sets[x[1:]]
			return !ok || portIn(v, port, sets)
		}
		n, e := strconv.Atoi(x)
		return e != nil || n == port
	case []any:
		for _, v := range x {
			if portIn(v, port, sets) {
				return true
			}
		}
		return false
	case map[string]any:
		if v, ok := x["set"]; ok {
			return portIn(v, port, sets)
		}
		if v, ok := x["range"]; ok {
			a := array(v)
			return len(a) != 2 || port >= int(number(a[0])) && port <= int(number(a[1]))
		}
		if v, ok := x["elem"]; ok {
			return portIn(v, port, sets)
		}
		if v, ok := x["val"]; ok {
			return portIn(v, port, sets)
		}
		return true
	default:
		return int(number(value)) == port
	}
}
func protoIn(v any, p string) bool {
	if list, ok := v.(map[string]any); ok {
		return protoIn(list["set"], p)
	}
	if list, ok := v.([]any); ok {
		for _, x := range list {
			if protoIn(x, p) {
				return true
			}
		}
		return false
	}
	s := fmt.Sprint(v)
	return s == p || (p == "tcp" && s == "6") || (p == "udp" && s == "17")
}
func rulePossible(expr []any, p string, port int, sets map[string]any) bool {
	for _, x := range expr {
		m, _ := x.(map[string]any)
		v, ok := m["match"].(map[string]any)
		if !ok {
			continue
		}
		left, _ := v["left"].(map[string]any)
		right := v["right"]
		if name, ok := right.(string); ok && strings.HasPrefix(name, "@") {
			if value, found := sets[name[1:]]; found {
				right = value
			} else {
				continue
			}
		}
		matches, known := true, false
		if meta, ok := left["meta"].(map[string]any); ok && meta["key"] == "l4proto" {
			matches = protoIn(right, p)
			known = true
		}
		if pl, ok := left["payload"].(map[string]any); ok {
			if pl["field"] == "dport" {
				if pr := pl["protocol"]; pr == "tcp" || pr == "udp" {
					if pr != p {
						return false
					}
				}
				matches = portIn(right, port, sets)
				known = true
			}
			if pl["protocol"] == "ip" && pl["field"] == "protocol" {
				matches = protoIn(right, p)
				known = true
			}
		}
		if known {
			switch v["op"] {
			case "!=":
				matches = !matches
			case "==", "in":
			default:
				continue
			}
			if !matches {
				return false
			}
		}
	}
	return true
}

type nativePortRules struct {
	chains map[string][]map[string]any
	sets   map[string]map[string]any
	starts []string
}

func parseNativePorts(doc M) nativePortRules {
	chains := map[string][]map[string]any{}
	sets := map[string]map[string]any{}
	starts := []string{}
	for _, x := range array(doc["nftables"]) {
		m, _ := x.(map[string]any)
		for typ, raw := range m {
			v, ok := raw.(map[string]any)
			if !ok {
				continue
			}
			family := fmt.Sprint(v["family"])
			if family != "ip" && family != "inet" {
				continue
			}
			table := fmt.Sprint(v["table"])
			if table == tableName {
				continue
			}
			base := family + "/" + table + "/"
			switch typ {
			case "chain":
				if v["hook"] == "prerouting" {
					starts = append(starts, base+fmt.Sprint(v["name"]))
				}
			case "rule":
				key := base + fmt.Sprint(v["chain"])
				chains[key] = append(chains[key], v)
			case "set", "map":
				if sets[base] == nil {
					sets[base] = map[string]any{}
				}
				sets[base][fmt.Sprint(v["name"])] = v["elem"]
			}
		}
	}
	return nativePortRules{chains, sets, starts}
}
func (rules nativePortRules) conflict(p string, port int) string {
	chains, sets, starts := rules.chains, rules.sets, rules.starts
	var visit func(string, map[string]bool) string
	visit = func(key string, seen map[string]bool) string {
		if seen[key] {
			return ""
		}
		seen[key] = true
		parts := strings.Split(key, "/")
		base := strings.Join(parts[:2], "/") + "/"
		for _, r := range chains[key] {
			expr := array(r["expr"])
			if !rulePossible(expr, p, port, sets[base]) {
				continue
			}
			for _, x := range expr {
				m, _ := x.(map[string]any)
				for k, v := range m {
					switch k {
					case "dnat", "redirect", "tproxy":
						return key
					case "jump", "goto":
						t, _ := v.(map[string]any)
						if conflict := visit(base+fmt.Sprint(t["target"]), seen); conflict != "" {
							return conflict
						}
					case "vmap":
						return key + " (动态映射需人工确认)"
					}
				}
			}
		}
		return ""
	}
	for _, start := range starts {
		if key := visit(start, map[string]bool{}); key != "" {
			return key
		}
	}
	return ""
}
func socketPort(line string) (string, int) {
	f := strings.Fields(line)
	if len(f) < 5 {
		return "", 0
	}
	host, port, e := net.SplitHostPort(f[3])
	if e != nil {
		return "", 0
	}
	n, _ := strconv.Atoi(port)
	return host, n
}
func (a *Agent) checkConflicts(c Config) error {
	if len(c.Rules) == 0 {
		return nil
	}
	b, e := a.prepareCommand(nil, "nft", "-j", "list", "ruleset")
	if e != nil {
		return fmt.Errorf("端口检查失败：%w", e)
	}
	native, e := decodeJSON(b)
	if e != nil {
		return e
	}
	return a.checkConflictsWith(c, native)
}

// Reuse only the snapshot from this locked preparation, never a cached ruleset.
func (a *Agent) checkConflictsWith(c Config, native M) error {
	a.quality = ""
	for _, item := range array(native["nftables"]) {
		if m, ok := item.(map[string]any); ok && m["flowtable"] != nil {
			a.quality = "存在 flowtable 卸载，转发计数可能不完整"
		}
	}
	sockets := map[string]map[int]bool{}
	for _, p := range []string{"tcp", "udp"} {
		flag := "-lnt"
		if p == "udp" {
			flag = "-aun"
		}
		data, e := a.prepareCommand(nil, "ss", "-H", flag)
		if e != nil {
			return fmt.Errorf("端口检查失败：%w", e)
		}
		sockets[p] = map[int]bool{}
		for _, line := range strings.Split(string(data), "\n") {
			host, port := socketPort(line)
			ip := net.ParseIP(host)
			if host == "*" || host == "::" || ip != nil && ip.To4() != nil {
				sockets[p][port] = true
			}
		}
	}
	peer := "/root/iptables-forward-conf/managed-forwards.conf"
	if a.root != "/var/lib/ictl" {
		peer = a.path("peer.conf")
	}
	reserved := map[string]bool{}
	data, e := os.ReadFile(peer)
	if e != nil && !os.IsNotExist(e) {
		return e
	}
	for _, line := range strings.Split(string(data), "\n") {
		f := strings.Fields(line)
		if len(f) == 0 || strings.HasPrefix(f[0], "#") {
			continue
		}
		if len(f) != 5 && len(f) != 7 {
			return fmt.Errorf("端口检查失败：ipt.sh 配置损坏")
		}
		// v3: id incoming host target protocol comment ip; v2 legacy: incoming host target protocol comment.
		pos := 0
		if len(f) == 7 {
			pos = 1
		}
		port, err := strconv.Atoi(f[pos])
		if err != nil || port < 1 || port > 65535 {
			return fmt.Errorf("端口检查失败：ipt.sh 端口无效")
		}
		proto := f[pos+3]
		if proto != "tcp" && proto != "udp" && proto != "both" {
			return fmt.Errorf("端口检查失败：ipt.sh 协议无效")
		}
		for _, p := range []string{"tcp", "udp"} {
			if proto == p || proto == "both" {
				reserved[fmt.Sprintf("%s/%d", p, port)] = true
			}
		}
	}
	var legacy string
	if _, err := exec.LookPath("iptables-save"); err == nil {
		b, e := a.prepareCommand(nil, "iptables-save")
		if e != nil {
			return fmt.Errorf("端口检查失败：%w", e)
		}
		legacy = string(b)
	}
	nativeRules, legacyRules := parseNativePorts(native), parseLegacyPorts(legacy)
	for _, r := range c.Rules {
		for _, p := range protocols(r) {
			key := fmt.Sprintf("%s/%d", p, r.Port)
			if reserved[key] {
				return fmt.Errorf("端口冲突：%s 被 ipt.sh 保存配置占用", key)
			}
			if sockets[p][r.Port] {
				return fmt.Errorf("端口冲突：%s 被本机套接字占用", key)
			}
			if where := nativeRules.conflict(p, r.Port); where != "" {
				return fmt.Errorf("端口冲突：%s 与 nft %s 重叠", key, where)
			}
			if where := legacyRules.conflict(p, r.Port); where != "" {
				return fmt.Errorf("端口冲突：%s 与 iptables %s 重叠", key, where)
			}
		}
	}
	return nil
}
func shellWords(line string) []string {
	out := []string{}
	var b strings.Builder
	var quote rune
	escape := false
	for _, c := range line {
		if escape {
			b.WriteRune(c)
			escape = false
			continue
		}
		if c == '\\' {
			escape = true
			continue
		}
		if quote != 0 {
			if c == quote {
				quote = 0
			} else {
				b.WriteRune(c)
			}
			continue
		}
		if c == '\'' || c == '"' {
			quote = c
			continue
		}
		if c == ' ' || c == '\t' {
			if b.Len() > 0 {
				out = append(out, b.String())
				b.Reset()
			}
		} else {
			b.WriteRune(c)
		}
	}
	if b.Len() > 0 {
		out = append(out, b.String())
	}
	return out
}

type legacyPortRules map[string][][]string

func parseLegacyPorts(text string) legacyPortRules {
	chains := map[string][][]string{}
	table := ""
	for _, line := range strings.Split(text, "\n") {
		if strings.HasPrefix(line, "*") {
			table = line[1:]
			continue
		}
		f := shellWords(line)
		if len(f) > 2 && f[0] == "-A" && (table == "nat" || table == "mangle") {
			key := table + "/" + f[1]
			chains[key] = append(chains[key], f[2:])
		}
	}
	return chains
}
func (chains legacyPortRules) conflict(p string, port int) string {
	var visit func(string, map[string]bool) string
	visit = func(key string, seen map[string]bool) string {
		if seen[key] {
			return ""
		}
		seen[key] = true
		table, _, _ := strings.Cut(key, "/")
		for _, f := range chains[key] {
			possible := true
			target := ""
			neg := false
			for i := 0; i < len(f); i++ {
				v := f[i]
				if v == "!" {
					neg = true
					continue
				}
				if i+1 >= len(f) {
					continue
				}
				next := f[i+1]
				switch v {
				case "--comment":
					i++
				case "-p", "--protocol":
					ok := next == p || (next == "6" && p == "tcp") || (next == "17" && p == "udp") || next == "all"
					if neg {
						ok = !ok
					}
					possible = possible && ok
					neg = false
					i++
				case "--dport", "--destination-port", "--dports":
					ok := false
					for _, s := range strings.Split(next, ",") {
						parts := strings.Split(strings.ReplaceAll(s, "-", ":"), ":")
						lo, _ := strconv.Atoi(parts[0])
						hi := lo
						if len(parts) > 1 {
							hi, _ = strconv.Atoi(parts[1])
						}
						ok = ok || port >= lo && port <= hi
					}
					if neg {
						ok = !ok
					}
					possible = possible && ok
					neg = false
					i++
				case "-j", "-g", "--jump", "--goto":
					target = next
					i++
				}
			}
			if !possible {
				continue
			}
			switch target {
			case "DNAT", "REDIRECT", "TPROXY":
				return key
			case "ACCEPT", "RETURN", "DROP", "REJECT", "":
			default:
				if got := visit(table+"/"+target, seen); got != "" {
					return got
				}
			}
		}
		return ""
	}
	for _, t := range []string{"nat", "mangle"} {
		if got := visit(t+"/PREROUTING", map[string]bool{}); got != "" {
			return got
		}
	}
	return ""
}
