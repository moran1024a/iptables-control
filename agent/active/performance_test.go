package main

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/coder/websocket"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"syscall"
	"testing"
	"time"
)

func nftFixture(c Config, a *Agent) M {
	rows := []any{}
	for _, item := range array(a.transaction(c, M{}, false)["nftables"]) {
		add, _ := item.(M)["add"].(M)
		if add != nil {
			rows = append(rows, add)
		}
	}
	return M{"nftables": rows}
}
func fakeAppliedAgent(t *testing.T) *Agent {
	t.Helper()
	root := t.TempDir()
	c := testConfig(t)
	c.Rules[0].IP = c.Rules[0].Host
	c.Epoch = randomID()
	a := &Agent{root: root, installation: randomID(), config: c, usage: map[string]*Usage{}}
	a.initRuntime()
	if err := atomicJSON(filepath.Join(root, "kernel.json"), nftFixture(c, a)); err != nil {
		t.Fatal(err)
	}
	script := `#!/usr/bin/env python3
import os,sys,json,pathlib
root=pathlib.Path(os.environ['ICTL_FAKE_ROOT']); path=root/'kernel.json'
doc=json.loads(path.read_text()); rows=doc['nftables']
with (root/'queries').open('a') as f: f.write(' '.join(sys.argv[1:])+'\n')
if '-c' in sys.argv: sys.exit(0)
if '-f' in sys.argv:
 with (root/'writes').open('a') as f: f.write('write\n')
 for command in json.load(sys.stdin)['nftables']:
  action,value=next(iter(command.items())); kind,body=next(iter(value.items()))
  if action=='flush': rows[:]=[x for x in rows if x.get('rule',{}).get('chain')!=body['name']]
  elif action=='delete': rows[:]=[x for x in rows if x.get(kind,{}).get('name')!=body['name']]
  elif action=='add': rows.append({kind:body})
 path.write_text(json.dumps(doc))
elif sys.argv[1:]==['-j','list','tables']:
 doc={'nftables':[x for x in rows if 'table' in x]}
elif sys.argv[1:]==['-j','list','table','ip','ictl_active']:
 doc={'nftables':[x for x in rows for k,v in x.items() if v.get('family')=='ip' and v.get('name' if k=='table' else 'table')=='ictl_active']}
print(json.dumps(doc))
`
	if err := os.WriteFile(filepath.Join(root, "nft"), []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"ss", "iptables-save", "conntrack"} {
		if err := os.WriteFile(filepath.Join(root, name), []byte("#!/bin/sh\nexit 0\n"), 0700); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("PATH", root+":"+os.Getenv("PATH"))
	t.Setenv("ICTL_FAKE_ROOT", root)
	return a
}
func TestUnchangedRulesSkipWritesButRepairKernelDrift(t *testing.T) {
	a := fakeAppliedAgent(t)
	for _, generation := range []int64{1, 2, 2} {
		c := a.config
		c.Generation = generation
		if err := a.apply(c); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := os.Stat(a.path("writes")); !os.IsNotExist(err) {
		t.Fatal("unchanged rules were rewritten", err)
	}
	if a.config.Generation != 2 {
		t.Fatal("metadata generation was not committed")
	}
	var doc M
	if err := readJSON(a.path("kernel.json"), &doc); err != nil {
		t.Fatal(err)
	}
	rows := array(doc["nftables"])
	for i, item := range rows {
		if item.(M)["rule"] != nil {
			doc["nftables"] = append(rows[:i], rows[i+1:]...)
			break
		}
	}
	if err := atomicJSON(a.path("kernel.json"), doc); err != nil {
		t.Fatal(err)
	}
	if err := a.apply(a.config); err != nil {
		t.Fatal(err)
	}
	if readText(a.path("writes")) != "write" {
		t.Fatal("drift was not repaired with a single transaction")
	}
}

func TestPreparationSharesOneFreshRulesetAndSeesForeignConflicts(t *testing.T) {
	a := fakeAppliedAgent(t)
	if err := a.apply(a.config); err != nil {
		t.Fatal(err)
	}
	if got := readText(a.path("queries")); got != "-j list ruleset" {
		t.Fatalf("unexpected preparation reads: %s", got)
	}
	var doc M
	if err := readJSON(a.path("kernel.json"), &doc); err != nil {
		t.Fatal(err)
	}
	doc["nftables"] = append(array(doc["nftables"]),
		obj("table", M{"family": "ip", "name": "foreign"}),
		obj("chain", M{"family": "ip", "table": "foreign", "name": "pre", "type": "nat", "hook": "prerouting"}),
		obj("rule", M{"family": "ip", "table": "foreign", "chain": "pre", "expr": []any{match(payload("tcp", "dport"), a.config.Rules[0].Port), obj("dnat", M{"addr": "192.0.2.99"})}}))
	// An identically named table in another family must not affect ownership.
	doc["nftables"] = append(array(doc["nftables"]),
		obj("table", M{"family": "inet", "name": tableName, "comment": "foreign"}),
		obj("chain", M{"family": "inet", "table": tableName, "name": "owner_foreign"}))
	state, exists := ownedNFTState(doc)
	if !exists || a.checkNFTOwner(state) != nil || !a.rulePlan(a.config).matches(state) {
		t.Fatal("owned table was mixed with unrelated objects")
	}
	if err := atomicJSON(a.path("kernel.json"), doc); err != nil {
		t.Fatal(err)
	}
	if err := a.apply(a.config); err == nil || !strings.Contains(err.Error(), "端口冲突") {
		t.Fatalf("new external conflict was missed: %v", err)
	}
	if _, err := os.Stat(a.path("writes")); !os.IsNotExist(err) {
		t.Fatal("conflicting configuration changed the kernel")
	}
}

func TestRulePlanInvalidatesOnEffectiveChanges(t *testing.T) {
	a := fakeAppliedAgent(t)
	c := a.config
	p := a.rulePlan(c)
	c.Generation++
	c.Hash = "metadata-only"
	if a.rulePlan(c) != p {
		t.Fatal("metadata-only update recompiled rules")
	}
	for _, change := range []func(*Config){
		func(c *Config) { c.Rules[0].IP = "192.0.2.55" },
		func(c *Config) { c.Rules[0].Route = "v2,198.18.0.1,-,-,-"; c.Rules[0].ListenIP = "198.18.0.1" },
		func(c *Config) { c.Rules[0].LocalListen = true },
		func(c *Config) { c.Rules[0].ListenIP = "198.18.0.2" },
		func(c *Config) { c.Retired = append(c.Retired, c.Rules[0]); c.Retired[0].Port++ },
		func(c *Config) { c.Epoch = randomID() },
	} {
		change(&c)
		next := a.rulePlan(c)
		if next == p {
			t.Fatal("effective change reused an obsolete plan")
		}
		current := nftFixture(c, a)
		if !next.matches(current) || p.matches(current) {
			t.Fatal("plan comparison did not track changed forwarding rules")
		}
		p = next
	}
}

func TestHealthSnapshotIsFreshAndBoundToConfiguration(t *testing.T) {
	a := fakeAppliedAgent(t)
	if err := a.apply(a.config); err != nil {
		t.Fatal(err)
	}
	snapshot := a.snapshot()
	cached := snapshot.freshNFT(time.Time{})
	if cached == nil || snapshot.forwardingHealthState(cached.doc, cached.exists, nil)["state"] != "ok" {
		t.Fatal("fresh verified sample was not reusable")
	}
	if snapshot.freshNFT(cached.at) != nil {
		t.Fatal("one sample was reused for successive reports")
	}
	snapshot.config.Generation++
	if snapshot.freshNFT(time.Time{}) != nil {
		t.Fatal("previous generation's health was reused")
	}
	snapshot = a.snapshot()
	snapshot.config.Rules = slices.Clone(snapshot.config.Rules)
	snapshot.config.Rules[0].IP = "192.0.2.99"
	if snapshot.freshNFT(time.Time{}) != nil {
		t.Fatal("previous DNS result's health was reused")
	}
	snapshot = a.snapshot()
	expired := *cached
	expired.at = time.Now().Add(-2 * time.Second)
	snapshot.kernelSnapshot = &expired
	if snapshot.freshNFT(time.Time{}) != nil {
		t.Fatal("expired sample was reused after maintenance stopped")
	}
	if err := atomicJSON(a.path("kernel.json"), M{"nftables": []any{}}); err != nil {
		t.Fatal(err)
	}
	if snapshot.forwardingHealth()["state"] != "error" {
		t.Fatal("fallback inspection missed missing kernel rules")
	}
}

func TestConntrackDeduplicatesExactTuplesAndKeepsFailedCleanup(t *testing.T) {
	a := fakeAppliedAgent(t)
	script := `#!/bin/sh
printf '%s\n' "$*" >> "$ICTL_FAKE_ROOT/conntrack-calls"
test ! -f "$ICTL_FAKE_ROOT/fail-conntrack"
`
	if err := os.WriteFile(a.path("conntrack"), []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	r := a.config.Rules[0]
	same := r
	same.ID++
	same.Protocol = "tcp"
	checked := map[connectionKey]connectionResult{}
	for _, rule := range []Rule{r, same, r} {
		if live, err := a.hasConnections(rule, checked); err != nil || live {
			t.Fatal(live, err)
		}
	}
	if err := deleteConnections([]Rule{r, same, r}); err != nil {
		t.Fatal(err)
	}
	calls := strings.Split(readText(a.path("conntrack-calls")), "\n")
	if len(calls) != 4 || !strings.HasSuffix(calls[2], "--dst-nat") || !strings.HasSuffix(calls[3], "--dst-nat") {
		t.Fatalf("expected one query/deletion per TCP/UDP tuple: %v", calls)
	}
	if err := os.WriteFile(a.path("fail-conntrack"), nil, 0600); err != nil {
		t.Fatal(err)
	}
	if live, err := a.hasConnections(r, map[connectionKey]connectionResult{}); err == nil || !live {
		t.Fatal("read failure was treated as no connections")
	}
	old := a.config
	if err := a.apply(Config{Generation: old.Generation + 1, Hash: "e3b0c44298fc"}); err == nil {
		t.Fatal("failed deletion was accepted")
	}
	if !sameConfig(a.config, old) || a.forwardingHealth()["state"] != "ok" {
		t.Fatal("failed deletion did not retain the old configuration and rules")
	}
}
func TestForegroundCancelsMaintenanceLockWait(t *testing.T) {
	a := &Agent{root: t.TempDir(), usage: map[string]*Usage{}}
	a.initRuntime()
	if err := atomicBytes(a.path("active/enabled"), []byte("confirmed"), 0600); err != nil {
		t.Fatal(err)
	}
	f, err := os.OpenFile(a.path("lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		t.Fatal(err)
	}
	defer syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); a.maintain(ctx, time.Millisecond) }()
	defer func() { cancel(); <-done }()
	time.Sleep(20 * time.Millisecond)
	resume := a.maintenance.foreground()
	defer resume()
	ready := make(chan struct{})
	go func() { a.stateMu.Lock(); a.stateMu.Unlock(); close(ready) }()
	select {
	case <-ready:
	case <-time.After(time.Second):
		t.Fatal("configuration blocked behind maintenance lock wait")
	}
}
func trafficAck(message M) M {
	rows := []any{}
	for _, u := range message["data"].([]Usage) {
		rows = append(rows, M{"rule_id": json.Number(fmt.Sprint(u.RuleID)), "protocol": u.Protocol, "epoch": u.Epoch, "up": u.Up, "down": u.Down, "up_packets": u.UpPackets, "down_packets": u.DownPackets})
	}
	return M{"id": message["id"], "data": rows}
}
func TestTrafficRetriesAndArchivesWithoutRedundantWrites(t *testing.T) {
	u := &Usage{RuleID: 1, Protocol: "tcp", Epoch: randomID(), Up: "10", Down: "20", UpPackets: "1", DownPackets: "2", Quality: "ok", Active: true}
	a := &Agent{root: t.TempDir(), usage: map[string]*Usage{usageKey(*u): u}}
	ledger := &trafficLedger{}
	messages := []M{}
	send := func(m M) error { messages = append(messages, m); return nil }
	if err := ledger.send(a.traffic(), send); err != nil {
		t.Fatal(err)
	}
	if err := ledger.send(a.traffic(), send); err != nil {
		t.Fatal(err)
	}
	if len(messages) != 2 {
		t.Fatal("unacknowledged values were not retried")
	}
	if err := ledger.acknowledge(a, trafficAck(messages[1])); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(a.path("active/traffic.json")); !os.IsNotExist(err) {
		t.Fatal("active ACK rewrote disk")
	}
	_ = ledger.send(a.traffic(), send)
	if len(messages) != 2 {
		t.Fatal("unchanged acknowledged values sent again")
	}
	u.Up = "11"
	u.Active = false
	if err := ledger.prune(a); err != nil {
		t.Fatal(err)
	}
	if len(a.usage) != 1 {
		t.Fatal("unacknowledged final increment was discarded")
	}
	_ = ledger.send(a.traffic(), send)
	if err := ledger.acknowledge(a, trafficAck(messages[2])); err != nil {
		t.Fatal(err)
	}
	if len(a.usage) != 0 {
		t.Fatal("acknowledged archive was retained")
	}
	// Archiving after an active ACK requires no redundant network round trip.
	u.Active = true
	a.usage[usageKey(*u)] = u
	fresh := &trafficLedger{}
	_ = fresh.send(a.traffic(), send)
	if err := fresh.acknowledge(a, trafficAck(messages[len(messages)-1])); err != nil {
		t.Fatal(err)
	}
	u.Active = false
	if err := fresh.prune(a); err != nil {
		t.Fatal(err)
	}
	if len(a.usage) != 0 {
		t.Fatal("already acknowledged archive was retained")
	}
}
func BenchmarkConflictBatch(b *testing.B) {
	for _, size := range []int{10, 100, 500} {
		entries := []any{M{"chain": M{"family": "ip", "table": "peer", "name": "pre", "hook": "prerouting"}}}
		var text strings.Builder
		text.WriteString("*nat\n:PREROUTING ACCEPT [0:0]\n")
		for i := 0; i < size; i++ {
			port := 10000 + i
			entries = append(entries, M{"rule": M{"family": "ip", "table": "peer", "chain": "pre", "expr": []any{match(payload("tcp", "dport"), port), obj("dnat", M{"addr": "192.0.2.1", "port": 443})}}})
			fmt.Fprintf(&text, "-A PREROUTING -p tcp --dport %d -j DNAT --to-destination 192.0.2.1:443\n", port)
		}
		text.WriteString("COMMIT\n")
		doc, legacy := M{"nftables": entries}, text.String()
		for _, indexed := range []bool{false, true} {
			b.Run(fmt.Sprintf("%d/indexed_%t", size, indexed), func(b *testing.B) {
				b.ReportAllocs()
				for i := 0; i < b.N; i++ {
					var nativeRules nativePortRules
					var legacyRules legacyPortRules
					if indexed {
						nativeRules = parseNativePorts(doc)
						legacyRules = parseLegacyPorts(legacy)
					}
					for j := 0; j < size; j++ {
						for _, p := range []string{"tcp", "udp"} {
							var n, l string
							if indexed {
								n = nativeRules.conflict(p, 30000+j)
								l = legacyRules.conflict(p, 30000+j)
							} else {
								n = parseNativePorts(doc).conflict(p, 30000+j)
								l = parseLegacyPorts(legacy).conflict(p, 30000+j)
							}
							if n != "" || l != "" {
								b.Fatal(n, l)
							}
						}
					}
				}
			})
		}
	}
}
func TestConfigContextCancellationLeavesKernelUntouched(t *testing.T) {
	a := fakeAppliedAgent(t)
	before, err := os.ReadFile(a.path("kernel.json"))
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := a.applyContext(ctx, a.config); err == nil {
		t.Fatal("cancelled preparation accepted")
	}
	after, _ := os.ReadFile(a.path("kernel.json"))
	if string(before) != string(after) {
		t.Fatal("cancelled preparation changed rules")
	}
	if _, err := os.Stat(a.path("active/pending.json")); !os.IsNotExist(err) {
		t.Fatal("cancelled preparation created pending commit")
	}
}

func TestConfigReceiptPrecedesBackgroundTraffic(t *testing.T) {
	a := fakeAppliedAgent(t)
	u := &Usage{RuleID: 1, Protocol: "tcp", Epoch: randomID(), Up: "10", Down: "20", UpPackets: "1", DownPackets: "2", Quality: "ok", Active: false}
	a.usage[usageKey(*u)] = u
	c := a.config
	c.Generation++
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ws, err := websocket.Accept(w, r, nil)
		if err != nil {
			t.Error(err)
			return
		}
		defer ws.CloseNow()
		ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
		defer cancel()
		send := func(m M) error {
			m["session"] = "test"
			b, _ := json.Marshal(m)
			return ws.Write(ctx, websocket.MessageText, b)
		}
		if _, _, err = ws.Read(ctx); err != nil {
			t.Error(err)
			return
		}
		if err = send(M{"type": "welcome"}); err != nil {
			t.Error(err)
			return
		}
		b, _ := json.Marshal(c)
		var message M
		_ = json.Unmarshal(b, &message)
		message["type"] = "config"
		message["op"] = "test-operation"
		if err = send(message); err != nil {
			t.Error(err)
			return
		}
		_, b, err = ws.Read(ctx)
		if err != nil {
			t.Error(err)
			return
		}
		result, _ := decodeJSON(b)
		if result["type"] != "apply_result" || result["ok"] != true {
			t.Errorf("configuration receipt was delayed or failed: %s", b)
		}
		if err = send(M{"type": "quiesce"}); err != nil {
			t.Error(err)
			return
		}
		_, _, _ = ws.Read(ctx)
	}))
	defer server.Close()
	old := http.DefaultClient
	http.DefaultClient = server.Client()
	defer func() { http.DefaultClient = old }()
	a.url = server.URL
	ctx, cancel := context.WithTimeout(context.Background(), 7*time.Second)
	defer cancel()
	if err := a.session(ctx); err != nil {
		t.Fatal(err)
	}
}

func TestSlowTelemetryDoesNotHoldConfigurationLock(t *testing.T) {
	a := fakeAppliedAgent(t)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	ready := make(chan struct{})
	close(ready)
	blocked, release, done := make(chan struct{}), make(chan struct{}), make(chan error, 1)
	send := func(m M) error {
		if m["type"] == "status" {
			select {
			case <-blocked:
			default:
				close(blocked)
			}
			select {
			case <-release:
			case <-ctx.Done():
				return ctx.Err()
			}
		}
		return ctx.Err()
	}
	go func() { done <- a.collect(ctx, ready, &trafficLedger{}, send, time.Millisecond) }()
	select {
	case <-blocked:
	case <-ctx.Done():
		t.Fatal("collector did not start")
	}
	applied := make(chan error, 1)
	go func() {
		a.stateMu.Lock()
		defer a.stateMu.Unlock()
		c := a.config
		c.Generation++
		applied <- a.apply(c)
	}()
	select {
	case err := <-applied:
		if err != nil {
			t.Error(err)
		}
	case <-time.After(time.Second):
		t.Error("configuration waited for a blocked telemetry send")
	}
	cancel()
	close(release)
	<-done
	a.stateMu.Lock()
	defer a.stateMu.Unlock()
	if a.lastMetrics.IsZero() {
		t.Error("rate baseline was not preserved for reconnection")
	}
}
