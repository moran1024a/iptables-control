# iptables-control (ictl)

轻量的中心化 iptables 端口转发管理。

- **服务端** `server/`：Web 管理面板 + 节点同步接口，同一个端口。Python 3 标准库 + SQLite + openssl 命令行，无第三方依赖。
- **节点端** `agent/ictl.sh`：从服务端拉取规则，应用到 iptables，上报结果。bash + curl + openssl。

只由节点发起请求，服务端从不主动连接节点。请求和响应的内容都用节点密钥加密，HTTP 和 HTTPS 均可使用。`ipt.sh` 是旧的单机版脚本，保留备用，不要和 ictl 在同一台机器上同时使用。

> 当前为早期版本，尚未经过真机长期运行验证。

## 概念

- **节点**：名称 + 备注（选填，节点的说明）+ 入站地址（IPv4 或域名，手动填写，表示其他机器从这个地址连到该节点）+ 允许的入站端口（默认 10000-65535，可用逗号分隔多个端口或范围，如 `10000-20000,30000`）。每个节点一个密钥，修改入站地址会重新生成密钥。
- **转发**：由一个或多个节点依次组成的链路。单跳转发就是只经过一个节点。

```
单跳：A:10001 → 1.2.3.4:443
两跳：A:10002 → B:20002 → 8.8.8.8:53
```

转发的每一跳会展开为对应节点上的一条规则：非最后一跳指向下一跳节点的 `入站地址:入站端口`，最后一跳指向转发的目标。转发协议目前固定为 TCP+UDP。

## 服务端

### 一键部署（Docker）

需要已安装并启动 Docker。在服务端执行：

```bash
curl -fsSL https://github.com/moran1024a/iptables-control/releases/latest/download/install-server.sh | bash
```

对外端口默认 8730，可用 `| bash -s -- --port 9000` 修改。国内服务器见[国内加速](#国内加速)。

脚本会拉取 `ghcr.io/moran1024a/ictl-server` 镜像，创建容器 `ictl-server`，数据保存在 Docker 卷 `ictl-data` 中。首次启动会随机生成**面板路径**和**同步路径**并打印出来：

```
ictl 服务端 0.1.0 已启动
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
mkdir -p /opt/ictl/agent && cp -r server/ictl-server.py server/web /opt/ictl/ && cp agent/ictl.sh /opt/ictl/agent/
python3 /opt/ictl/ictl-server.py serve [--listen 0.0.0.0:8730]
```

命令同上（`info`、`reset-admin`）。数据库默认 `/var/lib/ictl-server/ictl.db`，可用环境变量 `ICTL_DB` 覆盖。数据库保存节点密钥原文，权限为 600，请妥善保管。

### HTTPS

同步载荷自带加密，走 HTTP 也不会泄露规则和密钥；但**面板登录在 HTTP 下是明文**，面板会在顶部给出提示。建议用反向代理提供 HTTPS，然后在面板「设置 → 对外地址」中填写 `https://域名`，节点的同步地址会随之更新。Caddy 示例：

```
panel.example.com {
    reverse_proxy 127.0.0.1:8730
}
```

服务端会自动识别反向代理：直接连接来自本机或内网地址（包括 Docker 网桥）时，从 `X-Forwarded-For` 获取真实来源 IP；公网客户端直连时忽略该请求头，无法伪造。配置好后可以用防火墙关闭 8730 端口的公网访问。

### 面板

- **节点**：每个节点显示名称、入站地址和备注（过长时省略，详情页显示完整内容）、状态、最后上报时间，以及最近 1 小时的状态条（每格 60 秒：绿色正常、黄色故障、红色失联、灰色无数据）。点开查看详情：同步地址和密钥、转发规则、更多信息和事件记录（默认折叠）。可编辑、重置密钥、删除；修改入站地址会自动重新生成密钥。每个节点可设置允许的入站端口（端口或范围，逗号分隔），转发的入站端口必须在其中；修改时若已有转发使用了范围外的端口则拒绝修改。
- **转发**：每条转发显示别名、入口地址（第一跳节点的 `入站地址:入站端口`，可一键复制）和经过的节点（带状态）。新增和编辑时依次选择节点并填写入站端口（下拉框中显示各节点允许的端口），最后填写目标地址和端口。任何一跳（包括入口）的端口留空时，会在该节点允许的端口中随机分配一个未使用的端口，保存后提示分配结果。
  - 新增、修改后显示「下发中…」，并列出尚未确认的节点（含离线等状态）；相关节点全部拉取并应用新配置后变为「已生效」。修改时被移出的节点也需要确认，确保旧规则已清除。
  - 删除后显示「删除中…」，相关节点全部确认移除规则后才从列表中消失；期间端口保持占用，不能修改。节点长期离线时可「强制删除」，该节点恢复连接后会在同步时自动清除对应规则。
- **分组**：节点和转发各自有独立的分组，「新建分组」后可在新增、编辑时选择分组，不选则放入默认分组。默认分组可以改名、不能删除；删除其他分组时，组内的节点或转发移到默认分组。分组标题可点击折叠，折叠状态保存在浏览器中；标题旁显示数量以及离线、异常（或下发中、删除中）的数量。只有默认分组时不显示分组标题。
- **搜索与筛选**：节点可按名称搜索，按状态、分组筛选；转发可按别名搜索，按入口节点、经过的节点、目标地址、状态、分组筛选。搜索不区分大小写，名称中包含完整关键词才显示。搜索或筛选时隐藏没有匹配项的分组，有匹配项的分组临时展开。条件不保存，刷新页面后清空。
- **排序**：节点和转发列表右上角的「排序」进入排序模式，拖动分组标题左侧的手柄调整分组顺序，拖动卡片左侧的手柄调整组内顺序，点「完成」保存。更换分组需在编辑中选择。搜索或筛选时不能排序。转发表单中的节点下拉框先按分组、再按组内顺序排列。
- **设置**：修改账号密码、深色模式、对外地址。

| 状态 | 含义 |
|---|---|
| 未连接 | 从未收到该节点的请求 |
| 离线 | 超过 120 秒没有请求 |
| 时间不一致 | 节点时间与服务端相差超过 60 秒，服务端不处理其请求 |
| 同步中 | 节点上报的版本与应有版本不一致 |
| 已同步 | 版本一致，规则全部应用成功 |
| 失败 | 版本一致，但应用有问题（如域名解析失败） |
| 删除中 | 已下发空配置，等待节点确认后自动删除 |

状态条和事件记录保留 7 天。

## 节点端

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

节点端默认**跟随服务端自动更新**（0.1.7 起）：节点每次同步都会上报自己的版本，低于服务端版本时，服务端在响应中附带更新信号（目标版本和脚本的 sha256），节点随即通过同步通道从服务端下载同版本的节点端脚本，校验哈希、版本号和语法后替换 `/usr/local/bin/ictl`，守护进程原地切换到新版本。

- 只升级不降级：节点版本等于或高于服务端时不会收到更新信号。
- 更新只替换脚本，不重建 iptables 规则，转发不中断。
- 下载走同步通道，内容加密并经 HMAC 认证，节点不需要访问 GitHub。
- 下载或校验失败时，10 分钟后再试。
- 可在菜单中选 5 关闭，或把 `/etc/ictl/agent.conf` 中的 `AUTO_UPDATE` 改为 `0`。

所以服务端更新后，节点会在一个同步周期内自动更新到相同版本。面板的节点详情会显示节点端版本和更新情况（等待更新、已关闭自动更新、高于服务端等），版本变化记入事件记录。

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

节点端版本（0.1.7 及以上）低于服务端且开启了自动更新时，响应末尾附加一行 `update=<目标版本> <脚本 sha256>`。节点在请求中加一行 `get=agent` 时，服务端返回同版本节点端脚本的内容。

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

## 发布

推送 `v*` 标签（如 `v0.1.0`）后，GitHub Actions 会：

1. 检查 `server/ictl-server.py`、`agent/ictl.sh`、`deploy/install-server.sh` 中的版本号与标签一致；
2. 构建 `linux/amd64` 和 `linux/arm64` 镜像，推送到 `ghcr.io/moran1024a/ictl-server:<版本>` 和 `:latest`；
3. 创建 GitHub Release，附带 `ictl.sh` 和 `install-server.sh`。

## 注意事项

- 节点本机端口冲突（如把 SSH 端口设成转发）由使用者自己负责。
- 云服务器需在安全组放行入站端口。
- DNAT + MASQUERADE 方式下，目标看到的来源 IP 是最后一跳节点的 IP。

## 致谢

参考了 [exing567/iptables-control](https://github.com/exing567/iptables-control) 的实现。
