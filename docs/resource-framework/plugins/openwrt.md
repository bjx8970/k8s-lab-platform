# OpenWrt 插件设计

插件 ID：`openwrt`；驱动：`openwrt.uci/v1`；连接类型：`openwrt.ssh/v1`。遵循[框架契约](../architecture.md)。

## 定位与连接

管理路由器及具体 UCI 资源，接收明确参数执行操作。它不规划一套网络，不检查网络被谁使用，不评估改动会断开什么连接，也不处理与 VM/K8s 的创建顺序。

一台路由器对应一个 domain，SSH host/port/user/secret_ref 保存为独立 connection。PVE 与 OpenWrt 的配对在业务模块中；插件不读取 PVEServer.ow_*。

## 资源与动作

| 类型（均为 /v1） | 输入/定位 | 首版动作 |
|---|---|---|
| openwrt.router | SSH connection | probe、observe、commit、reload_service、restart_service |
| openwrt.vlan | package/section、parent_device、tag、device_name | create、observe、configure、delete |
| openwrt.interface | section、device、proto、IP、netmask | create、observe、configure、delete |
| openwrt.dhcp_pool | section、interface、start、limit、leasetime、dynamic_enabled | create、observe、configure、delete |
| openwrt.dnsmasq | section、监听接口/地址、域名 | create、observe、configure、delete |
| openwrt.firewall_zone | section、zone 属性 | discover、observe、add_interface、remove_interface |
| openwrt.dhcp_host | section、IP、MAC | create、observe、configure、delete |
| openwrt.port_forward | section、协议、源/目标 zone、IP/端口 | create、observe、configure、delete |

参数是调用方准备好的平台值，例如 interface 名、目标 IP；不要求提交 VM resource_id 或一张依赖图。可选 metadata 中的关联 ID 不参与执行判断。

`openwrt.network` 这样的多对象网络组合由其他模块管理，不作为基础插件的自动编排资源。需要逻辑网络条目时，其他模块可以注册自己的类型，框架仅登记和分发其 handler。

## 技术校验与身份

校验 package/section 名、IP/MAC 格式、端口/VLAN 类型及协议允许的范围，正确编码 SSH/UCI 参数。不会检查地址规划是否合理、是否和别的网络重叠、是否影响共享 zone、是否满足业务依赖。

新建 section 可使用调用方明确名称，或按本资源 UUID 生成 `rf_<uuidhex>`。绑定保存实际 package/section，不使用 `@host[-1]` 等位置表达式长期定位。已有匿名节由发现接口输出实际标识；身份无法唯一解析时返回定位错误，不猜另一个节。

`openwrt_sections(binding_id FK,domain_id FK,package,section_name,section_type)` 对有效 `(domain_id,package,section_name)` 唯一，这是同一配置对象不重复登记的约束，不是网络业务冲突检查。

UCI 的节表示参考 [OpenWrt 官方接口说明](https://openwrt.org/docs/guide-developer/ubus/uci)。首版继续使用 SSH/UCI。

## 提交和服务应用

写动作使用显式 `apply_mode`。首版只支持以下安全模式：

- `commit`（默认）：写入并 commit 相关 package。
- `reload`：写入、commit，并 reload 调用方指定的 service。
- `restart`：写入、commit，并 restart 调用方指定的 service。

reload/restart 的 service 必填，由插件支持的服务名称 schema 校验；不会根据业务关系自动推断要重启哪些服务。router 的 commit/reload_service/restart_service 也可单独调用。

保存配置与服务应用分别返回结果，参考 [OpenWrt 防火墙配置说明](https://openwrt.org/docs/guide-user/firewall/firewall_configuration)。例如 commit 成功而 restart 断线时，返回 committed=true、服务执行结果 unknown，不报告整套网络已经可用。

同一条写指令的 set/commit/apply 序列使用 PostgreSQL advisory lock 或数据库租约按 domain 跨进程串行，避免多个 Executor 命令交错。首版禁止 `apply_mode=none`，因为 UCI 未提交候选配置是路由器全局状态，无法安全归属于单个 Operation。未来如需批处理，必须设计显式 change-set/session 及跨 Operation 锁。

## 删除与失败

delete_interface 只删除指定 interface 节及执行明确的 apply_mode，不自动遍历 zone 或删除 DHCP。delete_vlan 不寻找引用它的 interface。delete_dhcp_host 按绑定节删除，不仅凭 IP 搜索一条就删除。

调用方有意要求删除仍被使用的配置时，插件照常提交；UCI/服务拒绝则返回实际错误。插件不生成 DeletePlanRequired、DependencyInUse 等业务拒绝。

部分提交、服务失败、SSH 断线均返回具体执行阶段和已知结果；不恢复快照、不清理其他节、不释放地址、不自动重试。补偿需要调用方另外发送指令。

## 现有实现适配

当前 OpenWrtClient 的多个方法立即 commit/restart，delete_interface 还会遍历 zone 移除关联。新的 handler 需要拆出只操作指定对象的底层方法，并让服务应用受 apply_mode 控制。

旧业务要保持原来的组合行为，应显式调用 remove_interface、delete_interface、reload_service 等指令。不会把旧方法的隐式关联修改直接带入基础插件。

## 验收

1. 不提供 VM/实验环境信息也能创建和查询 VLAN/interface。
2. 重叠网络、业务依赖等标签不导致插件自行拒绝；平台结果照实返回。
3. delete_interface 不枚举或修改其他配置节。
4. apply_mode=commit 不额外 restart；明确 restart 时执行所指定服务。
5. commit 成功而 reload 失败时返回部分结果，不自动恢复配置。
6. UCI 节重复登记属于身份冲突；匿名节位置变化不会令指令改作用于其他节。
