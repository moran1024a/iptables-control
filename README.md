# ipt-forward-manager

一个用于在 Linux 服务器上管理 `iptables` 端口转发规则的交互式 Shell 脚本。

脚本通过菜单方式帮助你完成端口转发的添加、批量添加、删除、修改、查询、备份、回滚、导入导出、自检和 DDNS 刷新等操作，适合需要经常维护 DNAT/MASQUERADE 转发规则的服务器场景。

## 功能特性

- 检查 `iptables`、`iptables-save`、`netfilter-persistent` 是否可用
- 自动开启并持久化 IPv4 转发（写入 `/etc/sysctl.d/99-ipt-forward-manager.conf`）
- 添加单条 TCP、UDP 或 TCP+UDP 端口转发，目标支持 IPv4 或域名
- 批量添加端口转发规则，逐行校验格式
- 支持 DDNS 场景：保存域名配置，定时或手动刷新解析后的转发 IP
- 从编号列表中选择要删除或修改的转发，修改时原值作为默认值
- 删除时可只删 TCP 或 UDP，未选择的协议会保留
- 批量按备注 `comment` 删除规则
- 查看转发规则摘要、完整 NAT/FORWARD 规则、带备注规则
- 按端口、IP、备注搜索规则，查看指定端口的规则命中次数
- 导出当前转发规则为 `.conf`，从 `.conf` 导入并预览
- 改动规则前自动备份，可按编号选择备份回滚
- 输入校验：端口、IPv4、域名、备注格式不合法时原地提示重输
- 端口冲突检测：入站端口已被其他转发占用时拒绝添加
- 修改失败时自动恢复为修改前的规则
- 文件锁防止定时任务与手工操作同时修改规则
- 记录操作日志到 `/var/log/ipt-forward-manager.log`

## 适用环境

- Linux 服务器
- Bash
- root 权限
- `iptables`（legacy 或 nf_tables 后端均可）
- 推荐：`iptables-persistent` / `netfilter-persistent`，用于开机自动恢复规则
- 可选：`nc` 或 `timeout`，用于测试目标端口 TCP 可达性

脚本内置的安装命令使用 `apt`，因此自动安装依赖更适合 Debian、Ubuntu 等系统。其他发行版可以自行安装 `iptables` 相关组件后再运行脚本。

## 快速开始

```bash
curl -L https://raw.githubusercontent.com/exing567/iptables-control/main/ipt.sh -o ipt.sh && chmod +x ipt.sh && sudo bash ipt.sh
```

运行此指令一次之后，再次运行只用输入 `sudo bash ipt.sh` 即可。

启动后会进入交互菜单，顶部显示 IPv4 转发状态、转发配置条数和 DDNS 定时服务状态，根据编号选择对应功能。

首次使用建议：

1. 执行 `1) 检查 iptables 环境`，确认已安装 `iptables-persistent`，否则重启后规则不会自动恢复。
2. 如果转发目标使用域名/DDNS，先把脚本放到固定位置（如 `/root/ipt.sh`），再执行 `20) 安装 DDNS 定时服务`。

## 常用操作

所有交互输入均可输入 `q` 取消；有默认值时直接回车即使用默认值。输入不合法时会提示原因并要求重新输入。

### 添加单条端口转发

在菜单中选择 `8) 添加单条转发`，按提示输入：

```text
入站端口: 8443
目标 IP/域名: home.example.com
目标端口 [8443]: 443
协议  1) TCP  2) UDP  3) TCP+UDP [3]:
备注 [xjj-forward-8443-to-home.example.com-443]:
```

备注只能包含字母、数字和 `. _ : -`，以字母或数字开头，最长 200 个字符。

添加后脚本会创建对应的：

- `nat` 表 `PREROUTING` DNAT 规则
- `nat` 表 `POSTROUTING` MASQUERADE 规则
- `filter` 表 `FORWARD` ACCEPT 规则（插入到链首）

首次添加时还会在 `FORWARD` 链首插入一条回程放行规则（备注 `ipt-forward-manager-established`），保证 FORWARD 默认策略为 DROP 时转发仍然可用。

如果入站端口已被其他转发占用，脚本会提示占用者并拒绝添加；三条规则中任何一条添加失败，已添加的部分会被撤销。

### 批量添加转发

在菜单中选择 `9) 批量添加转发`，每行一条，输入 `END` 结束：

```text
入站端口 目标IP或域名 目标端口 [协议] [备注]
```

示例：

```text
443 1.1.1.1 443 both xjj-forward-443
8443 home.example.com 443 tcp
END
```

协议支持 `tcp`、`udp`、`both`，省略时默认 `both`。省略备注时自动生成，例如：

```text
xjj-forward-8443-to-home.example.com-443
```

格式错误的行会在输入时立即提示并被忽略，确认前会以表格预览所有有效规则。

### 使用域名/DDNS

如果目标是家宽动态 IP，可以把目标地址填写为 DDNS 域名，例如：

```text
8443 home.example.com 443 both home-https
```

脚本会在添加时解析 `home.example.com`，把解析到的 IPv4 写入 `iptables`，同时把域名原值保存到托管配置：

```text
/root/iptables-forward-conf/managed-forwards.conf
```

DDNS 解析变化后，执行 `23) 立即刷新 DDNS 转发`，或安装定时服务自动刷新。脚本会重新解析托管配置中的域名，发现与当前 DNAT 规则不一致时，删除旧 IP 规则并添加新 IP 规则，然后保存。

### 删除规则

- `10) 选择删除转发`：列出当前带备注的转发配置，选择序号；如果该转发同时有 TCP 和 UDP，可以只删除其中一个协议。
- `11) 按备注批量删除`：先列出现有备注，每行输入一个备注，确认前预览将被删除的规则原文。

### 修改规则

在菜单中选择 `12) 修改已有转发`，从列表中选择序号，按提示修改入站端口、目标、目标端口、协议和备注，直接回车保留原值。

脚本会先解析新目标并检查端口冲突，全部通过后才删除旧规则、添加新规则。如果添加失败，会自动恢复为修改前的规则。

## 配置导入导出

### 导出

在菜单中选择 `13) 导出为 .conf`，默认导出到：

```text
/root/iptables-forward-conf/forwards-YYYYMMDD-HHMMSS.conf
```

导出文件格式：

```text
in_port target_ip_or_domain target_port protocol comment
```

示例：

```text
8443   home.example.com               443    both  home-https
9000   5.6.7.8                        90     tcp   web
```

- 同一转发的 TCP 和 UDP 会合并为 `both`。
- 使用域名的转发导出的是域名而不是当前 IP。
- 没有备注的 DNAT 规则（非本脚本创建）不会导出，脚本会提示跳过的条数。

### 导入

在菜单中选择 `14) 从 .conf 导入`。配置文件支持 `#` 注释和空行：

```text
# format: in_port target_ip_or_domain target_port protocol comment
443 1.1.1.1 443 both xjj-forward-443
8443 home.example.com 443 tcp xjj-forward-8443
```

导入前会逐行校验并以表格预览，错误行会标明行号和原因，确认后只导入有效行。已存在的规则不会重复添加。

## 文件布局

```text
/root/iptables-forward-conf/
├── managed-forwards.conf         # 托管配置，记录每条转发的原始目标（可为域名），DDNS 刷新使用
└── forwards-<时间>.conf          # 菜单 13 导出的配置

/root/iptables-backup/
└── iptables-<时间>[-N].rules     # 每次改动前的 iptables-save 全量快照

/etc/iptables/rules.v4            # 持久化的规则
/etc/sysctl.d/99-ipt-forward-manager.conf               # net.ipv4.ip_forward=1
/etc/systemd/system/ipt-forward-selfcheck.{service,timer}  # 可选，菜单 20 安装
/var/log/ipt-forward-manager.log  # 操作日志
/run/ipt-forward-manager.lock     # 运行时锁文件
```

各部分的关系：

- 真正生效的是内核中的 iptables 规则，脚本通过规则上的备注识别自己管理的转发。
- `managed-forwards.conf` 只用于 DDNS 刷新，不负责开机恢复规则。
- 备份只在回滚或修改失败自动恢复时使用。
- 导出的 `.conf` 用于查看和迁移，脚本运行时不会读取。

## 备份与回滚

以下操作会在改动前自动备份当前规则：添加、批量添加、删除、批量删除、修改、导入、从备份回滚。同一秒内的多次备份会自动加序号，不会互相覆盖。

也可以在菜单中手动执行：

```text
15) 备份当前规则
16) 查看备份列表
17) 从备份回滚
```

回滚时按时间倒序列出备份，选择序号即可。回滚会使用 `iptables-restore` 覆盖当前规则，执行失败时当前规则保持不变。

备份文件不会自动清理，长期使用后可手动删除旧备份。

## 持久化

| 内容 | 重启后是否保留 | 说明 |
|---|---|---|
| iptables 规则 | 安装 `iptables-persistent` 时自动恢复 | 开机由 `netfilter-persistent.service` 加载 `/etc/iptables/rules.v4` |
| iptables 规则 | 未安装时不会自动恢复 | 文件仍会写入，但需手动 `iptables-restore < /etc/iptables/rules.v4` |
| IPv4 转发 | 保留 | 写在 `/etc/sysctl.d/`，开机由 systemd-sysctl 加载 |
| 托管配置、备份、日志 | 保留 | 普通文件 |

每次增删改后脚本都会自动保存规则：优先使用 `netfilter-persistent save`，未安装时写入 `/etc/iptables/rules.v4`。

开启 IPv4 转发时，如果 `/etc/sysctl.conf` 中有 `net.ipv4.ip_forward=0`，脚本会将其注释掉，避免覆盖新配置。

## 自检/DDNS 定时服务

脚本本身不常驻后台。执行 `20) 安装 DDNS 定时服务` 后会安装：

```text
ipt-forward-selfcheck.service   # oneshot，执行完即退出
ipt-forward-selfcheck.timer     # 开机 1 分钟后触发，之后每 5 分钟一次
```

每次触发执行：

```bash
bash /脚本绝对路径 --self-check
```

自检内容：

- 检查 `iptables` 是否存在
- 检查运行时 IPv4 转发，未开启则开启
- 统计 NAT 表中的 DNAT 规则数量
- 刷新托管配置中的域名/DDNS 转发规则，有变化时保存
- 写入日志

如果你正在菜单中修改规则，定时任务会等待；等待超时则跳过本次刷新并记录日志。

服务文件中记录的是安装时的脚本绝对路径，移动或删除脚本后需要重新执行菜单 20。如需卸载，执行 `21) 卸载 DDNS 定时服务`。

## 日志

日志文件路径：

```text
/var/log/ipt-forward-manager.log
```

可在菜单中选择 `22) 查看日志`（显示最近 100 行），也可以直接查看：

```bash
sudo tail -n 100 /var/log/ipt-forward-manager.log
```

日志不会自动清理或轮转。

## 菜单概览

```text
基础检查
   1) 检查 iptables 环境            2) 检查/修复 IPv4 转发

规则查看
   3) 转发规则摘要                  4) 全部 NAT/FORWARD 规则
   5) 搜索规则                      6) 带备注的规则
   7) 端口命中统计

添加 / 删除 / 修改
   8) 添加单条转发                  9) 批量添加转发
  10) 选择删除转发                 11) 按备注批量删除
  12) 修改已有转发

导入导出
  13) 导出为 .conf                 14) 从 .conf 导入

备份 / 回滚 / 测试
  15) 备份当前规则                 16) 查看备份列表
  17) 从备份回滚                   18) 测试目标 TCP 连通
  19) 保存当前规则

自检 / DDNS / 日志
  20) 安装 DDNS 定时服务           21) 卸载 DDNS 定时服务
  22) 查看日志                     23) 立即刷新 DDNS 转发

   0) 退出
```

## 注意事项

- 请使用 root 权限运行脚本。
- 建议所有增删改都通过脚本完成，不要手动修改带脚本备注的规则，否则可能与托管配置不一致。没有备注的规则脚本不会删除或修改。
- 如果服务器使用云厂商安全组，还需要同时放行对应入站端口；目标机器也需要允许来自转发服务器的访问。
- 转发不通时，可用 `7) 端口命中统计` 排查：计数不增加说明流量没到本机；计数增加则检查目标服务，可用 `18) 测试目标 TCP 连通`。
- 同时使用 Docker、ufw、firewalld 等工具时，它们重载规则可能覆盖本脚本的规则，可通过 `3) 转发规则摘要` 检查，必要时从备份回滚或重新添加。
- 域名/DDNS 仅使用 IPv4 A 记录，解析失败时会跳过该规则。
- UDP 可达性无法像 TCP 一样可靠检测，脚本只测试 TCP。
- 迁移服务器时，可在旧机器导出 `.conf`，在新机器导入。
