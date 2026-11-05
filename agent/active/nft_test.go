package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
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

func emptyNFTFixture() M {
	return M{"nftables": []any{
		obj("table", M{"family": "ip", "name": tableName, "handle": 4}),
		obj("chain", M{"family": "ip", "table": tableName, "name": "pre", "type": "nat", "hook": "prerouting", "prio": -105, "policy": "accept"}),
		obj("chain", M{"family": "ip", "table": tableName, "name": "post", "type": "nat", "hook": "postrouting", "prio": 105, "policy": "accept"}),
		obj("chain", M{"family": "ip", "table": tableName, "name": "forward", "type": "filter", "hook": "forward", "prio": 10, "policy": "accept"}),
	}}
}

func TestNFTOwnershipCompatibility(t *testing.T) {
	a := &Agent{installation: strings.Repeat("a", 32)}
	for _, tc := range []struct {
		name, comment, marker string
		want                  bool
	}{
		{"unmarked", "", "", false},
		{"legacy", "ictl:" + a.installation, "", true},
		{"nft-1.0.6", "", a.ownerChain(), true},
		{"both", "ictl:" + a.installation, a.ownerChain(), true},
		{"foreign-comment", "ictl:other", a.ownerChain(), false},
		{"foreign-marker", "ictl:" + a.installation, "owner_other", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			d := emptyNFTFixture()
			if tc.comment != "" {
				d["nftables"].([]any)[0].(M)["table"].(M)["comment"] = tc.comment
			}
			if tc.marker != "" {
				d["nftables"] = append(array(d["nftables"]), obj("chain", M{"name": tc.marker}))
			}
			if got := a.checkNFTOwner(d) == nil; got != tc.want {
				t.Fatalf("ownership = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestRecoverOnlyRecordedEmptyTable(t *testing.T) {
	for _, tc := range []string{"empty", "with-rule", "with-counter", "with-comment", "hidden-comment", "changed-hook", "wrong-journal"} {
		t.Run(tc, func(t *testing.T) {
			a := &Agent{root: t.TempDir(), installation: strings.Repeat("a", 32)}
			d := emptyNFTFixture()
			journal := "PHASE=failed\nTARGET=active\nFROM=none\nHASH=e3b0c44298fc\n"
			switch tc {
			case "with-rule":
				d["nftables"] = append(array(d["nftables"]), obj("rule", M{"chain": "pre", "expr": []any{}}))
			case "with-counter":
				d["nftables"] = append(array(d["nftables"]), obj("counter", M{"name": "foreign"}))
			case "with-comment":
				d["nftables"].([]any)[0].(M)["table"].(M)["comment"] = "foreign"
			case "changed-hook":
				d["nftables"].([]any)[1].(M)["chain"].(M)["hook"] = "output"
			case "wrong-journal":
				journal = strings.ReplaceAll(journal, "failed", "installed")
			}
			if err := atomicJSON(a.path("native.json"), d); err != nil {
				t.Fatal(err)
			}
			if err := atomicBytes(a.path("switch-work/transaction"), []byte(journal), 0600); err != nil {
				t.Fatal(err)
			}
			script := "#!/bin/sh\ncase \"$*\" in\n '-j list tables'|'-j list table ip ictl_active') /bin/cat \"$NFT_STATE\" ;;\n '-n list table ip ictl_active') printf '%s\\n' \"$NFT_TEXT\" ;;\n '-j -f -') /bin/cat > \"$NFT_CHANGE\" ;;\n *) exit 2 ;;\nesac\n"
			if err := os.WriteFile(a.path("nft"), []byte(script), 0700); err != nil {
				t.Fatal(err)
			}
			t.Setenv("PATH", a.root)
			t.Setenv("NFT_STATE", a.path("native.json"))
			t.Setenv("NFT_CHANGE", a.path("change.json"))
			t.Setenv("NFT_TEXT", "table ip ictl_active {}")
			if tc == "hidden-comment" {
				t.Setenv("NFT_TEXT", "table ip ictl_active { comment \"foreign\"; }")
			}
			err := a.recoverEmptyTable()
			if tc == "empty" {
				if err != nil {
					t.Fatal(err)
				}
				b, err := os.ReadFile(a.path("change.json"))
				if err != nil || !strings.Contains(string(b), a.ownerChain()) || strings.Contains(string(b), "delete") {
					t.Fatalf("unexpected repair: %s, %v", b, err)
				}
			} else {
				if err == nil {
					t.Fatal("accepted foreign or unrecorded table")
				}
				if _, err := os.Stat(a.path("change.json")); !os.IsNotExist(err) {
					t.Fatal("changed rejected table")
				}
			}
		})
	}
}

func TestInstallationIdentityIsStableAndNotReplacedWhenDamaged(t *testing.T) {
	root := t.TempDir()
	conf := filepath.Join(root, "identity")
	if err := os.WriteFile(conf, []byte("URL=https://example.invalid/sync\nKEY="+strings.Repeat("ab", 32)+"\n"), 0600); err != nil {
		t.Fatal(err)
	}
	a, err := loadAgent(root, conf)
	if err != nil {
		t.Fatal(err)
	}
	b, err := loadAgent(root, conf)
	if err != nil || a.installation != b.installation {
		t.Fatal("installation identity changed", err)
	}
	if err := os.WriteFile(a.path("installation-id"), nil, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := loadAgent(root, conf); err == nil {
		t.Fatal("silently replaced damaged identity")
	}
}

// Only run inside an explicitly created, disposable network namespace.
func TestKernelRoundTrip(t *testing.T) {
	if os.Getenv("ICTL_KERNEL_TEST") != "1" {
		t.Skip("requires isolated root network namespace")
	}
	a := &Agent{root: t.TempDir(), installation: randomID(), usage: map[string]*Usage{}}
	// Reproduce the exact empty, uncommented table and failure record from 1.0.6.
	fixture := emptyNFTFixture()
	commands := []any{}
	for _, x := range array(fixture["nftables"]) {
		for kind, raw := range x.(M) {
			v := raw.(M)
			delete(v, "handle")
			commands = append(commands, op("add", kind, v))
		}
	}
	if err := executeNFT(M{"nftables": commands}, false); err != nil {
		t.Fatal(err)
	}
	if _, _, err := a.nftState(); err == nil {
		t.Fatal("unmarked table was accepted without recovery")
	}
	if err := atomicBytes(a.path("switch-work/transaction"), []byte("PHASE=failed\nTARGET=active\nFROM=none\nHASH=e3b0c44298fc\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := a.recoverEmptyTable(); err != nil {
		t.Fatal(err)
	}
	if err := a.clear(); err != nil {
		t.Fatal(err)
	}
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
