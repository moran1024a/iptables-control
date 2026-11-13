package main

import (
	"bytes"
	"context"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestNegationAppliesOnlyToTheNextOption(t *testing.T) {
	rules := parseLegacyPorts("*nat\n-A PREROUTING -m addrtype --dst-type LOCAL -j DOCKER\n-A DOCKER ! -i docker0 -p tcp -m tcp --dport 8080 -j DNAT --to-destination 172.17.0.2:80\nCOMMIT\n")
	if got := rules.conflict("tcp", 8080); got != "nat/DOCKER" {
		t.Fatalf("tcp/8080 conflict = %q", got)
	}
	if got := rules.conflict("udp", 8080); got != "" {
		t.Fatalf("udp/8080 conflict = %q", got)
	}
	negated := parseLegacyPorts("*nat\n-A PREROUTING ! -p tcp -m udp --dport 53 -j DNAT --to-destination 10.0.0.1:53\nCOMMIT\n")
	if negated.conflict("udp", 53) == "" || negated.conflict("tcp", 53) != "" {
		t.Fatal("an explicit protocol negation must still be honoured")
	}
}

func TestConnectedUDPClientSocketIsNotAPortConflict(t *testing.T) {
	bin := t.TempDir()
	script := "#!/bin/sh\ncase \"$*\" in *-aun*) printf 'UNCONN 0 0 0.0.0.0:20001 0.0.0.0:*\\nESTAB 0 0 203.0.113.5:20002 198.51.100.9:443\\n';; esac\n"
	if err := os.WriteFile(filepath.Join(bin, "ss"), []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", bin)
	a := &Agent{root: t.TempDir(), usage: map[string]*Usage{}}
	a.initRuntime()
	rule := func(port int) Config {
		return Config{Rules: []Rule{{ID: 1, Protocol: "udp", Port: port, Host: "192.0.2.2", TargetPort: 53}}}
	}
	if err := a.checkConflictsWith(rule(20002), M{"nftables": []any{}}); err != nil {
		t.Fatalf("connected client socket reported as a conflict: %v", err)
	}
	if err := a.checkConflictsWith(rule(20001), M{"nftables": []any{}}); err == nil {
		t.Fatal("a bound UDP service must still conflict")
	}
}

func TestBackToBackProbesBothReturnResults(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var mu sync.Mutex
	got := map[any]bool{}
	q := newQualityMonitor(ctx, func(m M) error { mu.Lock(); got[m["id"]] = true; mu.Unlock(); return nil })
	for _, id := range []string{"probe-1", "probe-2"} {
		q.probeWithAgent(nil, M{"id": id, "protocol": "udp-echo-v1", "host": "127.0.0.1", "port": 9, "key": strings.Repeat("ab", 32)})
	}
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		done := len(got) == 2
		mu.Unlock()
		if done {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("results = %v, want both probes answered", got)
}

func TestCommandOutputCapCoversCopyFastPath(t *testing.T) {
	var out cappedBuffer
	// os/exec copies with io.Copy, which prefers a destination's ReadFrom.
	if _, ok := any(&out).(io.ReaderFrom); ok {
		t.Fatal("cappedBuffer must not expose ReadFrom")
	}
	if _, err := io.Copy(&out, bytes.NewReader(make([]byte, 6*1024*1024))); err != nil {
		t.Fatal(err)
	}
	if len(out.Bytes()) != 4*1024*1024 {
		t.Fatalf("kept %d bytes", len(out.Bytes()))
	}
}

func TestRouteOnlyChangeDoesNotKeepARetiredCopy(t *testing.T) {
	old := Rule{ID: 1, Protocol: "both", Port: 1000, IP: "198.51.100.1", TargetPort: 80, Route: "v2,in.example.com,192.0.2.10,eth0,-"}
	live := old
	live.Route = "v2,in.example.com,192.0.2.20,eth1,-"
	if !routeOnlyChange(old, []Rule{live}) {
		t.Fatal("a route-only change must drop the retired copy")
	}
	moved := live
	moved.IP = "198.51.100.2"
	if routeOnlyChange(old, []Rule{moved}) {
		t.Fatal("a changed target still drains through its retired copy")
	}
	narrowed := live
	narrowed.Protocol = "tcp"
	if routeOnlyChange(old, []Rule{narrowed}) {
		t.Fatal("the removed protocol still drains through its retired copy")
	}
}
