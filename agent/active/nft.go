package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"reflect"
	"slices"
	"sort"
	"strconv"
	"strings"
	"time"
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
	d, exists, err := readNFTWith(func(args ...string) ([]byte, error) { return a.prepareCommand(nil, "nft", args...) })
	if err == nil && exists {
		err = a.checkNFTOwner(d)
	}
	return d, exists, err
}

// The full ruleset is one atomic read shared by ownership and port checks.
// Filter by family as well as table name: an inet/ip6 table may share the name.
func ownedNFTState(doc M) (M, bool) {
	rows := []any{}
	exists := false
	for _, item := range array(doc["nftables"]) {
		objects, _ := item.(map[string]any)
		for kind, raw := range objects {
			v, _ := raw.(map[string]any)
			if v["family"] != "ip" {
				continue
			}
			if kind == "table" && v["name"] == tableName {
				exists = true
				rows = append(rows, item)
			} else if kind != "table" && v["table"] == tableName {
				rows = append(rows, item)
			}
		}
	}
	return M{"nftables": rows}, exists
}

func (a *Agent) prepareNFT(c Config) (M, bool, error) {
	if len(c.Rules) == 0 {
		return a.nftState()
	}
	b, err := a.prepareCommand(nil, "nft", "-j", "list", "ruleset")
	if err != nil {
		return nil, false, err
	}
	full, err := decodeJSON(b)
	if err != nil {
		return nil, false, err
	}
	state, exists := ownedNFTState(full)
	if exists {
		if err = a.checkNFTOwner(state); err != nil {
			return nil, false, err
		}
	}
	return state, exists, a.checkConflictsWith(c, full)
}

func readNFTState() (M, bool, error) {
	return readNFTWith(func(args ...string) ([]byte, error) { return run(nil, "nft", args...) })
}

func readNFTWith(read func(...string) ([]byte, error)) (M, bool, error) {
	b, e := read("-j", "list", "tables")
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
	b, e = read("-j", "list", "table", "ip", tableName)
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

// Immutable after compilation, so telemetry snapshots can share the plan.
// Match the effective rules, not the server hash: DNS and local IPs can change.
type nftRulePlan struct {
	config     Config
	commands   []any
	normalized any
}

func (a *Agent) rulePlan(c Config) *nftRulePlan {
	p := a.nftPlan
	if p != nil && p.config.Epoch == c.Epoch && slices.Equal(p.config.Rules, c.Rules) && slices.Equal(p.config.Retired, c.Retired) {
		return p
	}
	c.Rules = slices.Clone(c.Rules)
	c.Retired = slices.Clone(c.Retired)
	p = &nftRulePlan{config: c, commands: compileNFTRules(c)}
	rules := []any{}
	for _, item := range p.commands {
		add := item.(M)["add"].(M)
		if r, ok := add["rule"].(M); ok {
			rules = append(rules, M{"chain": r["chain"], "expr": r["expr"]})
		}
	}
	p.normalized = normalizeNFTRules(rules)
	a.nftPlan = p
	return p
}

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
	for _, item := range a.rulePlan(c).commands {
		add := item.(M)["add"].(M)
		if counter, ok := add["counter"].(M); ok && counters[counter["name"].(string)] {
			continue
		}
		commands = append(commands, item)
	}
	return M{"nftables": commands}
}

func compileNFTRules(c Config) []any {
	commands := []any{}
	counters := map[string]bool{}
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
	return commands
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
func flowKey(r Rule) Rule {
	return Rule{Route: r.Route, ID: r.ID, Protocol: r.Protocol, Port: r.Port, IP: r.IP, TargetPort: r.TargetPort}
}
func sameConfig(a, b Config) bool {
	return a.Generation == b.Generation && a.Hash == b.Hash && a.Epoch == b.Epoch && slices.Equal(a.Rules, b.Rules) && slices.Equal(a.Retired, b.Retired)
}
func unusedCounters(c Config, doc M) bool {
	needed := map[string]bool{}
	for _, r := range append(append([]Rule{}, c.Rules...), c.Retired...) {
		for _, p := range protocols(r) {
			for _, dir := range []string{"up", "down"} {
				needed[counterName(r, p, dir, c.Epoch)] = true
			}
		}
	}
	for _, item := range array(doc["nftables"]) {
		m, _ := item.(map[string]any)
		v, _ := m["counter"].(map[string]any)
		name, _ := v["name"].(string)
		if counterRE.MatchString(name) && !needed[name] {
			return true
		}
	}
	return false
}
func nftChainsReady(doc M) bool {
	chains := map[string]M{}
	for _, item := range array(doc["nftables"]) {
		m, _ := item.(map[string]any)
		v, _ := m["chain"].(map[string]any)
		if name, ok := v["name"].(string); ok {
			chains[name] = v
		}
	}
	for _, ch := range []struct{ name, kind, hook, priority string }{
		{"pre", "nat", "prerouting", "-105"}, {"post", "nat", "postrouting", "105"}, {"forward", "filter", "forward", "10"}, {"route", "filter", "prerouting", "-90"},
	} {
		v := chains[ch.name]
		if v["type"] != ch.kind || v["hook"] != ch.hook || fmt.Sprint(v["prio"]) != ch.priority || v["policy"] != "accept" {
			return false
		}
	}
	return true
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
func (a *Agent) apply(c Config) error { return a.applyContext(context.Background(), c) }
func (a *Agent) applyContext(ctx context.Context, c Config) error {
	a.kernelSnapshot = nil
	a.prepareCtx = ctx
	defer func() { a.prepareCtx = nil }()
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
	return a.lockedContext(ctx, func() (result error) {
		readAt := time.Now()
		state, exists, e := a.prepareNFT(c)
		if e != nil {
			return e
		}
		old := a.config
		ids := map[int64]bool{}
		next := map[Rule]bool{}
		for _, r := range c.Rules {
			ids[r.ID] = true
			next[flowKey(r)] = true
		}

		c.Epoch = old.Epoch
		if c.Epoch == "" || !exists {
			c.Epoch = randomID()
		}
		if !exists && old.Epoch != "" {
			a.stateError = "统计缺口：内核计数器已重建"
		}
		c.Retired = append([]Rule{}, old.Retired...)
		for _, r := range old.Rules {
			if !next[flowKey(r)] && ids[r.ID] {
				c.Retired = append(c.Retired, r)
			}
		}
		// Garbage collection uses actual conntrack state. Failure keeps the reservation.
		kept := []Rule{}
		checked := map[connectionKey]connectionResult{}
		retired := map[Rule]bool{}
		for _, r := range c.Retired {
			if !ids[r.ID] || retired[r] {
				continue
			}
			retired[r] = true
			live, err := a.hasConnections(r, checked)
			if err != nil || live {
				kept = append(kept, r)
			}
		}
		c.Retired = kept
		plan := a.rulePlan(c)
		rewrite := !exists || !nftChainsReady(state) || !plan.matches(state)
		var tx M
		if rewrite {
			tx = a.transaction(c, state, exists)
			raw, err := json.Marshal(tx)
			if err != nil {
				return err
			}
			if _, err = a.prepareCommand(raw, "nft", "-c", "-j", "-f", "-"); err != nil {
				return err
			}
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		// Cancellation stops preparation, never an in-progress route/NAT commit.
		a.prepareCtx = nil
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
			if a.root == "/var/lib/ictl" && readText("/proc/sys/net/ipv4/ip_forward") != "1" {
				if e = os.WriteFile("/proc/sys/net/ipv4/ip_forward", []byte("1\n"), 0644); e != nil {
					return e
				}
			}
			if e = a.prepareRoutes(c); e != nil {
				return e
			}
		}
		if a.root == "/var/lib/ictl" {
			if readText("/proc/sys/net/ipv4/ip_forward") != "1" {
				if e = os.WriteFile("/proc/sys/net/ipv4/ip_forward", []byte("1\n"), 0644); e != nil {
					return e
				}
			}
			if e = atomicBytesChanged("/etc/sysctl.d/99-ictl.conf", []byte("net.ipv4.ip_forward=1\n"), 0644); e != nil {
				return e
			}
		}
		_, pendingErr := os.Stat(a.path("active/pending.json"))
		if !rewrite && sameConfig(c, old) && os.IsNotExist(pendingErr) && !unusedCounters(c, state) {
			if e = atomicJSONChanged(a.path("active/config.json"), c); e != nil {
				return e
			}
			if e = atomicBytesChanged(a.path("config"), a.projection(c), 0600); e != nil {
				return e
			}
			if e = a.sampleState(state, true, readAt); e != nil {
				return e
			}
			return a.pruneRoutes(c)
		}
		if e = atomicJSON(a.path("active/pending.json"), c); e != nil {
			return e
		}
		union := c
		union.Retired = append(append([]Rule{}, c.Retired...), old.Rules...)
		if e = atomicBytesChanged(a.path("config"), a.projection(union), 0600); e != nil {
			return e
		}
		if rewrite {
			e = executeNFT(tx, false)
		}
		if e != nil {
			_ = atomicBytesChanged(a.path("config"), a.projection(old), 0600)
			return e
		}
		defer func() {
			if result == nil {
				return
			}
			a.kernelSnapshot = nil
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
			_ = atomicJSONChanged(a.path("active/config.json"), old)
			_ = atomicBytesChanged(a.path("config"), a.projection(old), 0600)
		}()
		verifyAt := time.Now()
		verify, ok, e := a.nftState()
		if e != nil || !ok {
			return fmt.Errorf("规则应用后无法读取: %v", e)
		}
		if !plan.matches(verify) {
			return errors.New("规则应用后核对失败")
		}
		removed := []Rule{}
		for _, r := range append(append([]Rule{}, old.Rules...), old.Retired...) {
			if !ids[r.ID] {
				removed = append(removed, r)
			}
		}
		if e = deleteConnections(removed); e != nil {
			return e
		}
		if e = atomicJSONChanged(a.path("active/config.json"), c); e != nil {
			return e
		}
		a.config = c
		if e = atomicBytesChanged(a.path("config"), a.projection(c), 0600); e != nil {
			return e
		}
		if e = a.sampleState(verify, true, verifyAt); e != nil {
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
			if e = atomicJSONChanged(a.path("active/traffic.json"), a.usage); e != nil {
				return e
			}
		}
		if e = a.pruneRoutes(c); e != nil {
			return e
		}
		return removeIfExists(a.path("active/pending.json"))
	})
}
func (p *nftRulePlan) matches(current M) bool {
	actual := []any{}
	for _, x := range array(current["nftables"]) {
		m, _ := x.(map[string]any)
		if r, ok := m["rule"].(map[string]any); ok {
			actual = append(actual, M{"chain": r["chain"], "expr": r["expr"]})
		}
	}
	return reflect.DeepEqual(p.normalized, normalizeNFTRules(actual))
}

func normalizeNFTRules(rules []any) any {
	// nft emits rules grouped by chain; compare order inside each chain.
	sort.SliceStable(rules, func(i, j int) bool { return rules[i].(M)["chain"].(string) < rules[j].(M)["chain"].(string) })
	// Round-trip normalizes native numeric types. Kernel may normalize fib flags and NAT family.
	raw, _ := json.Marshal(rules)
	var value any
	_ = json.Unmarshal(raw, &value)
	return normalizeNFT(value)
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

type connectionKey struct {
	protocol string
	port     int
	ip       string
	target   int
}
type connectionResult struct {
	live bool
	err  error
}

func (k connectionKey) args(action string) []string {
	return []string{action, "-f", "ipv4", "-p", k.protocol, "--orig-port-dst", strconv.Itoa(k.port), "--reply-src", k.ip, "--reply-port-src", strconv.Itoa(k.target)}
}

func (a *Agent) hasConnections(r Rule, checked map[connectionKey]connectionResult) (bool, error) {
	for _, p := range protocols(r) {
		key := connectionKey{p, r.Port, r.IP, r.TargetPort}
		result, ok := checked[key]
		if !ok {
			b, err := a.prepareCommand(nil, "conntrack", key.args("-L")...)
			result.err = err
			for _, line := range strings.Split(string(b), "\n") {
				if strings.HasPrefix(line, p+" ") {
					result.live = true
					break
				}
			}
			checked[key] = result
		}
		if result.err != nil || result.live {
			return true, result.err
		}
	}
	return false, nil
}
func deleteConnections(rules []Rule) error {
	deleted := map[connectionKey]bool{}
	for _, r := range rules {
		for _, p := range protocols(r) {
			key := connectionKey{p, r.Port, r.IP, r.TargetPort}
			if deleted[key] {
				continue
			}
			_, e := run(nil, "conntrack", append(key.args("-D"), "--dst-nat")...)
			if e != nil && !strings.Contains(e.Error(), "0 flow entries") {
				return e
			}
			deleted[key] = true
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
	defer func() { a.kernelSnapshot = nil }()
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
		if e = atomicJSONChanged(a.path("active/traffic.json"), a.usage); e != nil {
			return e
		}
		removed := []Rule{}
		for _, r := range append(append([]Rule{}, a.config.Rules...), a.config.Retired...) {
			if net.ParseIP(r.IP) != nil {
				removed = append(removed, r)
			}
		}
		if e = deleteConnections(removed); e != nil {
			return e
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

type nftSnapshot struct {
	config Config
	doc    M
	exists bool
	at     time.Time
}

// Reuse this round's counter read at most once per report, and only for exactly
// the same configuration. A one-second limit keeps older samples from delaying
// fault detection; delayed/failed maintenance falls back to a fresh read.
func (a *Agent) freshNFT(after time.Time) *nftSnapshot {
	s := a.kernelSnapshot
	if s != nil && s.at.After(after) && time.Since(s.at) < time.Second && sameConfig(s.config, a.config) {
		return s
	}
	return nil
}

// Report evidence about the installed rules separately from update/metrics errors.
// A failed read is unknown, not proof that traffic stopped forwarding.
func (a *Agent) forwardingHealth() M {
	if a.config.Hash == "" || len(a.config.Rules) == 0 {
		return a.forwardingHealthState(nil, false, nil)
	}
	// Collection operates on an immutable Agent snapshot, outside stateMu.
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	current, exists, err := readNFTWith(func(args ...string) ([]byte, error) {
		return command(ctx, nil, "nft", args...)
	})
	return a.forwardingHealthState(current, exists, err)
}

func (a *Agent) forwardingHealthState(current M, exists bool, readErr error) M {
	state, message := "unknown", ""
	check := func() error {
		if a.config.Hash == "" {
			return nil
		}
		if len(a.config.Rules) == 0 {
			state = "ok"
			return nil
		}
		if readErr != nil {
			return readErr
		}
		if exists {
			if err := a.checkNFTOwner(current); err != nil {
				return err
			}
		}
		if !exists || !a.rulePlan(a.config).matches(current) {
			state, message = "error", "已应用的转发规则缺失或被修改"
			return nil
		}
		if !nftChainsReady(current) {
			state, message = "error", "转发链挂载点异常"
			return nil
		}
		if a.root == "/var/lib/ictl" {
			enabled, err := os.ReadFile("/proc/sys/net/ipv4/ip_forward")
			if err != nil {
				return err
			}
			if strings.TrimSpace(string(enabled)) != "1" {
				state, message = "error", "系统 IPv4 转发已关闭"
				return nil
			}
		}
		state = "ok"
		return nil
	}
	if err := check(); err != nil {
		message = err.Error()
	}
	return M{"state": state, "error": message, "generation": a.config.Generation, "hash": a.config.Hash}
}
