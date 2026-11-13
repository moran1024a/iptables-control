package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestEmptyReplayCacheIsAnEmptyCache(t *testing.T) {
	path := filepath.Join(t.TempDir(), "wake-replay.json")
	if err := os.WriteFile(path, nil, 0600); err != nil {
		t.Fatal(err)
	}
	guard, err := openGuard(path)
	if err != nil {
		t.Fatalf("an empty cache left by a power loss must not stop the daemon: %v", err)
	}
	if !guard.accept("nonce", 100, 100) || guard.accept("nonce", 100, 100) {
		t.Fatal("replay protection must work after recovering from an empty cache")
	}
	if err := os.WriteFile(path, []byte("{broken"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := openGuard(path); err == nil {
		t.Fatal("a corrupt cache is still rejected")
	}
}
