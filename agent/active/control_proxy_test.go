package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/coder/websocket"
)

func proxyURI(method, key string) string {
	return "ss://" + url.UserPassword(method, key).String() + "@127.0.0.1:18888#test"
}

func withProxyInput(t *testing.T, data string, run func() error) error {
	t.Helper()
	path := filepath.Join(t.TempDir(), "stdin")
	if err := os.WriteFile(path, []byte(data), 0600); err != nil {
		t.Fatal(err)
	}
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	old := os.Stdin
	os.Stdin = f
	defer func() { os.Stdin = old; f.Close() }()
	return run()
}
func TestControlProxyParsingAndPersistence(t *testing.T) {
	key := base64.StdEncoding.EncodeToString(make([]byte, 32))
	valid := proxyURI("2022-blake3-aes-256-gcm", key)
	p, err := parseControlProxy(valid)
	if err != nil {
		t.Fatal(err)
	}
	encoded := "ss://" + base64.RawURLEncoding.EncodeToString([]byte(p.Method+":"+key)) + "@localhost:4567"
	if _, err := parseControlProxy(encoded); err != nil {
		t.Fatal(err)
	}
	for _, text := range []string{strings.Replace(valid, "2022-blake3-aes-256-gcm", "aes-256-gcm", 1), valid[:strings.Index(valid, "#")] + "?plugin=foo", proxyURI(p.Method, "password"), "ss://bad@host:0", "http://127.0.0.1:8888"} {
		if _, err := parseControlProxy(text); err == nil {
			t.Fatalf("accepted invalid link: %q", text)
		}
	}
	path := filepath.Join(t.TempDir(), "proxy.json")
	if err := atomicJSON(path, p); err != nil {
		t.Fatal(err)
	}
	info, _ := os.Stat(path)
	if info.Mode().Perm() != 0600 {
		t.Fatal("credentials are not private")
	}
	got, err := readControlProxy(path)
	if err != nil || got != p {
		t.Fatal(got, err)
	}
	os.WriteFile(path, []byte("broken"), 0600)
	if _, err := readControlProxy(path); err == nil {
		t.Fatal("corrupt proxy silently fell back to direct")
	}
}

func TestControlTransportScopeAndEnvironment(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/redirect" {
			http.Redirect(w, r, "https://example.invalid/", 307)
			return
		}
		w.Write([]byte("ok"))
	}))
	defer server.Close()
	saved := http.DefaultClient
	http.DefaultClient = server.Client()
	defer func() { http.DefaultClient = saved }()
	t.Setenv("HTTPS_PROXY", "http://127.0.0.1:1")
	t.Setenv("ALL_PROXY", "http://127.0.0.1:1")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	client, closeClient, err := openControlClient(ctx, server.URL, controlProxy{}, "")
	if err != nil {
		t.Fatal(err)
	}
	defer closeClient()
	response, err := client.Get(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if _, err := client.Get("https://example.invalid/"); err == nil {
		t.Fatal("other destination accepted")
	}
	if _, err := client.Get(server.URL + "/redirect"); err == nil {
		t.Fatal("redirect accepted")
	}
	if http.DefaultClient != server.Client() {
		t.Fatal("global transport changed")
	}
	p := controlProxy{true, "127.0.0.1", 1, "2022-blake3-aes-256-gcm", base64.StdEncoding.EncodeToString(make([]byte, 32))}
	if _, _, err := openControlClient(ctx, server.URL, p, filepath.Join(t.TempDir(), "missing-helper")); err == nil {
		t.Fatal("missing helper fell back to direct")
	}
}

func TestControlLoopbackDoesNotBlockForwardingPort(t *testing.T) {
	a := fakeAppliedAgent(t)
	port := strconv.Itoa(a.config.Rules[0].Port)
	for _, host := range []string{"127.0.0.1", "0.0.0.0", "192.0.2.1", "::"} {
		address := net.JoinHostPort(host, port)
		script := "#!/bin/sh\necho 'LISTEN 0 128 " + address + " 0.0.0.0:*'\n"
		if err := os.WriteFile(a.path("ss"), []byte(script), 0700); err != nil {
			t.Fatal(err)
		}
		err := a.checkConflictsWith(a.config, M{"nftables": []any{}})
		if (err == nil) != (host == "127.0.0.1") {
			t.Fatalf("host %s: %v", host, err)
		}
	}
}

func TestControlReloadReconnectsWithoutReapplyingRules(t *testing.T) {
	a := fakeAppliedAgent(t)
	if err := atomicBytes(a.path("active/enabled"), []byte("confirmed"), 0600); err != nil {
		t.Fatal(err)
	}
	connected := make(chan struct{}, 4)
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ws, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer ws.CloseNow()
		ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
		defer cancel()
		if _, _, err = ws.Read(ctx); err != nil {
			return
		}
		if err = ws.Write(ctx, websocket.MessageText, []byte(`{"type":"welcome","session":"reload-test"}`)); err != nil {
			return
		}
		connected <- struct{}{}
		ws.Read(ctx)
	}))
	defer server.Close()
	old := http.DefaultClient
	http.DefaultClient = server.Client()
	defer func() { http.DefaultClient = old }()
	a.url = server.URL
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	done := make(chan error, 1)
	go func() { done <- a.daemon(ctx) }()
	defer func() { cancel(); <-done }()
	waitConnected := func() {
		t.Helper()
		select {
		case <-connected:
		case <-ctx.Done():
			t.Fatal("daemon did not reconnect")
		}
	}
	waitConnected()
	before, _ := os.ReadFile(a.path("writes"))
	if err := syscall.Kill(os.Getpid(), syscall.SIGHUP); err != nil {
		t.Fatal(err)
	}
	waitConnected()
	after, _ := os.ReadFile(a.path("writes"))
	if string(before) != string(after) {
		t.Fatal("management reload rewrote forwarding rules")
	}
}

func TestSS2022ControlIntegration(t *testing.T) {
	bin := os.Getenv("ICTL_SS_TEST_DIR")
	if bin == "" {
		t.Skip("set ICTL_SS_TEST_DIR to a verified shadowsocks-rust binary directory")
	}
	for _, method := range []string{"2022-blake3-aes-128-gcm", "2022-blake3-aes-256-gcm", "2022-blake3-chacha20-poly1305"} {
		t.Run(method, func(t *testing.T) {
			server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/ws" {
					ws, err := websocket.Accept(w, r, nil)
					if err != nil {
						return
					}
					defer ws.CloseNow()
					ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
					defer cancel()
					kind, data, err := ws.Read(ctx)
					if err == nil {
						ws.Write(ctx, kind, data)
					}
					return
				}
				io.Copy(w, r.Body)
			}))
			server.EnableHTTP2 = true
			server.StartTLS()
			defer server.Close()
			useHTTP2DefaultClient(t, server)
			size := 32
			if strings.Contains(method, "128") {
				size = 16
			}
			key := base64.StdEncoding.EncodeToString(make([]byte, size))
			l, err := net.Listen("tcp4", "127.0.0.1:0")
			if err != nil {
				t.Fatal(err)
			}
			port := l.Addr().(*net.TCPAddr).Port
			l.Close()
			dir := t.TempDir()
			config := filepath.Join(dir, "server.json")
			data, _ := json.Marshal(map[string]any{"server": "127.0.0.1", "server_port": port, "method": method, "password": key, "mode": "tcp_only"})
			os.WriteFile(config, data, 0600)
			cmd := exec.Command(filepath.Join(bin, "ssserver"), "-c", config)
			if err = cmd.Start(); err != nil {
				t.Fatal(err)
			}
			stopped := false
			defer func() {
				if !stopped {
					cmd.Process.Kill()
					cmd.Wait()
				}
			}()
			for i := 0; i < 100; i++ {
				c, e := net.DialTimeout("tcp4", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)), 20*time.Millisecond)
				if e == nil {
					c.Close()
					break
				}
				time.Sleep(20 * time.Millisecond)
			}
			p := controlProxy{true, "127.0.0.1", port, method, key}
			ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
			defer cancel()
			client, closeClient, err := openControlClient(ctx, server.URL, p, filepath.Join(bin, "sslocal"))
			if err != nil {
				t.Fatal(err)
			}
			defer closeClient()
			req, _ := http.NewRequestWithContext(ctx, "POST", server.URL, strings.NewReader("control-body"))
			response, err := client.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			body, _ := io.ReadAll(response.Body)
			response.Body.Close()
			if string(body) != "control-body" || response.ProtoMajor != 1 {
				t.Fatal(string(body), response.Proto)
			}
			ws, _, err := websocket.Dial(ctx, "wss"+strings.TrimPrefix(server.URL, "https")+"/ws", &websocket.DialOptions{HTTPClient: client})
			if err != nil {
				t.Fatal(err)
			}
			if err = ws.Write(ctx, websocket.MessageText, []byte("heartbeat")); err != nil {
				t.Fatal(err)
			}
			_, body, err = ws.Read(ctx)
			ws.CloseNow()
			if err != nil || string(body) != "heartbeat" {
				t.Fatal(string(body), err)
			}
			if strings.Contains(method, "256") {
				profile := filepath.Join(dir, "profile.json")
				helper := filepath.Join(bin, "sslocal")
				uri := "ss://" + url.UserPassword(method, key).String() + "@127.0.0.1:" + strconv.Itoa(port)
				if err := withProxyInput(t, uri, func() error {
					return proxyCommand("set", "", profile, server.URL, helper, "", 5)
				}); err != nil {
					t.Fatal(err)
				}
				got, err := readControlProxy(profile)
				if err != nil || got != p {
					t.Fatal(got, err)
				}
				// A bad replacement must leave the working profile byte-for-byte intact.
				previous, _ := os.ReadFile(profile)
				wrongKey := base64.StdEncoding.EncodeToString([]byte(strings.Repeat("x", 32)))
				badURI := "ss://" + url.UserPassword(method, wrongKey).String() + "@127.0.0.1:" + strconv.Itoa(port)
				if err := withProxyInput(t, badURI, func() error {
					return proxyCommand("set", "", profile, server.URL, helper, "", 1)
				}); err == nil {
					t.Fatal("bad credentials replaced working proxy")
				}
				after, _ := os.ReadFile(profile)
				if string(after) != string(previous) {
					t.Fatal("failed replacement changed profile")
				}
				output := filepath.Join(dir, "response")
				if err := withProxyInput(t, "encrypted-manager-body", func() error {
					return proxyCommand("request", "", profile, server.URL, helper, output, 5)
				}); err != nil {
					t.Fatal(err)
				}
				body, _ = os.ReadFile(output)
				if string(body) != "encrypted-manager-body" {
					t.Fatal("manager response corrupted", string(body))
				}
				if err := proxyCommand("disable", "", profile, server.URL, helper, "", 5); err != nil {
					t.Fatal(err)
				}
				got, _ = readControlProxy(profile)
				if got.Enabled || got.Password != key {
					t.Fatal("disable lost saved credentials")
				}
				if err := proxyCommand("enable", "", profile, server.URL, helper, "", 5); err != nil {
					t.Fatal(err)
				}
			}
			// Proxy failure must not use the still reachable direct TLS endpoint.
			cmd.Process.Kill()
			cmd.Wait()
			stopped = true
			client.CloseIdleConnections()
			broken, cancelBroken := context.WithTimeout(ctx, time.Second)
			defer cancelBroken()
			req, _ = http.NewRequestWithContext(broken, "GET", server.URL, nil)
			if response, err = client.Do(req); err == nil {
				response.Body.Close()
				t.Fatal("proxy loss fell back to direct")
			}
			if response, err = server.Client().Get(server.URL); err != nil {
				t.Fatal("direct endpoint was not available", err)
			}
			response.Body.Close()
		})
	}
}
