package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
)

type Usage struct {
	RuleID      int64    `json:"rule_id"`
	Protocol    string   `json:"protocol"`
	Epoch       string   `json:"epoch"`
	Up          string   `json:"up"`
	Down        string   `json:"down"`
	UpPackets   string   `json:"up_packets"`
	DownPackets string   `json:"down_packets"`
	Quality     string   `json:"quality"`
	KernelEpoch string   `json:"kernel_epoch,omitempty"`
	Active      bool     `json:"active,omitempty"`
	Offsets     []uint64 `json:"offsets,omitempty"`
}

var counterRE = regexp.MustCompile(`^r([0-9]+)_(tcp|udp)_(up|down)_([a-f0-9]{32})$`)

func uintText(n uint64) string  { return strconv.FormatUint(n, 10) }
func parseUint(s string) uint64 { n, _ := strconv.ParseUint(s, 10, 64); return n }
func (a *Agent) sample() error {
	doc, exists, e := a.nftState()
	if e != nil {
		return e
	}
	if !exists {
		for _, u := range a.usage {
			if u.Active {
				u.Active = false
				u.Quality = "统计缺口：内核计数器丢失"
			}
		}
		return atomicJSON(a.path("active/traffic.json"), a.usage)
	}
	type value struct {
		id                               int64
		protocol, epoch                  string
		up, down, upPackets, downPackets uint64
	}
	values := map[string]*value{}
	for _, x := range array(doc["nftables"]) {
		m, _ := x.(map[string]any)
		v, ok := m["counter"].(map[string]any)
		if !ok {
			continue
		}
		match := counterRE.FindStringSubmatch(fmt.Sprint(v["name"]))
		if match == nil {
			continue
		}
		key := match[1] + "/" + match[2] + "/" + match[4]
		item := values[key]
		if item == nil {
			id, _ := strconv.ParseInt(match[1], 10, 64)
			item = &value{id: id, protocol: match[2], epoch: match[4]}
			values[key] = item
		}
		if match[3] == "up" {
			item.up = number(v["bytes"])
			item.upPackets = number(v["packets"])
		} else {
			item.down = number(v["bytes"])
			item.downPackets = number(v["packets"])
		}
	}
	for _, v := range values {
		var use *Usage
		for _, u := range a.usage {
			if u.Active && u.RuleID == v.id && u.Protocol == v.protocol && u.KernelEpoch == v.epoch {
				use = u
				break
			}
		}
		quality := "ok"
		if a.quality != "" {
			quality = a.quality
		}
		raw := []uint64{v.up, v.down, v.upPackets, v.downPackets}
		previous := make([]uint64, 4)
		reset := false
		if use != nil {
			for i, text := range []string{use.Up, use.Down, use.UpPackets, use.DownPackets} {
				previous[i] = parseUint(text)
				if len(use.Offsets) == 4 {
					previous[i] += use.Offsets[i]
				}
				reset = reset || raw[i] < previous[i]
			}
		}
		if reset {
			use.Active = false
			use.Quality = "统计缺口：计数器被外部重置"
			use = nil
			quality = "计数器重置后的新周期"
		}
		if use == nil {
			use = &Usage{RuleID: v.id, Protocol: v.protocol, Epoch: randomID(), KernelEpoch: v.epoch, Active: true, Quality: quality}
			if reset {
				// A reset of one direction must not count the other direction twice.
				use.Offsets = make([]uint64, 4)
				for i := range raw {
					if raw[i] >= previous[i] {
						use.Offsets[i] = previous[i]
					}
				}
			}
			a.usage[fmt.Sprintf("%d/%s/%s", v.id, v.protocol, use.Epoch)] = use
		}
		if len(use.Offsets) == 4 {
			for i := range raw {
				raw[i] -= use.Offsets[i]
			}
		}
		use.Up = uintText(raw[0])
		use.Down = uintText(raw[1])
		use.UpPackets = uintText(raw[2])
		use.DownPackets = uintText(raw[3])
		if quality != "ok" {
			use.Quality = quality
		}
	}
	for _, u := range a.usage {
		key := fmt.Sprintf("%d/%s/%s", u.RuleID, u.Protocol, u.KernelEpoch)
		if u.Active && values[key] == nil {
			u.Active = false
			u.Quality = "统计缺口：计数器消失或系统重启"
		}
	}
	return atomicJSON(a.path("active/traffic.json"), a.usage)
}
func (a *Agent) traffic() []Usage {
	out := []Usage{}
	for _, u := range a.usage {
		row := *u
		row.KernelEpoch = ""
		row.Active = false
		row.Offsets = nil
		out = append(out, row)
	}
	sort.Slice(out, func(i, j int) bool {
		return fmt.Sprintf("%d%s%s", out[i].RuleID, out[i].Protocol, out[i].Epoch) < fmt.Sprintf("%d%s%s", out[j].RuleID, out[j].Protocol, out[j].Epoch)
	})
	return out
}
func readText(path string) string { b, _ := os.ReadFile(path); return strings.TrimSpace(string(b)) }
func fieldsFile(path string) map[string]uint64 {
	out := map[string]uint64{}
	for _, line := range strings.Split(readText(path), "\n") {
		f := strings.Fields(line)
		if len(f) >= 2 {
			n, _ := strconv.ParseUint(f[1], 10, 64)
			if len(f) > 2 && f[2] == "kB" {
				n *= 1024
			}
			out[strings.TrimSuffix(f[0], ":")] = n
		}
	}
	return out
}
func (a *Agent) metrics() M {
	now := time.Now()
	mem := fieldsFile("/proc/meminfo")
	proc := fieldsFile("/proc/self/status")
	result := M{"sampled_at": now.UnixMilli(), "scope": "current Linux namespace", "memory": mem, "agent_rss": proc["VmRSS"], "load": strings.Fields(readText("/proc/loadavg")), "uptime": strings.Fields(readText("/proc/uptime"))}
	line := strings.Split(readText("/proc/stat"), "\n")[0]
	f := strings.Fields(line)
	var total, idle uint64
	var cpuParts [8]uint64
	for i, v := range f[1:] {
		if i >= 8 {
			break
		}
		n, _ := strconv.ParseUint(v, 10, 64)
		cpuParts[i] = n
		total += n
		if i == 3 || i == 4 {
			idle += n
		}
	}
	if total > a.lastCPU[0] && a.lastCPU[0] > 0 {
		result["cpu_percent"] = 100 * (1 - float64(idle-a.lastCPU[1])/float64(total-a.lastCPU[0]))
		parts := M{}
		for i, key := range []string{"user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"} {
			parts[key] = 100 * float64(cpuParts[i]-a.lastCPUParts[i]) / float64(total-a.lastCPU[0])
		}
		result["cpu_parts_percent"] = parts
	}
	a.lastCPU = [2]uint64{total, idle}
	a.lastCPUParts = cpuParts
	var usage syscall.Rusage
	if syscall.Getrusage(syscall.RUSAGE_SELF, &usage) == nil {
		seconds := float64(usage.Utime.Sec+usage.Stime.Sec) + float64(usage.Utime.Usec+usage.Stime.Usec)/1e6
		if !a.lastMetrics.IsZero() {
			result["agent_cpu_percent"] = 100 * (seconds - a.lastProcessCPU) / now.Sub(a.lastMetrics).Seconds()
		}
		a.lastProcessCPU = seconds
	}
	var disk syscall.Statfs_t
	if e := syscall.Statfs(a.root, &disk); e == nil {
		result["disk"] = M{"total": uintText(disk.Blocks * uint64(disk.Bsize)), "available": uintText(disk.Bavail * uint64(disk.Bsize)), "files": disk.Files, "files_free": disk.Ffree}
	}
	interfaces := M{}
	nextNet := map[string][2]uint64{}
	elapsed := now.Sub(a.lastMetrics).Seconds()
	for _, line := range strings.Split(readText("/proc/net/dev"), "\n") {
		name, right, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		f := strings.Fields(right)
		if len(f) < 16 {
			continue
		}
		name = strings.TrimSpace(name)
		row := M{"rx_bytes": f[0], "rx_packets": f[1], "rx_errors": f[2], "rx_dropped": f[3], "tx_bytes": f[8], "tx_packets": f[9], "tx_errors": f[10], "tx_dropped": f[11]}
		current := [2]uint64{parseUint(f[0]), parseUint(f[8])}
		nextNet[name] = current
		if prev, ok := a.lastNet[name]; ok && elapsed > 0 && current[0] >= prev[0] && current[1] >= prev[1] {
			row["rx_bytes_per_second"] = float64(current[0]-prev[0]) / elapsed
			row["tx_bytes_per_second"] = float64(current[1]-prev[1]) / elapsed
		}
		interfaces[name] = row
	}
	a.lastMetrics = now
	a.lastNet = nextNet
	result["interfaces"] = interfaces
	result["conntrack_count"] = readText("/proc/sys/net/netfilter/nf_conntrack_count")
	result["conntrack_max"] = readText("/proc/sys/net/netfilter/nf_conntrack_max")
	result["diskstats"] = readText("/proc/diskstats")
	result["error"] = a.stateError
	result["traffic_quality"] = a.quality
	return result
}
func (a *Agent) inventory() M {
	result := M{"sampled_at": time.Now().UnixMilli(), "version": version, "os": readText("/etc/os-release"), "kernel": readText("/proc/sys/kernel/osrelease"), "arch": runtime.GOARCH, "cpus": runtime.NumCPU(), "boot_id": a.boot, "installation_id": a.installation, "backend": "nftables", "cgroup": readText("/proc/self/cgroup")}
	if b, e := run(nil, "nft", "--version"); e == nil {
		result["nft_version"] = strings.TrimSpace(string(b))
	}
	if ifaces, e := net.Interfaces(); e == nil {
		rows := []M{}
		complete := true
		for _, i := range ifaces {
			addrs, err := i.Addrs()
			if err != nil {
				complete = false
			}
			addresses := []string{}
			for _, v := range addrs {
				addresses = append(addresses, v.String())
			}
			rows = append(rows, M{"name": i.Name, "mtu": i.MTU, "addresses": addresses, "flags": i.Flags.String()})
		}
		result["interfaces"] = rows
		result["network_complete"] = complete
	}
	return result
}
func (a *Agent) portSnapshot(ctx context.Context, detail bool) M {
	rows := []M{}
	errors := []string{}
	for _, p := range []string{"tcp", "udp"} {
		flag := "-lnt"
		if p == "udp" {
			flag = "-aun"
		}
		args := []string{"-H", flag}
		if detail {
			args = append(args, "-p")
		}
		b, e := command(ctx, nil, "ss", args...)
		if e != nil {
			errors = append(errors, e.Error())
			continue
		}
		for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
			if line == "" {
				continue
			}
			host, port := socketPort(line)
			rows = append(rows, M{"source": "socket", "protocol": p, "address": host, "port": port, "detail": line})
		}
	}
	for _, r := range append(append([]Rule{}, a.config.Rules...), a.config.Retired...) {
		rows = append(rows, M{"source": "ictl", "rule_id": r.ID, "protocol": r.Protocol, "port": r.Port, "target": net.JoinHostPort(r.Host, strconv.Itoa(r.TargetPort))})
	}
	// Native and legacy rule listings are included on demand; socket rows are not a NAT inventory.
	total := len(rows)
	if len(rows) > 1000 {
		rows = rows[:1000]
	}
	out := M{"sampled_at": time.Now().UnixMilli(), "rows": rows, "errors": errors, "total": total, "truncated": total > len(rows)}
	if detail {
		for _, tool := range []struct {
			name string
			args []string
		}{{"nft", []string{"-j", "list", "ruleset"}}, {"iptables-save", nil}} {
			b, e := command(ctx, nil, tool.name, tool.args...)
			if e != nil {
				out[tool.name+"_error"] = e.Error()
			} else {
				out[tool.name] = string(b)
			}
		}
	}
	return out
}
func (a *Agent) query(body M) M { return a.queryContext(context.Background(), body) }
func (a *Agent) queryContext(parent context.Context, body M) M {
	kind, _ := body["kind"].(string)
	timeout := 10 * time.Second
	if kind == "diagnose" {
		timeout = 30 * time.Second
	}
	ctx, cancel := context.WithTimeout(parent, timeout)
	defer cancel()
	out := M{"sampled_at": time.Now().UnixMilli(), "kind": kind}
	var b []byte
	var e error
	switch kind {
	case "inventory":
		return a.inventory()
	case "ports", "port_detail":
		out = a.portSnapshot(ctx, true)
	case "network":
		b, e = command(ctx, nil, "ip", "-j", "address")
		out["addresses"] = string(b)
		b, e = command(ctx, nil, "ip", "-j", "route")
		out["routes"] = string(b)
		out["dns"] = readText("/etc/resolv.conf")
	case "rules":
		b, e = command(ctx, nil, "nft", "-j", "list", "table", "ip", tableName)
		out["rules"] = string(b)
	case "logs":
		b, e = command(ctx, nil, "journalctl", "-u", "ictl-agent", "-n", "200", "--no-pager", "-o", "short-iso")
		out["logs"] = string(b)
	case "connections":
		id := int64(number(body["rule_id"]))
		var selected *Rule
		for i := range a.config.Rules {
			if a.config.Rules[i].ID == id {
				r := a.config.Rules[i]
				selected = &r
				break
			}
		}
		if selected == nil {
			e = fmt.Errorf("请选择当前转发 rule_id")
			break
		}
		connections := M{}
		for _, p := range protocols(*selected) {
			b, e = command(ctx, nil, "conntrack", "-L", "-f", "ipv4", "-p", p, "--orig-port-dst", strconv.Itoa(selected.Port), "--reply-src", selected.IP, "--reply-port-src", strconv.Itoa(selected.TargetPort))
			connections[p] = string(b)
			if e != nil {
				break
			}
		}
		out["connections"] = connections
	case "diagnose":
		id := int64(number(body["rule_id"]))
		var r *Rule
		for i := range a.config.Rules {
			if a.config.Rules[i].ID == id {
				x := a.config.Rules[i]
				r = &x
			}
		}
		if r == nil {
			e = fmt.Errorf("只允许诊断当前配置的目标")
			break
		}
		start := time.Now()
		ips, err := net.DefaultResolver.LookupIP(ctx, "ip4", r.Host)
		out["dns_ms"] = float64(time.Since(start).Microseconds()) / 1000
		if err != nil || len(ips) == 0 {
			e = err
			if e == nil {
				e = fmt.Errorf("没有 IPv4 解析结果")
			}
			break
		}
		out["addresses"] = ips
		if r.Protocol == "udp" {
			out["note"] = "UDP 无响应不能判断故障，本次仅解析地址"
			break
		}
		dialer := routeDialer("tcp-connect-v1")
		if r.Route != "" {
			route, err := parseRoute(r.Route)
			if err != nil {
				e = err
				break
			}
			dialer = routeDialer("tcp-connect-v1", route.Out)
		}
		destination := ips[0].String()
		if net.ParseIP(r.IP) != nil {
			destination = r.IP
		}
		out["resolved_ip"] = destination
		start = time.Now()
		conn, err := dialer.DialContext(ctx, "tcp4", net.JoinHostPort(destination, strconv.Itoa(r.TargetPort)))
		out["tcp_connect_ms"] = float64(time.Since(start).Microseconds()) / 1000
		if err != nil {
			e = err
		} else {
			conn.Close()
			out["reachable"] = true
		}
	default:
		e = fmt.Errorf("不支持的查询类型")
	}
	if e != nil {
		out["error"] = e.Error()
	}
	encoded, _ := json.Marshal(out)
	if len(encoded) > 900*1024 {
		return M{"sampled_at": time.Now().UnixMilli(), "error": "结果过大，请缩小查询范围", "truncated": true}
	}
	return out
}
