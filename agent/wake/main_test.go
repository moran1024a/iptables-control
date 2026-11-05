package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func signed(key []byte, stamp int64) []byte {
	body := "IWK1|" + strconv.FormatInt(stamp, 10) + "|" + strings.Repeat("a", 32) + "|" + strings.Repeat("b", 32)
	h := hmac.New(sha256.New, key)
	h.Write([]byte(body))
	return []byte(body + "|" + hex.EncodeToString(h.Sum(nil)))
}

func TestAuthenticationAndPersistedReplay(t *testing.T) {
	key := []byte("test authentication key")
	packet := signed(key, 1700000000)
	token, nonce, stamp, err := verify(packet, key, 1700000000)
	if err != nil || token != strings.Repeat("a", 32) {
		t.Fatal("valid packet rejected", err)
	}
	for _, now := range []int64{1699999900, 1700000100} {
		if _, _, _, err := verify(packet, key, now); err == nil {
			t.Fatal("expired packet accepted")
		}
	}
	packet[len(packet)-1] ^= 1
	if _, _, _, err := verify(packet, key, 1700000000); err == nil {
		t.Fatal("tampered packet accepted")
	}
	path := filepath.Join(t.TempDir(), "replay.json")
	guard, err := openGuard(path)
	if err != nil {
		t.Fatal(err)
	}
	if !guard.accept(nonce, stamp, 1700000000) {
		t.Fatal("new nonce rejected")
	}
	restarted, err := openGuard(path)
	if err != nil {
		t.Fatal(err)
	}
	if restarted.accept(nonce, stamp, 1700000000) {
		t.Fatal("replayed nonce accepted after restart")
	}
}

func TestDefaultDisabledAndStrictAllowlist(t *testing.T) {
	path := filepath.Join(t.TempDir(), "wake.conf")
	if _, err := loadConfig(path); err == nil {
		t.Fatal("missing config enables listener")
	}
	os.WriteFile(path, []byte("ENABLED=0\nPORT=39001\nALLOW=127.0.0.1\n"), 0600)
	if _, err := loadConfig(path); err == nil {
		t.Fatal("disabled config enables listener")
	}
	for _, allow := range []string{"", "0.0.0.0", "::", "192.0.2.1/24", "example.com", "224.0.0.1"} {
		if _, err := parseConfig("39001", allow); err == nil {
			t.Fatal("invalid allowlist accepted", allow)
		}
	}
}

func TestUDPIsSilentAndOnlyAuthenticatedAllowlistedPacketsTrigger(t *testing.T) {
	conn, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	guard, err := openGuard(filepath.Join(t.TempDir(), "replay.json"))
	if err != nil {
		t.Fatal(err)
	}
	key := []byte("test key")
	requests := make(chan string, 1)
	c := config{Allowed: map[string]bool{"127.0.0.1": true}}
	done := make(chan struct{})
	go func() { receive(conn, c, key, guard, requests); close(done) }()
	defer func() { conn.Close(); <-done }()
	send := func(source string, data []byte) {
		peer, err := net.DialUDP("udp4", &net.UDPAddr{IP: net.ParseIP(source)}, conn.LocalAddr().(*net.UDPAddr))
		if err != nil {
			t.Fatal(err)
		}
		defer peer.Close()
		if _, err = peer.Write(data); err != nil {
			t.Fatal(err)
		}
		peer.SetReadDeadline(time.Now().Add(80 * time.Millisecond))
		var buffer [256]byte
		_, err = peer.Read(buffer[:])
		if ne, ok := err.(net.Error); !ok || !ne.Timeout() {
			t.Fatal("receiver did not stay silent", err)
		}
	}
	send("127.0.0.1", []byte("invalid request"))
	send("127.0.0.2", signed(key, time.Now().Unix()))
	select {
	case <-requests:
		t.Fatal("unauthorized request triggered sync")
	default:
	}
	packet := signed(key, time.Now().Unix())
	send("127.0.0.1", packet)
	select {
	case token := <-requests:
		if token != strings.Repeat("a", 32) {
			t.Fatal(token)
		}
	default:
		t.Fatal("no sync request")
	}
	send("127.0.0.1", packet)
	select {
	case <-requests:
		t.Fatal("replayed request triggered sync")
	default:
	}
}
