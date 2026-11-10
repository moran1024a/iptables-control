# iptables-control（ictl）

轻量的 IPv4 端口转发管理工具。通过 Web 面板管理多台服务器，也可使用 `ipt.sh` 独立管理单机转发。

**当前版本：0.6.0**。支持可视化拓扑、多入口与多目标、主副 IP、权重灾备、订阅和 7 天网络质量图。主动节点使用 Go + nftables，被动节点使用 Bash + iptables。

[下载版本](https://github.com/moran1024a/iptables-control/releases) · [更新记录](CHANGELOG.md) · [问题反馈](https://github.com/moran1024a/iptables-control/issues)

## 目录

- [安装](#安装)
- [升级](#升级)
- [卸载](#卸载)
- [使用指南](#使用指南)
- [主动与被动模式](#主动与被动模式)
- [常用命令与排查](#常用命令与排查)
- [开发与许可证](#开发与许可证)

## 安装

管理多台服务器，安装**服务端 + 节点 Agent**；只管理本机，安装**单机脚本**即可。

### 服务端

在已安装并启动 Docker 的 Linux 服务器上执行：

```bash
curl -fsSL https://github.com/moran1024a/iptables-control/releases/download/v0.6.0/install-server.sh | bash
```

打开安装结果中的**完整面板地址**，使用 `admin` / `admin_pass` 登录并按提示修改账号密码。

默认端口 `8730`，容器名 `ictl-server`，数据卷 `ictl-data`。自定义端口可将命令末尾改为 `| bash -s -- --port 9000`；使用镜像加速可追加 `--mirror`。

使用主动 Agent 前，需配置可信证书的 HTTPS 反向代理，支持 WebSocket，并转发 `/sub/` 订阅路径。在面板“设置 → 对外地址”填写节点可访问的 HTTPS 地址。

### 节点 Agent

节点需使用 Linux + systemd，以 root 权限安装。主动 Agent 支持 amd64／arm64，要求 Linux 5.10+、nftables 1.0+。Debian／Ubuntu 可由安装器补齐依赖，其他系统需自行准备。

1. 在面板“节点”新增节点，填写名称、主 IPv4／域名和允许使用的端口范围，选择主动或被动模式。
2. 打开节点详情，复制“一键接入”命令，在对应节点服务器执行。
3. 回到面板确认接入成功。以后运行 `sudo ictl` 即可打开节点管理菜单。

请在云安全组和系统防火墙中放行转发使用的 TCP／UDP 端口。

### 单机脚本

不需要服务端或 Agent，在使用 systemd 的 Linux 服务器执行：

```bash
curl -fsSL https://github.com/moran1024a/iptables-control/releases/download/v0.6.0/ipt.sh -o ipt.sh.download &&
sudo install -m 755 ipt.sh.download /root/ipt.sh.new &&
sudo mv -f /root/ipt.sh.new /root/ipt.sh &&
sudo bash /root/ipt.sh
```

进入菜单后选择“添加转发”，填写入站端口、目标地址、目标端口和协议。脚本支持开机恢复、域名刷新及配置备份。

## 升级

**先升级服务端，再升级节点。** 服务端重新执行安装命令即可；安装器保留升级前数据库备份，启动失败时尝试回退。相同版本需要重建时追加 `--force`。自定义 Docker Compose 部署请沿用原配置更换镜像版本。

主动节点开启自动更新后会升级到 **0.6.0**。被动节点，或关闭自动更新的主动节点，需在本机执行：

```bash
sudo ictl --update
sudo ictl --sync
```

单机脚本重新执行其安装命令即可；自定义安装路径的用户请覆盖原路径。

**升级数据说明：**

- **0.6.0-rc.7 → 0.6.0**：保留全部数据，数据库仍为 603。
- **0.6.0 RC1～RC6 → 0.6.0**：移除受管 VLESS Reality 目标及关联配置，其他数据保留。旧规则在节点同步成功后清理。
- **0.5.3 → 0.6.0**：仅保留节点身份、IP、分组、模式、接入设置和管理员设置；目标、链路、配置、订阅及历史记录会清空。被动节点首次迁移需执行 `sudo ictl --sync`。更早版本请先升级至 0.5.3。

Docker 升级备份位于数据卷内的 `/data/ictl.db.pre-deploy-*.bak`。回退须同时恢复旧数据库与旧镜像；详细变更见 [CHANGELOG](CHANGELOG.md)。

## 卸载

**节点：** 先从链路移除，等待规则清理，再在面板删除节点并完成删除同步，最后执行：

```bash
sudo ictl --uninstall
```

**服务端：** 删除容器和镜像，保留数据卷：

```bash
curl -fsSL https://github.com/moran1024a/iptables-control/releases/download/v0.6.0/install-server.sh | bash -s -- --uninstall
```

追加 `--purge` 会永久删除默认数据卷。卸载服务端不会清理远端节点规则。

**单机脚本：** 先在菜单中删除所有转发，再执行以下命令，之后可删除脚本：

```bash
sudo systemctl disable --now ipt-forward-selfcheck.timer ipt-forward-selfcheck.service
```

## 使用指南

### 创建转发

1. **节点**：接入入口服务器和需要的中转服务器。
2. **目标**：填写 IPv4／域名和端口，或导入 SS／SS2022／VLESS ENC 链接。目标代理服务需自行部署。
3. **链路**：新建链路，在画布添加入口、中转节点和目标，从右侧连接点拖线到下一节点左侧连接点。
4. **保存**：系统校验并整理布局，随后同步配置。主动节点自动同步；被动节点需手动同步或开启远程唤醒。
5. **配置**：显示“已应用”后，复制入口地址或导入链接使用，也可加入订阅。

每个入口只为沿连线可达的目标生成配置，不可达的组合不生成。节点离线不影响保存，但需要同步成功才能生效。

### IP 与路径切换

- **主 IP／副 IP**都是别人连接该节点时使用的地址，支持手动填写，不要求绑定在本机网卡上，但流量必须能到达节点。
- 连线时选择接收 IP；发出方向通常选**自动出口**。需要指定源 IP 时选择对应地址，NAT 映射或特殊路由可在地址的“高级设置”中配置本机源 IP、网卡和网关。
- 目标也可添加多个接入地址，共用原协议和认证信息；连线到需要的地址即可。
- 非编辑状态下，点击中间节点快速切换，或点击入口逐跳选择。系统尽量保留原有上游，各入口—目标独立处理。

修改已使用的地址时，先新增地址、调整连线并同步成功，再删除旧地址。

### 自动灾备与质量图

节点权重默认为 **0**，可正可负，越高越优先；`direct` 可调整一条连线的权重。在链路详情开启自动灾备后，正在使用的主动中间节点持续失联或转发异常时，会按路径权重选择可用备路。入口、目标和被动节点不作为故障触发源，不按延迟自动换路，也不自动回切。

失联等待默认 **60 秒**，可在设置中改为 **30～3600 秒**。悬停中间节点可预览备用路径。被动入口参与自动切换需开启远程唤醒。

链路详情的“网络质量”显示入口之后的全段及分段曲线，支持 **15 分钟、1 小时、24 小时、7 天**视图。默认每 10 秒探测，共享相同探测任务；无法测量的片段显示未知。UDP 探测需放行节点探测端口（默认 `33445`），Ping 需允许 ICMP。

### 配置与订阅

链路生成的配置使用固定 **C 编号**，跟随链路分组和排序；“配置 → 导入配置”可手动导入外部节点，使用固定 **E 编号**，可独立分组和排序。两者均可改名，导入链接和订阅使用配置名称。

外部导入支持九类：**SS／SS2022、VMess、VLESS、Trojan、Hysteria2、TUIC v5、AnyTLS、SOCKS5、HTTP／HTTPS**。只支持单节点链接，不导入第三方订阅地址；外部配置由客户端直接连接。

在“订阅”新增订阅，勾选所需 C／E 配置，复制 Mihomo 或 v2rayN 订阅链接到客户端。页面会提示协议兼容情况；HTTP／HTTPS 代理仅导出到 Mihomo，纯 IP＋端口的 C 配置不能作为代理订阅项。

订阅链接包含获取代理凭据的权限，请勿公开分享。

## 主动与被动模式

| 模式 | 同步方式 | 程序升级 |
| --- | --- | --- |
| 主动 | 长连接，即时推送；自动采集状态与网络质量 | 开启自动更新后自动升级 |
| 被动 | 默认仅手动同步，可选认证 UDP 唤醒；状态条白底，同步活动标绿 | 本机手动更新 |

切换模式：在面板修改节点模式，然后到节点执行 `sudo ictl --sync` 确认。交接失败时按提示运行 `sudo ictl --recover`，不要手动删除交接记录。

**被动远程唤醒默认关闭。** 先在节点运行 `sudo ictl --wake-settings`，填写 UDP 端口和服务端实际出口 IP 白名单；再在面板为对应节点填写相同端口并启用，同时放行云安全组。配置变更后会自动触发同步，30 秒内未确认则等待手动同步。远程唤醒不升级程序、不确认模式切换。

两种 Agent 都可与 `ipt.sh` 共存，各自管理自己的规则。入站端口不可冲突；Agent 同时占用 TCP 和 UDP。冲突时保留原规则并报错，手动调整端口后重新同步。

## 常用命令与排查

| 用途 | 命令 |
| --- | --- |
| 节点菜单 | `sudo ictl` |
| 手动同步／更新程序 | `sudo ictl --sync` / `sudo ictl --update` |
| 修改节点接入信息 | `sudo ictl --credentials` |
| 查看 Agent 日志 | `sudo journalctl -u ictl-agent -n 100 --no-pager` |
| 查看服务端日志 | `docker logs --tail 100 ictl-server` |
| 找回面板地址 | `docker exec ictl-server python3 /app/ictl-server.py info` |
| 重置管理员账号 | `docker exec ictl-server python3 /app/ictl-server.py reset-admin` |

- **面板 404**：使用安装时输出的完整地址，其中包含随机路径。
- **配置等待中**：查看链路阶段和节点错误；被动节点或待确认模式切换需执行 `sudo ictl --sync`。
- **已应用但不通**：检查各跳地址、目标服务、回程路由和防火墙。当前转发仅支持 IPv4。
- **端口冲突**：修改或删除占用配置后重新同步；只停止另一个程序不一定释放其保留端口。

## 开发与许可证

服务端使用 Python 3.10+，依赖见 [requirements.txt](server/requirements.txt)；Go 版本以 [go.mod](agent/active/go.mod) 为准。测试位于 [tests](tests/)，构建与发布见 [release.yml](.github/workflows/release.yml)。欢迎提交 Issue 或 PR，请隐去密钥、密码和订阅链接。

服务端、两种 Agent 和管理器均为 **0.6.0**；独立组件版本见 [components.json](server/components.json)。项目采用 [MIT 许可证](LICENSE)。
