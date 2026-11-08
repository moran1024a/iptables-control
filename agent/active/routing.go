package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

// A profile is a local source address and its explicitly selected network.
// The same on-disk format is used by the Bash agent during handover.
type routeProfile struct{ Local, Device, Gateway string }
type ruleRoute struct {
	In, Out routeProfile
	Listen  string
}

var deviceRE = regexp.MustCompile(`^[A-Za-z0-9_.:-]{1,15}$`)
var listenRE = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9.-]*$`)

func parseRoute(value string) (ruleRoute, error) {
	var r ruleRoute
	if value == "" {
		return r, nil
	}
	p := strings.Split(value, ",")
	if len(p) == 5 && p[0] == "v2" {
		if len(p[1]) > 253 || !listenRE.MatchString(p[1]) {
			return r, errors.New("无效的接收地址")
		}
		r.Listen = p[1]
		if p[2] == "-" && p[3] == "-" && p[4] == "-" {
			return r, nil
		}
		if p[2] == "-" || p[3] == "-" {
			return r, errors.New("指定出口缺少源 IP 或网卡")
		}
		// Reuse the legacy profile validator; receiving itself has no local
		// interface prerequisite. Only a selected source needs this profile.
		legacy, err := parseRoute(strings.Join([]string{p[2], p[2], p[3], p[3], p[4], p[4]}, ","))
		r.Out = legacy.Out
		return r, err
	}
	if len(p) != 6 {
		return r, errors.New("无效的地址路由配置")
	}
	for _, i := range []int{0, 1, 4, 5} {
		if i >= 4 && p[i] == "-" {
			continue
		}
		ip := net.ParseIP(p[i])
		if ip == nil || ip.To4() == nil || ip.IsUnspecified() || ip.IsMulticast() || ip.IsLoopback() || ip.String() != p[i] {
			return r, errors.New("路由需要有效的本地 IPv4 地址")
		}
	}
	if !deviceRE.MatchString(p[2]) || !deviceRE.MatchString(p[3]) {
		return r, errors.New("无效的路由网卡")
	}
	r.In = routeProfile{p[0], p[2], p[4]}
	r.Out = routeProfile{p[1], p[3], p[5]}
	return r, nil
}
func (p routeProfile) enabled() bool { return p.Local != "" }
func (p routeProfile) key() string   { return p.Local + "," + p.Device + "," + p.Gateway }
func (p routeProfile) table() int {
	h := sha256.Sum256([]byte(p.key()))
	n, _ := strconv.ParseInt(hex.EncodeToString(h[:])[:5], 16, 32)
	return 100000 + int(n)
}
func (p routeProfile) mark() int     { return 0x49000000 + p.table() }
func (p routeProfile) priority() int { return 18000 + p.table()%10000 }
func (a *Agent) profilePath(p routeProfile) string {
	return a.path(fmt.Sprintf("routing/%d", p.table()))
}

func (a *Agent) ownsPolicyRule(r map[string]any) bool {
	table, err := strconv.Atoi(fmt.Sprint(r["table"]))
	if err != nil || table < 100000 || fmt.Sprint(r["protocol"]) != "242" {
		return false
	}
	b, err := os.ReadFile(a.path(fmt.Sprintf("routing/%d", table)))
	if err != nil {
		return false
	}
	parts := strings.Split(strings.TrimSpace(string(b)), ",")
	if len(parts) != 3 {
		return false
	}
	p := routeProfile{parts[0], parts[1], parts[2]}
	mark, mask := fmt.Sprint(r["fwmark"]), fmt.Sprint(r["fwmask"])
	return p.table() == table && int(number(r["priority"])) == p.priority() &&
		(mark == fmt.Sprintf("0x%x", p.mark()) || mark == strconv.Itoa(p.mark())) &&
		(r["fwmask"] == nil || mask == "4294967295" || mask == "0xffffffff")
}
func ipJSON(args ...string) ([]map[string]any, error) {
	b, e := run(nil, "ip", append([]string{"-N", "-j", "-4"}, args...)...)
	var rows []map[string]any
	if e == nil {
		decoder := json.NewDecoder(bytes.NewReader(b))
		decoder.UseNumber()
		e = decoder.Decode(&rows)
	}
	return rows, e
}
func (a *Agent) prepareProfile(p routeProfile) error {
	addresses, e := ipJSON("address", "show", "dev", p.Device)
	if e != nil {
		return e
	}
	found := false
	for _, row := range addresses {
		for _, raw := range array(row["addr_info"]) {
			v, _ := raw.(map[string]any)
			found = found || v["local"] == p.Local
		}
	}
	if !found {
		return fmt.Errorf("地址 %s 未绑定在网卡 %s", p.Local, p.Device)
	}
	path := a.profilePath(p)
	prior, readErr := os.ReadFile(path)
	if readErr != nil && !os.IsNotExist(readErr) {
		return readErr
	}
	if readErr == nil && strings.TrimSpace(string(prior)) != p.key() {
		return errors.New("路由表标识冲突")
	}
	table := strconv.Itoa(p.table())
	routes, e := ipJSON("route", "show", "table", table)
	// iproute2 reports an absent FIB table as an error on some kernels.
	if e != nil && !strings.Contains(e.Error(), "FIB table does not exist") {
		return e
	}
	rules, e := ipJSON("rule", "show")
	if e != nil {
		return e
	}
	existingRule := false
	for _, r := range rules {
		priority := int(number(r["priority"]))
		if priority != p.priority() {
			continue
		}
		// Equal priorities are valid for our disjoint, exact fwmark selectors.
		// Accept only journalled rules; never absorb another tool's selectors.
		if !a.ownsPolicyRule(r) {
			return errors.New("策略路由优先级被其他规则占用")
		}
		existingRule = existingRule || fmt.Sprint(r["table"]) == table
	}
	for _, r := range routes {
		if readErr != nil || fmt.Sprint(r["protocol"]) != "242" {
			return errors.New("策略路由表被其他工具占用")
		}
	}
	if e = atomicBytes(path, []byte(p.key()+"\n"), 0600); e != nil {
		return e
	}
	// Copy only connected routes belonging to this device. Never flush main.
	wanted := map[string]bool{"default/32767": true}
	connected, e := ipJSON("route", "show", "dev", p.Device, "scope", "link")
	if e != nil {
		return e
	}
	for _, r := range connected {
		dst, _ := r["dst"].(string)
		if dst == "" || dst == "default" {
			continue
		}
		if _, e = run(nil, "ip", "-4", "route", "replace", dst, "dev", p.Device, "src", p.Local, "table", table, "proto", "242"); e != nil {
			return e
		}
		wanted[dst+"/0"] = true
	}
	gateway := p.Gateway
	if gateway == "-" {
		defaults, e := ipJSON("route", "show", "default", "dev", p.Device)
		if e != nil {
			return e
		}
		for _, r := range defaults {
			g, _ := r["gateway"].(string)
			if g == "" {
				continue
			}
			if gateway != "-" && gateway != g {
				return errors.New("网卡存在多个网关，请明确指定")
			}
			gateway = g
		}
	}
	args := []string{"-4", "route", "replace", "unreachable", "default", "metric", "32767", "table", table, "proto", "242"}
	if _, e = run(nil, "ip", args...); e != nil {
		return e
	}
	if gateway != "-" {
		if _, e = run(nil, "ip", "-4", "route", "replace", "default", "via", gateway, "dev", p.Device, "src", p.Local, "metric", "10", "table", table, "proto", "242"); e != nil {
			return e
		}
		wanted["default/10"] = true
	}
	// Reconcile withdrawals as well as additions. Install replacements first so
	// an unchanged route never disappears while this profile carries traffic.
	for _, r := range routes {
		dst, _ := r["dst"].(string)
		metric := strconv.Itoa(int(number(r["metric"])))
		if !wanted[dst+"/"+metric] {
			if _, e = run(nil, "ip", "-4", "route", "del", dst, "metric", metric, "table", table, "proto", "242"); e != nil {
				return e
			}
		}
	}
	if !existingRule {
		if _, e = run(nil, "ip", "-4", "rule", "add", "priority", strconv.Itoa(p.priority()), "fwmark", fmt.Sprintf("0x%x/0xffffffff", p.mark()), "table", table, "protocol", "242"); e != nil {
			return e
		}
	}
	// Strict reverse-path filtering rejects legitimate asymmetric forwarding.
	sys := filepath.Join("/proc/sys/net/ipv4/conf", p.Device, "rp_filter")
	value, e := os.ReadFile(sys)
	if e != nil {
		return e
	}
	all, e := os.ReadFile("/proc/sys/net/ipv4/conf/all/rp_filter")
	if e != nil {
		return e
	}
	if strings.TrimSpace(string(value)) == "1" || (strings.TrimSpace(string(value)) == "0" && strings.TrimSpace(string(all)) == "1") {
		backup := a.path("routing/rp_" + p.Device)
		if _, e = os.Stat(backup); os.IsNotExist(e) {
			if e = atomicBytes(backup, value, 0600); e != nil {
				return e
			}
		}
		if e = os.WriteFile(sys, []byte("2\n"), 0644); e != nil {
			return e
		}
	}
	return nil
}
func profiles(c Config) map[string]routeProfile {
	out := map[string]routeProfile{}
	for _, r := range append(append([]Rule{}, c.Rules...), c.Retired...) {
		if r.Route != "" {
			route, _ := parseRoute(r.Route)
			for _, p := range []routeProfile{route.In, route.Out} {
				if p.enabled() {
					out[p.key()] = p
				}
			}
		}
	}
	return out
}
func (a *Agent) prepareRoutes(c Config) error {
	for _, p := range profiles(c) {
		if e := a.prepareProfile(p); e != nil {
			return e
		}
	}
	for _, r := range c.Rules {
		if r.Route != "" {
			route, _ := parseRoute(r.Route)
			if !route.Out.enabled() {
				continue
			}
			rows, e := ipJSON("route", "get", r.IP, "from", route.Out.Local, "mark", fmt.Sprint(route.Out.mark()))
			if e != nil {
				return e
			}
			if len(rows) != 1 || rows[0]["dev"] != route.Out.Device {
				return errors.New("所选出口无法路由到下一跳")
			}
		}
	}
	return nil
}
func (a *Agent) pruneRoutes(c Config) error {
	wanted := profiles(c)
	files, e := os.ReadDir(a.path("routing"))
	if os.IsNotExist(e) {
		return nil
	}
	if e != nil {
		return e
	}
	devices := map[string]bool{}
	for _, p := range wanted {
		devices[p.Device] = true
	}
	for _, f := range files {
		if f.IsDir() || strings.HasPrefix(f.Name(), "rp_") {
			continue
		}
		if _, e = strconv.Atoi(f.Name()); e != nil {
			continue
		}
		path := a.path("routing/" + f.Name())
		b, e := os.ReadFile(path)
		if e != nil {
			return e
		}
		parts := strings.Split(strings.TrimSpace(string(b)), ",")
		if len(parts) != 3 {
			return errors.New("本地路由记录损坏")
		}
		p := routeProfile{parts[0], parts[1], parts[2]}
		if _, ok := wanted[p.key()]; ok {
			continue
		}
		if strconv.Itoa(p.table()) != f.Name() {
			return errors.New("本地路由归属不一致")
		}
		// Exact selectors ensure unrelated policy rules are never removed.
		rules, e := ipJSON("rule", "show")
		if e != nil {
			return e
		}
		for _, r := range rules {
			if int(number(r["priority"])) == p.priority() && fmt.Sprint(r["table"]) == f.Name() && fmt.Sprint(r["protocol"]) == "242" {
				if _, e = run(nil, "ip", "-4", "rule", "del", "priority", strconv.Itoa(p.priority()), "fwmark", fmt.Sprintf("0x%x/0xffffffff", p.mark()), "table", f.Name(), "protocol", "242"); e != nil {
					return e
				}
			}
		}
		if _, e = run(nil, "ip", "-4", "route", "flush", "table", f.Name(), "proto", "242"); e != nil {
			return e
		}
		if e = os.Remove(path); e != nil {
			return e
		}
	}
	for _, f := range files {
		if strings.HasPrefix(f.Name(), "rp_") && !devices[strings.TrimPrefix(f.Name(), "rp_")] {
			device := strings.TrimPrefix(f.Name(), "rp_")
			if !deviceRE.MatchString(device) {
				return errors.New("本地网卡记录损坏")
			}
			path := a.path("routing/" + f.Name())
			old, e := os.ReadFile(path)
			if e != nil {
				return e
			}
			sys := filepath.Join("/proc/sys/net/ipv4/conf", device, "rp_filter")
			current, e := os.ReadFile(sys)
			if e == nil && strings.TrimSpace(string(current)) == "2" {
				if e = os.WriteFile(sys, old, 0644); e != nil {
					return e
				}
			}
			if e = os.Remove(path); e != nil {
				return e
			}
		}
	}
	return nil
}
