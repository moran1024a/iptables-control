package main

// Only Server HTTP/WSS clients use this transport. Forwarding and probe dialers
// never receive it; sslocal has one fixed destination and no SOCKS/TUN listener.
import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"
)

type controlProxy struct {
	Enabled  bool   `json:"enabled"`
	Server   string `json:"server"`
	Port     int    `json:"server_port"`
	Method   string `json:"method"`
	Password string `json:"password"`
}

func (p controlProxy) validate() error {
	size := 32
	switch p.Method {
	case "2022-blake3-aes-128-gcm":
		size = 16
	case "2022-blake3-aes-256-gcm", "2022-blake3-chacha20-poly1305":
	default:
		return errors.New("仅支持 SS2022 AES-128、AES-256 或 ChaCha20")
	}
	if p.Server == "" || len(p.Server) > 253 || strings.ContainsAny(p.Server, " /\\\r\n\t\x00") || p.Port < 1 || p.Port > 65535 {
		return errors.New("代理地址或端口无效")
	}
	keys := strings.Split(p.Password, ":")
	if len(keys) > 8 || (strings.Contains(p.Method, "chacha20") && len(keys) != 1) {
		return errors.New("SS2022 密钥链无效")
	}
	for _, key := range keys {
		b, err := base64.StdEncoding.DecodeString(key)
		if err != nil || len(b) != size {
			return errors.New("SS2022 Base64 密钥长度不正确")
		}
	}
	return nil
}

func parseControlProxy(text string) (controlProxy, error) {
	var p controlProxy
	u, err := url.Parse(strings.TrimSpace(text))
	if err != nil || u.Scheme != "ss" || u.User == nil || u.Hostname() == "" || (u.Path != "" && u.Path != "/") || u.RawQuery != "" {
		return p, errors.New("需要不含插件或额外参数的 SS2022 链接")
	}
	method := u.User.Username()
	password, ok := u.User.Password()
	if !ok {
		var raw []byte
		for _, enc := range []*base64.Encoding{base64.RawURLEncoding, base64.URLEncoding, base64.RawStdEncoding, base64.StdEncoding} {
			raw, err = enc.DecodeString(method)
			if err == nil {
				break
			}
		}
		if err != nil {
			return p, errors.New("代理认证信息格式无效")
		}
		method, password, ok = strings.Cut(string(raw), ":")
		if !ok {
			return p, errors.New("代理缺少加密方式或密钥")
		}
	}
	port, err := strconv.Atoi(u.Port())
	if err != nil {
		return p, errors.New("代理端口无效")
	}
	p = controlProxy{true, u.Hostname(), port, method, password}
	return p, p.validate()
}

func readControlProxy(path string) (controlProxy, error) {
	var p controlProxy
	if path == "" {
		return p, nil
	}
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return p, nil
	}
	if err != nil {
		return p, errors.New("无法读取代理设置")
	}
	if len(data) > 16384 || json.Unmarshal(data, &p) != nil {
		return p, errors.New("代理设置损坏，请在本机菜单修复；未回退直连")
	}
	if p.Enabled {
		return p, p.validate()
	}
	return p, nil
}

func controlEndpoint(endpoint string) (*url.URL, string, int, error) {
	u, err := url.Parse(endpoint)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return nil, "", 0, errors.New("管理连接需要有效的 HTTPS Server 地址")
	}
	port := 443
	if u.Port() != "" {
		port, err = strconv.Atoi(u.Port())
	}
	if err != nil || port < 1 || port > 65535 {
		return nil, "", 0, errors.New("Server 端口无效")
	}
	return u, net.JoinHostPort(u.Hostname(), strconv.Itoa(port)), port, nil
}

func openControlClient(ctx context.Context, endpoint string, p controlProxy, binary string) (*http.Client, func(), error) {
	u, authority, port, err := controlEndpoint(endpoint)
	if err != nil {
		return nil, nil, err
	}
	// Clone instead of mutating the process-wide transport used by other work.
	base, _ := http.DefaultClient.Transport.(*http.Transport)
	if base == nil {
		base = http.DefaultTransport.(*http.Transport)
	}
	tr := base.Clone()
	tr.Proxy = nil
	tr.DialTLSContext = nil
	tr.DialTLS = nil
	tr.ForceAttemptHTTP2 = false
	tr.ResponseHeaderTimeout = 15 * time.Second
	dial := (&net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}).DialContext
	address := authority
	cleanup := func() {}
	if p.Enabled {
		if err = p.validate(); err != nil {
			return nil, nil, err
		}
		address, cleanup, err = startControlTunnel(ctx, u.Hostname(), port, p, binary)
		if err != nil {
			return nil, nil, err
		}
	}
	tr.DialContext = func(ctx context.Context, network, destination string) (net.Conn, error) {
		if destination != authority {
			return nil, errors.New("管理代理拒绝访问其他地址")
		}
		return dial(ctx, "tcp", address)
	}
	client := &http.Client{Transport: tr, CheckRedirect: func(*http.Request, []*http.Request) error { return errors.New("管理连接不允许重定向") }}
	return client, func() { tr.CloseIdleConnections(); cleanup() }, nil
}

func startControlTunnel(ctx context.Context, host string, port int, p controlProxy, binary string) (string, func(), error) {
	// Reserve an ephemeral loopback endpoint, never a public listening address.
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		return "", nil, err
	}
	address := listener.Addr().String()
	localPort := listener.Addr().(*net.TCPAddr).Port
	dir, err := os.MkdirTemp("", "ictl-control-")
	if err != nil {
		listener.Close()
		return "", nil, err
	}
	conf := map[string]any{"server": p.Server, "server_port": p.Port, "method": p.Method, "password": p.Password, "mode": "tcp_only", "locals": []any{map[string]any{"local_address": "127.0.0.1", "local_port": localPort, "protocol": "tunnel", "forward_address": host, "forward_port": port, "mode": "tcp_only"}}}
	data, _ := json.Marshal(conf)
	if err = os.WriteFile(filepath.Join(dir, "config.json"), data, 0600); err != nil {
		listener.Close()
		os.RemoveAll(dir)
		return "", nil, err
	}
	child, cancel := context.WithCancel(ctx)
	cmd := exec.CommandContext(child, binary, "--single-threaded", "--tcp-no-delay", "-c", filepath.Join(dir, "config.json"))
	cmd.SysProcAttr = &syscall.SysProcAttr{Pdeathsig: syscall.SIGKILL}
	listener.Close()
	if err = cmd.Start(); err != nil {
		cancel()
		os.RemoveAll(dir)
		return "", nil, errors.New("SS2022 组件无法启动，请重新设置代理")
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	cleanup := func() { cancel(); <-done; os.RemoveAll(dir) }
	timeout := time.NewTimer(5 * time.Second)
	defer timeout.Stop()
	tick := time.NewTicker(25 * time.Millisecond)
	defer tick.Stop()
	for {
		select {
		case <-done:
			cancel()
			os.RemoveAll(dir)
			return "", nil, errors.New("SS2022 组件退出，未回退直连")
		case <-ctx.Done():
			cleanup()
			return "", nil, ctx.Err()
		case <-timeout.C:
			cleanup()
			return "", nil, errors.New("SS2022 组件启动超时")
		case <-tick.C:
			c, e := net.DialTimeout("tcp4", address, 50*time.Millisecond)
			if e == nil {
				c.Close()
				return address, cleanup, nil
			}
		}
	}
}

func (a *Agent) controlClient(ctx context.Context) (*http.Client, func(), error) {
	p, err := readControlProxy(a.proxyFile)
	if err != nil {
		return nil, nil, err
	}
	binary := a.proxyBinary
	if binary == "" {
		binary = "/usr/local/lib/ictl/sslocal"
	}
	return openControlClient(ctx, a.url, p, binary)
}

// Fetch a pinned, checksum-verified upstream binary only on explicit setup.
// No package installation, global proxy variables, or network-rule changes.
func ensureControlHelper(binary string) error {
	if s, err := os.Stat(binary); err == nil && s.Mode().IsRegular() && s.Mode().Perm()&0111 != 0 {
		return nil
	}
	arch, hash := "", ""
	switch runtime.GOARCH {
	case "amd64":
		arch = "x86_64"
		hash = "8439bf43c324b0fc273e663d0b1f8926fd8f666cbd1e0fd59b35096f0e778e92"
	case "arm64":
		arch = "aarch64"
		hash = "2c24915b56a05effdab5898848bacfbcb625bc277b18febafc7a3aa45f87f3b0"
	default:
		return errors.New("SS2022 组件仅支持 amd64 / arm64")
	}
	dir, err := os.MkdirTemp("", "ictl-sslocal-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(dir)
	tr := http.DefaultTransport.(*http.Transport).Clone()
	tr.Proxy = nil
	defer tr.CloseIdleConnections()
	client := &http.Client{Transport: tr, Timeout: 120 * time.Second}
	response, err := client.Get("https://github.com/shadowsocks/shadowsocks-rust/releases/download/v1.25.0/shadowsocks-v1.25.0." + arch + "-unknown-linux-musl.tar.xz")
	if err != nil {
		return errors.New("下载 SS2022 组件失败，请检查 GitHub 连通性")
	}
	defer response.Body.Close()
	if response.StatusCode != 200 {
		return fmt.Errorf("下载 SS2022 组件失败：HTTP %d", response.StatusCode)
	}
	archive := filepath.Join(dir, "ss.tar.xz")
	f, err := os.OpenFile(archive, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	digest := sha256.New()
	_, err = io.Copy(io.MultiWriter(f, digest), io.LimitReader(response.Body, 64<<20))
	closeErr := f.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	if fmt.Sprintf("%x", digest.Sum(nil)) != hash {
		return errors.New("SS2022 组件校验失败")
	}
	if err = exec.Command("tar", "-xJf", archive, "-C", dir, "sslocal").Run(); err != nil {
		return errors.New("解压 SS2022 组件失败，请安装 tar / xz")
	}
	data, err := os.ReadFile(filepath.Join(dir, "sslocal"))
	if err != nil {
		return err
	}
	return atomicBytes(binary, data, 0755)
}

func proxyCommand(action, conf, path, endpoint, binary, output string, seconds int) error {
	if endpoint == "" {
		data, err := os.ReadFile(conf)
		if err != nil {
			return err
		}
		for _, line := range strings.Split(string(data), "\n") {
			if strings.HasPrefix(line, "URL=") {
				endpoint = strings.TrimPrefix(line, "URL=")
			}
		}
	}
	p, err := readControlProxy(path)
	// Explicit set repairs corrupt settings without an unsafe implicit fallback.
	if err != nil && action != "set" && action != "disable" {
		return err
	}
	switch action {
	case "status":
		if p.Server == "" {
			fmt.Println("管理代理：关闭")
			return nil
		}
		state := "关闭"
		if p.Enabled {
			state = "开启"
		}
		fmt.Printf("管理代理：%s，%s，%s（密钥隐藏）\n", state, net.JoinHostPort(p.Server, strconv.Itoa(p.Port)), p.Method)
		return nil
	case "disable":
		p.Enabled = false
		return atomicJSON(path, p)
	case "set":
		data, e := io.ReadAll(io.LimitReader(os.Stdin, 16385))
		if e != nil {
			return e
		}
		if len(data) > 16384 {
			return errors.New("代理链接过长")
		}
		p, err = parseControlProxy(string(data))
		if err != nil {
			return err
		}
	case "enable":
		p.Enabled = true
		if err = p.validate(); err != nil {
			return err
		}
	case "request":
	default:
		return errors.New("无效代理操作")
	}
	if action != "request" {
		if err = ensureControlHelper(binary); err != nil {
			return err
		}
	}
	if seconds < 1 || seconds > 180 {
		return errors.New("请求超时范围无效")
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(seconds)*time.Second)
	defer cancel()
	client, closeClient, err := openControlClient(ctx, endpoint, p, binary)
	if err != nil {
		return err
	}
	defer closeClient()
	method := http.MethodGet
	var body io.Reader
	if action == "request" {
		method = http.MethodPost
		body = os.Stdin
	}
	req, err := http.NewRequestWithContext(ctx, method, endpoint, body)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "text/plain")
	response, err := client.Do(req)
	if err != nil {
		return errors.New("无法经当前连接方式访问 Server，未回退直连")
	}
	defer response.Body.Close()
	if action != "request" {
		if response.StatusCode >= 500 {
			return errors.New("Server 暂时不可用，未保存代理")
		}
		if err = atomicJSON(path, p); err == nil {
			fmt.Println("代理已验证并保存")
		}
		return err
	}
	f, err := os.OpenFile(output, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	_, err = io.Copy(f, io.LimitReader(response.Body, 128<<20))
	closeErr := f.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	fmt.Print(response.StatusCode)
	return nil
}
