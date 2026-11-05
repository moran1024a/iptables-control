package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const version = "0.4.3"
const tableName = "ictl_active"

type M = map[string]any

type Rule struct {
	ID         int64  `json:"id"`
	Protocol   string `json:"protocol"`
	Port       int    `json:"port"`
	Host       string `json:"host"`
	TargetPort int    `json:"tport"`
	IP         string `json:"ip"`
}
type Config struct {
	Generation int64  `json:"generation"`
	Hash       string `json:"hash"`
	Rules      []Rule `json:"rules"`
	Retired    []Rule `json:"retired,omitempty"`
	Epoch      string `json:"epoch"`
}
type Agent struct {
	stateMu        *sync.Mutex // Shared by control and local maintenance; snapshots are read-only.
	root           string
	url            string
	key            string
	installation   string
	config         Config
	usage          map[string]*Usage
	offline        bool
	stateError     string
	boot           string
	lastCPU        [2]uint64
	lastCPUParts   [8]uint64
	lastProcessCPU float64
	lastNet        map[string][2]uint64
	lastMetrics    time.Time
	quality        string
	autoUpdate     bool
}

func randomID() string {
	b := make([]byte, 16)
	if _, e := rand.Read(b); e != nil {
		panic(e)
	}
	return hex.EncodeToString(b)
}
func (a *Agent) path(s string) string { return filepath.Join(a.root, s) }
func readJSON(path string, v any) error {
	b, e := os.ReadFile(path)
	if e != nil {
		return e
	}
	d := json.NewDecoder(bytes.NewReader(b))
	d.UseNumber()
	return d.Decode(v)
}
func atomicBytes(path string, data []byte, mode os.FileMode) error {
	if e := os.MkdirAll(filepath.Dir(path), 0700); e != nil {
		return e
	}
	f, e := os.CreateTemp(filepath.Dir(path), ".ictl-")
	if e != nil {
		return e
	}
	name := f.Name()
	defer os.Remove(name)
	if e = f.Chmod(mode); e == nil {
		_, e = f.Write(data)
	}
	if e == nil {
		e = f.Sync()
	}
	ce := f.Close()
	if e == nil {
		e = ce
	}
	if e != nil {
		return e
	}
	if e = os.Rename(name, path); e != nil {
		return e
	}
	d, e := os.Open(filepath.Dir(path))
	if e != nil {
		return e
	}
	defer d.Close()
	return d.Sync()
}
func atomicJSON(path string, v any) error {
	b, e := json.Marshal(v)
	if e != nil {
		return e
	}
	return atomicBytes(path, append(b, '\n'), 0600)
}
func command(ctx context.Context, input []byte, name string, args ...string) ([]byte, error) {
	c := exec.CommandContext(ctx, name, args...)
	c.Stdin = bytes.NewReader(input)
	// Bound diagnostic output without terminating the command halfway through a firewall transaction.
	var out, stderr cappedBuffer
	c.Stdout = &out
	c.Stderr = &stderr
	e := c.Run()
	if e != nil {
		return out.Bytes(), fmt.Errorf("%s: %w: %.1500s", name, e, stderr.Bytes())
	}
	return out.Bytes(), nil
}

type cappedBuffer struct{ bytes.Buffer }

func (b *cappedBuffer) Write(p []byte) (int, error) {
	n := len(p)
	if b.Len() < 4*1024*1024 {
		keep := min(n, 4*1024*1024-b.Len())
		_, _ = b.Buffer.Write(p[:keep])
	}
	return n, nil
}
func run(input []byte, name string, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	return command(ctx, input, name, args...)
}
func (a *Agent) locked(fn func() error) error {
	if os.Getenv("ICTL_LOCK_HELD") == "1" {
		return fn()
	}
	path := "/run/ictl.lock"
	if a.root != "/var/lib/ictl" {
		path = a.path("lock")
	}
	f, e := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0600)
	if e != nil {
		return e
	}
	defer f.Close()
	deadline := time.Now().Add(60 * time.Second)
	for {
		e = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if e == nil {
			break
		}
		if !errors.Is(e, syscall.EWOULDBLOCK) || time.Now().After(deadline) {
			return fmt.Errorf("规则锁: %w", e)
		}
		time.Sleep(100 * time.Millisecond)
	}
	defer syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
	return fn()
}
func loadAgent(root, conf string) (*Agent, error) {
	a := &Agent{root: root, usage: map[string]*Usage{}, autoUpdate: true}
	b, e := os.ReadFile(conf)
	if e != nil {
		return nil, e
	}
	for _, line := range strings.Split(string(b), "\n") {
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		switch k {
		case "URL":
			a.url = strings.TrimRight(v, "/")
		case "KEY":
			a.key = v
		case "AUTO_UPDATE":
			a.autoUpdate = v != "0"
		}
	}
	u, e := url.Parse(a.url)
	if e != nil || u.Scheme != "https" || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return nil, errors.New("新版主动 Agent 需要有效的 HTTPS 同步地址")
	}
	if raw, e := hex.DecodeString(a.key); e != nil || len(raw) != 32 {
		return nil, errors.New("节点密钥无效")
	}
	if e = readJSON(a.path("active/config.json"), &a.config); e != nil && !os.IsNotExist(e) {
		return nil, e
	}
	if e = readJSON(a.path("active/traffic.json"), &a.usage); e != nil && !os.IsNotExist(e) {
		return nil, e
	}
	id, e := os.ReadFile(a.path("installation-id"))
	if e != nil && !os.IsNotExist(e) {
		return nil, e
	}
	a.installation = strings.TrimSpace(string(id))
	if os.IsNotExist(e) {
		a.installation = randomID()
		if e = atomicBytes(a.path("installation-id"), []byte(a.installation), 0600); e != nil {
			return nil, e
		}
	} else if raw, err := hex.DecodeString(a.installation); err != nil || len(raw) != 16 {
		return nil, errors.New("本地 installation-id 损坏，请保留安装状态并恢复原文件")
	}
	boot, _ := os.ReadFile("/proc/sys/kernel/random/boot_id")
	a.boot = strings.TrimSpace(string(boot))
	return a, nil
}
func parseConfig(data []byte, gen int64) (Config, error) {
	c := Config{Generation: gen}
	s := bufio.NewScanner(bytes.NewReader(data))
	if !s.Scan() {
		return c, errors.New("缺少配置头")
	}
	var ok bool
	_, c.Hash, ok = strings.Cut(s.Text(), "=")
	if !ok || !strings.HasPrefix(s.Text(), "v=") {
		return c, errors.New("配置头无效")
	}
	for s.Scan() {
		p := strings.Fields(s.Text())
		if len(p) == 0 {
			continue
		}
		if len(p) != 5 {
			return c, errors.New("配置行无效")
		}
		id, e := strconv.ParseInt(p[0], 10, 64)
		if e != nil {
			return c, e
		}
		port, e := strconv.Atoi(p[2])
		if e != nil {
			return c, e
		}
		tp, e := strconv.Atoi(p[4])
		if e != nil {
			return c, e
		}
		c.Rules = append(c.Rules, Rule{ID: id, Protocol: p[1], Port: port, Host: p[3], TargetPort: tp})
	}
	if e := s.Err(); e != nil {
		return c, e
	}
	return c, validateConfig(c)
}
func validateConfig(c Config) error {
	lines := []string{}
	seen := map[int]bool{}
	for _, r := range c.Rules {
		if r.ID < 1 || r.Port < 1 || r.Port > 65535 || r.TargetPort < 1 || r.TargetPort > 65535 || len(r.Host) > 253 || r.Host == "" || strings.ContainsAny(r.Host, " \t\n/:;\"'\\") {
			return errors.New("转发配置无效")
		}
		if r.Protocol != "both" && r.Protocol != "tcp" && r.Protocol != "udp" {
			return errors.New("无效协议")
		}
		if seen[r.Port] {
			return errors.New("重复入站端口")
		}
		seen[r.Port] = true
		lines = append(lines, fmt.Sprintf("%d %s %d %s %d", r.ID, r.Protocol, r.Port, r.Host, r.TargetPort))
	}
	sum := sha256.Sum256([]byte(strings.Join(lines, "\n")))
	if c.Hash != hex.EncodeToString(sum[:])[:12] {
		return errors.New("配置校验和不匹配")
	}
	return nil
}
func protocols(r Rule) []string {
	if r.Protocol == "both" {
		return []string{"tcp", "udp"}
	}
	return []string{r.Protocol}
}
func (a *Agent) resolve(c *Config) error {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	resolved := map[string]string{}
	for i := range c.Rules {
		r := &c.Rules[i]
		if ip := net.ParseIP(r.Host); ip != nil && ip.To4() != nil {
			r.IP = ip.To4().String()
			continue
		}
		if ip := resolved[r.Host]; ip != "" {
			r.IP = ip
			continue
		}
		ips, e := net.DefaultResolver.LookupIP(ctx, "ip4", r.Host)
		if e == nil && len(ips) > 0 {
			r.IP = ips[0].To4().String()
			resolved[r.Host] = r.IP
			continue
		}
		for _, old := range a.config.Rules {
			if old.Host == r.Host && net.ParseIP(old.IP).To4() != nil {
				r.IP = old.IP
				break
			}
		}
		if r.IP == "" {
			return fmt.Errorf("DNS 解析失败: %s", r.Host)
		}
		a.stateError = "DNS 解析失败，使用缓存: " + r.Host
	}
	return nil
}
func number(v any) uint64 {
	switch n := v.(type) {
	case json.Number:
		u, _ := strconv.ParseUint(string(n), 10, 64)
		return u
	case float64:
		return uint64(n)
	case int:
		return uint64(n)
	}
	return 0
}
func decodeJSON(b []byte) (map[string]any, error) {
	var v map[string]any
	d := json.NewDecoder(bytes.NewReader(b))
	d.UseNumber()
	e := d.Decode(&v)
	return v, e
}
func copyLimited(dst io.Writer, src io.Reader) error {
	_, e := io.Copy(dst, io.LimitReader(src, 1024*1024))
	return e
}
