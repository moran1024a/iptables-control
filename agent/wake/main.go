// A receive-only wake-up listener. Configuration and rule application stay in Bash.
package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const version = "0.6.3-rc.2"
const configPath = "/etc/ictl/wake.conf"
const replayPath = "/var/lib/ictl/wake-replay.json"

type config struct {
	Port    int
	Allowed map[string]bool
}

func values(path string) (map[string]string, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	m := map[string]string{}
	for _, line := range strings.Split(string(b), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			return nil, errors.New("invalid configuration")
		}
		if _, exists := m[k]; exists {
			return nil, errors.New("duplicate configuration key")
		}
		m[k] = v
	}
	return m, nil
}

func parseConfig(port, allow string) (config, error) {
	n, err := strconv.Atoi(port)
	c := config{Port: n, Allowed: map[string]bool{}}
	if err != nil || n < 1 || n > 65535 {
		return c, errors.New("端口必须为 1–65535")
	}
	for _, item := range strings.Split(allow, ",") {
		ip := net.ParseIP(strings.TrimSpace(item))
		if ip == nil || ip.IsUnspecified() || ip.IsMulticast() {
			return c, errors.New("白名单只接受明确的 IPv4/IPv6 地址")
		}
		c.Allowed[ip.String()] = true
	}
	if len(c.Allowed) > 16 {
		return c, errors.New("白名单最多 16 个 IP")
	}
	return c, nil
}

func loadConfig(path string) (config, error) {
	m, err := values(path)
	if err != nil {
		return config{}, err
	}
	if m["ENABLED"] != "1" {
		return config{}, errors.New("远程唤醒未启用")
	}
	return parseConfig(m["PORT"], m["ALLOW"])
}

func authKey(path string) ([]byte, error) {
	m, err := values(path)
	if err != nil {
		return nil, err
	}
	key, err := hex.DecodeString(m["KEY"])
	if err != nil || len(key) != 32 {
		return nil, errors.New("invalid node key")
	}
	h := hmac.New(sha256.New, key)
	h.Write([]byte("ictl-wake-v1"))
	return h.Sum(nil), nil
}

func verify(data, key []byte, now int64) (token, nonce string, stamp int64, err error) {
	err = errors.New("invalid wake-up")
	if len(data) > 192 {
		return
	}
	p := strings.Split(string(data), "|")
	if len(p) != 5 || p[0] != "IWK1" {
		return
	}
	stamp, e := strconv.ParseInt(p[1], 10, 64)
	if e != nil || stamp < now-60 || stamp > now+60 {
		return
	}
	for _, x := range p[2:4] {
		b, e := hex.DecodeString(x)
		if e != nil || len(b) != 16 || x != strings.ToLower(x) {
			return
		}
	}
	sig, e := hex.DecodeString(p[4])
	if e != nil {
		return
	}
	h := hmac.New(sha256.New, key)
	h.Write([]byte(strings.Join(p[:4], "|")))
	if !hmac.Equal(sig, h.Sum(nil)) {
		return
	}
	return p[2], p[3], stamp, nil
}

type replayGuard struct {
	mu   sync.Mutex
	seen map[string]int64
	path string
}

func openGuard(path string) (*replayGuard, error) {
	g := &replayGuard{seen: map[string]int64{}, path: path}
	b, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return g, nil
	}
	if err != nil {
		return nil, err
	}
	// A power loss can leave the file empty; that is an empty cache, not a fatal error.
	if len(b) == 0 {
		return g, nil
	}
	if err = json.Unmarshal(b, &g.seen); err != nil {
		return nil, err
	}
	if g.seen == nil {
		return nil, errors.New("invalid replay cache")
	}
	return g, nil
}

func (g *replayGuard) accept(nonce string, stamp, now int64) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	for key, expiry := range g.seen {
		if expiry < now {
			delete(g.seen, key)
		}
	}
	if _, ok := g.seen[nonce]; ok || len(g.seen) >= 256 {
		return false
	}
	g.seen[nonce] = stamp + 60
	b, err := json.Marshal(g.seen)
	if err == nil {
		err = os.WriteFile(g.path+".tmp", b, 0600)
	}
	if err == nil {
		err = os.Rename(g.path+".tmp", g.path)
	}
	if err != nil {
		delete(g.seen, nonce)
		return false
	}
	return true
}

func listen(c config) ([]*net.UDPConn, error) {
	var conns []*net.UDPConn
	families := map[string]bool{}
	for ip := range c.Allowed {
		if net.ParseIP(ip).To4() != nil {
			families["udp4"] = true
		} else {
			families["udp6"] = true
		}
	}
	for family := range families {
		conn, err := net.ListenUDP(family, &net.UDPAddr{Port: c.Port})
		if err != nil {
			for _, old := range conns {
				old.Close()
			}
			return nil, err
		}
		conns = append(conns, conn)
	}
	return conns, nil
}

func receive(conn *net.UDPConn, c config, key []byte, guard *replayGuard, requests chan string) {
	buf := make([]byte, 256)
	window, count := int64(0), 0
	for {
		n, peer, err := conn.ReadFromUDP(buf)
		if err != nil {
			return
		}
		if !c.Allowed[peer.IP.String()] {
			continue
		}
		now := time.Now().Unix()
		if window != now {
			window, count = now, 0
		}
		count++
		if count > 100 {
			continue
		}
		token, nonce, stamp, err := verify(buf[:n], key, now)
		if err != nil || !guard.accept(nonce, stamp, now) {
			continue
		}
		// Keep only the newest request. Never write to the UDP socket.
		select {
		case requests <- token:
		default:
			select {
			case <-requests:
			default:
			}
			select {
			case requests <- token:
			default:
			}
		}
	}
}

func run() error {
	c, err := loadConfig(configPath)
	if err != nil {
		return err
	}
	key, err := authKey("/etc/ictl/agent.conf")
	if err != nil {
		return err
	}
	guard, err := openGuard(replayPath)
	if err != nil {
		return err
	}
	conns, err := listen(c)
	if err != nil {
		return err
	}
	requests := make(chan string, 1)
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	defer signal.Stop(stop)
	var readers sync.WaitGroup
	for _, conn := range conns {
		readers.Add(1)
		go func(cn *net.UDPConn) {
			defer readers.Done()
			receive(cn, c, key, guard, requests)
		}(conn)
	}
	done := make(chan error, 1)
	running, pending := "", ""
	start := func(token string) {
		running = token
		go func() {
			cmd := exec.Command("/usr/local/bin/ictl", "--wake-sync", token)
			cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
			done <- cmd.Run()
		}()
	}
	for {
		select {
		case <-stop:
			for _, conn := range conns {
				conn.Close()
			}
			readers.Wait()
			if running != "" {
				<-done
			}
			return nil
		case err := <-done:
			if err != nil {
				log.Printf("配置同步未完成：%v", err)
			}
			running = ""
			if pending != "" {
				start(pending)
				pending = ""
			}
		case token := <-requests:
			if running != "" {
				if token != running {
					pending = token
				}
			} else {
				start(token)
			}
		}
	}
}

func main() {
	args := os.Args[1:]
	if len(args) == 1 && args[0] == "--version" {
		fmt.Println(version)
		return
	}
	if len(args) == 2 && (args[0] == "--check-config" || args[0] == "--check-listen") {
		c, err := loadConfig(args[1])
		if err != nil {
			log.Fatal(err)
		}
		if args[0] == "--check-listen" {
			conns, err := listen(c)
			if err != nil {
				log.Fatal(err)
			}
			for _, conn := range conns {
				conn.Close()
			}
		}
		return
	}
	if len(args) == 3 && args[0] == "--make-config" {
		c, err := parseConfig(args[1], args[2])
		if err != nil {
			log.Fatal(err)
		}
		ips := []string{}
		for ip := range c.Allowed {
			ips = append(ips, ip)
		}
		fmt.Printf("ENABLED=1\nPORT=%d\nALLOW=%s\n", c.Port, strings.Join(ips, ","))
		return
	}
	if len(args) != 1 || args[0] != "--daemon" {
		log.Fatal("usage: wake --daemon|--version|--check-config path|--check-listen path|--make-config port ip,ip")
	}
	if err := run(); err != nil {
		log.Fatal(err)
	}
}
