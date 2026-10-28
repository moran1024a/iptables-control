# iptables-control (ictl)

个人使用的轻量节点、链路与隧道管理面板。当前服务端版本 **0.2.0**。

本版变化见[更新记录](CHANGELOG.md)，旧版用户请先阅读[升级说明](#从-01x-升级)。

- **服务端** `server/`：Python 标准库 + SQLite + openssl，原生 JavaScript 面板。
- **转发 Agent** `agent/ictl.sh`：保持 **0.1.8**，原有配置和通信方式不变。
- **隧道 Agent** `tunnel-agent/`：独立 Go 程序 **0.1.0**，管理专属 **sing-box 1.14.4**。

两种 Agent 都由节点发起管理连接。转发 Agent 每 20–40 秒进行一次加密短请求；隧道 Agent 使用 HTTPS SSE 通知和默认 10 秒的独立心跳。隧道只记录 Agent 在线，不做核心、端口或延迟探测。`ipt.sh` 仍保留为旧单机脚本。

> 已进行隔离回归和本机协议测试，尚需真实 Linux 节点的长期运行验证。

## 0.2.0 的四个板块

| 板块 | 用途 |
|---|---|
| 节点 | 转发节点、隧道节点两个子页面，各自安装对应 Agent |
| 链路 | 只保存有序转发节点，例如 A → B → C；不分配端口、不下发规则 |
| 隧道 | 在隧道节点上创建 Shadowsocks、VLESS Reality 或 SOCKS5 监听 |
| 配置 | 支持直连隧道、链路＋隧道、链路＋IP/域名:端口；展示最终连接信息 |

直连隧道无需创建链路，直接使用隧道节点地址和已有监听端口，不分配转发端口。使用链路的配置独立占用各跳转发端口。多个配置引用同一条链路时，修改链路会**自动更新全部引用配置**；端口不足时整个保存回滚。未变化节点尽量保留端口和规则 ID，移除的端口在对应节点确认清理后才允许复用。入口节点变化会改变连接地址，跨节点更新可能短暂中断。

隧道监听端口属于隧道本身。多个配置引用同一个隧道时，共享监听与凭据；删除配置不会删除共享隧道。被引用的节点、链路或隧道不能直接删除。隧道先应用成功，引用它的新配置才开始下发转发规则。“已应用”表示 Agent 执行成功，不代表已经检测连接可用。

转发节点和隧道节点使用独立数据表。转发节点、隧道节点、链路、隧道、配置分别维护自己的分组、分组顺序、组内顺序和筛选/折叠状态，互不影响。分组可改名、删除；删除非默认分组会将该类记录移入本类默认分组。列表中的拖动排序只改变显示顺序，不改变链路的实际跳序或下发配置。

系统设置通过右上角齿轮进入，与连接“配置”分开。

## 从 0.1.x 升级

沿用下面的服务端升级命令即可，转发 Agent 不需要重新安装。

启动时会在数据库旁生成 `ictl.db.pre-0.2.0.bak` 一致性备份，再事务迁移。每条旧转发生成一条链路和一个配置，不自动合并相同链路。保留节点密钥、管理路径、端口、规则 ID、排序、目标及待确认状态；程序会比较迁移前后每个节点的规则文本，不一致时中止迁移。

从首版 0.2.0 开发数据库升级时，会额外生成 `ictl.db.pre-layout-201.bak` 备份。原共用节点分组复制为两套独立分组，保留名称、成员归属和排序，节点 ID、密钥、历史记录及现有规则保持。

回退必须停止服务，恢复旧镜像**以及升级前数据库备份**，并移除新库对应的 `-wal` / `-shm` 文件；仅切换旧镜像不受支持。回退后不会保留升级后新增的数据。

## 服务端

### 一键部署（Docker）

需要已安装并启动 Docker。在服务端执行：

```bash
curl -fsSL https://github.com/moran1024a/iptables-control/releases/latest/download/install-server.sh | bash
```

对外端口默认 8730，可用 `| bash -s -- --port 9000` 修改。国内服务器见[国内加速](#国内加速)。

脚本会拉取 `ghcr.io/moran1024a/ictl-server` 镜像，创建容器 `ictl-server`，数据保存在 Docker 卷 `ictl-data` 中。首次启动会随机生成**面板路径**和**同步路径**并打印出来：

```
ictl 服务端 0.2.0 已启动
  面板：    http://<服务器IP>:8730/<面板路径>/
  同步路径：/<同步路径>
  默认账号：admin / admin_pass（首次登录后须修改）
```

面板和同步接口共用一个端口，其他任何路径都返回 404。用浏览器打开面板地址，用默认账号登录后必须同时修改用户名和密码。

### 更新

在服务端重新执行一键部署命令即可：

```bash
curl -fsSL https://github.com/moran1024a/iptables-control/releases/latest/download/install-server.sh | bash
```

已是最新版本时直接退出；有新版本时拉取新镜像并重建容器。数据卷、端口和绑定地址沿用现有容器，账号、节点、转发等配置都保留。加 `--port <端口>` 可在更新时修改端口，加 `--force` 可在版本相同时强制重建容器。

### 卸载

```bash
curl -fsSL https://github.com/moran1024a/iptables-control/releases/latest/download/install-server.sh | bash -s -- --uninstall
```

删除容器和镜像，**保留数据卷** `ictl-data`，之后重新部署可继续使用原有数据。追加 `--purge`（即 `--uninstall --purge`）会同时删除数据卷，账号、节点、转发等所有数据都会清除。

服务端卸载不会影响节点：节点上的转发规则继续生效，只是不再同步。

### 常用命令

```bash
docker logs ictl-server                                              # 查看日志
docker exec ictl-server python3 /app/ictl-server.py info             # 再次查看面板地址
docker exec ictl-server python3 /app/ictl-server.py reset-admin      # 忘记密码时重置为默认账号
```

### 不使用 Docker

需要 Python 3.8+（自带 SQLite 3.25+）和 openssl 命令行：

```bash
mkdir -p /opt/ictl/agent /opt/ictl/assets
cp -r server/ictl-server.py server/tunnel_config.py server/components.json server/web /opt/ictl/
cp agent/ictl.sh /opt/ictl/agent/
# 将 Release 的 tunnel-agent-linux-amd64 和 tunnel-agent-linux-arm64 放入 /opt/ictl/assets/
python3 /opt/ictl/ictl-server.py serve [--listen 0.0.0.0:8730]
```

命令同上（`info`、`reset-admin`）。数据库默认 `/var/lib/ictl-server/ictl.db`，可用环境变量 `ICTL_DB` 覆盖。数据库保存节点密钥原文，权限为 600，请妥善保管。

### HTTPS

转发 Agent 的同步载荷自带加密，但**面板登录在 HTTP 下是明文**，隧道 Agent 的管理连接也**必须使用 HTTPS**。请用反向代理提供 HTTPS，然后在面板「设置 → 对外地址」中填写 `https://域名`。修改后重新复制节点详情中的接入命令，在节点执行以更新管理连接地址；已安装 Agent 的地址不会自动改写。Caddy 示例：

```
panel.example.com {
    reverse_proxy 127.0.0.1:8730
}
```

服务端会自动识别反向代理：直接连接来自本机或内网地址（包括 Docker 网桥）时，从 `X-Forwarded-For` 获取真实来源 IP；公网客户端直连时忽略该请求头，无法伪造。配置好后可以用防火墙关闭 8730 端口的公网访问。

### 面板与状态

转发节点保留原有管理方式、分组、排序和最近 1 小时状态条（每格 60 秒）。原“转发”操作位于“配置”中，可以选择链路模板后设置目标和各跳端口，或选择“无链路（直连隧道）”。转发端口留空自动分配。配置仍支持分组、筛选、排序及删除确认。

隧道节点状态条显示最近 10 分钟（每格 10 秒），根据心跳有效期判断在线，默认连续 30 秒未收到心跳后离线。全局心跳可在设置中调整为 5–3600 秒，不支持逐节点设置。配置应用失败单独显示，不会把仍在心跳的 Agent 误标为离线。

配置删除后等待相关节点确认再释放端口；强制删除配置仍会为连接过的节点保留未确认清理的端口。长时间无法恢复的机器可在解除所有引用后强制删除节点，但该机器上的规则不会被主动清理。

状态与事件保留 7 天。系统设置包括账号密码、主题、对外地址、全局心跳和各组件版本。

| 转发节点状态 | 含义 |
|---|---|
| 未连接 | 从未收到请求 |
| 离线 | 超过 120 秒没有请求 |
| 时间不一致 | 时差超过 60 秒，请求不生效 |
| 同步中 | 已上报规则版本与目标不一致 |
| 已同步 | 版本一致，规则应用成功 |
| 失败 | 应用存在错误 |
| 删除中 | 等待空配置应用确认 |

## 转发节点端

依赖：bash、curl、openssl（1.1.1 及以上）、iptables（需 `addrtype`、`conntrack` 匹配）、getent、flock、systemd。节点时间需与服务端相差不超过 60 秒（启用 NTP 即可）。

### 一键接入

在面板的节点详情页复制「一键接入」命令，在节点上执行即可完成安装和接入：

```bash
curl -fsSL https://github.com/moran1024a/iptables-control/releases/download/v<版本>/ictl.sh -o ictl.sh && sudo bash ictl.sh --url '<同步地址>' --key '<密钥>'
```

会安装 `/usr/local/bin/ictl` 和常驻服务 `ictl-agent`（开机自启）。

### 交互菜单

也可以下载后直接运行，手动填写同步地址和密钥：

```bash
curl -fsSL https://github.com/moran1024a/iptables-control/releases/latest/download/ictl.sh -o ictl.sh && sudo bash ictl.sh
```

安装后用 `sudo ictl` 进入维护菜单：

```
1) 设置服务端地址和密钥
2) 查看状态
3) 立即同步
4) 更新节点端
5) 自动更新：已开启（切换）
6) 卸载
```

### 更新

转发 Agent 默认自动更新（0.1.7 起）。服务端按 `server/components.json` 中独立的 `forward_agent` 版本决定更新信号，不再与服务端版本直接比较。节点通过原加密同步通道下载匹配脚本，校验哈希、版本和语法后替换，守护进程原地切换。

- 只升级不降级：节点版本等于或高于目标转发 Agent 版本时不会收到更新信号。
- 更新只替换脚本，不重建 iptables 规则，转发不中断。
- 下载走同步通道，内容加密并经 HMAC 认证，节点不需要访问 GitHub。
- 下载或校验失败时，10 分钟后再试。
- 可在菜单中选 5 关闭，或把 `/etc/ictl/agent.conf` 中的 `AUTO_UPDATE` 改为 `0`。

服务端升级至 0.2.0 后，转发 Agent 仍为 0.1.8，不会因为服务端版本较高而反复更新。节点详情显示实际版本和目标版本，版本变化仍记入事件记录。

0.1.6 的节点会通过自身的 6 小时定时检查从 GitHub 更新到新版本，之后改为跟随服务端；0.1.5 及更早的节点需要手动更新一次。

也可以随时手动从 GitHub 更新到最新版本：

```bash
sudo ictl --update            # 国内服务器可加 --mirror 经加速代理下载
```

有新版本时替换 `/usr/local/bin/ictl` 并重启服务，已是最新版本时不做改动。服务端地址、密钥和本地缓存的规则都保留。也可以在菜单中选 4。

### 卸载

```bash
sudo ictl --uninstall        # 需要确认；加 -y 跳过确认
```

停止并删除 `ictl-agent` 服务，删除 ictl 的全部 iptables 规则（专用链和跳转），删除配置、缓存和 `/usr/local/bin/ictl`。也可以在菜单中选 6。

建议先在面板中删除该节点：服务端会下发空配置，节点确认清空规则后自动删除记录；再在节点上执行卸载。如果先卸载了节点，它在面板中会显示为离线，此时在面板中选择强制删除即可。

### 工作方式

- 每 20-40 秒（随机）向服务端发一次请求，同步和上报合并在一个请求里；`sudo ictl` 菜单中的「立即同步」可随时手动同步。
- 规则放在专用链 `ICTL-PRE`、`ICTL-POST`（nat）、`ICTL-FWD`（filter）中，系统链里各只有一条跳转。每次整体重建专用链，不影响其他规则；跳转被其他工具冲掉时自动补回。
- DNAT 只匹配发给本机的流量（`addrtype LOCAL`）；MASQUERADE 和 FORWARD 放行只作用于经过 ictl DNAT 的连接。
- 开机时先按本地缓存恢复规则，再连服务端；服务端不可用时继续使用缓存的规则。
- 目标为域名时由节点解析，解析失败沿用上次成功的 IP。

| 路径 | 用途 |
|---|---|
| `/etc/ictl/agent.conf` | 同步地址、密钥、自动更新设置（权限 600） |
| `/var/lib/ictl/` | 配置缓存、已应用规则、域名缓存、状态 |
| `/etc/systemd/system/ictl-agent.service` | 常驻服务 |
| `/etc/sysctl.d/99-ictl.conf` | `net.ipv4.ip_forward=1` |

## 隧道节点端

在“节点 → 隧道节点”新增节点，复制详情页的一键接入命令执行。仅支持 Linux amd64 / arm64 和 systemd；安装脚本需要 bash、curl、Python 3、sha256sum、flock。**必须使用 HTTPS**，在系统设置填写 HTTPS 对外地址，反向代理传入 `X-Forwarded-Proto: https`。SSE 路径需要关闭代理缓冲，允许至少 30 秒的空闲时间；Nginx 可使用 `proxy_buffering off; proxy_read_timeout 60s;`。

安装包从服务端经 HTTPS 下载，按组件清单校验 SHA-256；sing-box 从官方固定版本下载，同样校验 SHA-256。专属服务不接管机器上已有的 sing-box，也不会修改转发 Agent 或防火墙。请自行在安全组和防火墙中放行隧道监听端口。

| 路径/服务 | 用途 |
|---|---|
| `/etc/ictl-tunnel/agent.json` | HTTPS 地址与认证密钥，权限 600 |
| `/usr/local/bin/ictl-tunnel-agent` | 独立 Agent |
| `/usr/local/lib/ictl-tunnel/sing-box` | 专属核心 |
| `/var/lib/ictl-tunnel/` | 配置、已应用版本与互斥锁 |
| `ictl-tunnel-agent.service` | Agent 服务 |
| `ictl-tunnel-core.service` | 专属核心服务 |

创建隧道后，Agent 获取全量配置，先执行 `sing-box check`，再替换并重启专属核心，成功后才确认版本。应用失败恢复上一份配置；该节点的多个监听共用核心，重启可能短暂影响其他隧道。心跳与配置应用分开执行。

协议首版限制：

- Shadowsocks：固定 `2022-blake3-aes-128-gcm`，自动生成密码，支持 TCP/UDP；客户端需支持 Shadowsocks 2022。
- VLESS Reality：固定 TCP + Vision，填写 TLS 1.3 握手站点及 SNI，自动生成 UUID、密钥和 short ID。
- SOCKS5：强制用户名密码；经过链路的组合仅保证 TCP，不保证 UDP ASSOCIATE。

在“配置”中查看最终连接链接，链路组合使用入口地址，直连使用隧道节点地址，两者均使用隧道凭据；Reality 的 SNI、公钥、short ID 保持不变。SOCKS5 链接能否直接导入取决于客户端。

```bash
sudo ictl-tunnel-agent version
sudo ictl-tunnel-agent update        # 分别判断 Agent 和兼容核心的更新，失败回退
journalctl -u ictl-tunnel-agent -f
```

先在面板解除配置引用、删除隧道并等待确认，再删除节点。最后在节点执行下载的 `install-tunnel.sh --uninstall` 卸载，追加 `-y` 可跳过本地确认。卸载只清理上述专属文件与服务。

核心和协议字段参考 [sing-box 官方文档](https://sing-box.sagernet.org/configuration/inbound/)，首版固定 [1.14.4](https://github.com/SagerNet/sing-box/releases/tag/v1.14.4)。

## 国内加速

服务器访问 GitHub 或 ghcr.io 较慢时，可以使用加速源，用法与原版相同：

- 脚本和节点端通过 [gh-proxy.com](https://gh-proxy.com/) 下载：在 GitHub 地址前加上 `https://gh-proxy.com/`；
- 服务端镜像从南京大学镜像站 `ghcr.nju.edu.cn` 拉取：部署时加 `--mirror`。

**服务端部署 / 更新**

```bash
curl -fsSL "https://gh-proxy.com/https://github.com/moran1024a/iptables-control/releases/latest/download/install-server.sh?t=$(date +%s)" | bash -s -- --mirror
```

地址末尾的 `?t=...` 用于避免代理返回缓存的旧脚本。使用 `--mirror` 部署后，之后更新时不加参数也会自动沿用镜像站。其他参数（`--port`、`--force`、`--uninstall`、`--purge`）用法不变。

**节点一键接入**：复制面板中的一键接入命令，把其中的 `https://github.com/` 改为 `https://gh-proxy.com/https://github.com/`：

```bash
curl -fsSL https://gh-proxy.com/https://github.com/moran1024a/iptables-control/releases/download/v<版本>/ictl.sh -o ictl.sh && sudo bash ictl.sh --url '<同步地址>' --key '<密钥>'
```

**节点手动更新**

```bash
sudo ictl --update --mirror
```

节点的自动更新从服务端下载，不需要加速。

加速源均为第三方公益服务，可用性不由本项目保证；gh-proxy.com 不可用时，也可以换成其他 GitHub 代理（如 `https://ghfast.top/`）。节点与服务端之间的同步通信不经过这些服务。

## 同步协议

```
POST <同步地址>
Content-Type: text/plain

<base64 密文>
```

请求明文（每行 `key=value`）：

```
t=<节点 Unix 时间>
v=<已处理的版本>
state=<init|ok|error>
err=<单行错误说明>
agent=<节点端版本>
au=<1|0，是否开启自动更新>
```

响应明文：版本一致返回 `same`；否则返回完整配置：

```
v=<版本>
<规则id> <协议> <入站端口> <目标地址> <目标端口>
```

转发 Agent 版本（0.1.7 及以上）低于组件清单中的目标版本且开启了自动更新时，响应末尾附加一行 `update=<目标版本> <脚本 sha256>`。节点在请求中加一行 `get=agent` 时，服务端返回同版本节点端脚本的内容。

节点时间与服务端相差超过 60 秒时，服务端只记录时差并标记"时间不一致"，返回 `time=<服务端时间>`，不处理本次请求。版本为节点规则文本的 sha256 前 12 位。

加密：AES-256-CTR + HMAC-SHA256。

- 节点密钥为 32 字节，以 64 位十六进制文本表示。
- 子密钥：`EK = hex(HMAC-SHA256(密钥, "ictl-enc"))`，`MK = HMAC-SHA256(密钥, "ictl-mac")`。
- 加密：`openssl enc -aes-256-ctr -pbkdf2 -iter 1 -md sha256`，以 `EK` 的十六进制文本作为口令。每条消息随机生成 8 字节 salt，派生独立的密钥和 IV；传输时去掉 openssl 输出开头固定的 `Salted__`。
- 校验：HMAC-SHA256，密钥为 `MK`。

```
请求：salt(8) | 密文 | HMAC(salt | 密文)
响应：salt(8) | 密文 | HMAC(请求的 HMAC | salt | 密文)
```

请求中不含节点标识，服务端逐个用节点密钥校验 HMAC 来识别节点。响应绑定本次请求，无法被重放。HMAC 校验失败、路径错误或格式错误一律返回 404。

## 开发与发布

组件版本由 `server/components.json` 管理。服务端、转发 Agent、隧道 Agent、sing-box 分别独立升级。旧的 `ictl.sh` Release 下载入口继续保留。

```bash
python3 deploy/check-versions.py
python3 -m unittest discover -s tests
(cd tunnel-agent && go test -race ./...)
node --check server/web/app.js
```

Go 程序仅使用标准库；本地编译可以使用 Go 1.23+，发布固定 Go 1.27.3。若从源码运行服务端并需要安装隧道节点：

```bash
mkdir -p server/assets
(cd tunnel-agent && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -buildvcs=false -o ../server/assets/tunnel-agent-linux-amd64 .)
(cd tunnel-agent && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -buildvcs=false -o ../server/assets/tunnel-agent-linux-arm64 .)
```

推送服务端 `v*` 标签后，现有单个发布工作流检查组件版本、运行回归测试、构建 amd64/arm64 镜像并发布 Release。镜像包含两种架构的隧道 Agent，以供节点安装和更新；Release 附带两个 Agent、两个安装脚本、组件清单和 `SHA256SUMS`。没有增加其他 CI 服务。

标签版本对应服务端版本，Agent 的实际版本以组件清单为准。等待发布工作流成功、Release 安装包和对应版本镜像均可用后，再运行升级命令。

## 注意事项

- 节点本机端口冲突（如把 SSH 端口设成转发）由使用者自己负责。
- 云服务器需在安全组放行入站端口。
- DNAT + MASQUERADE 方式下，目标看到的来源 IP 是最后一跳节点的 IP。

## 致谢

参考了 [exing567/iptables-control](https://github.com/exing567/iptables-control) 的实现。
