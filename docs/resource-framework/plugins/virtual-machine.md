# 虚拟机插件设计

插件 ID：`virtual_machine`；资源类型：`compute.vm/v1`。遵循[资源执行层契约](../architecture.md)。

## 定位

定义通用 VM 的属性、状态、动作名称和驱动接口。PVE 专属参数和 API 调用交给 [PVE 驱动](pve.md)。一台 PVE VM 只有一个 compute.vm 资源记录。

插件只处理收到的 VM 指令，不查询教师/学生、K8s 成员、实验环境依赖或使用者。client/master/worker 角色可以由调用方保存在不透明 metadata，插件不解释。

## 创建输入与属性

| 字段 | 说明 |
|---|---|
| driver_id | 首版 pve.qemu/v1 |
| connection_id | 明确的虚拟化平台连接 |
| name | 外部 VM 名称，作为属性而非身份 |
| cpu.cores | 正整数 |
| memory_mib | 正整数，单位 MiB |
| nics | 调用方给出的 MAC、bridge/VLAN、地址模式等 |
| initialization | 调用方指定的用户名、公钥、DNS/cloud-init 字段 |
| provider_options | 驱动 schema 声明的 node、template_vmid、vmid、storage、clone_mode 等 |

参数按插件/驱动 schema 校验类型和编码。MAC/IP/VLAN 分配、容量配额、模板选择、网络是否连通由其他模块处理；缺少驱动必需值则返回 InvalidParameters。

VMID、当前 node 和外部连接保存在绑定中。记录元数据更新不等于外部 VM 配置变更；改变外部配置必须明确调用 configure。驱动不支持的字段返回 UnsupportedParameter，不静默丢弃。

## 动作

| 动作 | 输入 | 插件执行内容 |
|---|---|---|
| create | 完整创建参数 | 调用驱动创建这一台 VM，返回 VMID/绑定和外部任务 |
| observe | 当前绑定 | 查询实际电源、配置和当前定位 |
| start | 可选后端参数 | 提交指定 VM 的启动命令 |
| stop | mode=graceful/force | 按指定模式提交关机/强停，不自行切换模式 |
| reboot | 可选后端参数 | 提交一次重启命令 |
| configure | 明确字段 | 更新这一台 VM 的指定配置 |
| delete | 明确后端删除参数 | 删除这一台 VM；平台拒绝则原样报告 |

create 不隐式开机。delete 不隐式删除网络、K8s 或其他 VM，也不检查它们是否正在使用此 VM。当前设计不包含 stop_first 复合参数；需要先关机时由调用方先发送 stop 并等待结果。

观察是状态查询，不是业务准入判断。驱动可以按平台协议查询状态确认动作结果，但不能因此添加“正在使用，不允许关机”等业务规则。平台原生限制仍由平台返回。

## 公共驱动协议

协议位于 `resource_framework/contracts/compute.py`：

```python
class VirtualMachineDriver:
    def describe_actions(self) -> list[ActionDescriptor]: ...
    def normalize_identity(self, connection, locator) -> ExternalIdentity: ...
    def discover(self, ctx, connection, cursor=None) -> DiscoveryPage: ...
    def observe(self, ctx, target) -> VmObservation: ...
    def execute(self, ctx, target, action, parameters) -> ExecutionResult: ...
    def poll(self, ctx, external_task_ref, exec_data) -> ExecutionResult: ...
```

通用插件规范化动作和结果，具体驱动提供平台字段 schema。协议不接受业务策略、关系图或其他资源服务的自动调用入口。增加其他虚拟化平台只需注册兼容驱动。

单条创建动作内部如需等待 clone 再配置该 VM，可以由驱动保存执行位置；这是底层 API 的执行细节，框架不建模跨资源步骤依赖。

## 状态与结果

VmObservation 包含 power_state、实际 CPU/内存、当前 node/locator、observed_at、查询错误。指令结果保存后端任务状态，不能仅凭请求提交就更新 power_state=running。

failed/unknown 及部分创建结果交给调用方；插件不自动删除创建了一半的 VM，不自动重新克隆。调用方明确发 configure、delete 或新的 create 请求时再执行。

## 当前代码接入

- VM routes 先由应用鉴权/投票，再调用 ResourceService。
- `_check_vm_access()` 和业务 Cluster 查询仍在应用层。
- 旧 `Vm` 可持有 resource_id FK，资源属性由框架维护，业务归属由原模型维护。
- `k8s_manager.py` 的 VM 创建/回滚循环仍由业务模块决定，只把实际动作替换为框架调用。
- 三个页面按 resource_id 定位状态；用户可见范围由应用控制。

## 验收

1. 两域同 node/VMID 分别执行，无跨域状态覆盖。
2. 不传角色、集群、投票或依赖信息也能执行技术上有效的内部命令。
3. VM metadata 带有 K8s/共享使用标记时，插件不据此拒绝 stop/delete。
4. stop(graceful) 不变成 force；delete 不隐式 stop；后端错误如实返回。
5. create 部分失败保留外部 ID/结果，不自行清理或重试。
