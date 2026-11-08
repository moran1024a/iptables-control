package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"reflect"
	"sort"
	"strconv"
	"strings"
)

func obj(kind string, v M) M        { return M{kind: v} }
func op(action, kind string, v M) M { return M{action: obj(kind, v)} }
func match(left, right any) M       { return M{"match": M{"op": "==", "left": left, "right": right}} }
func meta(key string) M             { return obj("meta", M{"key": key}) }
func ct(key, dir string) M {
	m := M{"key": key}
	if dir != "" {
		m["dir"] = dir
	}
	if key == "saddr" || key == "daddr" {
		m["key"] = "ip " + key
	}
	return obj("ct", m)
}
func payload(proto, field string) M { return obj("payload", M{"protocol": proto, "field": field}) }
func counterName(r Rule, p, dir, epoch string) string {
	return fmt.Sprintf("r%d_%s_%s_%s", r.ID, p, dir, epoch)
}
func connMatch(r Rule, p string) []any {
	expr := []any{match(meta("l4proto"), p), M{"match": M{"op": "in", "left": ct("status", ""), "right": "dnat"}}, match(ct("proto-dst", "original"), r.Port), match(ct("saddr", "reply"), r.IP), match(ct("proto-src", "reply"), r.TargetPort)}
	if r.Route != "" {
		route, _ := parseRoute(r.Route)
		if route.In.enabled() {
			expr = append(expr, match(ct("daddr", "original"), route.In.Local))
		}
	}
	return expr
}

func (a *Agent) nftState() (M, bool, error) {
	d, exists, err := readNFTState()
	if err == nil && exists {
		err = a.checkNFTOwner(d)
	}
	return d, exists, err
}

func readNFTState() (M, bool, error) {
	b, e := run(nil, "nft", "-j", "list", "tables")
	if e != nil {
		return nil, false, e
	}
	d, e := decodeJSON(b)
	if e != nil {
		return nil, false, e
	}
	found := false
	for _, x := range array(d["nftables"]) {
		m, _ := x.(map[string]any)
		t, _ := m["table"].(map[string]any)
		if t["family"] == "ip" && t["name"] == tableName {
			found = true
		}
	}
	if !found {
		return M{"nftables": []any{}}, false, nil
	}
	b, e = run(nil, "nft", "-j", "list", "table", "ip", tableName)
	if e != nil {
		return nil, false, e
	}
	d, e = decodeJSON(b)
	if e != nil {
		return nil, false, e
	}
	return d, true, nil
}

// Empty, unhooked chains work on nft 1.0.x, whose JSON frontend does not
// preserve table comments until 1.0.8. Keep accepting comments from old agents.
func (a *Agent) ownerChain() string { return "owner_" + a.installation }

func (a *Agent) checkNFTOwner(d M) error {
	owned := false
	for _, x := range array(d["nftables"]) {
		m, _ := x.(map[string]any)
		if t, ok := m["table"].(map[string]any); ok && t["name"] == tableName {
			if comment, _ := t["comment"].(string); comment != "" {
				if comment != "ictl:"+a.installation {
					return errors.New("同名 nft 表不属于本节点安装")
				}
				owned = true
			}
		}
		if ch, ok := m["chain"].(map[string]any); ok {
			if name, _ := ch["name"].(string); strings.HasPrefix(name, "owner_") {
				if name != a.ownerChain() || ch["hook"] != nil {
					return errors.New("同名 nft 表不属于本节点安装")
				}
				owned = true
			}
		}
	}
	if !owned {
		return errors.New("同名 nft 表缺少本节点归属标记；请保留失败事务并重新运行新版安装器")
	}
	return nil
}

// Only repair the empty table left by the pre-1.0.8 JSON comment bug, and only
// when a failed local installation records that exact empty configuration.
func (a *Agent) recoverEmptyTable() error {
	return a.locked(func() error {
		d, exists, err := readNFTState()
		if err != nil || !exists {
			return err
		}
		if a.checkNFTOwner(d) == nil {
			return nil
		}
		journal, err := os.ReadFile(a.path("switch-work/transaction"))
		if err != nil {
			return err
		}
		fields := map[string]string{}
		for _, line := range strings.Split(string(journal), "\n") {
			k, v, ok := strings.Cut(line, "=")
			if ok {
				fields[k] = v
			}
		}
		if fields["PHASE"] != "failed" || fields["TARGET"] != "active" || fields["HASH"] != "e3b0c44298fc" ||
			(fields["FROM"] != "none" && fields["FROM"] != "legacy" && fields["FROM"] != "passive") || !emptyLegacyTable(d) {
			return errors.New("无法自动恢复：同名 nft 表不是已确认的空配置安装残留，请保留现场")
		}
		// Old JSON output hides even comments written through the text frontend.
		// Do not claim an explicitly labelled table belonging to another tool.
		text, err := run(nil, "nft", "-n", "list", "table", "ip", tableName)
		if err != nil {
			return err
		}
		if strings.Contains(string(text), "comment ") {
			return errors.New("无法自动恢复：同名 nft 表已有注释标记，请保留现场")
		}
		// Claim it atomically instead of deleting it. The normal apply or rollback
		// can now handle it using the same ownership checks as every other table.
		return executeNFT(M{"nftables": []any{op("add", "chain", M{"family": "ip", "table": tableName, "name": a.ownerChain()})}}, false)
	})
}

func emptyLegacyTable(d M) bool {
	want := map[string]struct {
		kind, hook string
		priority   int64
	}{"pre": {"nat", "prerouting", -105}, "post": {"nat", "postrouting", 105}, "forward": {"filter", "forward", 10}}
	tables := 0
	for _, x := range array(d["nftables"]) {
		m, ok := x.(map[string]any)
		if !ok || len(m) != 1 {
			return false
		}
		for kind, raw := range m {
			v, ok := raw.(map[string]any)
			if !ok {
				return false
			}
			switch kind {
			case "metainfo":
			case "table":
				if v["family"] != "ip" || v["name"] != tableName || v["comment"] != nil || v["flags"] != nil {
					return false
				}
				tables++
			case "chain":
				name, _ := v["name"].(string)
				ch, ok := want[name]
				if !ok || v["family"] != "ip" || v["table"] != tableName || v["type"] != ch.kind ||
					v["hook"] != ch.hook || fmt.Sprint(v["prio"]) != fmt.Sprint(ch.priority) || v["policy"] != "accept" || v["comment"] != nil {
					return false
				}
				delete(want, name)
			default:
				return false // Rules, counters, sets and other objects are not empty.
			}
		}
	}
	return tables == 1 && len(want) == 0
}
func array(v any) []any { a, _ := v.([]any); return a }
func (a *Agent) transaction(c Config, current M, exists bool) M {
	commands := []any{}
	chains := map[string]bool{}
	counters := map[string]bool{}
	for _, x := range array(current["nftables"]) {
		m, _ := x.(map[string]any)
		if v, ok := m["chain"].(map[string]any); ok {
			chains[fmt.Sprint(v["name"])] = true
		}
		if v, ok := m["counter"].(map[string]any); ok {
			counters[fmt.Sprint(v["name"])] = true
		}
	}
	if !exists {
		commands = append(commands, op("add", "table", M{"family": "ip", "name": tableName, "comment": "ictl:" + a.installation}))
	}
	if !chains[a.ownerChain()] {
		commands = append(commands, op("add", "chain", M{"family": "ip", "table": tableName, "name": a.ownerChain()}))
	}
	for _, ch := range []struct {
		name, kind, hook string
		priority         int
	}{{"pre", "nat", "prerouting", -105}, {"post", "nat", "postrouting", 105}, {"forward", "filter", "forward", 10}, {"route", "filter", "prerouting", -90}} {
		base := M{"family": "ip", "table": tableName, "name": ch.name}
		if chains[ch.name] {
			commands = append(commands, op("flush", "chain", base))
		} else {
			base["type"] = ch.kind
			base["hook"] = ch.hook
			base["prio"] = ch.priority
			base["policy"] = "accept"
			commands = append(commands, op("add", "chain", base))
		}
	}
	addRule := func(chain string, expr []any) {
		commands = append(commands, op("add", "rule", M{"family": "ip", "table": tableName, "chain": chain, "expr": expr}))
	}
	for _, r := range c.Rules {
		for _, p := range protocols(r) {
			route, _ := parseRoute(r.Route)
			local := match(obj("fib", M{"result": "type", "flags": []string{"daddr"}}), "local")
			expr := []any{local, match(payload(p, "dport"), r.Port)}
			if route.Listen != "" {
				// Unbound advertised addresses may arrive unchanged (routed IP)
				// or translated to a local IP by the provider. Ports are reserved
				// node-wide; unrelated transit destinations must still pass through.
				exact := []any{match(payload("ip", "daddr"), r.ListenIP), match(payload(p, "dport"), r.Port)}
				addRule("pre", append(exact, obj("dnat", M{"addr": r.IP, "port": r.TargetPort})))
				if r.LocalListen {
					continue
				}
			} else if route.In.enabled() {
				expr = append(expr, match(payload("ip", "daddr"), route.In.Local))
			}
			addRule("pre", append(expr, obj("dnat", M{"addr": r.IP, "port": r.TargetPort})))
		}
	}
	seen := map[string]bool{}
	for _, r := range append(append([]Rule{}, c.Rules...), c.Retired...) {
		for _, p := range protocols(r) {
			key := fmt.Sprintf("%d/%s/%d/%s/%d", r.ID, p, r.Port, r.IP, r.TargetPort)
			if seen[key] {
				continue
			}
			seen[key] = true
			nat := M{"masquerade": nil}
			if r.Route != "" {
				route, _ := parseRoute(r.Route)
				if route.Out.enabled() {
					nat = obj("snat", M{"addr": route.Out.Local})
				}
				for dir, profile := range map[string]routeProfile{"original": route.Out, "reply": route.In} {
					if !profile.enabled() {
						continue
					}
					expr := append(connMatch(r, p), match(ct("direction", ""), dir), obj("mangle", M{"key": meta("mark"), "value": profile.mark()}))
					addRule("route", expr)
				}
			}
			addRule("post", append(connMatch(r, p), nat))
			for _, dir := range []string{"up", "down"} {
				name := counterName(r, p, dir, c.Epoch)
				if !counters[name] {
					commands = append(commands, op("add", "counter", M{"family": "ip", "table": tableName, "name": name}))
					counters[name] = true
				}
				direction := "original"
				if dir == "down" {
					direction = "reply"
				}
				addRule("forward", append(connMatch(r, p), match(ct("direction", ""), direction), M{"counter": name}, M{"accept": nil}))
			}
		}
	}
	return M{"nftables": commands}
}
func executeNFT(data M, check bool) error {
	b, e := json.Marshal(data)
	if e != nil {
		return e
	}
	args := []string{"-j", "-f", "-"}
	if check {
		args = append([]string{"-c"}, args...)
	}
	_, e = run(b, "nft", args...)
	return e
}
func sameRule(a, b Rule) bool {
	return a.Route == b.Route && a.ID == b.ID && a.Protocol == b.Protocol && a.Port == b.Port && a.IP == b.IP && a.TargetPort == b.TargetPort
}
func (a *Agent) projection(c Config) []byte {
	rows := append(append([]Rule{}, c.Rules...), c.Retired...)
	seen := map[string]bool{}
	lines := []string{"v=" + c.Hash}
	for _, r := range rows {
		line := fmt.Sprintf("%d %s %d %s %d", r.ID, r.Protocol, r.Port, r.Host, r.TargetPort)
		if r.Route != "" {
			line += " " + r.Route
		}
		if !seen[line] {
			lines = append(lines, line)
			seen[line] = true
		}
	}
	return []byte(strings.Join(lines, "\n") + "\n")
}
func (a *Agent) apply(c Config) error {
	// Resolution must not overwrite the currently applied addresses or query snapshots.
	c.Rules = append([]Rule(nil), c.Rules...)
	if e := validateConfig(c); e != nil {
		return e
	}
	if e := a.resolveCached(&c); e != nil {
		return e
	}
	if e := prepareIngress(&c); e != nil {
		return e
	}
	return a.locked(func() (result error) {
		state, exists, e := a.nftState()
		if e != nil {
			return e
		}
		if e = a.checkConflicts(c); e != nil {
			return e
		}
		old := a.config
		c.Epoch = old.Epoch
		if c.Epoch == "" || !exists {
			c.Epoch = randomID()
		}
		if !exists && old.Epoch != "" {
			a.stateError = "统计缺口：内核计数器已重建"
		}
		c.Retired = append([]Rule{}, old.Retired...)
		for _, r := range old.Rules {
			found, logical := false, false
			for _, n := range c.Rules {
				found = found || sameRule(r, n)
				logical = logical || r.ID == n.ID
			}
			if !found && logical {
				c.Retired = append(c.Retired, r)
			}
		}
		// Garbage collection uses actual conntrack state. Failure keeps the reservation.
		kept := []Rule{}
		for _, r := range c.Retired {
			logical := false
			for _, n := range c.Rules {
				logical = logical || r.ID == n.ID
			}
			if !logical {
				continue
			}
			live, err := hasConnections(r)
			if err != nil || live {
				kept = append(kept, r)
			}
		}
		c.Retired = kept
		if len(profiles(c)) > 0 || len(profiles(old)) > 0 {
			defer func() {
				if result != nil {
					if err := a.prepareRoutes(old); err != nil {
						result = fmt.Errorf("%w; 恢复旧路由失败: %v", result, err)
					} else if err := a.pruneRoutes(old); err != nil {
						result = fmt.Errorf("%w; 清理新路由失败: %v", result, err)
					}
				}
			}()
		}
		if len(profiles(c)) > 0 {
			if a.root == "/var/lib/ictl" {
				if e = os.WriteFile("/proc/sys/net/ipv4/ip_forward", []byte("1\n"), 0644); e != nil {
					return e
				}
			}
			if e = a.prepareRoutes(c); e != nil {
				return e
			}
		}
		tx := a.transaction(c, state, exists)
		if e = executeNFT(tx, true); e != nil {
			return e
		}
		if e = atomicJSON(a.path("active/pending.json"), c); e != nil {
			return e
		}
		union := c
		union.Retired = append(append([]Rule{}, c.Retired...), old.Rules...)
		if e = atomicBytes(a.path("config"), a.projection(union), 0600); e != nil {
			return e
		}
		if e = executeNFT(tx, false); e != nil {
			_ = atomicBytes(a.path("config"), a.projection(old), 0600)
			return e
		}
		defer func() {
			if result == nil {
				return
			}
			current, present, err := a.nftState()
			if err == nil && present {
				if exists {
					err = executeNFT(a.transaction(old, current, true), false)
				} else {
					err = executeNFT(M{"nftables": []any{op("delete", "table", M{"family": "ip", "name": tableName})}}, false)
				}
			}
			if err != nil {
				result = fmt.Errorf("%w; 回退失败: %v", result, err)
			}
			a.config = old
			_ = atomicJSON(a.path("active/config.json"), old)
			_ = atomicBytes(a.path("config"), a.projection(old), 0600)
		}()
		// Only this host's forwarding switch is enabled; no global firewall rules are replaced.
		if a.root == "/var/lib/ictl" {
			if e = os.WriteFile("/proc/sys/net/ipv4/ip_forward", []byte("1\n"), 0644); e != nil {
				return e
			}
			if e = atomicBytes("/etc/sysctl.d/99-ictl.conf", []byte("net.ipv4.ip_forward=1\n"), 0644); e != nil {
				return e
			}
		}
		verify, ok, e := a.nftState()
		if e != nil || !ok {
			return fmt.Errorf("规则应用后无法读取: %v", e)
		}
		if !sameNFT(tx, verify) {
			return errors.New("规则应用后核对失败")
		}
		for _, r := range append(append([]Rule{}, old.Rules...), old.Retired...) {
			found := false
			for _, n := range c.Rules {
				found = found || n.ID == r.ID
			}
			if !found {
				if e = deleteConnections(r); e != nil {
					return e
				}
			}
		}
		if e = atomicJSON(a.path("active/config.json"), c); e != nil {
			return e
		}
		a.config = c
		if e = atomicBytes(a.path("config"), a.projection(c), 0600); e != nil {
			return e
		}
		if e = a.sample(); e != nil {
			return e
		}
		// Unreferenced counters are stable after the rule transaction. Persist first,
		// then remove only those objects; archived usage remains until server ACK.
		needed := map[string]bool{}
		for _, r := range append(append([]Rule{}, c.Rules...), c.Retired...) {
			for _, p := range protocols(r) {
				for _, dir := range []string{"up", "down"} {
					needed[counterName(r, p, dir, c.Epoch)] = true
				}
			}
		}
		deletes := []any{}
		for _, item := range array(verify["nftables"]) {
			m, _ := item.(map[string]any)
			counter, _ := m["counter"].(map[string]any)
			name, _ := counter["name"].(string)
			if counterRE.MatchString(name) && !needed[name] {
				deletes = append(deletes, op("delete", "counter", M{"family": "ip", "table": tableName, "name": name}))
			}
		}
		if len(deletes) > 0 {
			if e = executeNFT(M{"nftables": deletes}, false); e != nil {
				return e
			}
			for _, u := range a.usage {
				if !needed[counterName(Rule{ID: u.RuleID}, u.Protocol, "up", u.KernelEpoch)] {
					u.Active = false
				}
			}
			if e = atomicJSON(a.path("active/traffic.json"), a.usage); e != nil {
				return e
			}
		}
		if e = a.pruneRoutes(c); e != nil {
			return e
		}
		return removeIfExists(a.path("active/pending.json"))
	})
}
func sameNFT(tx, current M) bool {
	expected, actual := []any{}, []any{}
	for _, x := range array(tx["nftables"]) {
		m := x.(M)
		add, _ := m["add"].(M)
		if r, ok := add["rule"].(M); ok {
			expected = append(expected, M{"chain": r["chain"], "expr": r["expr"]})
		}
	}
	for _, x := range array(current["nftables"]) {
		m, _ := x.(map[string]any)
		if r, ok := m["rule"].(map[string]any); ok {
			actual = append(actual, M{"chain": r["chain"], "expr": r["expr"]})
		}
	}
	// nft emits rules grouped by chain; compare order inside each chain.
	sort.SliceStable(expected, func(i, j int) bool { return expected[i].(M)["chain"].(string) < expected[j].(M)["chain"].(string) })
	sort.SliceStable(actual, func(i, j int) bool { return actual[i].(M)["chain"].(string) < actual[j].(M)["chain"].(string) })
	// Round-trip normalizes native numeric types. Kernel may normalize fib flags and NAT family.
	a, _ := json.Marshal(expected)
	b, _ := json.Marshal(actual)
	var av, bv any
	_ = json.Unmarshal(a, &av)
	_ = json.Unmarshal(b, &bv)
	return reflect.DeepEqual(normalizeNFT(av), normalizeNFT(bv))
}
func normalizeNFT(v any) any {
	switch x := v.(type) {
	case []any:
		for i := range x {
			x[i] = normalizeNFT(x[i])
		}
	case map[string]any:
		delete(x, "family")
		for k, val := range x {
			x[k] = normalizeNFT(val)
		}
	}
	return v
}
func hasConnections(r Rule) (bool, error) {
	for _, p := range protocols(r) {
		b, e := run(nil, "conntrack", "-L", "-f", "ipv4", "-p", p, "--orig-port-dst", strconv.Itoa(r.Port), "--reply-src", r.IP, "--reply-port-src", strconv.Itoa(r.TargetPort))
		if e != nil {
			return true, e
		}
		for _, line := range strings.Split(string(b), "\n") {
			if strings.HasPrefix(line, p+" ") {
				return true, nil
			}
		}
	}
	return false, nil
}
func deleteConnections(r Rule) error {
	for _, p := range protocols(r) {
		_, e := run(nil, "conntrack", "-D", "-f", "ipv4", "-p", p, "--orig-port-dst", strconv.Itoa(r.Port), "--reply-src", r.IP, "--reply-port-src", strconv.Itoa(r.TargetPort), "--dst-nat")
		if e != nil && !strings.Contains(e.Error(), "0 flow entries") {
			return e
		}
	}
	return nil
}
func (a *Agent) heldPorts() []int {
	set := map[int]bool{}
	for _, r := range a.config.Retired {
		set[r.Port] = true
	}
	for _, r := range a.config.Rules {
		delete(set, r.Port)
	}
	out := []int{}
	for p := range set {
		out = append(out, p)
	}
	sort.Ints(out)
	return out
}
func (a *Agent) clear() error {
	return a.locked(func() error {
		state, exists, e := a.nftState()
		if e != nil {
			return e
		}
		if !exists {
			return a.pruneRoutes(Config{})
		}
		cmds := []any{}
		for _, ch := range []string{"pre", "post", "forward"} {
			cmds = append(cmds, op("flush", "chain", M{"family": "ip", "table": tableName, "name": ch}))
		}
		for _, item := range array(state["nftables"]) {
			m, _ := item.(map[string]any)
			ch, _ := m["chain"].(map[string]any)
			if ch["name"] == "route" {
				cmds = append(cmds, op("flush", "chain", M{"family": "ip", "table": tableName, "name": "route"}))
			}
		}
		if e = executeNFT(M{"nftables": cmds}, false); e != nil {
			return e
		}
		if e = a.sample(); e != nil {
			return e
		}
		for _, u := range a.usage {
			u.Active = false
		}
		if e = atomicJSON(a.path("active/traffic.json"), a.usage); e != nil {
			return e
		}
		for _, r := range append(append([]Rule{}, a.config.Rules...), a.config.Retired...) {
			if net.ParseIP(r.IP) != nil {
				if e = deleteConnections(r); e != nil {
					return e
				}
			}
		}
		_ = state
		if e = executeNFT(M{"nftables": []any{op("delete", "table", M{"family": "ip", "name": tableName})}}, false); e != nil {
			return e
		}
		return a.pruneRoutes(Config{})
	})
}
func removeIfExists(path string) error {
	e := os.Remove(path)
	if os.IsNotExist(e) {
		return nil
	}
	return e
}

func (a *Agent) resolveCached(c *Config) error {
	if !a.offline {
		return a.resolve(c)
	}
	for _, r := range c.Rules {
		if ip := net.ParseIP(r.IP); ip == nil || ip.To4() == nil {
			return errors.New("缺少可用的离线地址缓存")
		}
		route, _ := parseRoute(r.Route)
		if route.Listen != "" && net.ParseIP(r.ListenIP).To4() == nil {
			return errors.New("缺少接收地址的离线缓存")
		}
	}
	return nil
}

func prepareIngress(c *Config) error {
	needed := false
	for _, r := range c.Rules {
		route, _ := parseRoute(r.Route)
		needed = needed || route.Listen != ""
	}
	if !needed {
		return nil
	}
	addresses, err := net.InterfaceAddrs()
	if err != nil {
		return fmt.Errorf("读取本机地址: %w", err)
	}
	local := map[string]bool{}
	for _, address := range addresses {
		if ip, _, err := net.ParseCIDR(address.String()); err == nil {
			local[ip.String()] = true
		}
	}
	for i := range c.Rules {
		c.Rules[i].LocalListen = local[c.Rules[i].ListenIP]
	}
	return nil
}
func (a *Agent) restore() error {
	a.offline = true
	defer func() { a.offline = false }()
	return a.apply(a.config)
}
