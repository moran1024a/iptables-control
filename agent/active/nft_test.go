package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"testing"
)

func TestUnconfirmedDaemonDoesNotRestoreRules(t *testing.T) {
	a := &Agent{root: t.TempDir(), config: testConfig(t)}
	if err := os.Mkdir(a.path("active"), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", t.TempDir())
	if err := a.daemon(context.Background()); err != nil {
		t.Fatal(err)
	}
	if a.stateError != "" {
		t.Fatal("unconfirmed daemon attempted a restore", a.stateError)
	}
}

func testConfig(t *testing.T) Config {
	t.Helper()
	line := "1 both 30123 192.0.2.2 30443"
	sum := sha256.Sum256([]byte(line))
	c, err := parseConfig([]byte("v="+hex.EncodeToString(sum[:])[:12]+"\n"+line+"\n"), 1)
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestConfigRejectsChangedSnapshot(t *testing.T) {
	c := testConfig(t)
	c.Rules[0].Port++
	if validateConfig(c) == nil {
		t.Fatal("accepted altered snapshot")
	}
}

func TestFailedApplyDoesNotMutateAppliedSnapshot(t *testing.T) {
	c := testConfig(t)
	c.Rules[0].IP = "192.0.2.1"
	a := &Agent{root: t.TempDir(), config: c}
	t.Setenv("PATH", t.TempDir()) // Fail after resolution, before invoking any firewall command.
	if err := a.apply(a.config); err == nil {
		t.Fatal("expected unavailable nft")
	}
	if a.config.Rules[0].IP != "192.0.2.1" {
		t.Fatal("resolution mutated the last applied snapshot")
	}
}

// Only run inside an explicitly created, disposable network namespace.
func TestKernelRoundTrip(t *testing.T) {
	if os.Getenv("ICTL_KERNEL_TEST") != "1" {
		t.Skip("requires isolated root network namespace")
	}
	a := &Agent{root: t.TempDir(), installation: randomID(), usage: map[string]*Usage{}}
	c := testConfig(t)
	if err := a.apply(c); err != nil {
		c.Rules[0].IP = c.Rules[0].Host
		c.Epoch = randomID()
		tx := a.transaction(c, M{}, false)
		_ = executeNFT(tx, false)
		actual, _, _ := a.nftState()
		b, _ := json.Marshal(actual)
		t.Log(string(b))
		t.Fatal(err)
	}
	first := a.config.Epoch
	if err := a.apply(c); err != nil {
		t.Fatal(err)
	}
	if a.config.Epoch != first {
		t.Fatal("ordinary update reset epoch")
	}
	if len(a.traffic()) != 2 {
		t.Fatalf("expected TCP/UDP counters: %+v", a.traffic())
	}
	if err := a.clear(); err != nil {
		t.Fatal(err)
	}
	if _, exists, err := a.nftState(); err != nil || exists {
		t.Fatalf("clear: %v %v", exists, err)
	}
}
