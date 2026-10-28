// ictl-tunnel-agent controls only its own sing-box instance. No inbound management port.
package main

import (
	"archive/tar"
	"bufio"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"math/rand"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const Version = "0.1.0"
const defaultConfig = "/etc/ictl-tunnel/agent.json"
const coreService = "ictl-tunnel-core.service"

type Options struct {
	URL            string `json:"url"`
	Token          string `json:"token"`
	DataDir        string `json:"data_dir"`
	Core           string `json:"core"`
	AllowLocalHTTP bool   `json:"allow_local_http,omitempty"`
}
type State struct {
	Revision string `json:"revision"`
	State    string `json:"state"`
	Error    string `json:"error"`
}
type Snapshot struct {
	API         int             `json:"api"`
	Revision    string          `json:"revision"`
	Config      json.RawMessage `json:"config"`
	CoreVersion string          `json:"core_version"`
}
type Heartbeat struct {
	API int `json:"api"`
	State
	AgentVersion string `json:"agent_version"`
	CoreVersion  string `json:"core_version"`
}
type Reply struct {
	Revision string `json:"revision"`
	Interval int    `json:"heartbeat_interval"`
	Deleted  bool   `json:"deleted"`
}
type Asset struct {
	URL    string `json:"url"`
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}
type Manifest struct {
	API    int              `json:"api"`
	Agent  string           `json:"tunnel_agent"`
	Assets map[string]Asset `json:"agent_assets"`
	Core   struct {
		Version string           `json:"version"`
		Assets  map[string]Asset `json:"assets"`
	} `json:"sing_box"`
}
type Agent struct {
	opts         Options
	client       *http.Client
	streamClient *http.Client
	mu           sync.Mutex
	state        State
	coreVersion  string
	notify       chan struct{}
	deleted      chan struct{}
	deleteOnce   sync.Once
	restart      func() error
}

func loadOptions(path string) (Options, error) {
	var o Options
	b, err := os.ReadFile(path)
	if err != nil {
		return o, err
	}
	if err = json.Unmarshal(b, &o); err != nil {
		return o, err
	}
	o.URL = strings.TrimRight(o.URL, "/")
	u, err := url.Parse(o.URL)
	if err != nil || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return o, errors.New("invalid server URL")
	}
	local := u.Hostname() == "127.0.0.1" || u.Hostname() == "localhost" || u.Hostname() == "::1"
	if u.Scheme != "https" && !(u.Scheme == "http" && local && o.AllowLocalHTTP) {
		return o, errors.New("server URL must use HTTPS")
	}
	if len(o.Token) != 64 {
		return o, errors.New("invalid token")
	}
	if _, err = hex.DecodeString(o.Token); err != nil {
		return o, errors.New("invalid token")
	}
	if o.DataDir == "" {
		o.DataDir = "/var/lib/ictl-tunnel"
	}
	if o.Core == "" {
		o.Core = "/usr/local/lib/ictl-tunnel/sing-box"
	}
	if !filepath.IsAbs(o.DataDir) || !filepath.IsAbs(o.Core) {
		return o, errors.New("paths must be absolute")
	}
	return o, nil
}

func noRedirect(req *http.Request, via []*http.Request) error { return http.ErrUseLastResponse }
func newAgent(o Options) *Agent {
	a := &Agent{opts: o, client: &http.Client{Timeout: 30 * time.Second, CheckRedirect: noRedirect},
		streamClient: &http.Client{Transport: &http.Transport{ResponseHeaderTimeout: 20 * time.Second}, CheckRedirect: noRedirect},
		notify:       make(chan struct{}, 1), deleted: make(chan struct{}), state: State{State: "init"}}
	a.restart = func() error {
		if _, err := command("systemctl", "restart", coreService); err != nil {
			return err
		}
		_, err := command("systemctl", "is-active", "--quiet", coreService)
		return err
	}
	a.coreVersion = versionOf(o.Core)
	// A startup reconciliation reapplies the cached desired config; only execution success is acknowledged.
	return a
}
func command(name string, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	output, err := exec.CommandContext(ctx, name, args...).CombinedOutput()
	if err != nil {
		return nil, fmt.Errorf("%s failed: %w", filepath.Base(name), err)
	}
	return output, nil // Don't include core output; it can contain credentials.
}
func versionOf(path string) string {
	out, err := command(path, "version")
	if err != nil {
		return ""
	}
	fields := strings.Fields(string(out))
	if len(fields) >= 3 && fields[0] == "sing-box" && fields[1] == "version" {
		return fields[2]
	}
	return ""
}
func atomicWrite(path string, data []byte, mode os.FileMode) error {
	f, err := os.CreateTemp(filepath.Dir(path), ".ictl-*")
	if err != nil {
		return err
	}
	name := f.Name()
	defer os.Remove(name)
	if err = f.Chmod(mode); err == nil {
		_, err = f.Write(data)
	}
	if err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err == nil {
		err = closeErr
	}
	if err != nil {
		return err
	}
	return os.Rename(name, path)
}
func (a *Agent) request(ctx context.Context, method, path string, body any) (*http.Response, error) {
	var data io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		data = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, a.opts.URL+"/"+path, data)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+a.opts.Token)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	client := a.client
	if path == "events" {
		client = a.streamClient
	}
	res, err := client.Do(req)
	if err != nil {
		return nil, errors.New("server request failed")
	}
	if res.StatusCode != 200 {
		res.Body.Close()
		return nil, fmt.Errorf("server returned HTTP %d", res.StatusCode)
	}
	return res, nil
}
func decode(res *http.Response, v any) error {
	defer res.Body.Close()
	data, err := io.ReadAll(io.LimitReader(res.Body, 2*1024*1024+1))
	if err != nil {
		return err
	}
	if len(data) > 2*1024*1024 {
		return errors.New("response too large")
	}
	return json.Unmarshal(data, v)
}
func (a *Agent) wake() {
	select {
	case a.notify <- struct{}{}:
	default:
	}
}
func (a *Agent) heartbeat(ctx context.Context) (Reply, error) {
	a.mu.Lock()
	b := Heartbeat{API: 1, State: a.state, AgentVersion: Version, CoreVersion: a.coreVersion}
	a.mu.Unlock()
	res, err := a.request(ctx, "POST", "heartbeat", b)
	if err != nil {
		return Reply{}, err
	}
	var reply Reply
	err = decode(res, &reply)
	if err == nil && reply.Deleted {
		a.deleteOnce.Do(func() { close(a.deleted) })
	}
	if err == nil && !reply.Deleted && reply.Revision != b.Revision {
		a.wake()
	}
	return reply, err
}
func (a *Agent) setError(err error) {
	a.mu.Lock()
	a.state.State = "error"
	a.state.Error = err.Error()
	a.mu.Unlock()
	log.Printf("configuration apply failed: %s", err)
}
func (a *Agent) apply(s Snapshot) error {
	if s.API != 1 || len(s.Revision) != 64 || !json.Valid(s.Config) {
		return errors.New("invalid configuration response")
	}
	// The server hashes canonical JSON. Compact input from the server is stored as received;
	// revision is an opaque token and is only acknowledged after local application succeeds.
	a.mu.Lock()
	current := a.state
	core := a.coreVersion
	a.mu.Unlock()
	if s.CoreVersion != core {
		return fmt.Errorf("sing-box version %s required; run ictl-tunnel-agent update", s.CoreVersion)
	}
	if current.State == "ok" && current.Revision == s.Revision {
		return nil
	}
	if err := os.MkdirAll(a.opts.DataDir, 0700); err != nil {
		return err
	}
	path := filepath.Join(a.opts.DataDir, "config.json")
	candidate := filepath.Join(a.opts.DataDir, "config.pending.json")
	if err := atomicWrite(candidate, s.Config, 0600); err != nil {
		return err
	}
	defer os.Remove(candidate)
	if _, err := command(a.opts.Core, "check", "-c", candidate); err != nil {
		return errors.New("sing-box configuration check failed")
	}
	previous, readErr := os.ReadFile(path)
	if readErr != nil && !os.IsNotExist(readErr) {
		return readErr
	}
	if err := atomicWrite(path, s.Config, 0600); err != nil {
		return err
	}
	rollback := func(cause error) error {
		var err error
		if readErr == nil {
			err = atomicWrite(path, previous, 0600)
		} else {
			err = os.Remove(path)
		}
		if err == nil && readErr == nil {
			err = a.restart()
		}
		if err != nil {
			return fmt.Errorf("%v; rollback failed", cause)
		}
		return cause
	}
	if err := a.restart(); err != nil {
		return rollback(errors.New("sing-box restart failed"))
	}
	next := State{Revision: s.Revision, State: "ok"}
	b, _ := json.Marshal(next)
	if err := atomicWrite(filepath.Join(a.opts.DataDir, "state.json"), b, 0600); err != nil {
		return rollback(errors.New("saving applied revision failed"))
	}
	a.mu.Lock()
	a.state = next
	a.mu.Unlock()
	return nil
}
func (a *Agent) reconcile(ctx context.Context) {
	res, err := a.request(ctx, "GET", "config", nil)
	if err != nil {
		log.Print(err)
		return
	}
	var snapshot Snapshot
	if err = decode(res, &snapshot); err == nil {
		err = a.apply(snapshot)
	}
	if err != nil {
		a.setError(err)
	}
	if _, err = a.heartbeat(ctx); err != nil {
		log.Print(err)
	}
}
func wait(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}
func (a *Agent) events(ctx context.Context) {
	backoff := time.Second
	for ctx.Err() == nil {
		streamCtx, cancel := context.WithTimeout(ctx, 6*time.Minute)
		res, err := a.request(streamCtx, "GET", "events", nil)
		if err == nil {
			backoff = time.Second
			a.wake()
			scanner := bufio.NewScanner(res.Body)
			for scanner.Scan() {
				if strings.HasPrefix(scanner.Text(), "data:") {
					a.wake()
				}
			}
			res.Body.Close()
		}
		cancel()
		if !wait(ctx, backoff+time.Duration(rand.Intn(1000))*time.Millisecond) {
			return
		}
		if backoff < 30*time.Second {
			backoff *= 2
		}
	}
}
func (a *Agent) run(ctx context.Context) error {
	if err := os.MkdirAll(a.opts.DataDir, 0700); err != nil {
		return err
	}
	lock, err := os.OpenFile(filepath.Join(a.opts.DataDir, "agent.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	defer lock.Close()
	if err = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		return errors.New("another Agent is running")
	}
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	var workers sync.WaitGroup
	workers.Add(2)
	go func() { defer workers.Done(); a.events(ctx) }()
	go func() {
		defer workers.Done()
		for {
			select {
			case <-ctx.Done():
				return
			case <-a.notify:
				a.reconcile(ctx)
				a.mu.Lock()
				failed := a.state.State == "error"
				a.mu.Unlock()
				if failed && !wait(ctx, 30*time.Second) {
					return
				}
			}
		}
	}()
	a.wake()
	interval := 10 * time.Second
	for ctx.Err() == nil {
		reply, err := a.heartbeat(ctx)
		if err != nil {
			log.Print(err)
		} else {
			if reply.Deleted {
				cancel()
				workers.Wait()
				return nil
			}
			if reply.Interval >= 5 && reply.Interval <= 3600 {
				interval = time.Duration(reply.Interval) * time.Second
			}
		}
		select {
		case <-ctx.Done():
		case <-a.deleted:
			cancel()
		case <-time.After(interval):
		}
	}
	cancel()
	workers.Wait()
	return nil
}

func checkedDownload(client *http.Client, req *http.Request, expected string) ([]byte, error) {
	if len(expected) != 64 {
		return nil, errors.New("missing asset checksum")
	}
	res, err := client.Do(req)
	if err != nil {
		return nil, errors.New("download failed")
	}
	defer res.Body.Close()
	if res.StatusCode != 200 {
		return nil, fmt.Errorf("download returned HTTP %d", res.StatusCode)
	}
	data, err := io.ReadAll(io.LimitReader(res.Body, 128*1024*1024+1))
	if err != nil {
		return nil, err
	}
	if len(data) > 128*1024*1024 {
		return nil, errors.New("asset too large")
	}
	hash := sha256.Sum256(data)
	if hex.EncodeToString(hash[:]) != expected {
		return nil, errors.New("asset checksum mismatch")
	}
	return data, nil
}
func unpackCore(data []byte) ([]byte, error) {
	gz, err := gzip.NewReader(bytes.NewReader(data))
	if err != nil {
		return nil, err
	}
	defer gz.Close()
	tr := tar.NewReader(gz)
	for {
		h, err := tr.Next()
		if err != nil {
			return nil, err
		}
		if h.Typeflag == tar.TypeReg && filepath.Base(h.Name) == "sing-box" {
			if h.Size > 128*1024*1024 {
				return nil, errors.New("core binary too large")
			}
			return io.ReadAll(io.LimitReader(tr, 128*1024*1024))
		}
	}
}
func (a *Agent) manifest(ctx context.Context) (Manifest, error) {
	var m Manifest
	res, err := a.request(ctx, "GET", "components", nil)
	if err != nil {
		return m, err
	}
	err = decode(res, &m)
	if err == nil && m.API != 1 {
		err = errors.New("unsupported component API")
	}
	return m, err
}
func (a *Agent) installCore(ctx context.Context, m Manifest) error {
	if versionOf(a.opts.Core) == m.Core.Version {
		return nil
	}
	asset, ok := m.Core.Assets[runtime.GOARCH]
	if !ok {
		return errors.New("unsupported architecture")
	}
	if !strings.HasPrefix(asset.URL, "https://github.com/SagerNet/sing-box/releases/download/") {
		return errors.New("unexpected core download URL")
	}
	req, err := http.NewRequestWithContext(ctx, "GET", asset.URL, nil)
	if err != nil {
		return err
	}
	client := &http.Client{Timeout: 5 * time.Minute, CheckRedirect: func(req *http.Request, via []*http.Request) error {
		if len(via) > 5 || req.URL.Scheme != "https" {
			return errors.New("invalid download redirect")
		}
		return nil
	}}
	data, err := checkedDownload(client, req, asset.SHA256)
	if err != nil {
		return err
	}
	binary, err := unpackCore(data)
	if err != nil {
		return err
	}
	if err = os.MkdirAll(filepath.Dir(a.opts.Core), 0755); err != nil {
		return err
	}
	candidate := a.opts.Core + ".new"
	defer os.Remove(candidate)
	if err = atomicWrite(candidate, binary, 0755); err != nil {
		return err
	}
	if versionOf(candidate) != m.Core.Version {
		return errors.New("core version mismatch")
	}
	cfg := filepath.Join(a.opts.DataDir, "config.json")
	if _, err = os.Stat(cfg); err == nil {
		if _, err = command(candidate, "check", "-c", cfg); err != nil {
			return errors.New("new core rejected existing config")
		}
	}
	old, readErr := os.ReadFile(a.opts.Core)
	if readErr != nil && !os.IsNotExist(readErr) {
		return readErr
	}
	if err = atomicWrite(a.opts.Core, binary, 0755); err != nil {
		return err
	}
	if readErr == nil {
		if err = a.restart(); err != nil {
			restore := atomicWrite(a.opts.Core, old, 0755)
			if restore == nil {
				restore = a.restart()
			}
			if restore != nil {
				return errors.New("core update and rollback failed")
			}
			return errors.New("core update failed; restored previous core")
		}
	}
	a.mu.Lock()
	a.coreVersion = m.Core.Version
	a.mu.Unlock()
	return nil
}
func newer(candidate, current string) bool {
	parse := func(s string) []int {
		var out []int
		for _, p := range strings.Split(s, ".") {
			v, err := strconv.Atoi(p)
			if err != nil {
				return nil
			}
			out = append(out, v)
		}
		return out
	}
	x, y := parse(candidate), parse(current)
	if len(x) != 3 || len(y) != 3 {
		return false
	}
	for i := range x {
		if x[i] != y[i] {
			return x[i] > y[i]
		}
	}
	return false
}
func (a *Agent) update(ctx context.Context) error {
	// Separate from the runtime lock; installers and manual updates may not overlap.
	if err := os.MkdirAll(a.opts.DataDir, 0700); err != nil {
		return err
	}
	lock, err := os.OpenFile(filepath.Join(a.opts.DataDir, "update.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	defer lock.Close()
	if err = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		return errors.New("another update is running")
	}
	m, err := a.manifest(ctx)
	if err != nil {
		return err
	}
	if _, err = command("systemctl", "stop", "ictl-tunnel-agent.service"); err != nil {
		return err
	}
	defer command("systemctl", "start", "ictl-tunnel-agent.service")
	if err = a.installCore(ctx, m); err != nil {
		return err
	}
	if !newer(m.Agent, Version) {
		_, err = command("systemctl", "start", "ictl-tunnel-agent.service")
		return err
	}
	asset, ok := m.Assets[runtime.GOARCH]
	if !ok {
		return errors.New("Agent asset unavailable")
	}
	if asset.Path != "download/"+runtime.GOARCH {
		return errors.New("invalid Agent asset path")
	}
	req, err := http.NewRequestWithContext(ctx, "GET", a.opts.URL+"/"+asset.Path, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+a.opts.Token)
	data, err := checkedDownload(a.client, req, asset.SHA256)
	if err != nil {
		return err
	}
	path, err := os.Executable()
	if err != nil {
		return err
	}
	candidate := path + ".new"
	defer os.Remove(candidate)
	if err = atomicWrite(candidate, data, 0755); err != nil {
		return err
	}
	out, err := command(candidate, "version")
	if err != nil || strings.TrimSpace(string(out)) != m.Agent {
		return errors.New("Agent version mismatch")
	}
	previous, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	if err = atomicWrite(path, data, 0755); err != nil {
		return err
	}
	_, err = command("systemctl", "restart", "ictl-tunnel-agent.service")
	if err == nil {
		_, err = command("systemctl", "is-active", "--quiet", "ictl-tunnel-agent.service")
	}
	if err != nil {
		if e := atomicWrite(path, previous, 0755); e != nil {
			return errors.New("Agent update and rollback failed")
		}
		if _, e := command("systemctl", "restart", "ictl-tunnel-agent.service"); e != nil {
			return errors.New("Agent rollback restart failed")
		}
		return errors.New("Agent update failed; restored previous binary")
	}
	return nil
}
func main() {
	log.SetFlags(log.LstdFlags)
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: ictl-tunnel-agent run|update|install-core|version [--config PATH]")
		os.Exit(2)
	}
	if os.Args[1] == "version" {
		fmt.Println(Version)
		return
	}
	flags := flag.NewFlagSet(os.Args[1], flag.ExitOnError)
	config := flags.String("config", defaultConfig, "Agent config path")
	flags.Parse(os.Args[2:])
	opts, err := loadOptions(*config)
	if err != nil {
		log.Fatal(err)
	}
	a := newAgent(opts)
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	switch os.Args[1] {
	case "run":
		err = a.run(ctx)
	case "update":
		err = a.update(ctx)
	case "install-core":
		var m Manifest
		m, err = a.manifest(ctx)
		if err == nil {
			err = a.installCore(ctx, m)
		}
	default:
		err = errors.New("unknown command")
	}
	if err != nil {
		log.Fatal(err)
	}
}
