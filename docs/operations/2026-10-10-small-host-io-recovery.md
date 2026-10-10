# 小内存云主机磁盘读写阻塞恢复（2026-10-10）

## 故障与证据

本次为主机运行配置修复，应用构建仍为 `c2f67efdc8e9a553704fc2fcc7fb82461fe9e794`。
主机标称 2 核、2 GiB 内存、40 GiB 系统盘，系统实际可用物理内存约 1613 MiB，已有 2 GiB swap。
SSH 卡在握手、HTTPS 超时；阿里云诊断报告显示云盘读写延迟过长或达到 IOPS 上限。

所有时间均为北京时间。持久 sysstat 的 2026-10-10 07:52–08:54 区间显示：

| 指标 | 故障时观测 |
| --- | --- |
| CPU I/O 等待 | 平均 85.91% |
| 系统盘读 / 写 | 约 143334 KiB/s / 23 KiB/s |
| 系统盘请求 / 平均等待 | 约 2044 次/秒 / 99 ms |
| 可用内存 | 平均约 74 MiB |
| swap 换入 / 换出 | 均为 0 |
| 主缺页 | 约 192 次/秒 |
| 磁盘空间 | 重启后核验只使用 26%，仍有约 28 GiB 空闲 |

故障前 06:10 的 I/O 等待约 0.35%；06:22 的统计窗口内升至 28.64%，随后约 85%。
`apt-daily-upgrade.service` 于 06:18 启动，时间上与恶化一致，但没有故障时逐进程采样，
不能把该维护任务或邮件同步认定为唯一触发源。前一启动的内核日志未检出 OOM kill、
阻塞任务或块设备 I/O error，不能据此断言发生了 OOM 或磁盘硬件损坏。

当时 `/etc/sysctl.conf` 将 `vm.swappiness` 固定为 0；
`/etc/sysctl.d/99-sysctl.conf` 是它的符号链接。
TuneD 的 `virtual-guest` 继承 `throughput-performance`，实际系统盘预读量为 4096 KiB。
高读、低写、持续缺页/回收、低可用内存和零换页共同支持：
**小内存工作集挤压文件缓存，系统倾向持续回收并重读文件页，较大预读进一步放大读盘开销。**
这是证据支持的故障机制判断，不是对某一个进程的唯一归因。

## 已应用的修复

1. 将现有 `/etc/sysctl.conf` 中唯一的 `vm.swappiness = 0` 改为 `60`，保留其余设置。
2. 安装本仓 `infra/tuned/tradeos-small-server.conf` 至
   `/etc/tuned/tradeos-small-server/tuned.conf`。
   它继承原 `virtual-guest`，只覆盖 swap 倾向及磁盘预读：
   `vm.swappiness=60`、`readahead=128`（KiB）。
3. 执行 `tuned-adm profile tradeos-small-server`，并重新启动 TuneD 验证持久配置。

Linux 官方说明：swappiness 默认值为 60；为 0 时，要到可用页及文件页低于区域高水位才启动 swap。
参考 [内核 vm 文档](https://docs.kernel.org/admin-guide/sysctl/vm.html#swappiness)。
该参数不是“内存使用达到 60% 就交换”的阈值。

本次没有改业务数据、租户隔离、邮箱权限或应用代码。安全更新继续保持启用。
没有新增数据库备份、测试 Docker 容器或软件包。

## 应用与核验

此配置仅记录本次小内存主机的修复，不是所有生产主机的通用性能保证。
应用前核对当前 TuneD profile、swap、内存和实际磁盘设备。
先修正已有 `vm.swappiness = 0`，不要覆盖整个 sysctl 文件或堆叠矛盾配置。

```sh
sudo install -d -m 0755 /etc/tuned/tradeos-small-server
sudo install -m 0644 infra/tuned/tradeos-small-server.conf /etc/tuned/tradeos-small-server/tuned.conf
sudo tuned-adm profile tradeos-small-server
sudo sysctl -w vm.swappiness=60
sudo systemctl restart tuned
sudo tuned-adm verify
sysctl vm.swappiness
cat /sys/block/vda/queue/read_ahead_kb
systemctl is-active tradeos.service tradeos-mailbox.service
```

本次 TuneD 校验成功，实际值为 60 和 128；两个应用服务均 active。

随后进行了有保护的短时内存负载验证：
每 4 秒增加 32 MiB，上限 192 MiB，再保持约 30 秒；
独立临时 systemd 服务限制为 MemoryMax=256M、MemorySwapMax=128M、
CPUQuota=20%、RuntimeMaxSec=120、OOMScoreAdjust=800。
低可用内存、明显内存停顿或本机 HTTPS 非 200 时立即停止。

实际达到 192 MiB 上限并正常结束。全部 HTTPS 检查均返回 200；
swap 从约 13 MiB 增至约 210 MiB，持有负载时可用内存约 259–274 MiB，
释放后约 439 MiB。服务没有重启、没有 OOM。
随后磁盘采样的读速率为 0–18 KiB/s、I/O 等待为 0%，内存完整停顿 avg10 为 0。
这些是不同负载下的运行观测，不应描述成等负载性能基准。

Edge 的 jslt 登录、邮箱状态读取及手动同步已通过；
页面显示历史邮件已补齐，手动同步完成时间更新。未重发测试邮件。

临时压力进程已退出，不留下常驻压力任务。
后续沿用系统已有 sysstat 记录检查长期表现；短时验证不等于已经完成整夜稳定性验证。

## 回滚

保留的原值为：TuneD profile `virtual-guest`、sysctl 的 swappiness 为 0、
系统盘预读为 4096 KiB。需要回滚时，先核对当前主机仍对应本记录，
恢复 sysctl 原行，切回原 profile，再用 `sysctl -w vm.swappiness=0` 明确同步运行值。
不要重建数据库、删除持久卷或通过 `swapoff` 强行把交换页挤回小内存。
