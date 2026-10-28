package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func testAgent(t *testing.T) *Agent {
	t.Helper()
	dir := t.TempDir()
	core := filepath.Join(dir, "core")
	script := "#!/bin/sh\nif [ \"$1\" = version ]; then echo 'sing-box version 1.14.4'; exit 0; fi\nif [ -f \"$(dirname \"$0\")/reject\" ]; then exit 1; fi\nexit 0\n"
	if err := os.WriteFile(core, []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	return newAgent(Options{Core: core, DataDir: dir, Token: strings.Repeat("a", 64)})
}
func snapshot(rev string) Snapshot {
	return Snapshot{API: 1, Revision: strings.Repeat(rev, 64), CoreVersion: "1.14.4", Config: json.RawMessage(`{"inbounds":[],"outbounds":[{"type":"direct"}]}`)}
}
func TestApplyAndNoop(t *testing.T) {
	a := testAgent(t)
	calls := 0
	a.restart = func() error { calls++; return nil }
	if err := a.apply(snapshot("a")); err != nil {
		t.Fatal(err)
	}
	if err := a.apply(snapshot("a")); err != nil {
		t.Fatal(err)
	}
	if calls != 1 || a.state.Revision != strings.Repeat("a", 64) || a.state.State != "ok" {
		t.Fatal(calls, a.state)
	}
	info, _ := os.Stat(filepath.Join(a.opts.DataDir, "config.json"))
	if info.Mode().Perm() != 0600 {
		t.Fatal("unsafe config mode")
	}
}
func TestRejectedConfigKeepsAppliedRevision(t *testing.T) {
	a := testAgent(t)
	a.restart = func() error { return nil }
	if err := a.apply(snapshot("a")); err != nil {
		t.Fatal(err)
	}
	os.WriteFile(filepath.Join(a.opts.DataDir, "reject"), nil, 0600)
	if err := a.apply(snapshot("b")); err == nil {
		t.Fatal("accepted invalid config")
	}
	if a.state.Revision != strings.Repeat("a", 64) {
		t.Fatal("advanced failed revision")
	}
}
func TestRestartFailureRestoresPreviousConfig(t *testing.T) {
	a := testAgent(t)
	calls := 0
	a.restart = func() error {
		calls++
		if calls == 2 {
			return errors.New("failed")
		}
		return nil
	}
	if err := a.apply(snapshot("a")); err != nil {
		t.Fatal(err)
	}
	old, _ := os.ReadFile(filepath.Join(a.opts.DataDir, "config.json"))
	changed := snapshot("b")
	changed.Config = json.RawMessage(`{"inbounds":[]}`)
	if err := a.apply(changed); err == nil {
		t.Fatal("accepted restart failure")
	}
	restored, _ := os.ReadFile(filepath.Join(a.opts.DataDir, "config.json"))
	if !bytes.Equal(old, restored) || calls != 3 || a.state.Revision != strings.Repeat("a", 64) {
		t.Fatal("rollback failed")
	}
}
func TestStateWriteFailureRestoresPreviousConfig(t *testing.T) {
	a := testAgent(t)
	a.restart = func() error { return nil }
	path := filepath.Join(a.opts.DataDir, "config.json")
	old := []byte(`{"inbounds":[]}`)
	os.WriteFile(path, old, 0600)
	os.Mkdir(filepath.Join(a.opts.DataDir, "state.json"), 0700)
	if err := a.apply(snapshot("a")); err == nil {
		t.Fatal("accepted failed state save")
	}
	got, _ := os.ReadFile(path)
	if !bytes.Equal(old, got) || a.state.Revision != "" {
		t.Fatal("state failure did not roll back")
	}
}
func TestCoreVersionMismatchDoesNotRestart(t *testing.T) {
	a := testAgent(t)
	a.restart = func() error { t.Fatal("must not restart"); return nil }
	s := snapshot("a")
	s.CoreVersion = "1.15.0"
	if err := a.apply(s); err == nil {
		t.Fatal("accepted unsupported core")
	}
}
func TestHTTPSOnlyAndLocalException(t *testing.T) {
	for _, tc := range []struct {
		url          string
		local, valid bool
	}{{"http://example.com/a", false, false}, {"https://example.com/a", false, true}, {"http://127.0.0.1/a", true, true}, {"http://example.com/a", true, false}, {"https://user:pass@example.com/a", false, false}} {
		path := filepath.Join(t.TempDir(), "agent.json")
		b, _ := json.Marshal(Options{URL: tc.url, Token: strings.Repeat("a", 64), AllowLocalHTTP: tc.local})
		os.WriteFile(path, b, 0600)
		_, err := loadOptions(path)
		if (err == nil) != tc.valid {
			t.Fatalf("%s: %v", tc.url, err)
		}
	}
}
func TestHeartbeatContinuesWhileApplyBlocked(t *testing.T) {
	a := testAgent(t)
	started := make(chan struct{})
	release := make(chan struct{})
	a.restart = func() error { close(started); <-release; return nil }
	var beats atomic.Int32
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer "+a.opts.Token {
			t.Error("missing auth")
		}
		beats.Add(1)
		fmt.Fprint(w, `{"revision":"next","heartbeat_interval":10}`)
	}))
	defer server.Close()
	a.opts.URL = server.URL
	a.client = server.Client()
	done := make(chan error, 1)
	go func() { done <- a.apply(snapshot("a")) }()
	<-started
	if _, err := a.heartbeat(context.Background()); err != nil {
		t.Fatal(err)
	}
	close(release)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if beats.Load() != 1 {
		t.Fatal("heartbeat blocked")
	}
}
func TestEventReconnectTriggersReconciliation(t *testing.T) {
	a := testAgent(t)
	var requests atomic.Int32
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "event: config\ndata: {}\n\n")
	}))
	defer server.Close()
	a.opts.URL = server.URL
	a.streamClient = server.Client()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { a.events(ctx); close(done) }()
	select {
	case <-a.notify:
	case <-time.After(2 * time.Second):
		t.Fatal("lost event")
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("stream did not cancel")
	}
}
func TestDownloadRejectsWrongChecksum(t *testing.T) {
	data := []byte("binary")
	hash := sha256.Sum256(data)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write(data) }))
	defer server.Close()
	req, _ := http.NewRequest("GET", server.URL, nil)
	if _, err := checkedDownload(server.Client(), req, strings.Repeat("0", 64)); err == nil {
		t.Fatal("accepted corrupted asset")
	}
	req, _ = http.NewRequest("GET", server.URL, nil)
	got, err := checkedDownload(server.Client(), req, hex.EncodeToString(hash[:]))
	if err != nil || !bytes.Equal(got, data) {
		t.Fatal(err)
	}
}
func TestNoCredentialRedirect(t *testing.T) {
	a := testAgent(t)
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { t.Error("followed redirect") }))
	defer target.Close()
	origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, target.URL, 302) }))
	defer origin.Close()
	a.opts.URL = origin.URL
	if _, err := a.request(context.Background(), "GET", "config", nil); err == nil {
		t.Fatal("accepted redirect")
	}
}
func TestIndependentVersionComparison(t *testing.T) {
	if !newer("0.1.10", "0.1.9") || newer("0.1.0", "0.2.0") || newer("0.1.0", "0.1.0") || newer("bad", "0.1.0") {
		t.Fatal("incorrect comparison")
	}
}

func TestDeletionAckFromReconcileStopsRun(t *testing.T) {
	a := testAgent(t)
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"revision":"done","heartbeat_interval":10,"deleted":true}`)
	}))
	defer server.Close()
	a.opts.URL = server.URL
	a.client = server.Client()
	for i := 0; i < 2; i++ {
		if _, err := a.heartbeat(context.Background()); err != nil {
			t.Fatal(err)
		}
	}
	select {
	case <-a.deleted:
	default:
		t.Fatal("deletion was not propagated")
	}
}
