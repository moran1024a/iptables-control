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
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/coder/websocket"
)

func (a *Agent) session(ctx context.Context) error {
	headers := http.Header{}
	headers.Set("Authorization", "Bearer "+a.key)
	ws, _, e := websocket.Dial(ctx, "wss"+strings.TrimPrefix(a.url, "https")+"/v3/ws", &websocket.DialOptions{HTTPHeader: headers})
	if e != nil {
		return e
	}
	defer ws.CloseNow()
	ws.SetReadLimit(1024 * 1024)
	var mu sync.Mutex
	session := ""
	send := func(message M) error {
		mu.Lock()
		defer mu.Unlock()
		message["session"] = session
		b, e := json.Marshal(message)
		if e != nil {
			return e
		}
		writeCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
		defer cancel()
		return ws.Write(writeCtx, websocket.MessageText, b)
	}
	if e = send(M{"type": "hello", "protocol": 3, "version": version, "installation_id": a.installation, "boot_id": a.boot, "generation": a.config.Generation, "hash": a.config.Hash, "capabilities": []string{"nftables", "traffic", "metrics", "ports", "queries"}}); e != nil {
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
	receive := make(chan M, 1)
	fail := make(chan error, 1)
	child, stop := context.WithCancel(ctx)
	defer stop()
	queryCtx, stopQueries := context.WithCancel(child)
	defer stopQueries()
	go func() {
		for {
			_, b, e := ws.Read(child)
			if e != nil {
				select {
				case fail <- e:
				default:
				}
				return
			}
			m, e := decodeJSON(b)
			if e != nil {
				select {
				case fail <- e:
				default:
				}
				return
			}
			select {
			case receive <- m:
			case <-child.Done():
				return
			}
		}
	}()
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()
	count := 0
	lastUpdate := time.Time{}
	queries := make(chan struct{}, 2)
	// Reconcile authorization/configuration first; periodic collection begins after the first control response.
	ready := false
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case e := <-fail:
			return e
		case msg := <-receive:
			if msg["session"] != session {
				return errors.New("会话标识不匹配")
			}
			switch msg["type"] {
			case "traffic_ack":
				for _, item := range array(msg["data"]) {
					row, _ := item.(map[string]any)
					key := fmt.Sprintf("%d/%s/%s", number(row["rule_id"]), row["protocol"], row["epoch"])
					if u := a.usage[key]; u != nil && !u.Active && row["up"] == u.Up && row["down"] == u.Down && row["up_packets"] == u.UpPackets && row["down_packets"] == u.DownPackets {
						delete(a.usage, key)
					}
				}
				if e = atomicJSON(a.path("active/traffic.json"), a.usage); e != nil {
					return e
				}
			case "update":
				if a.autoUpdate && time.Since(lastUpdate) > time.Hour {
					lastUpdate = time.Now()
					cmd := exec.Command("systemd-run", "--quiet", "--collect", "--unit=ictl-agent-update", "--", "/usr/local/bin/ictl", "--auto-update")
					if err := cmd.Run(); err != nil {
						log.Printf("自动更新未启动: %v", err)
					}
				}
			case "quiesce":
				stopQueries()
				if e = atomicBytes(a.path("paused"), []byte("manual confirmation required\n"), 0600); e != nil {
					return e
				}
				_ = a.locked(a.sample)
				_ = send(M{"type": "quiesced"})
				_ = ws.Close(websocket.StatusNormalClosure, "paused")
				return nil
			case "config":
				ready = true
				raw, _ := json.Marshal(msg)
				var c Config
				if e = json.Unmarshal(raw, &c); e != nil {
					return e
				}
				for i := range c.Rules {
					if c.Rules[i].Protocol == "" {
						c.Rules[i].Protocol = "both"
					}
				}
				a.stateError = ""
				e = a.apply(c)
				text := ""
				if e != nil {
					text = e.Error()
					a.stateError = text
				}
				// Seal observations before a deletion receipt removes the online identity.
				rows := a.traffic()
				for start := 0; start < len(rows); start += 1000 {
					if e = send(M{"type": "traffic", "id": randomID(), "data": rows[start:min(start+1000, len(rows))]}); e != nil {
						return e
					}
				}
				if e = send(M{"type": "apply_result", "op": msg["op"], "generation": c.Generation, "hash": c.Hash, "mode": "active", "ok": text == "", "error": text, "version": version, "held_ports": a.heldPorts()}); e != nil {
					return e
				}
			case "apply_ack", "ready":
				ready = true
			case "blocked":
				ready = true
				a.stateError = fmt.Sprint(msg["error"])
			case "query":
				select {
				case queries <- struct{}{}:
					snapshot := *a
					body, _ := msg["query"].(map[string]any)
					id := msg["id"]
					go func() {
						defer func() { <-queries }()
						result := snapshot.queryContext(queryCtx, body)
						if child.Err() == nil {
							_ = send(M{"type": "query_result", "id": id, "data": result})
						}
					}()
				default:
					_ = send(M{"type": "query_result", "id": msg["id"], "error": "查询并发已满"})
				}
			case "error":
				return fmt.Errorf("服务端: %v", msg["error"])
			}
		case <-ticker.C:
			if !ready {
				_ = send(M{"type": "status", "generation": a.config.Generation, "hash": a.config.Hash, "state": "ok"})
				continue
			}
			pingCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
			e = ws.Ping(pingCtx)
			cancel()
			if e != nil {
				return e
			}
			count++
			if e = a.locked(a.sample); e != nil {
				a.stateError = "统计持久化失败: " + e.Error()
			}
			state := "ok"
			if a.stateError != "" {
				state = "error"
			}
			for _, message := range []M{{"type": "status", "generation": a.config.Generation, "hash": a.config.Hash, "state": state, "error": a.stateError, "auto_update": a.autoUpdate, "held_ports": a.heldPorts()}, {"type": "metrics", "data": a.metrics()}} {
				if e = send(message); e != nil {
					return e
				}
			}
			rows := a.traffic()
			for start := 0; start < len(rows); start += 1000 {
				if e = send(M{"type": "traffic", "id": randomID(), "data": rows[start:min(start+1000, len(rows))]}); e != nil {
					return e
				}
			}
			if count == 1 || count%6 == 0 {
				if e = send(M{"type": "inventory", "data": a.inventory()}); e != nil {
					return e
				}
				if a.config.Hash != "" {
					if err := a.apply(a.config); err != nil {
						a.stateError = err.Error()
					}
				}
			}
			if count == 1 || count%3 == 0 {
				qctx, cancel := context.WithTimeout(ctx, 5*time.Second)
				ports := a.portSnapshot(qctx, false)
				cancel()
				if e = send(M{"type": "ports", "data": ports}); e != nil {
					return e
				}
			}
		}
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
	if a.config.Hash != "" {
		if e = a.restore(); e != nil {
			a.stateError = e.Error()
			log.Print(e)
		}
	}
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
		e = a.session(ctx)
		if ctx.Err() != nil {
			return nil
		}
		if e == nil {
			return nil
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
	daemon := flag.Bool("daemon", false, "run active agent")
	status := flag.Bool("status", false, "print local state")
	flag.Parse()
	if *showVersion {
		fmt.Println(version)
		return
	}
	a, e := loadAgent(*root, *conf)
	if e != nil {
		log.Fatal(e)
	}
	if e = os.MkdirAll(a.path("active"), 0700); e != nil {
		log.Fatal(e)
	}
	switch {
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
		resp, err := http.DefaultClient.Do(req)
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
