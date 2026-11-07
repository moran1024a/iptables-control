package main

import (
	"fmt"
	"testing"
)

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
