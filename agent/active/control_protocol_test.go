package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// Match production's default Transport, including its initialized h2 ALPN list.
// A plain httptest.NewTLSServer only speaks HTTP/1 and cannot catch this bug.
func useHTTP2DefaultClient(t *testing.T, server *httptest.Server) {
	t.Helper()
	previousClient, previousTransport := http.DefaultClient, http.DefaultTransport
	base := http.DefaultTransport.(*http.Transport).Clone()
	if base.TLSClientConfig == nil {
		base.TLSClientConfig = server.Client().Transport.(*http.Transport).TLSClientConfig.Clone()
	} else {
		base.TLSClientConfig.RootCAs = server.Client().Transport.(*http.Transport).TLSClientConfig.RootCAs
	}
	http.DefaultTransport = base
	http.DefaultClient = &http.Client{}
	t.Cleanup(func() {
		base.CloseIdleConnections()
		http.DefaultClient, http.DefaultTransport = previousClient, previousTransport
	})
}

func TestControlRequestsAndTrafficWithHTTP2Server(t *testing.T) {
	a := fakeAppliedAgent(t)
	u := &Usage{RuleID: 1, Protocol: "tcp", Epoch: randomID(), Up: "10", Down: "20", UpPackets: "1", DownPackets: "2", Quality: "ok"}
	a.usage[usageKey(*u)] = u
	a.key = "test-key"
	var uploads atomic.Int32
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v3/traffic" {
			var rows []Usage
			if r.Method != "POST" || r.ProtoMajor != 1 || r.Header.Get("Authorization") != "Bearer test-key" || json.NewDecoder(r.Body).Decode(&rows) != nil || len(rows) == 0 {
				w.WriteHeader(400)
				return
			}
			uploads.Add(1)
		} else {
			io.Copy(io.Discard, r.Body)
		}
		io.WriteString(w, r.Proto)
	}))
	server.EnableHTTP2 = true
	server.StartTLS()
	defer server.Close()
	useHTTP2DefaultClient(t, server)
	a.url = server.URL
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	client, closeClient, err := a.controlClient(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer closeClient()
	for _, method := range []string{"GET", "POST"} {
		req, _ := http.NewRequestWithContext(ctx, method, server.URL+"/control", strings.NewReader("control-body"))
		response, err := client.Do(req)
		if err != nil {
			t.Fatalf("%s failed: %v", method, err)
		}
		body, _ := io.ReadAll(response.Body)
		response.Body.Close()
		// HTTP/1-only transports may omit ALPN and use the TLS default HTTP/1.1.
		if string(body) != "HTTP/1.1" || (response.TLS.NegotiatedProtocol != "" && response.TLS.NegotiatedProtocol != "http/1.1") {
			t.Fatalf("management protocol mismatch: %s / %s", body, response.TLS.NegotiatedProtocol)
		}
	}
	if err := a.flushTraffic(); err != nil {
		t.Fatal(err)
	}
	if uploads.Load() == 0 {
		t.Fatal("empty statistics skipped the regression scenario")
	}
	output := filepath.Join(t.TempDir(), "response")
	if err := withProxyInput(t, "manager-body", func() error {
		return proxyCommand("request", "", "", server.URL+"/control", "", output, 5)
	}); err != nil {
		t.Fatal(err)
	}
	if body, _ := os.ReadFile(output); string(body) != "HTTP/1.1" {
		t.Fatalf("unexpected manager response: %s", body)
	}
	// The fix must not disable HTTP/2 on unrelated clients or the global transport.
	req, _ := http.NewRequestWithContext(ctx, "GET", server.URL+"/default", nil)
	response, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.ProtoMajor != 2 {
		t.Fatal("global HTTP/2 behavior changed")
	}
}
