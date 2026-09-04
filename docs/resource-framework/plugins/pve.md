# PVE 插件设计

插件 ID：`pve`；VM 驱动：`pve.qemu/v1`。遵循[框架契约](../architecture.md)。

## 资源类型

| 类型 | 内容与首版动作 |
|---|---|
| pve.platform/v1 | 一套 PVE 平台；登记、发现、查询、查询候选 VMID |
| pve.node/v1 | 平台节点；登记、查询节点属性和 VM 列表 |
| pve.template/v1 | 现存 QEMU 模板；登记、查询模板配置 |
| compute.vm/v1 | 类型由 VM 插件定义；本插件实现创建、配置、启停、重启和删除 |

动作范围由已实现的 API 适配决定。未实现主机重装/关机或模板修改时返回 UnsupportedAction，不引入 protect 等业务策略。

平台/节点登记与连接记录分开。关闭它们的展示登记不级联处理 VM，也不扫描业务引用；只要指定连接仍存在，VM 仍按自己的绑定调用平台。

## 连接与身份

连接类型 `pve.api/v1`：domain_id、host、port、TLS 配置、API user、token secret_ref。多个访问端点可以由接入模块明确关联同一 domain。

VM 使用 resource_id 对外，绑定包含 domain、VMID、当前 node 和连接。node 不是全局唯一，不能省略平台作用域。节点迁移改变 locator，不自动创建另一条 VM 记录。

插件保存：

- `pve_platforms(resource_id FK,domain_id FK)`。
- `pve_guests(binding_id FK,domain_id FK,vmid,node,guest_kind,retired_at)`。
- 有效绑定的 `(domain_id,vmid)` 唯一；同域 `(domain_id,node,vmid)` 复合定位也建立约束。
- `(binding_id,domain_id)` 对应 rf_bindings 的完整唯一键，保证外键一致；node 可是平台原生名称，不要求先有一条业务节点资源。

这是外部身份与数据库一致性约束，不表示资源之间有生命周期依赖。VMID 身份与节点定位的区分参考 [PVE 配置与 VMID 说明](https://pve.proxmox.com/wiki/Migrate_to_Proxmox_VE)。

过渡期旧 vms 表添加 pve_server_id FK、三元复合唯一及 resource_id 映射，撤销 vmid 全局唯一。旧 server→domain 映射由迁移程序明确提供。

## 指令适配

创建 VM 需要调用方提供 node、template_vmid、目标 vmid、规格和克隆参数。需要平台建议的下一个 ID 时，调用方先对 platform 发送 `next_vmid` 查询，再把返回值用于 create。插件不为实验环境预留编号或规划资源数量；并发 ID 被占用时返回平台错误，调用方决定重新选取。

create 的协议实现：

1. 保存该指令的目标 node/template/vmid 和规范化参数。
2. 提交 clone，保留平台返回的 UPID。
3. 查询 clone 结果；成功后按本次 create 参数配置同一 VM。
4. 返回已产生的外部身份、任务及配置结果，登记绑定。
5. 部分失败返回外部 VMID 和已完成阶段，不自动删除或换 ID 重试。

平台任务的状态/日志是插件返回结果的一部分，参见 [PVE 官方管理指南](https://pve.proxmox.com/pve-docs/pve-admin-guide.pdf)。实现时核对实际部署版本的 API。

start、stop、reboot、delete 只执行对应命令：

- stop 的 graceful/force 模式由调用方指定。
- delete 使用明确的 purge 等后端参数；不主动先关机，不检查是否承载 K8s。
- 平台因为 VM 状态、锁或权限拒绝时返回 provider_code/message。
- configure 不隐式重启 VM；后端存在 pending 配置时返回该事实。
- 重启操作不能仅凭最后状态为 running 就判断完成，要返回平台任务实际结果。

## 执行恢复与查询

已保存 UPID 的操作在进程重启后可继续 poll。同一次操作不会因为重启而再次提交 clone/reboot。

如果请求可能已发送但任务 ID 未保存，返回 unknown 及已知外部身份。插件可查询并补充执行事实，但不自行认定应该重做、补偿或需要业务确认；下一条操作由调用方决定。

批量观察可共享 domain 的连接，输出按 resource_id 对应的状态。连接失败仅影响该域；不选择别的服务器继续查询同号 VM。

unregister 只关闭登记，保存历史绑定以满足记录完整性。物理删除需要明确的 delete 指令。平台本身由外部部署，首版没有销毁整个平台的动作。

## 现有实现需要调整之处

`PVEClient.clone_template()` 当前丢弃 clone 任务返回值；启停也提前返回完成消息。驱动需要保留 UPID 并区分提交与完成。

`release_vm()` 当前包含先停止运行 VM 的逻辑，不能直接用作新 delete 的透明适配。新驱动实现直接删除调用；原业务若需要原来的停止→删除行为，应在外部编排模块分别发送两条指令。

## 验收

- 两平台同 node/VMID 的绑定、查询、操作独立。
- 同平台多连接按明确 domain 映射解析同一 VM。
- create 不自行启动，delete 不自行关机或扫描其他资源。
- VMID 冲突/平台锁/权限错误按后端结果返回，不推导业务解释。
- clone 未完成不配置新 VM；部分成功可见，无自动回滚。
- 没有计划 ID、业务归属或依赖图时仍可调用这些动作。
