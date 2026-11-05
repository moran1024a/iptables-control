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
