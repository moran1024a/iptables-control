# ipt-forward-manager

在 Linux 服务器上管理 `iptables` 端口转发（DNAT + MASQUERADE）的交互式 Shell 脚本，当前版本 2.3。

支持单条/批量添加、选择删除、修改、搜索、命中统计、导入导出、自动备份与回滚，以及以域名/DDNS 为目标的定时刷新。

## 环境要求

- Linux、Bash、root 权限
- `iptables`（legacy 或 nf_tables 后端均可）
- 推荐 `iptables-persistent`：开机自动恢复规则
- 可选 `nc` 或 `timeout`：测试目标 TCP 连通性
- 可选 `systemd`：DDNS 定时刷新

菜单 1 可用 `apt` 自动安装依赖；非 Debian/Ubuntu 系统请自行安装。

## 快速开始

```bash
curl -L https://raw.githubusercontent.com/moran1024a/iptables-control/main/ipt.sh -o ipt.sh && chmod +x ipt.sh && sudo bash ipt.sh
```

之后运行 `sudo bash ipt.sh` 即可。首次使用建议：

1. 执行 `1) 检查 iptables 环境`，确认已安装 `iptables-persistent`，否则重启后规则不会恢复。
2. 如需 DDNS，先把脚本放到固定位置（如 `/root/ipt.sh`），再执行 `20) 安装 DDNS 定时服务`。

## 菜单

```text
基础检查        1) 检查 iptables 环境      2) 检查/修复 IPv4 转发
规则查看        3) 转发规则摘要            4) 全部 NAT/FORWARD 规则
                5) 搜索规则                6) 带备注的规则
                7) 端口命中统计
添加/删除/修改  8) 添加单条转发            9) 批量添加转发
               10) 选择删除转发           11) 按备注批量删除
               12) 修改已有转发
导入导出       13) 导出为 .conf           14) 从 .conf 导入
备份/回滚/测试 15) 备份当前规则           16) 查看备份列表
               17) 从备份回滚             18) 测试目标 TCP 连通
               19) 保存当前规则
自检/DDNS/日志 20) 安装 DDNS 定时服务     21) 卸载 DDNS 定时服务
               22) 查看日志（最近 100 行）23) 立即刷新 DDNS 转发
                0) 退出
```

交互输入均可输入 `q` 取消，有默认值时回车即采用；输入不合法会提示原因并要求重输。

## 添加转发

单条添加（菜单 8）：

```text
入站端口: 8443
目标 IP/域名: home.example.com
目标端口 [8443]: 443
协议  1) TCP  2) UDP  3) TCP+UDP [3]:
备注 [xjj-forward-8443-to-home.example.com-443]:
```

批量添加（菜单 9）和导入（菜单 14）使用同一种行格式，`#` 后为注释：

```text
入站端口 目标IP或域名 目标端口 [tcp|udp|both，默认 both] [备注]

443  1.1.1.1           443 both xjj-forward-443
8443 home.example.com  443 tcp
```

- 备注只能包含字母、数字和 `. _ : -`，以字母或数字开头，最长 200 字符，不能含空格；省略时自动生成。
- 每条转发会创建三条规则：`nat PREROUTING` DNAT、`nat POSTROUTING` MASQUERADE、`filter FORWARD` ACCEPT（插入链首）。首次添加还会插入回程放行规则（备注 `ipt-forward-manager-established`），保证 FORWARD 默认策略为 DROP 时仍可用。
- 入站端口已被其他转发占用时拒绝添加；三条规则任一失败，已添加的部分会被撤销。已存在的规则不会重复添加。
- 批量添加和导入会先逐行校验并以表格预览，错误行会被跳过。
- 添加前会自动开启 IPv4 转发。

## 删除与修改

- `10) 选择删除转发`：从列表选择；同时有 TCP 和 UDP 时可只删其一。
- `11) 按备注批量删除`：每行一个备注，删除 nat 表和 FORWARD 链中带该备注的所有规则，确认前预览规则原文。
- `12) 修改已有转发`：原值作为默认值。先解析新目标并检查端口冲突，通过后才替换旧规则；添加失败时自动恢复为修改前的规则。

脚本只识别带备注的规则，不会删除或修改没有备注的规则。请通过脚本完成增删改，手动改动带脚本备注的规则可能与托管配置不一致。

## 域名 / DDNS

目标可以填写域名。添加时解析 IPv4 A 记录写入 iptables，并把域名原值记录到托管配置 `managed-forwards.conf`。

`23) 立即刷新 DDNS 转发` 或定时服务会重新解析这些域名，IP 变化时替换对应规则并保存；解析失败的条目会被跳过。

`20) 安装 DDNS 定时服务` 会安装 `ipt-forward-selfcheck.service`（oneshot）和 `.timer`（开机 1 分钟后触发，之后每 5 分钟一次），每次执行 `bash <脚本绝对路径> --self-check`：确保运行时 IPv4 转发开启、统计 DNAT 规则数、刷新域名转发并写日志。

- 服务记录的是安装时的脚本路径，移动或删除脚本后需重新执行菜单 20；卸载用菜单 21。
- 脚本用文件锁防止定时任务与手工操作同时改规则，定时任务最多等待 120 秒，超时则跳过本次刷新。

## 导出

`13) 导出为 .conf` 默认写入 `/root/iptables-forward-conf/forwards-YYYYMMDD-HHMMSS.conf`，格式与导入相同：

```text
8443   home.example.com               443    both  home-https
9000   5.6.7.8                        90     tcp   web
```

同一转发的 TCP/UDP 合并为 `both`；使用域名的转发导出域名；没有备注的 DNAT 规则会被跳过。迁移服务器时可在旧机器导出、新机器导入。

## 备份、持久化与文件

添加、批量添加、删除、批量删除、修改、导入、回滚前都会自动备份（`iptables-save` 全量快照）。`17) 从备份回滚` 按时间倒序选择备份，用 `iptables-restore` 覆盖当前规则，失败时当前规则不变。

每次改动后自动保存：有 `netfilter-persistent` 时执行 `netfilter-persistent save`，否则写入 `/etc/iptables/rules.v4`（此时开机不会自动加载，需手动 `iptables-restore < /etc/iptables/rules.v4`）。

IPv4 转发持久化写入 sysctl.d；若 `/etc/sysctl.conf` 中有 `net.ipv4.ip_forward=0`，会被注释掉以免覆盖。

| 路径 | 用途 |
|---|---|
| `/root/iptables-forward-conf/managed-forwards.conf` | 托管配置，记录原始目标（可为域名），仅供 DDNS 刷新 |
| `/root/iptables-forward-conf/forwards-<时间>.conf` | 导出文件，脚本运行时不读取 |
| `/root/iptables-backup/iptables-<时间>[-N].rules` | 自动/手动备份 |
| `/etc/iptables/rules.v4` | 持久化规则 |
| `/etc/sysctl.d/99-ipt-forward-manager.conf` | `net.ipv4.ip_forward=1` |
| `/etc/systemd/system/ipt-forward-selfcheck.{service,timer}` | DDNS 定时服务（菜单 20） |
| `/var/log/ipt-forward-manager.log` | 操作日志 |
| `/run/ipt-forward-manager.lock` | 运行时锁 |

备份和日志不会自动清理或轮转。

## 排查与注意事项

- 使用云厂商安全组时需同时放行入站端口；目标机器也要允许来自转发服务器的访问。
- 转发不通时用 `7) 端口命中统计`：计数不增加说明流量没到本机；计数增加则检查目标服务，可用 `18) 测试目标 TCP 连通`（UDP 无法可靠探测，只测 TCP）。
- Docker、ufw、firewalld 等重载规则时可能覆盖本脚本的规则，可用菜单 3 检查，必要时回滚或重新添加。

## 致谢

本项目参考了 [exing567/iptables-control](https://github.com/exing567/iptables-control)，感谢原作者。
