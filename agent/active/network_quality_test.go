package main

import (
	"context"
	"encoding/binary"
	"net"
	"os"
	"testing"
	"time"
)

func TestICMPReplyValidation(t *testing.T) {
	if icmpChecksum([]byte{0, 1, 2, 3, 4, 5}) != 0xf9f6 || icmpChecksum([]byte{0, 1, 2}) != 0xfdfe {
		t.Fatal("incorrect Internet checksum")
	}
	request := icmpRequest([]byte("0123456789abcdef"))
	reply := append([]byte{}, request...)
	reply[0], reply[2], reply[3] = 0, 0, 0
	binary.BigEndian.PutUint16(reply[2:4], icmpChecksum(reply))
	if !validICMPReply(reply, request) {
		t.Fatal("valid echo reply rejected")
	}
	if validICMPReply(request, request) {
		t.Fatal("request treated as reply")
	}
	reply[10] ^= 1
	if validICMPReply(reply, request) {
		t.Fatal("damaged reply accepted")
	}
	reply[2], reply[3] = 0, 0
	binary.BigEndian.PutUint16(reply[2:4], icmpChecksum(reply))
	if validICMPReply(reply, request) {
		t.Fatal("unrelated reply accepted")
	}
}

func TestICMPKernelPing(t *testing.T) {
	if os.Getenv("ICTL_ICMP_TEST") != "1" {
		t.Skip("requires CAP_NET_RAW and isolated loopback")
	}
	result := probeNetworkQuality(context.Background(), "127.0.0.1", 0, nil, "icmp-echo-v1")
	if result["status"] != "ok" || result["rtt"].(float64) < 0 {
		t.Fatal(result)
	}
}

func TestQualityAuthenticatedUDPAndCancel(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	conn, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	key := []byte("0123456789abcdef0123456789abcdef")
	port := conn.LocalAddr().(*net.UDPAddr).Port
	finished := make(chan struct{})
	go func() { serveQuality(ctx, conn, key); close(finished) }()
	result := probeQuality(ctx, "127.0.0.1", port, key)
	if result["status"] != "ok" || result["rtt"].(float64) < 0 {
		t.Fatal(result)
	}
	client, err := net.DialUDP("udp4", nil, &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: port})
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	request := qualityPacket("IQP2", []byte("0123456789abcdef"), key)
	_, _ = client.Write(request)
	_ = client.SetReadDeadline(time.Now().Add(time.Second))
	b := make([]byte, 128)
	n, err := client.Read(b)
	if err != nil || !validQualityPacket(b[:n], "IQR2", key) {
		t.Fatalf("response: %v", err)
	}
	if validQualityPacket(request, "IQR2", key) {
		t.Fatal("request accepted as a response")
	}
	// Replayed and unauthenticated packets must not receive replies.
	_, _ = client.Write(request)
	request[30] ^= 1
	_, _ = client.Write(request)
	_ = client.SetReadDeadline(time.Now().Add(80 * time.Millisecond))
	if _, err = client.Read(b); err == nil {
		t.Fatal("replayed or forged request accepted")
	}
	cancel()
	select {
	case <-finished:
	case <-time.After(time.Second):
		t.Fatal("listener survived cancellation")
	}
	rebound, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: port})
	if err != nil {
		t.Fatal("probe port not released", err)
	}
	rebound.Close()
}

func TestQualityDoesNotAcceptUnrelatedEcho(t *testing.T) {
	conn, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	go func() {
		b := make([]byte, 128)
		n, addr, err := conn.ReadFromUDP(b)
		if err == nil {
			_, _ = conn.WriteToUDP(b[:n], addr)
		}
	}()
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	result := probeQuality(ctx, "127.0.0.1", conn.LocalAddr().(*net.UDPAddr).Port, []byte("0123456789abcdef0123456789abcdef"))
	if result["status"] == "ok" {
		t.Fatal("unrelated echo accepted", result)
	}
}

func TestQualitySocketYieldsToForwarding(t *testing.T) {
	conn, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	port := conn.LocalAddr().(*net.UDPAddr).Port
	q := &qualityMonitor{listener: conn, listenerPort: port, listenerStatus: M{"id": "setup"}, send: func(M) error { return nil }}
	a := &Agent{root: t.TempDir()}
	// No firewall commands: the invalid config still exercises socket handover.
	err = q.applyConfig(a, Config{Rules: []Rule{{Port: port, Protocol: "udp"}}})
	if err == nil {
		t.Fatal("expected invalid config")
	}
	if q.listener != nil {
		t.Fatal("probe socket still owns forwarding port")
	}
	rebound, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: port})
	if err != nil {
		t.Fatal(err)
	}
	rebound.Close()
}
