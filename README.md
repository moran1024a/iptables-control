# iptables-control（ictl）

轻量的 IPv4 端口转发管理工具，通过 Web 面板统一管理多台服务器的端口转发规则，也可以使用 `ipt.sh` 在单台服务器上独立管理。

支持多跳链路、SS／SS2022／VLESS Reality 目标、主动与被动同步，以及 Agent 与单机脚本共存。

**当前开发版：0.4.0-rc.1（测试版）**。主动节点改用 Go + nftables 和长连接；被动节点保留 Bash + iptables。正式版仍为 0.3.1，RC 不更新 `latest`。转发节点无需安装代理核心，目标服务需自行准备。

[版本下载](https://github.com/moran1024a/iptables-control/releases) · [更新记录](CHANGELOG.md) · [问题反馈](https://github.com/moran1024a/iptables-control/issues)

## 目录

- [安装](#安装)
- [升级](#升级)
- [切换主动与被动](#切换主动与被动)
- [卸载与删除](#卸载与删除)
- [使用指南](#使用指南)
- [单机脚本与共存](#单机脚本与共存)
- [常用命令](#常用命令)
- [常见问题](#常见问题)
- [开发与贡献](#开发与贡献)
- [组件版本](#组件版本)
- [许可证](#许可证)

## 安装

按使用场景选择：管理多台服务器，安装**服务端 + 节点 Agent**；只管理本机转发，安装**单机脚本**即可。以下命令固定使用本次 RC，需对应 Release 发布后才能下载；稳定版请使用 [0.3.1 文档](https://github.com/moran1024a/iptables-control/tree/v0.3.1)。

### 服务端

准备一台已安装并启动 Docker 的 Linux 服务器，使用有 Docker 权限的账号执行：

```bash
curl -fsSL https://github.com/moran1024a/iptables-control/releases/download/v0.4.0-rc.1/install-server.sh | bash
```

安装完成后，打开输出的**完整面板地址**。默认账号为 `admin`，密码为 `admin_pass`，首次登录必须修改。

默认端口为 `8730`，容器名为 `ictl-server`，数据卷为 `ictl-data`。如需指定端口或使用国内镜像：

```bash
curl -fsSL https://github.com/moran1024a/iptables-control/releases/download/v0.4.0-rc.1/install-server.sh | bash -s -- --port 9000 --mirror
```

新主动 Agent 必须使用有可信证书的 HTTPS。请配置支持 WebSocket 的反向代理，转发至服务端的实际端口（默认 `8730`），并在面板“设置 → 对外地址”填写节点可访问的 HTTPS 地址，再接入节点。

### 节点 Agent

节点需要使用 systemd 的 Linux，并以 root 权限安装。

- **主动**：amd64／arm64，Linux 5.10+、nftables 1.0+、iproute2、conntrack-tools，使用 HTTPS／WSS。
- **被动**：Bash、iptables（含 save／restore、addrtype、conntrack）、iproute2、getent。
- 两者都需要 curl、openssl 1.1.1+ 和 flock。安装器可在 Debian／Ubuntu 安装缺少的依赖，其他系统请先自行安装。

1. 在面板“节点 → 新增节点”中填写名称、入站 IPv4／域名、允许的入站端口，首次使用可保留“主动”模式。
2. 打开节点详情，复制“一键接入”命令，在**对应节点服务器**执行。
3. 回到面板确认节点状态；以后可在节点运行 `sudo ictl` 打开管理菜单。

入站地址用于客户端或上一跳节点连接此节点。允许端口可填写 `10000-20000,30000` 这样的范围和列表，并需在云安全组及系统防火墙中允许相应 TCP／UDP 流量。

### 单机脚本

不需要服务端或 Agent。在使用 systemd 的 Linux 服务器执行：

```bash
curl -fsSL https://github.com/moran1024a/iptables-control/releases/download/v0.4.0-rc.1/ipt.sh -o ipt.sh.download &&
sudo install -m 755 ipt.sh.download /root/ipt.sh.new &&
sudo mv -f /root/ipt.sh.new /root/ipt.sh &&
sudo bash /root/ipt.sh
```

首次运行会检查并安装缺少的 Python 3.8+、iptables 和 iproute2 软件包。后续运行 `sudo bash /root/ipt.sh` 即可进入菜单，具体操作见[单机脚本与共存](#单机脚本与共存)。

## 升级

**顺序：先升级服务端，再升级节点 Agent 和单机脚本。** 共存时，Agent 和单机脚本都需要升级到支持共存的版本，见[组件版本](#组件版本)。

### 服务端升级

重新执行[服务端安装命令](#服务端)即可。安装器沿用现有端口和数据挂载，保留升级前数据库备份；新容器启动或就绪检查失败时尝试恢复旧容器和数据库。相同版本需要重建时，将命令末尾的 `| bash` 改为 `| bash -s -- --force`。

> 从 0.2.x 或更早版本升级：数据库中的全部隧道节点、受管隧道及其关联配置会被删除。对应远端的旧隧道 Agent、sing-box 和配置文件需自行卸载。外部导入的 SS／VLESS 会转为“目标”，其余有效配置和节点信息保留。详情见 [0.3.0 更新记录](CHANGELOG.md)。

### 节点 Agent 升级

**服务端升级不会把旧 Bash Agent 自动替换为 Go。** 旧节点继续使用 Agent 0.2.1；要迁移到本次 RC，请在节点详情中重新复制“一键接入”命令执行。安装器保留节点身份和配置，并根据面板所选模式安装对应 Agent。

已完成迁移的节点可执行：

```bash
sudo ictl --update  # 只更新当前类型
sudo ictl --sync    # 同步配置，或确认面板要求的类型切换
```

新版主动 Agent 在 `AUTO_UPDATE=1` 时接受同类型更新通知；被动节点仅在本机手动操作时更新。可在 `/etc/ictl/agent.conf` 将 `AUTO_UPDATE` 设为 `0` 关闭主动自动更新，随后重启 `ictl-agent`。手动 `--update` 不受此开关限制。

迁移会替换可识别的 `/root/ictl.sh`、`/ictl.sh` 旧入口，使其指向新版 `ictl`。后端交接期间允许短暂断线；已有防火墙若默认拒绝 FORWARD，需要先明确放行自己的转发范围。

### 单机脚本升级

重新执行[单机脚本安装命令](#单机脚本)。**如果原脚本不在 `/root/ipt.sh`，请将命令中的安装路径改为原路径**，保证开机任务和定时任务运行新版。更新后进入菜单检查转发列表。

## 切换主动与被动

1. 在面板编辑节点，选择目标模式并保存。
2. 到该节点运行 `sudo ictl --sync`，或在 `sudo ictl` 菜单选择“同步更新／确认切换”。
3. 等待显示确认成功，再检查面板状态。

主动切被动时，收到通知的 Agent 会停止管理通信和采集，现有规则继续转发；若节点已经失联，停用通知需等重新连接后才能送达。被动切主动完全依靠本机操作。**两种方向都要替换 Agent 程序，不能只修改本地模式文件。**

切换工具先准备组件与配置，再备份、交接规则，最后向服务端确认。下载失败不动旧规则；交接失败尝试恢复。确认响应丢失时，重新运行 `sudo ictl --sync`；提示交接中断时，按提示运行 `sudo ictl --recover` 后重试。首次安装尚未生成 `ictl` 时，可用 `sudo bash /usr/local/lib/ictl/manage.sh --recover`。

## 卸载与删除

### 卸载服务端

删除容器和镜像，保留数据卷，之后可重新安装恢复使用：

```bash
curl -fsSL https://github.com/moran1024a/iptables-control/releases/download/v0.4.0-rc.1/install-server.sh | bash -s -- --uninstall
```

追加 `--purge` 会永久删除默认数据卷中的全部数据。卸载服务端不会清理远端节点规则；若要完整停用，请先处理节点。

### 卸载节点 Agent

1. 在面板解除该节点的链路和配置引用，再删除节点。
2. 等待节点完成清理同步；被动节点需在本机运行 `sudo ictl --sync`。
3. 清理确认后，在节点卸载 Agent：

```bash
sudo ictl --uninstall
```

命令会询问确认，并删除该 Agent 的规则、配置和服务；非交互执行可加 `-y`。旧隧道节点需使用对应旧 Release 的 `install-tunnel.sh --uninstall` 自行卸载。

### 停用单机脚本

当前没有一键卸载命令。先运行 `sudo bash /root/ipt.sh`，通过“删除转发”清空由它管理的转发，再停用开机恢复和定时刷新：

```bash
sudo systemctl disable --now ipt-forward-selfcheck.timer ipt-forward-selfcheck.service
```

完成后可删除脚本文件。配置目录 `/root/iptables-forward-conf/` 和备份目录 `/root/iptables-backup/` 可按需保留；仅删除脚本或停止服务不会清除现有转发规则。

### 删除面板对象

被引用的节点、链路和目标需先解除引用。删除配置后，要等相关节点确认清理才会释放端口；强制删除配置仍会保留未确认的端口。强制删除节点记录不会清理远端规则，需在节点本机卸载。

## 使用指南

### 创建第一条转发

| 对象 | 用途 | 示例 |
| --- | --- | --- |
| 节点 | 安装了 Agent 的转发服务器 | 入口服务器 A、中转服务器 B |
| 链路 | 指定经过哪些节点及其顺序 | A → B，或只经过 A |
| 目标 | 最终连接的服务 | `192.0.2.10:443` 或导入的 SS 节点 |
| 配置 | 将链路和目标组合成一条可用转发 | A → B → 目标 |

接入节点后，按以下步骤操作：

1. **添加目标**：进入“目标”，点击“添加地址目标”填写实际 IPv4／域名和端口；已有 SS／SS2022／VLESS Reality 分享链接时，选择“导入分享链接”。
2. **创建链路**：进入“链路 → 新增链路”，按流量经过的顺序选择节点。单跳转发也需要创建一条只含一个节点的链路。
3. **创建配置**：进入“配置 → 新增配置”，选择链路和目标。各跳端口留空时自动分配，也可在节点允许范围内手动指定。目标必须先在“目标”中保存。
4. **等待生效**：主动节点自动同步；链路里有被动节点时，需到每个被动节点运行 `sudo ictl --sync`。配置显示“已应用”后，说明相关节点已确认规则。
5. **连接使用**：普通地址目标使用配置中展示的入口地址和端口；SS／VLESS 目标可点击“连接信息 → 查看 / 复制”，将分享链接导入客户端。

选择“无链路（直连目标）”会直接使用目标地址，不经过转发节点，状态显示“已保存”。链路模板本身不占用端口，创建配置后才分配端口。

修改链路或目标会更新所有引用它的配置；更换入口节点或入口端口后，需要更新客户端的连接信息。四个板块均支持分组、排序和筛选。

### 主动与被动模式

| 行为 | 主动模式 | 被动模式 |
| --- | --- | --- |
| 配置同步 | WSS 长连接，配置变更即时推送 | 仅本机手动同步 |
| 面板修改后 | 自动获取并应用 | 保持“等待中”，直到同步成功 |
| 开机后 | 离线恢复规则，然后建立管理连接 | 只恢复已应用规则，不联网、不刷新域名 |
| 状态条 | 每格 20 秒，显示最近 20 分钟 | 每格 60 秒，显示最近 1 小时；白底，手动同步时标绿 |
| 长时间无上报 | 新主动 30 秒；旧版轮询 120 秒 | 不因时间流逝判离线 |

模式切换见[切换主动与被动](#切换主动与被动)。同步先上报，再获取并应用配置，最后上报结果。失败原因见节点详情和事件；被动状态条的绿色只表示发生了同步，是否成功以状态和事件为准。

### 主动节点的数据与诊断

节点详情可查看 CPU、内存、磁盘、网卡速率、端口快照和转发流量；每 10 秒采集主要指标，端口每 30 秒更新，基础信息每 60 秒更新。也可以请求系统、端口、网络、规则、最近日志，以及对当前转发的连接查询和目标诊断。断线期间本地累计流量保留，重连后补报；普通配置切换不会清空累计值。

流量按**本节点、每条转发、TCP／UDP、上下行**分别统计 IPv4 字节，包含协议头和重传。它不是应用下载量，也不是全机网卡流量；多跳不要直接相加。数字以整数保存，重复上报不会重复累加。意外掉电、外部清零或 flowtable 卸载可能造成缺口，可识别的缺口会在面板标明，不能当作完整账单。被动模式不新增采集，历史值保留。

本次仍使用手动链路，不包含池、自动择路或自动灾备。

## 单机脚本与共存

### 使用单机脚本

运行 `sudo bash /root/ipt.sh`，选择“添加转发”，依次填写：

| 项目 | 填写说明 |
| --- | --- |
| 入站端口 | 本机接收连接的端口，例如 `10000` |
| 目标 IPv4／域名 | 最终服务的实际地址 |
| 目标端口 | 最终服务监听的端口，例如 `443` |
| 协议 | `tcp`、`udp` 或 `both`（两者） |
| 备注 | 用于辨认规则，支持字母、数字、`.`、`_`、`:`、`-` |

确认后即可通过“本机地址:入站端口”访问目标，请同时放行相应的云安全组和系统防火墙端口。菜单还支持查看、修改、删除转发，以及导出、导入配置和恢复备份。输入 `q` 可取消当前输入。

脚本会配置开机恢复；使用域名目标时，每 5 分钟刷新一次。备份恢复仅处理单机脚本自身的转发。

### 与 Agent 共存

两者可以单独运行，也可以在同一台服务器使用，无需额外开关。双方会检查本机服务、现有转发规则和对方已保存的端口。

**同一协议的入站端口不能重复使用；Agent 同时占用 TCP 和 UDP。** 例如 Agent 使用 `10000` 后，单机脚本不能再用 `10000` 作为 TCP 或 UDP 入站端口，应另选端口。

发生冲突时：

- **Agent**：拒绝本次新配置，保留原规则，并在同步上报后让面板显示“异常”。到“配置 → 编辑”修改冲突节点对应的端口，再等待主动同步或执行手动同步。
- **单机脚本**：提示冲突并取消本次操作，返回菜单。修改端口后重新操作即可。

新版主动 Agent 会采集端口供查看，但服务端仍照常下发配置，由本机最终检查，不会自动换端口。同一份冲突配置不会被 Agent 自动反复应用；如果已通过移除其他占用解决冲突，请运行 `sudo ictl --sync` 重新检查。仅停止另一方服务不会释放它已保存的端口，需删除或修改对应配置。

## 常用命令

| 用途 | 命令 |
| --- | --- |
| 打开节点菜单 | `sudo ictl` |
| 手动同步配置 | `sudo ictl --sync` |
| 更新 Agent | `sudo ictl --update` |
| 查看 Agent 日志 | `sudo journalctl -u ictl-agent -n 100 --no-pager` |
| 打开单机脚本 | `sudo bash /root/ipt.sh` |
| 查看单机脚本日志 | `sudo tail -n 100 /var/log/ipt-forward-manager.log` |
| 查看服务端日志 | `docker logs --tail 100 ictl-server` |
| 查询面板地址 | `docker exec ictl-server python3 /app/ictl-server.py info` |
| 重置管理员账号 | `docker exec ictl-server python3 /app/ictl-server.py reset-admin` |

## 常见问题

**打不开面板，或访问端口后显示 404？**

请使用安装时输出的完整面板地址，其中包含随机生成的路径；忘记地址可用上表中的查询命令。仍无法访问时，检查容器日志、端口放行和反向代理配置。

**配置一直“等待中”？**

查看配置中尚未确认的节点。被动节点和切换模式后的节点需要本机手动同步；主动节点则检查服务、网络和节点详情中的错误。链路上的节点全部确认后才会显示“已应用”。

**节点显示“端口冲突”或“端口检查失败”？**

查看节点详情中的具体原因。冲突按[共存说明](#与-agent-共存)修改端口；提示缺少 `ss` 时，安装系统的 iproute2 软件包（部分发行版名为 iproute）。排除错误后运行 `sudo ictl --sync`，重启或普通心跳不会清除冲突异常。

**节点认证失败或提示时间不一致？**

确认节点使用当前详情页中的同步地址和密钥，并检查服务端与节点的系统时间，时差需在 60 秒内。修改节点入站地址会重新生成密钥，需要更新本机接入信息。

**显示“已应用”但无法连接？**

“已应用”表示转发规则已确认，不代表已测试目标连通性。请检查入口端口、各跳网络和目标服务是否可达，以及云安全组和防火墙是否放行。当前仅支持 IPv4；域名需要能解析到可用的 IPv4 地址。

**升级后需要回退怎么办？**

Docker 安装器将升级前备份保存在数据卷内的 `/data/ictl.db.pre-deploy-*.bak`，0.4.0 迁移还会在数据库旁保留 `ictl.db.pre-0.4.0.bak`。人工回退需停止服务，恢复升级前数据库及对应旧镜像，移除新数据库的 `-wal`／`-shm` 文件后再启动；回退会丢弃升级后的数据。源码部署或自定义数据库路径需自行备份和恢复。

## 开发与贡献

欢迎通过 [Issues](https://github.com/moran1024a/iptables-control/issues) 反馈问题，或提交 Pull Request。反馈时请附组件版本、操作步骤和相关错误日志，并隐去密钥、密码和分享链接中的凭据。

<details>
<summary>源码运行与开发检查</summary>

服务端需要 Python 3.10+、SQLite 3.35+、openssl 和 `server/requirements.txt` 中的依赖。主动二进制使用 `agent/active/go.mod` 指定的 Go 版本构建。在仓库根目录运行以下命令，数据库将保存在当前用户目录中：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r server/requirements.txt
mkdir -p dist
(cd agent/active && CGO_ENABLED=0 go build -o ../../dist/ictl-active-linux-amd64 .)
ICTL_DB="$HOME/.local/share/ictl/ictl.db" python3 server/ictl-server.py serve --listen 127.0.0.1:8730
```

默认数据库路径为 `/var/lib/ictl-server/ictl.db`，可通过 `ICTL_DB` 修改。不要让多个服务实例共用同一数据库。

按改动范围运行相关检查：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests
python3 deploy/check-versions.py
node --check server/web/app.js
(cd agent/active && go test ./...)
for file in agent/*.sh deploy/*.sh ipt.sh; do bash -n "$file"; done
```

常规测试中的 iptables、systemd 和 Docker 使用模拟命令。Linux 上可额外运行隔离网络测试（需要 root、iproute2、nftables、conntrack）：

```bash
(cd agent/active && go build -o /tmp/ictl-active .)
sudo unshare -nm env ICTL_KERNEL_TEST=1 python3 tests/kernel_network.py /tmp/ictl-active
```

该测试在新的网络和挂载命名空间内执行，检查真实转发、计数、冲突与清理。

</details>

## 组件版本

| 组件 | 当前版本 |
| --- | --- |
| 服务端／主动 Agent／被动 Agent／管理器 | 0.4.0-rc.1 |
| 旧版兼容 Agent | 0.2.1 |
| 单机脚本 | 3.2-rc.1 |

各组件版本号独立维护，具体变化见 [CHANGELOG.md](CHANGELOG.md)。

## 许可证

本项目采用 [MIT 许可证](LICENSE)。
