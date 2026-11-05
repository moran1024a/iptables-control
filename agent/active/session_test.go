package main

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"github.com/coder/websocket"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func TestTLSControlAndDurablePause(t *testing.T) {
	root := t.TempDir()
	bin := filepath.Join(root, "bin")
	if err := os.Mkdir(bin, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(bin, "nft"), []byte("#!/bin/sh\necho '{\"nftables\":[]}'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	for _, tool := range []string{"ss", "iptables-save"} {
		if err := os.WriteFile(filepath.Join(bin, tool), []byte("#!/bin/sh\nexit 0\n"), 0700); err != nil {
			t.Fatal(err)
		}
	}
	socket, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	port := socket.LocalAddr().(*net.UDPAddr).Port
	socket.Close()
	probeKey := []byte("0123456789abcdef0123456789abcdef")
	t.Setenv("PATH", bin+":"+os.Getenv("PATH"))
	a := &Agent{root: root, key: "test-key", installation: randomID(), usage: map[string]*Usage{}}
	a.stateMu = &sync.Mutex{}
	if err := atomicBytes(a.path("active/enabled"), []byte("confirmed\n"), 0600); err != nil {
		t.Fatal(err)
	}
	localCtx, stopLocal := context.WithCancel(context.Background())
	localDone := make(chan struct{})
	go func() { defer close(localDone); a.maintain(localCtx, time.Millisecond) }()
	defer func() { stopLocal(); <-localDone }()
	done := make(chan string, 1)
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer test-key" || r.URL.Path != "/v3/ws" {
			done <- "wrong authentication or path"
			w.WriteHeader(401)
			return
		}
		ws, err := websocket.Accept(w, r, nil)
		if err != nil {
			done <- err.Error()
			return
		}
		defer ws.CloseNow()
		ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
		defer cancel()
		_, raw, err := ws.Read(ctx)
		if err != nil {
			done <- err.Error()
			return
		}
		var hello M
		_ = json.Unmarshal(raw, &hello)
		if hello["type"] != "hello" {
			done <- "missing hello"
			return
		}
		for _, kind := range []string{"welcome", "ready"} {
			data, _ := json.Marshal(M{"type": kind, "session": "test-session"})
			if err = ws.Write(ctx, websocket.MessageText, data); err != nil {
				done <- err.Error()
				return
			}
		}
		for _, step := range []struct {
			command  M
			response string
		}{
			{M{"type": "quality_setup", "id": "setup", "port": port, "key": hex.EncodeToString(probeKey)}, "quality_listener"},
			{M{"type": "quality_probe", "id": "probe", "host": "127.0.0.1", "port": port, "key": hex.EncodeToString(probeKey)}, "quality_result"},
		} {
			step.command["session"] = "test-session"
			data, _ := json.Marshal(step.command)
			if err := ws.Write(ctx, websocket.MessageText, data); err != nil {
				done <- err.Error()
				return
			}
			_, data, err = ws.Read(ctx)
			if err != nil {
				done <- err.Error()
				return
			}
			var result M
			_ = json.Unmarshal(data, &result)
			if result["type"] != step.response || (result["error"] != nil && result["error"] != "") || (step.response == "quality_result" && result["status"] != "ok") {
				done <- string(data)
				return
			}
		}
		data, _ := json.Marshal(M{"type": "quiesce", "session": "test-session"})
		if err = ws.Write(ctx, websocket.MessageText, data); err != nil {
			done <- err.Error()
			return
		}
		_, raw, err = ws.Read(ctx)
		if err != nil {
			done <- err.Error()
			return
		}
		var ack M
		_ = json.Unmarshal(raw, &ack)
		if ack["type"] != "quiesced" || ack["session"] != "test-session" {
			done <- "pause not acknowledged"
			return
		}
		done <- ""
	}))
	defer server.Close()
	saved := http.DefaultClient
	http.DefaultClient = server.Client()
	defer func() { http.DefaultClient = saved }()
	a.url = server.URL
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := a.session(ctx); err != nil {
		t.Fatal(err)
	}
	if message := <-done; message != "" {
		t.Fatal(message)
	}
	if _, err := os.Stat(a.path("paused")); err != nil {
		t.Fatal("pause did not survive process exit", err)
	}
}

func TestOfflineMaintenancePersistsCountersAndStopsWhenPaused(t *testing.T) {
	root := t.TempDir()
	a := &Agent{root: root, installation: randomID(), usage: map[string]*Usage{}, stateMu: &sync.Mutex{}}
	bin := filepath.Join(root, "bin")
	if err := os.Mkdir(bin, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(bin, "nft"), []byte("#!/bin/sh\ncat \"$ICTL_TEST_NFT\"\n"), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", bin+":"+os.Getenv("PATH"))
	t.Setenv("ICTL_TEST_NFT", filepath.Join(root, "kernel"))
	epoch := randomID()
	setCounter := func(value uint64) {
		t.Helper()
		doc := M{"nftables": []any{obj("table", M{"family": "ip", "name": tableName}), obj("chain", M{"name": a.ownerChain()}), obj("counter", M{"name": "r1_udp_up_" + epoch, "bytes": value, "packets": 1})}}
		if err := atomicJSON(filepath.Join(root, "kernel"), doc); err != nil {
			t.Fatal(err)
		}
	}
	if err := atomicBytes(a.path("active/enabled"), []byte("confirmed\n"), 0600); err != nil {
		t.Fatal(err)
	}
	setCounter(100)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); a.maintain(ctx, 10*time.Millisecond) }()
	defer func() { cancel(); <-done }()
	waitCounter := func(want string) {
		t.Helper()
		deadline := time.Now().Add(2 * time.Second)
		for time.Now().Before(deadline) {
			var rows map[string]Usage
			if readJSON(a.path("active/traffic.json"), &rows) == nil {
				for _, r := range rows {
					if r.Up == want {
						return
					}
				}
			}
			time.Sleep(5 * time.Millisecond)
		}
		t.Fatal("offline counter was not persisted", want)
	}
	waitCounter("100")
	setCounter(900)
	waitCounter("900")
	if err := atomicBytes(a.path("paused"), []byte("manual confirmation required\n"), 0600); err != nil {
		t.Fatal(err)
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("maintenance continued while paused")
	}
}
