package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"net"
	"os"
	"strconv"
	"sync"
	"syscall"
	"time"
)

// Probe packets never enter a forwarding port. Requests and replies have distinct
// authenticated prefixes, a random nonce, and equal lengths (no amplification).
func qualityPacket(prefix string, nonce, key []byte) []byte {
	b := append([]byte(prefix), nonce...)
	h := hmac.New(sha256.New, key)
	h.Write(b)
	return append(b, h.Sum(nil)...)
}

func validQualityPacket(b []byte, prefix string, key []byte) bool {
	return len(b) == 52 && string(b[:4]) == prefix && hmac.Equal(b, qualityPacket(prefix, b[4:20], key))
}

func serveQuality(ctx context.Context, conn *net.UDPConn, key []byte) {
	defer conn.Close()
	raw, err := conn.SyscallConn()
	if err != nil {
		return
	}
	var optionErr error
	if err = raw.Control(func(fd uintptr) {
		optionErr = syscall.SetsockoptInt(int(fd), syscall.IPPROTO_IP, syscall.IP_PKTINFO, 1)
	}); err != nil || optionErr != nil {
		return
	}
	stop := context.AfterFunc(ctx, func() { conn.Close() })
	defer stop()
	buffer := make([]byte, 128)
	oob := make([]byte, 128)
	window, count := time.Now(), 0
	seen := map[string]time.Time{}
	for {
		n, oobn, _, peer, err := conn.ReadMsgUDP(buffer, oob)
		if err != nil {
			return
		}
		now := time.Now()
		if now.Sub(window) >= time.Second {
			window, count = now, 0
			for nonce, at := range seen {
				if now.Sub(at) > 30*time.Second {
					delete(seen, nonce)
				}
			}
		}
		count++
		if count > 20 || !validQualityPacket(buffer[:n], "IQP2", key) {
			continue
		}
		nonce := string(buffer[4:20])
		if _, duplicate := seen[nonce]; duplicate {
			continue
		}
		seen[nonce] = now
		// Reply from the exact local address that received the request.
		control := append([]byte(nil), oob[:oobn]...)
		messages, parseErr := syscall.ParseSocketControlMessage(control)
		if parseErr != nil {
			continue
		}
		for _, message := range messages {
			if message.Header.Level == syscall.IPPROTO_IP && message.Header.Type == syscall.IP_PKTINFO && len(message.Data) >= 12 {
				copy(message.Data[4:8], message.Data[8:12])
			}
		}
		_, _, _ = conn.WriteMsgUDP(qualityPacket("IQR2", buffer[4:20], key), control, peer)
	}
}

func probeQuality(ctx context.Context, host string, port int, key []byte) M {
	return probeNetworkQuality(ctx, host, port, key, "udp-echo-v1")
}

// RFC 792 Echo uses a checksum, identifier, sequence and an echoed payload.
// A fresh random payload prevents an unrelated or delayed reply becoming a sample.
func icmpChecksum(b []byte) uint16 {
	var sum uint32
	for len(b) >= 2 {
		sum += uint32(binary.BigEndian.Uint16(b))
		b = b[2:]
	}
	if len(b) > 0 {
		sum += uint32(b[0]) << 8
	}
	for sum>>16 != 0 {
		sum = (sum & 0xffff) + (sum >> 16)
	}
	return ^uint16(sum)
}

func icmpRequest(nonce []byte) []byte {
	b := make([]byte, 8+len(nonce))
	b[0] = 8
	copy(b[4:8], nonce[:4])
	copy(b[8:], nonce)
	binary.BigEndian.PutUint16(b[2:4], icmpChecksum(b))
	return b
}

func validICMPReply(b, request []byte) bool {
	return len(b) == len(request) && len(b) >= 8 && b[0] == 0 && b[1] == 0 &&
		icmpChecksum(b) == 0 && bytes.Equal(b[4:], request[4:])
}

func routeDialer(protocol string, profiles ...routeProfile) *net.Dialer {
	dialer := &net.Dialer{}
	if len(profiles) > 0 {
		p := profiles[0]
		ip := net.ParseIP(p.Local)
		switch protocol {
		case "tcp-connect-v1":
			dialer.LocalAddr = &net.TCPAddr{IP: ip}
		case "udp-echo-v1":
			dialer.LocalAddr = &net.UDPAddr{IP: ip}
		case "icmp-echo-v1":
			dialer.LocalAddr = &net.IPAddr{IP: ip}
		}
		dialer.Control = func(network, address string, raw syscall.RawConn) error {
			var err error
			if e := raw.Control(func(fd uintptr) { err = syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_MARK, p.mark()) }); e != nil {
				return e
			}
			return err
		}
	}
	return dialer
}

func probeNetworkQuality(ctx context.Context, host string, port int, key []byte, protocol string, profiles ...routeProfile) M {
	dialer := routeDialer(protocol, profiles...)
	result := M{"status": "error"}
	ctx, cancel := context.WithTimeout(ctx, 6*time.Second)
	defer cancel()
	dnsCtx, done := context.WithTimeout(ctx, 2*time.Second)
	ips, err := net.DefaultResolver.LookupIP(dnsCtx, "ip4", host)
	done()
	if err != nil || len(ips) == 0 {
		result["error"] = "无法解析探测 IPv4 地址"
		return result
	}
	result["resolved_ip"] = ips[0].String()
	if protocol == "tcp-connect-v1" {
		probeCtx, stop := context.WithTimeout(ctx, 3*time.Second)
		defer stop()
		start := time.Now()
		conn, err := dialer.DialContext(probeCtx, "tcp4", net.JoinHostPort(ips[0].String(), strconv.Itoa(port)))
		if err != nil {
			result["error"] = "TCP 建连失败: " + err.Error()
			if timeout, ok := err.(net.Error); ok && timeout.Timeout() && ctx.Err() == nil {
				result["status"] = "timeout"
			}
			return result
		}
		result["status"], result["rtt"] = "ok", float64(time.Since(start).Microseconds())/1000
		conn.Close() // No application payload is sent to the target service.
		return result
	}
	network, address := "udp4", net.JoinHostPort(ips[0].String(), strconv.Itoa(port))
	if protocol == "icmp-echo-v1" {
		network, address = "ip4:icmp", ips[0].String()
	}
	c, err := dialer.DialContext(ctx, network, address)
	if err != nil {
		result["error"] = "无法启动探测: " + err.Error()
		return result
	}
	defer c.Close()
	stop := context.AfterFunc(ctx, func() { c.Close() })
	defer stop()
	nonce := make([]byte, 16)
	if _, err = rand.Read(nonce); err != nil {
		result["error"] = err.Error()
		return result
	}
	packet := qualityPacket("IQP2", nonce, key)
	if protocol == "icmp-echo-v1" {
		packet = icmpRequest(nonce)
	}
	_ = c.SetDeadline(time.Now().Add(3 * time.Second))
	start := time.Now() // Monotonic clock; DNS resolution is excluded from RTT.
	if _, err = c.Write(packet); err != nil {
		result["error"] = err.Error()
		return result
	}
	buffer := make([]byte, 128)
	for {
		var n int
		var err error
		if ip, ok := c.(*net.IPConn); ok {
			var peer *net.IPAddr
			n, peer, err = ip.ReadFromIP(buffer)
			if err == nil && (peer == nil || !peer.IP.Equal(ips[0])) {
				continue
			}
		} else {
			n, err = c.Read(buffer)
		}
		if err != nil {
			result["error"] = err.Error()
			if timeout, ok := err.(net.Error); ok && timeout.Timeout() && ctx.Err() == nil {
				result["status"] = "timeout"
				result["error"] = "探测包 3 秒内未收到响应"
				if protocol == "icmp-echo-v1" {
					result["error"] = "ICMP 3 秒内未收到响应（可能禁 Ping、限速或丢包）"
				}
			}
			return result
		}
		matched := false
		if protocol == "icmp-echo-v1" {
			matched = validICMPReply(buffer[:n], packet)
		} else {
			matched = validQualityPacket(buffer[:n], "IQR2", key) && bytes.Equal(buffer[4:20], nonce)
		}
		if matched {
			result["status"] = "ok"
			result["rtt"] = float64(time.Since(start).Microseconds()) / 1000
			return result
		}
	}
}

type qualitySetup struct {
	body  M
	agent Agent // Snapshot; never mutate the forwarding agent from the probe worker.
}

type qualityMonitor struct {
	ctx            context.Context
	send           func(M) error
	setups         chan qualitySetup
	workers        chan struct{}
	mu             sync.Mutex
	last           time.Time
	setupCancel    context.CancelFunc
	applying       bool
	listenerMu     sync.Mutex
	listener       *net.UDPConn
	listenerPort   int
	listenerStatus M
}

func newQualityMonitor(ctx context.Context, send func(M) error) *qualityMonitor {
	q := &qualityMonitor{ctx: ctx, send: send, setups: make(chan qualitySetup, 1), workers: make(chan struct{}, 2)}
	go q.run()
	return q
}

func (q *qualityMonitor) setup(a *Agent, body M) {
	select {
	case <-q.setups:
	default:
	}
	select {
	case q.setups <- qualitySetup{body, *a}:
	case <-q.ctx.Done():
	}
}

// Stop a probe socket before applying a newly assigned forwarding port. Forwarding
// wins even if a disable/port-change message and a config arrive in reverse order.
func (q *qualityMonitor) applyConfig(a *Agent, c Config) error {
	return q.applyContext(a, c, context.Background())
}
func (q *qualityMonitor) applyContext(a *Agent, c Config, ctx context.Context) error {
	q.mu.Lock()
	q.applying = true
	if q.setupCancel != nil {
		q.setupCancel()
	}
	q.mu.Unlock()
	defer func() { q.mu.Lock(); q.applying = false; q.mu.Unlock() }()
	q.listenerMu.Lock()
	for _, r := range c.Rules {
		if q.listener != nil && r.Port == q.listenerPort && r.Protocol != "tcp" {
			q.listener.Close()
			q.listener = nil
			status := M{"type": "quality_listener", "id": q.listenerStatus["id"], "port": q.listenerPort, "error": "探测端口与新转发规则冲突，已停止探测"}
			go func() { _ = q.send(status) }()
			break
		}
	}
	q.listenerMu.Unlock()
	return a.applyContext(ctx, c)
}

func (q *qualityMonitor) run() {
	defer func() {
		q.listenerMu.Lock()
		defer q.listenerMu.Unlock()
		if q.listener != nil {
			q.listener.Close()
		}
	}()
	for {
		select {
		case <-q.ctx.Done():
			return
		case setup := <-q.setups:
			body, a := setup.body, &setup.agent
			port := int(number(body["port"]))
			key, err := hex.DecodeString(fmt.Sprint(body["key"]))
			if port < 0 || port > 65535 || err != nil || len(key) != 32 {
				continue
			}
			status := M{"type": "quality_listener", "id": body["id"], "port": port, "error": ""}
			q.listenerMu.Lock()
			q.mu.Lock()
			if q.applying {
				q.mu.Unlock()
				q.listenerMu.Unlock()
				status["error"] = "配置正在应用，稍后重试探测监听"
				if q.ctx.Err() == nil {
					_ = q.send(status)
				}
				continue
			}
			work, cancel := context.WithTimeout(q.ctx, 2*time.Second)
			q.setupCancel = cancel
			q.mu.Unlock()
			a.prepareCtx = work
			if q.listener != nil {
				q.listener.Close()
				q.listener = nil
			}
			q.listenerPort, q.listenerStatus = port, status
			if port != 0 {
				err = a.lockedContext(work, func() error {
					if q.ctx.Err() != nil {
						return q.ctx.Err()
					}
					var c Config
					if err := readJSON(a.path("active/config.json"), &c); err != nil {
						if !os.IsNotExist(err) {
							return err
						}
						c = a.config
					}
					for _, r := range append(append([]Rule{}, c.Rules...), c.Retired...) {
						if r.Port == port && r.Protocol != "tcp" {
							return fmt.Errorf("探测端口 UDP/%d 与转发配置冲突", port)
						}
					}
					if err := a.checkConflicts(Config{Rules: []Rule{{Protocol: "udp", Port: port}}}); err != nil {
						return err
					}
					if q.ctx.Err() != nil {
						return q.ctx.Err()
					}
					var err error
					q.listener, err = net.ListenUDP("udp4", &net.UDPAddr{Port: port})
					return err
				})
				if err == nil {
					go serveQuality(q.ctx, q.listener, key)
				} else {
					status["error"] = err.Error()
				}
			}
			q.mu.Lock()
			q.setupCancel = nil
			cancel()
			q.mu.Unlock()
			q.listenerMu.Unlock()
			if q.ctx.Err() == nil {
				_ = q.send(status)
			}
		}
	}
}

func (q *qualityMonitor) probe(body M) {
	q.probeWithAgent(nil, body)
}

func (q *qualityMonitor) probeWithAgent(a *Agent, body M) {
	var selected []routeProfile
	if key, _ := body["source_route"].(string); key != "" {
		if a != nil {
			for _, r := range a.config.Rules {
				if r.Route != "" {
					route, _ := parseRoute(r.Route)
					if route.Out.enabled() && route.Out.key() == key {
						selected = []routeProfile{route.Out}
						break
					}
				}
			}
		}
		if len(selected) == 0 {
			_ = q.send(M{"type": "quality_result", "id": body["id"], "status": "error", "error": "探测出口与已安装规则不一致"})
			return
		}
	}
	// Bind route probes to an installed rule before starting asynchronous I/O.
	// The caller provides an immutable configuration snapshot.
	if id := int64(number(body["rule_id"])); id != 0 {
		valid := false
		if a != nil && readText(a.path("paused")) == "" {
			for _, rule := range a.config.Rules {
				if rule.ID == id && rule.Host == body["host"] && rule.TargetPort == int(number(body["port"])) {
					valid = true
					if net.ParseIP(rule.IP) != nil {
						body["host"] = rule.IP
					}
					break
				}
			}
		}
		if !valid {
			_ = q.send(M{"type": "quality_result", "id": body["id"], "status": "error", "error": "探测路径与已安装规则不一致"})
			return
		}
	} else if body["protocol"] == "tcp-connect-v1" {
		return
	}
	q.mu.Lock()
	if time.Since(q.last) < 900*time.Millisecond {
		q.mu.Unlock()
		return
	}
	q.last = time.Now()
	q.mu.Unlock()
	select {
	case q.workers <- struct{}{}:
		go func() {
			defer func() { <-q.workers }()
			port := int(number(body["port"]))
			key, err := hex.DecodeString(fmt.Sprint(body["key"]))
			host, _ := body["host"].(string)
			protocol, _ := body["protocol"].(string)
			if protocol == "" {
				protocol = "udp-echo-v1"
			}
			if host == "" || (protocol != "icmp-echo-v1" && protocol != "udp-echo-v1" && protocol != "tcp-connect-v1") {
				return
			}
			if protocol == "udp-echo-v1" && (port < 1 || port > 65535 || err != nil || len(key) != 32) {
				return
			}
			if protocol == "tcp-connect-v1" && (port < 1 || port > 65535) {
				return
			}
			result := probeNetworkQuality(q.ctx, host, port, key, protocol, selected...)
			result["type"], result["id"] = "quality_result", body["id"]
			if q.ctx.Err() == nil {
				_ = q.send(result)
			}
		}()
	default:
	}
}
