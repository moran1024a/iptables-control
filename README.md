# iptables-control（ictl）

轻量的 IPv4 端口转发管理工具，通过 Web 面板统一管理多台服务器的 iptables 转发规则，也可以使用 `ipt.sh` 在单台服务器上独立管理。

支持多跳链路、SS／SS2022／VLESS Reality 目标、主动与被动同步，以及 Agent 与单机脚本共存。转发节点无需安装代理核心，目标服务需自行准备。

[版本下载](https://github.com/moran1024a/iptables-control/releases) · [更新记录](CHANGELOG.md) · [问题反馈](https://github.com/moran1024a/iptables-control/issues)

## 目录

- [安装](#安装)
- [升级](#升级)
- [卸载与删除](#卸载与删除)
- [使用指南](#使用指南)
- [单机脚本与共存](#单机脚本与共存)
- [常用命令](#常用命令)
- [常见问题](#常见问题)
- [开发与贡献](#开发与贡献)
- [组件版本](#组件版本)
- [许可证](#许可证)

## 安装

按使用场景选择：管理多台服务器，安装**服务端 + 节点 Agent**；只管理本机转发，安装**单机脚本**即可。以下命令下载最新 Release。

### 服务端

准备一台已安装并启动 Docker 的 Linux 服务器，使用有 Docker 权限的账号执行：

```bash
curl -fsSL https://github.com/moran1024a/iptables-control/releases/latest/download/install-server.sh | bash
```

安装完成后，打开输出的**完整面板地址**。默认账号为 `admin`，密码为 `admin_pass`，首次登录必须修改。

默认端口为 `8730`，容器名为 `ictl-server`，数据卷为 `ictl-data`。如需指定端口或使用国内镜像：

```bash
curl -fsSL https://github.com/moran1024a/iptables-control/releases/latest/download/install-server.sh | bash -s -- --port 9000 --mirror
```

公网访问建议配置 HTTPS 反向代理，转发至服务端的实际端口（默认 `8730`），并在面板“设置 → 对外地址”填写节点可访问的 HTTPS 地址，再接入节点。

### 节点 Agent

节点需要使用 systemd 的 Linux 系统，并以 root 权限安装。依赖 Bash、curl、openssl 1.1.1+、iptables（含 save／restore、addrtype、conntrack）、ss（iproute2）、getent 和 flock；缺少时请通过系统包管理器安装。

1. 在面板“节点 → 新增节点”中填写名称、入站 IPv4／域名、允许的入站端口，首次使用可保留“主动”模式。
2. 打开节点详情，复制“一键接入”命令，在**对应节点服务器**执行。
3. 回到面板确认节点状态；以后可在节点运行 `sudo ictl` 打开管理菜单。

入站地址用于客户端或上一跳节点连接此节点。允许端口可填写 `10000-20000,30000` 这样的范围和列表，并需在云安全组及系统防火墙中允许相应 TCP／UDP 流量。

### 单机脚本

不需要服务端或 Agent。在使用 systemd 的 Linux 服务器执行：

```bash
curl -fsSL https://github.com/moran1024a/iptables-control/releases/latest/download/ipt.sh -o ipt.sh.download &&
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

主动节点在开启“自动更新”时，会跟随服务端更新 Agent；被动节点需手动同步才会检查更新。也可以在节点直接执行：

```bash
sudo ictl --update
sudo ictl --sync
```

下载较慢时可使用 `sudo ictl --update --mirror`。运行 `sudo ictl`，选择菜单 5 可开关自动更新。

从旧协议版本升级的节点默认主动模式，升级并成功同步后才能切换；后续升级保留已选模式。Agent 0.2.1 起会整理 `/root/ictl.sh`、`/ictl.sh` 中可识别的旧版脚本，使这些旧入口运行新版，配置和转发规则保留。

### 单机脚本升级

重新执行[单机脚本安装命令](#单机脚本)。**如果原脚本不在 `/root/ipt.sh`，请将命令中的安装路径改为原路径**，保证开机任务和定时任务运行新版。更新后进入菜单检查转发列表。

## 卸载与删除

### 卸载服务端

删除容器和镜像，保留数据卷，之后可重新安装恢复使用：

```bash
curl -fsSL https://github.com/moran1024a/iptables-control/releases/latest/download/install-server.sh | bash -s -- --uninstall
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
| 配置同步 | 每 5–10 秒自动检查 | 仅本机手动同步 |
| 面板修改后 | 自动获取并应用 | 保持“等待中”，直到同步成功 |
| 开机后 | 恢复规则并继续轮询 | 只恢复已应用规则，不联网、不刷新域名 |
| 状态条 | 每格 20 秒，显示最近 20 分钟 | 每格 60 秒，显示最近 1 小时；白底，手动同步时标绿 |
| 长时间无上报 | 超过 120 秒显示离线 | 不因时间流逝判离线 |

在面板编辑节点的“同步模式”并保存后，需到节点执行 `sudo ictl --sync`，或运行 `sudo ictl` 选择菜单 3“同步更新”。**两个方向的模式切换都需要本机同步确认。** 主动切被动时，节点收到切换要求后停止后台同步；被动切主动时，手动同步成功后恢复轮询。

同步会先上报状态，再获取并应用配置，最后上报结果。失败时查看节点详情中的错误和事件，处理后再次同步。被动状态条的绿色只表示发生了同步，是否成功以状态和事件为准。

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

服务端不收集本机端口清单，也不会自动换端口。同一份冲突配置不会被 Agent 自动反复应用；如果已通过移除其他占用解决冲突，请运行 `sudo ictl --sync` 重新检查。仅停止另一方服务不会释放它已保存的端口，需删除或修改对应配置。

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

Docker 安装器将升级前备份保存在数据卷内的 `/data/ictl.db.pre-deploy-*.bak`，0.3.0 迁移还会在数据库旁保留 `ictl.db.pre-0.3.0.bak`。人工回退需停止服务，恢复升级前数据库及对应旧镜像，移除新数据库的 `-wal`／`-shm` 文件后再启动；回退会丢弃升级后的数据。源码部署或自定义数据库路径需自行备份和恢复。

## 开发与贡献

欢迎通过 [Issues](https://github.com/moran1024a/iptables-control/issues) 反馈问题，或提交 Pull Request。反馈时请附组件版本、操作步骤和相关错误日志，并隐去密钥、密码和分享链接中的凭据。

<details>
<summary>源码运行与开发检查</summary>

服务端需要 Python 3.10+、SQLite 3.35+ 和 openssl。在仓库根目录运行以下命令，数据库将保存在当前用户目录中：

```bash
ICTL_DB="$HOME/.local/share/ictl/ictl.db" python3 server/ictl-server.py serve --listen 127.0.0.1:8730
```

默认数据库路径为 `/var/lib/ictl-server/ictl.db`，可通过 `ICTL_DB` 修改。不要让多个服务实例共用同一数据库。

按改动范围运行相关检查：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests
python3 deploy/check-versions.py
node --check server/web/app.js
for file in agent/ictl.sh deploy/install-server.sh ipt.sh; do bash -n "$file"; done
```

测试中的 iptables、systemd 和 Docker 使用模拟命令，涉及实际系统行为的变更仍需在 Linux 环境验证。

</details>

## 组件版本

| 组件 | 当前版本 |
| --- | --- |
| 服务端 | 0.3.1 |
| 节点 Agent | 0.2.1 |
| 单机脚本 | 3.1 |

各组件版本号独立维护，具体变化见 [CHANGELOG.md](CHANGELOG.md)。

## 许可证

本项目采用 [MIT 许可证](LICENSE)。
