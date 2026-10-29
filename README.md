# iptables-control (ictl)

轻量的中心化 iptables 转发管理面板。服务端 **0.3.0**，Bash Agent **0.2.0**。以下下载命令获取最新 Release。

## 安装

**服务端**：先安装并启动 Docker，再执行：

```bash
curl -fsSL https://github.com/moran1024a/iptables-control/releases/latest/download/install-server.sh | bash
```

默认端口 `8730`、容器 `ictl-server`、数据卷 `ictl-data`。安装完成会打印面板地址，默认账号 `admin`、密码 `admin_pass`，首次登录必须修改。自定义端口或使用国内镜像，可将末尾改为 `| bash -s -- --port 9000 --mirror`。

**转发节点**：在面板新增节点，复制详情页的“一键接入”命令，到节点执行。安装后运行 `sudo ictl` 进入菜单。

节点需要 Bash、curl、openssl 1.1.1+、iptables（含 save／restore、addrtype、conntrack）、getent、flock 和 systemd；与服务端时差不能超过 60 秒。

面板登录应使用 HTTPS：通过反向代理转发到 `127.0.0.1:8730`，在“设置 → 对外地址”填写 HTTPS 地址，再复制节点接入命令。

## 升级

**服务端**：重新执行上面的安装命令。默认沿用端口、绑定地址和数据挂载；`--force` 可强制重建。安装器保留升级前数据库备份，新容器启动或就绪检查失败时恢复旧容器和数据库。

**升级至 0.3.0 会删除数据库中全部隧道节点、受管隧道及其关联配置。远端旧 Agent、sing-box 和配置文件不做处理，需自行卸载。** 外部导入的 SS／VLESS 转为“目标”，原配置中的地址和端口逐条生成独立目标；其余有效配置、密钥、路径、端口、分组和排序保留。

**节点 Agent**：先升级服务端，再等待主动节点自动更新，或在节点执行：

```bash
sudo ictl --update           # 更新程序；国内下载可加 --mirror
sudo ictl --sync             # 同步配置并上报执行结果
```

旧节点升级后默认主动，成功上报新协议后才能切换模式；以后升级保留已选模式。被动节点只在手动同步时检查自动更新，菜单 5 可开关程序自动更新。

迁移备份为数据库旁的 `ictl.db.pre-0.3.0.bak`，安装器另保留 `/data/ictl.db.pre-deploy-*.bak`。**回退须停止服务，同时恢复旧镜像和升级前数据库，移除新库的 `-wal`／`-shm` 后再启动**；回退会丢弃升级后新增的数据。自定义数据库路径请手动备份、升级和回退。

## 卸载与删除

**卸载服务端，保留数据卷：**

```bash
curl -fsSL https://github.com/moran1024a/iptables-control/releases/latest/download/install-server.sh | bash -s -- --uninstall
```

追加 `--purge` 会删除默认数据卷中的全部数据。服务端卸载不会清理远端节点规则。

**卸载转发节点：** 先解除面板中的引用、删除节点并完成清理同步，再在节点执行：

```bash
sudo ictl --uninstall        # 需要本机确认；非交互可加 -y
```

被动节点需先运行 `sudo ictl --sync` 确认清理。卸载会删除本 Agent 的规则、配置和服务。旧隧道节点使用对应旧 Release 的 `install-tunnel.sh --uninstall` 自行卸载。

**删除面板对象：** 被引用的节点、链路或目标需先解除引用。删除配置要等相关节点确认清理才释放端口；强制删除配置仍保留未确认的端口。强制删除节点记录不会清理远端规则。

## 常用命令

```bash
sudo ictl                                                        # 节点菜单、状态和维护
docker logs ictl-server                                          # 服务端日志
docker exec ictl-server python3 /app/ictl-server.py info           # 面板地址
docker exec ictl-server python3 /app/ictl-server.py health         # 就绪检查（默认 8730）
docker exec ictl-server python3 /app/ictl-server.py reset-admin    # 重置管理员
```

## 使用说明

| 板块 | 用途 |
|---|---|
| 节点 | 纯 iptables 转发，支持主动／被动模式 |
| 链路 | 保存节点顺序，例如 A → B → C，模板本身不占端口 |
| 目标 | 导入 SS／SS2022、VLESS Reality，或填写 IPv4／域名＋端口 |
| 配置 | 选择已保存的目标，组合链路或直连；每个配置独立分配端口 |

四个板块各自支持分组、排序和筛选。修改链路或目标会更新引用配置，端口不足时整次保存回滚。分享链接使用链路入口地址和端口，保留原凭据及传输参数；仅修改凭据不触发转发同步。不支持 IPv6，不安装代理核心或探测外部连通性。直连显示“已保存”，链路的“已应用”表示规则确认成功。

### 主动与被动

| 行为 | 主动 | 被动 |
|---|---|---|
| 同步 | 每 5–10 秒自动轮询 | 仅本机 `sudo ictl --sync`，或菜单 3“同步更新” |
| 开机 | 恢复缓存规则并轮询 | 只恢复已应用规则，不联网、不刷新 DNS |
| 待更新 | 自动获取并应用 | 持续“等待中”，直到当前变更成功确认 |
| 无上报 | 超过 120 秒显示离线 | 不因时间流逝判离线 |
| 状态条 | 每格 20 秒，最近 20 分钟的在线／故障记录 | 每格 60 秒、最近 1 小时；白底，收到手动同步的时间格标绿 |

在面板修改模式后，**两个方向的切换都需要本机手动同步确认**。主动切被动时，下一次轮询会先停止后台同步；被动切主动时，本机同步成功后恢复轮询。混合链路不保证所有节点同时生效。

手动同步按“上报 → 获取配置 → 应用 → 上报结果”执行。失败、旧确认或应用期间出现新变更不会清除等待；确认丢失时有限重试，仍失败请再次手动同步。绿色只代表收到同步请求，成功或失败见事件记录。两种状态条均保留 60 格，记录保留 7 天；主动模式的 20 秒记录从更新后开始积累。

## 源码部署

需要 Python 3.10+、SQLite 3.35+ 和 openssl，在仓库目录执行：

```bash
mkdir -p /opt/ictl/agent
cp -r server/ictl-server.py server/target_config.py server/components.json server/web /opt/ictl/
cp agent/ictl.sh /opt/ictl/agent/
python3 /opt/ictl/ictl-server.py serve --listen 0.0.0.0:8730
```

数据库默认 `/var/lib/ictl-server/ictl.db`，可用 `ICTL_DB` 覆盖。升级前自行备份；不要同时运行多个服务实例。节点配置位于 `/etc/ictl/agent.conf`，规则、模式和状态位于 `/var/lib/ictl/`，服务名为 `ictl-agent`。旧单机脚本 `ipt.sh` 仍独立保留。

## 开发验证

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests
python3 deploy/check-versions.py
node --check server/web/app.js
for file in agent/ictl.sh deploy/install-server.sh ipt.sh; do bash -n "$file"; done
```

测试覆盖临时数据库迁移、本机 HTTP、真实 Bash 加密协议和 Chrome 界面；iptables／systemd／Docker 使用模拟命令，真实 Linux 集成及长期运行仍待验证。版本变化见 [CHANGELOG](CHANGELOG.md)。
