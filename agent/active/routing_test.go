package main

import (
	"fmt"
	"net"
	"os"
	"strconv"
	"testing"
	"time"
)

func TestDiagnosticRouteKernel(t *testing.T) {
	if os.Getenv("ICTL_ROUTING_KERNEL") != "1" {
		t.Skip("requires the isolated multi-IP kernel fixture")
	}
	listener, err := net.ListenTCP("tcp4", &net.TCPAddr{IP: net.ParseIP("198.51.100.1")})
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	host, port, _ := net.SplitHostPort(listener.Addr().String())
	n, _ := strconv.Atoi(port)
	a := &Agent{config: Config{Rules: []Rule{{ID: 1, Host: host, TargetPort: n, Protocol: "tcp",
		Route: "192.0.2.3,198.51.100.3,client,target,-,-"}}}}
	for _, explicit := range []bool{true, false} {
		if !explicit {
			a.config.Rules[0].Route = ""
		}
		result := a.query(M{"kind": "diagnose", "rule_id": 1})
		if result["reachable"] != true {
			t.Fatal(result)
		}
		listener.SetDeadline(time.Now().Add(time.Second))
		conn, err := listener.AcceptTCP()
		if err != nil {
			t.Fatal(err)
		}
		got := conn.RemoteAddr().(*net.TCPAddr).IP.String()
		conn.Close()
		want := "198.51.100.1"
		if explicit {
			want = "198.51.100.3"
		}
		if got != want {
			t.Fatalf("diagnostic source = %s, want %s", got, want)
		}
	}
}

func TestSharedPolicyPriorityRequiresExactOwnedMarks(t *testing.T) {
	a := &Agent{root: t.TempDir()}
	first := routeProfile{"10.0.0.95", "eth0", "-"}
	second := routeProfile{"10.0.0.108", "eth0", "-"}
	if first.priority() != second.priority() || first.mark() == second.mark() {
		t.Fatal("fixture must have colliding priorities and distinct marks")
	}
	for _, p := range []routeProfile{first, second} {
		if err := atomicBytes(a.profilePath(p), []byte(p.key()+"\n"), 0600); err != nil {
			t.Fatal(err)
		}
		rule := map[string]any{"table": p.table(), "priority": p.priority(), "fwmark": fmt.Sprintf("0x%x", p.mark()), "protocol": "242"}
		if !a.ownsPolicyRule(rule) {
			t.Fatal("an owned rule at the shared priority was rejected")
		}
		rule["fwmask"] = "0xffffff00"
		if a.ownsPolicyRule(rule) {
			t.Fatal("an overlapping mark mask was accepted")
		}
		delete(rule, "fwmask")
		rule["fwmark"] = "0x1"
		if a.ownsPolicyRule(rule) {
			t.Fatal("a foreign mark was accepted")
		}
	}
}
