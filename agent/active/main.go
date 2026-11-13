package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"math/rand/v2"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/coder/websocket"
)

// The server repeats an update notice about every ten minutes while the agent stays outdated.
const updateRetry = 9 * time.Minute

// Control reads must keep draining ping/pong frames while rule operations run.
func readControl(ctx context.Context, ws *websocket.Conn, receive chan<- M) error {
	for {
		_, b, err := ws.Read(ctx)
		if err != nil {
			return err
		}
		m, err := decodeJSON(b)
		if err != nil {
			return err
		}
		select {
		case receive <- m:
		case <-ctx.Done():
			return ctx.Err()
		default:
			// Probes are periodic and expendable; a long rule operation must not
			// cost the session and its pending apply result.
			if m["type"] == "quality_probe" {
				continue
			}
			return errors.New("control queue full")
		}
	}
}

func keepControlAlive(ctx context.Context, ws *websocket.Conn, send func(M) error, interval time.Duration) error {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
			pingCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
			err := ws.Ping(pingCtx)
			cancel()
			if err != nil {
				return err
			}
			// Liveness only: never claim a configuration or health check succeeded.
			if err := send(M{"type": "heartbeat"}); err != nil {
				return err
			}
		}
	}
}

func (a *Agent) session(ctx context.Context) error {
	a.initRuntime()
	client, closeClient, err := a.controlClient(ctx)
	if err != nil {
		return err
	}
	defer closeClient()
	headers := http.Header{}
	headers.Set("Authorization", "Bearer "+a.key)
	ws, _, e := websocket.Dial(ctx, "wss"+strings.TrimPrefix(a.url, "https")+"/v3/ws", &websocket.DialOptions{HTTPHeader: headers, HTTPClient: client})
	if e != nil {
		return e
	}
	defer ws.CloseNow()
	ws.SetReadLimit(1024 * 1024)
	child, stop := context.WithCancel(ctx)
	defer stop()
	session := ""
	write := func(message M) error {
		envelope := M{"session": session}
		for key, value := range message {
			envelope[key] = value
		}
		b, err := json.Marshal(envelope)
		if err != nil {
			return err
		}
		writeCtx, cancel := context.WithTimeout(child, 10*time.Second)
		defer cancel()
		return ws.Write(writeCtx, websocket.MessageText, b)
	}
	send := write
	a.stateMu.Lock()
	current := a.config
	a.stateMu.Unlock()
	if e = send(M{"type": "hello", "protocol": 3, "version": version, "auto_update": a.autoUpdate, "installation_id": a.installation, "boot_id": a.boot, "generation": current.Generation, "hash": current.Hash, "capabilities": []string{"address_routing", "nftables", "traffic", "metrics", "ports", "queries", "network_quality", "network_quality_icmp", "network_quality_tcp", "forwarding_health"}}); e != nil {
		return e
	}
	helloCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	_, b, e := ws.Read(helloCtx)
	cancel()
	if e != nil {
		return e
	}
	welcome, e := decodeJSON(b)
	if e != nil || welcome["type"] != "welcome" {
		return errors.New("无效握手响应")
	}
	session, _ = welcome["session"].(string)
	receive := make(chan M, 64)
	fail := make(chan error, 1)
	queryCtx, stopQueries := context.WithCancel(child)
	defer stopQueries()
	qualityCtx, stopQuality := context.WithCancel(child)
	defer stopQuality()
	send, report := messageWriter(child, write)
	monitor := newQualityMonitor(qualityCtx, report)
	reportFailure := func(err error) {
		// Record the cause before cancelling, or the session only ever reports "context canceled".
		select {
		case fail <- err:
		default:
		}
		stop()
	}
	go func() { reportFailure(readControl(child, ws, receive)) }()
	go func() { reportFailure(keepControlAlive(child, ws, send, 10*time.Second)) }()
	ledger := &trafficLedger{}
	ready := make(chan struct{})
	isReady := false
	markReady := func() {
		if !isReady {
			close(ready)
			isReady = true
		}
	}
	go func() { reportFailure(a.collect(child, ready, ledger, report, 10*time.Second)) }()
	lastUpdate := time.Time{}
	queries := make(chan struct{}, 2)
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case err := <-fail:
			return err
		case <-child.Done():
			select {
			case err := <-fail:
				return err
			default:
			}
			return child.Err()
		case msg := <-receive:
			if msg["session"] != session {
				return errors.New("会话标识不匹配")
			}
			switch msg["type"] {
			case "traffic_ack":
				a.stateMu.Lock()
				err := ledger.acknowledge(a, msg)
				a.stateMu.Unlock()
				if err != nil {
					return err
				}
			case "update":
				// A working update stops this process within seconds. Still running
				// means it failed, so try again soon instead of once an hour.
				if a.autoUpdate && time.Since(lastUpdate) > updateRetry {
					lastUpdate = time.Now()
					go func() {
						runCtx, cancel := context.WithTimeout(child, 10*time.Second)
						defer cancel()
						cmd := exec.CommandContext(runCtx, "systemd-run", "--quiet", "--collect", "--unit=ictl-agent-update", "--", "/usr/local/bin/ictl", "--auto-update")
						if err := cmd.Run(); err != nil {
							log.Printf("自动更新未启动: %v", err)
						}
					}()
				}
			case "quiesce":
				stopQuality()
				stopQueries()
				resume := a.maintenance.foreground()
				a.stateMu.Lock()
				err := atomicBytes(a.path("paused"), []byte("manual confirmation required\n"), 0600)
				if err == nil {
					sampleCtx, cancel := context.WithTimeout(child, 5*time.Second)
					a.prepareCtx = sampleCtx
					_ = a.lockedContext(sampleCtx, a.sample)
					a.prepareCtx = nil
					cancel()
				}
				a.stateMu.Unlock()
				resume()
				if err != nil {
					return err
				}
				_ = send(M{"type": "quiesced"})
				_ = ws.Close(websocket.StatusNormalClosure, "paused")
				return nil
			case "config":
				started := time.Now()
				raw, _ := json.Marshal(msg)
				var c Config
				if err := json.Unmarshal(raw, &c); err != nil {
					return err
				}
				for i := range c.Rules {
					if c.Rules[i].Protocol == "" {
						c.Rules[i].Protocol = "both"
					}
				}
				resume := a.maintenance.foreground()
				a.stateMu.Lock()
				a.stateError = ""
				applyCtx, cancel := context.WithTimeout(child, 60*time.Second)
				err := monitor.applyContext(a, c, applyCtx)
				cancel()
				text := ""
				if err != nil {
					text = err.Error()
					a.stateError = text
				}
				held := a.heldPorts()
				var finalRows []Usage
				if len(c.Rules) == 0 {
					finalRows = a.traffic()
				}
				a.stateMu.Unlock()
				resume()
				// Empty configurations can delete the online identity; seal final traffic first.
				if len(finalRows) > 0 {
					if err := ledger.send(finalRows, send); err != nil {
						return err
					}
				}
				if err := send(M{"type": "apply_result", "op": msg["op"], "generation": c.Generation, "hash": c.Hash, "mode": "active", "ok": text == "", "error": text, "version": version, "held_ports": held}); err != nil {
					return err
				}
				log.Printf("配置 generation=%d 完成，耗时=%s，成功=%t", c.Generation, time.Since(started).Round(time.Millisecond), text == "")
				markReady()
			case "apply_ack", "ready":
				markReady()
			case "blocked":
				a.stateMu.Lock()
				a.stateError = fmt.Sprint(msg["error"])
				a.stateMu.Unlock()
				markReady()
			case "quality_setup":
				snapshot := a.snapshot()
				monitor.setup(&snapshot, msg)
			case "quality_probe":
				snapshot := a.snapshot()
				monitor.probeWithAgent(&snapshot, msg)
			case "query":
				select {
				case queries <- struct{}{}:
					snapshot := a.snapshot()
					body, _ := msg["query"].(map[string]any)
					id := msg["id"]
					go func() {
						defer func() { <-queries }()
						result := snapshot.queryContext(queryCtx, body)
						if child.Err() == nil {
							_ = report(M{"type": "query_result", "id": id, "data": result})
						}
					}()
				default:
					_ = report(M{"type": "query_result", "id": msg["id"], "error": "查询并发已满"})
				}
			case "error":
				return fmt.Errorf("服务端: %v", msg["error"])
			}
		}
	}
}

// Local counters and DNS/rule maintenance must continue during a control outage.
func (a *Agent) maintain(ctx context.Context, interval time.Duration) {
	a.initRuntime()
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	count := 0
	lastError := ""
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		work, done, ok := a.maintenance.begin(ctx)
		if !ok {
			continue
		}
		a.stateMu.Lock()
		if ctx.Err() != nil || readText(a.path("paused")) != "" {
			a.stateMu.Unlock()
			done()
			return
		}
		if _, err := os.Stat(a.path("active/enabled")); err != nil {
			a.stateMu.Unlock()
			done()
			return
		}
		count++
		a.prepareCtx = work
		var err error
		if (count == 1 || count%6 == 0) && a.config.Hash != "" {
			err = a.applyContext(work, a.config)
		} else {
			err = a.lockedContext(work, a.sample)
		}
		a.prepareCtx = nil
		// Yielding to an incoming configuration is not a forwarding fault.
		if !errors.Is(work.Err(), context.Canceled) {
			if err != nil {
				lastError = "本地维护失败: " + err.Error()
				a.stateError = lastError
			} else if a.stateError == lastError {
				a.stateError = ""
				lastError = ""
			}
		}
		a.stateMu.Unlock()
		done()
	}
}

func (a *Agent) daemon(ctx context.Context) error {
	lock, e := os.OpenFile(a.path("active/instance.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if e != nil {
		return e
	}
	defer lock.Close()
	if e = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); e != nil {
		return errors.New("已有主动 Agent 运行")
	}
	// During an unconfirmed handover the old service must not restore old rules.
	if _, e = os.Stat(a.path("active/enabled")); e != nil {
		return nil
	}
	if _, e = os.Stat(a.path("paused")); e == nil {
		return nil
	}
	// Registered before the restore: a settings reload during startup must not kill the process.
	reload := make(chan os.Signal, 1)
	signal.Notify(reload, syscall.SIGHUP)
	defer signal.Stop(reload)
	if a.config.Hash != "" {
		if e = a.restore(); e != nil {
			a.stateError = e.Error()
			log.Print(e)
		}
	}
	a.initRuntime()
	localCtx, stopLocal := context.WithCancel(ctx)
	localDone := make(chan struct{})
	go func() { defer close(localDone); a.maintain(localCtx, 10*time.Second) }()
	defer func() { stopLocal(); <-localDone }()
	backoff := []time.Duration{1, 2, 5, 10, 30}
	attempt := 0
	for {
		if _, e = os.Stat(a.path("paused")); e == nil {
			return nil
		}
		if _, e = os.Stat(a.path("active/enabled")); e != nil {
			return nil
		}
		start := time.Now()
		sessionCtx, cancelSession := context.WithCancel(ctx)
		watcherDone := make(chan struct{})
		watcherExited := make(chan struct{})
		go func() {
			defer close(watcherExited)
			select {
			case <-reload:
				cancelSession()
			case <-watcherDone:
			}
		}()
		e = a.session(sessionCtx)
		close(watcherDone)
		<-watcherExited
		changed := sessionCtx.Err() != nil
		cancelSession()
		if ctx.Err() != nil {
			return nil
		}
		if e == nil {
			return nil
		}
		if changed {
			attempt = 0
			continue
		}
		log.Printf("管理连接断开: %v", e)
		if time.Since(start) > time.Minute {
			attempt = 0
		}
		delay := backoff[min(attempt, len(backoff)-1)]*time.Second + time.Duration(rand.IntN(500))*time.Millisecond
		attempt++
		select {
		case <-ctx.Done():
			return nil
		case <-reload:
			attempt = 0
		case <-time.After(delay):
		}
	}
}

func main() {
	showVersion := flag.Bool("version", false, "print version")
	root := flag.String("state-root", "/var/lib/ictl", "state directory")
	conf := flag.String("conf", "/etc/ictl/agent.conf", "identity configuration")
	applyFile := flag.String("apply-file", "", "apply v2-format snapshot")
	generation := flag.Int64("generation", 0, "snapshot generation")
	clear := flag.Bool("clear", false, "remove owned rules and seal counters")
	flush := flag.Bool("flush-traffic", false, "upload final counters")
	restore := flag.Bool("restore", false, "restore cached forwarding offline")
	genOnly := flag.Bool("generation-only", false, "print local generation")
	held := flag.Bool("held-ports", false, "print draining ports")
	checkpoint := flag.Bool("checkpoint", false, "persist counters")
	recoverEmpty := flag.Bool("recover-empty-table", false, "repair an empty table from a recorded failed installation")
	daemon := flag.Bool("daemon", false, "run active agent")
	status := flag.Bool("status", false, "print local state")
	proxyAction := flag.String("proxy-action", "", "local control proxy: status/set/enable/disable/request")
	proxyFile := flag.String("proxy-file", "", "control proxy settings file")
	proxyBinary := flag.String("proxy-helper", "/usr/local/lib/ictl/sslocal", "SS2022 client binary")
	serverURL := flag.String("server-url", "", "Server URL for a management request")
	controlOutput := flag.String("control-output", "", "encrypted management response file")
	controlTimeout := flag.Int("control-timeout", 15, "management timeout in seconds")
	flag.Parse()
	if *showVersion {
		fmt.Println(version)
		return
	}
	if *proxyAction != "" {
		if *proxyFile == "" {
			*proxyFile = filepath.Join(filepath.Dir(*conf), "proxy.json")
		}
		if err := proxyCommand(*proxyAction, *conf, *proxyFile, *serverURL, *proxyBinary, *controlOutput, *controlTimeout); err != nil {
			log.Fatal(err)
		}
		return
	}
	a, e := loadAgent(*root, *conf)
	if e != nil {
		log.Fatal(e)
	}
	a.proxyBinary = *proxyBinary
	if *proxyFile != "" {
		a.proxyFile = *proxyFile
	}
	if e = os.MkdirAll(a.path("active"), 0700); e != nil {
		log.Fatal(e)
	}
	switch {
	case *recoverEmpty:
		e = a.recoverEmptyTable()
	case *held:
		e = json.NewEncoder(os.Stdout).Encode(a.heldPorts())
	case *genOnly:
		fmt.Println(a.config.Generation)
	case *restore:
		e = a.restore()
	case *flush:
		e = a.flushTraffic()
	case *applyFile != "":
		b, err := os.ReadFile(*applyFile)
		if err != nil {
			e = err
			break
		}
		c, err := parseConfig(bytes.TrimSpace(b), *generation)
		if err != nil {
			e = err
			break
		}
		e = a.apply(c)
	case *clear:
		e = a.clear()
	case *checkpoint:
		e = a.locked(a.sample)
	case *status:
		e = json.NewEncoder(os.Stdout).Encode(M{"version": version, "config": a.config, "traffic": a.traffic()})
	case *daemon:
		ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
		defer cancel()
		e = a.daemon(ctx)
	default:
		flag.Usage()
		os.Exit(2)
	}
	if e != nil {
		log.Fatal(e)
	}
}

func (a *Agent) flushTraffic() error {
	if err := a.locked(a.sample); err != nil {
		return err
	}
	clientCtx, stopClient := context.WithCancel(context.Background())
	defer stopClient()
	client, closeClient, err := a.controlClient(clientCtx)
	if err != nil {
		return err
	}
	defer closeClient()
	rows := a.traffic()
	for start := 0; start < len(rows); start += 1000 {
		body, err := json.Marshal(rows[start:min(start+1000, len(rows))])
		if err != nil {
			return err
		}
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		req, err := http.NewRequestWithContext(ctx, "POST", a.url+"/v3/traffic", bytes.NewReader(body))
		if err != nil {
			cancel()
			return err
		}
		req.Header.Set("Authorization", "Bearer "+a.key)
		req.Header.Set("Content-Type", "application/json")
		resp, err := client.Do(req)
		if err != nil {
			cancel()
			return err
		}
		resp.Body.Close()
		cancel()
		if resp.StatusCode != 200 {
			return fmt.Errorf("统计确认失败: HTTP %d", resp.StatusCode)
		}
	}
	return nil
}
