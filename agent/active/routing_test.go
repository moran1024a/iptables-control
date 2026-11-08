package main

import (
	"encoding/json"
	"fmt"
	"net"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestAdvertisedAddressAndOptionalSource(t *testing.T) {
	for _, value := range []string{"v2,198.18.0.7,-,-,-", "v2,cloud.example,-,-,-", "v2,198.18.0.7,10.0.0.2,ens3,-"} {
		route, err := parseRoute(value)
		if err != nil || route.In.enabled() || route.Listen == "" {
			t.Fatalf("%s: %+v %v", value, route, err)
		}
		c := Config{Rules: []Rule{{ID: 1, Protocol: "tcp", Port: 10001, Host: "192.0.2.9", IP: "192.0.2.9", TargetPort: 443, Route: value, ListenIP: "198.18.0.7"}}}
		if len(profiles(c)) != map[bool]int{true: 1, false: 0}[route.Out.enabled()] {
			t.Fatal(profiles(c))
		}
		a := &Agent{installation: "test"}
		encoded, _ := json.Marshal(a.transaction(c, M{}, false))
		if strings.Contains(string(encoded), `"right":""`) {
			t.Fatal(string(encoded))
		}
		if !route.Out.enabled() && (strings.Contains(string(encoded), `"snat"`) || strings.Contains(string(encoded), `"mangle"`)) {
			t.Fatal(string(encoded))
		}
	}
	for _, value := range []string{"v2,,-,-,-", "v2,host/name,-,-,-", "v2,198.18.0.7,-,ens3,-", "v2,198.18.0.7,10.0.0.2,-,-", "v2,198.18.0.7,-,-,-,extra"} {
		if _, err := parseRoute(value); err == nil {
			t.Fatalf("invalid route accepted: %s", value)
		}
	}
}

func TestReceiveAddressSurvivesOfflineRestore(t *testing.T) {
	a := &Agent{offline: true}
	c := Config{Rules: []Rule{{Route: "v2,cloud.example,-,-,-", IP: "192.0.2.9", ListenIP: "198.18.0.7"}}}
	if err := a.resolveCached(&c); err != nil {
		t.Fatal(err)
	}
	c.Rules[0].ListenIP = ""
	if err := a.resolveCached(&c); err == nil {
		t.Fatal("missing receive DNS cache was accepted")
	}
}

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
		Route: "v2,192.0.2.3,198.51.100.3,target,-"}}}}
	for _, route := range []string{"v2,192.0.2.3,198.51.100.3,target,-", "v2,198.18.0.7,198.51.100.3,target,-", "v2,198.18.0.7,-,-,-", ""} {
		a.config.Rules[0].Route = route
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
		if strings.Contains(route, "198.51.100.3") {
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
