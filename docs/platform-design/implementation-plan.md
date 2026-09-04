# 实施、迁移与交接计划

返回[接手指南](README.md)。状态：规划，未执行控制面重构或数据库迁移。核对日期：2026-09-04。

## 1. 当前代码与目标差距

以下基于本地源码只读核对，不表示已验证真实部署：

| 当前入口 | 当前情况 | 目标处理 |
|---|---|---|
| `app.py` | Flask 路由已接入授权、CSRF/Origin 与审计，但同步调用客户端和业务 manager | 收缩为 Platform API Server/应用适配，只提交对象或 Operation |
| `modules/authz.py`、`security_service.py` | 已有纯授权策略、主体/对象重载和 worker 再授权 | 复用到 API admission 与 ExecutionAuthorizationGate |
| `audit.py`、`credential_store.py` | 已有脱敏审计、流式秘密抑制、Fernet 和显式迁移 | 作为 API/Executor 宿主适配，增加 versioned secret ref |
| `modules/db.py` | Vm.vmid 全局唯一；find_cluster_by_vm(node,vmid) 缺平台维度 | 先做 P1 完整身份，再建控制面对象和 resource UUID |
| `modules/db.py` | `_create_engine()` 只接受 PostgreSQL且会吞启动错误 | 目标生产进程 fail fast，健康检查区分未配置和连接失败 |
| `modules/k8s_manager.py` | 集中创建/删除/部署；安全重试已存在；任务仍在内存 | 逐步拆为对象 admission、controllers 和持久化 Operation |
| `modules/task_queue.py` | 导入即启动内存 worker，异常被吞 | 改为显式 worker 启动；进程内队列只保存对象 key |
| `modules/status_cache.py` | 缓存缺平台维度，命令提交后会被乐观写状态 | 过渡期完整身份，目标 Resource Observation+observedAt |
| `modules/pve_client.py` | clone 丢弃 UPID，启停提前报告完成，delete 隐式 stop | PVE Executor 保存 UPID、poll；动作单一化 |
| `modules/openwrt_client.py` | 写入立即 commit，delete_interface 隐式改 zone | 拆为明确 Operation；domain 跨进程串行，不支持隐式级联 |
| `modules/ssh_terminal.py` | WebSSH 以用户/Cluster 关联 | 保留接管规则，增加 Environment/Resource 映射 |

目前不存在 Platform API 对象层、Scheduler、Controller Manager、PlanRevision、持久化 Operation 或 resource_framework。`docs/` 中描述的是目标设计。

现有测试覆盖授权、HTTP/Socket.IO、任务安全重试和秘密边界；历史验收记录为 152 项通过。后续每阶段必须重新运行并扩展，不能把历史结果当作当前工作树验证。

## 2. 已确认决策

| ID | 决定 | 说明 |
|---|---|---|
| D01 | 五个逻辑角色：API Server、PostgreSQL、Scheduler、Controller Manager、Resource Executor | 组件围绕持久对象协作，不使用 API→编排→API→执行调用链 |
| D02 | 首版两个物理进程：API + control-plane worker | 保持部署简单；逻辑边界不等于微服务 |
| D03 | Environment 使用 metadata/spec/status、generation/observedGeneration、conditions | 明确期望、处理进度和实际事实 |
| D04 | PostgreSQL 是事实来源；outbox/NOTIFY 只负责唤醒，full resync 保证正确性 | 不引入 etcd/消息总线 |
| D05 | Scheduler 只做 placement/allocation；PlanBuilder 位于 Controller Manager | Scheduler 无外部副作用 |
| D06 | Definition 更名并重定位为不可变 PlanRevision | 重规划产生新 revision，旧版保留 |
| D07 | 所有一次性副作用都是持久化 Operation | 防止 reconcile 重放 reboot/delete/deploy |
| D08 | Task 是用户投影视图，不驱动控制面 | 避免演变为通用 workflow engine |
| D09 | 删除使用 finalizer；unknown 不释放 allocation | 保持外部事实和删除状态一致 |
| D10 | 默认 `recoveryPolicy=retain_and_block` | 首版不静默自愈外部漂移 |
| D11 | 每条新变更 Operation 前实时再授权 | 撤权阻止新副作用，已提交操作继续跟踪事实 |
| D12 | 资源框架及插件只执行固定目标的明确命令 | 权限、投票、placement、补偿留在控制面 |
| D13 | 过渡期 PVE VM 唯一键 `(pve_server_id,vmid)`；node 是 locator | 与目标 `(domain_id,vmid)` 一致，支持节点迁移 |

更新决定时必须同步修改架构、契约、模板、示例、资源框架和验收。

## 3. 分阶段实施

平台阶段使用 P 前缀；资源子项目 M0–M6 见[资源实施计划](../resource-framework/implementation-plan.md)。

| 阶段 | 工作范围 | 完成条件 |
|---|---|---|
| P0 契约固化 | 确认对象 schema、状态转换、repository 写边界、watch/resync、授权闸门、日志/保留策略 | JSON Schema/DDL 草案和状态不变量可审查；无跨进程回调歧义 |
| P1 身份修复 | Vm.pve_server_id FK、`(pve_server_id,vmid)` 唯一、node locator、查询/缓存/页面/权限/投票完整定位 | 两平台同 node/VMID 隔离；同平台节点迁移不产生重复身份 |
| P2 Operation 纵向闭环 | Resource/Binding/Connection、provisional identity、Operation lease/去重/快照、测试插件、显式 worker | 重启可恢复；Unknown 不重发；框架无业务模型也可运行 |
| P3 PVE/VM 接入 | PVE domain、VM driver、UPID、poll、observe、create/start/stop/delete 单一动作 | 真实测试资源的 Operation 状态与 PVE 事实一致 |
| P4 API 对象层 | Environment metadata/spec/status、resourceVersion、generation、admission、outbox、Task View | HTTP 只提交对象；通知丢失不影响恢复；安全回归保持 |
| P5 Scheduler/Plan | Placement、Allocation、Filter/Score/Reserve/Bind、Template/Profile、PlanBuilder/PlanRevision | 无外部副作用完成单 VM 确定性 plan；冲突产生新 revision |
| P6 Controller/Python | Environment/Plan/Observation/Finalizer controller、authorization gate、通用 UI | Python 模板可创建/停启/删除；重复 reconcile 不重复执行 |
| P7 OpenWrt/K8s | domain 锁、UCI 单项动作、远端 K8s job protocol、K8s conditions | 无隐藏跨资源动作；断线/重启可恢复外部作业 |
| P8 K8s 迁移 | 旧 Cluster 映射 Environment，网络/VM/安装 plan，WebSSH/access，旧入口适配 | 原业务行为保留，Task 降为投影，单一执行路径 |
| P9 切换交接 | 数据回填、旧 worker 停止、在途核对、运维/备份/回退 | 无双写双执行，迁移可重跑，runbook 完整 |

P1 可独立发布。P2 先用模拟 handler 验证持久化和恢复，再接真实 PVE。P6 的 Python 单 VM 是首次完整控制面纵向切片；不要先用复杂 K8s 验证基础对象模型。

## 4. P0 必须固化的细节

进入 P2/P4 前至少产出：

1. Environment、Placement、Allocation、PlanRevision、Operation、Task View 的 JSON Schema/DDL；
2. spec/status 列级写入边界和 repository API；
3. generation/resourceVersion/observedGeneration 更新规则；
4. Operation target snapshot、claimRevision、lease 和 request scope；
5. provisional Resource/Binding 与 existenceState；
6. finalizer、cleanup responsibility 和 break-glass 政策；
7. controller reconcile key、退避、full resync 间隔和最大并发；
8. OpenWrt domain 锁与首版禁用 apply_mode=none；
9. K8s 远端 job id/status/exit/log 协议；
10. 日志大小、游标、脱敏、保留和审计保留期。

## 5. 第一个可交付变更：PVE 身份修复

不要一次重写 `app.py`。P1：

1. 在数据库副本梳理 Vm→Cluster→PVEServer 映射，报告 null、server=0、无效和歧义记录；
2. Vm 增加明确 pve_server_id FK；回填后设置非空；
3. 撤销 vmid 全局唯一，建立 `(pve_server_id,vmid)` 唯一，node 保留为可变 locator/index；
4. 修改 find_cluster_by_vm、资源路由、缓存、批量状态、投票、Socket.IO 和页面 DOM identity；
5. 用两个 PVE server 模拟相同 node/VMID；再模拟同一 VM node 迁移，验证身份不变。

同一真实 PVE domain 可能有多个 connection。P1 的 pve_server 是过渡作用域；进入 P3 时必须显式合并同 domain 的连接，不能把每个 URL 当成不同平台。

## 6. 数据迁移与切换

- 使用版本化迁移工具或显式 migration runner，记录 checksum；不再依赖导入模块时的零散 ALTER TABLE。
- 先加新表/可空列和索引，再回填、验证、添加非空/唯一约束；重跑复用 UUID。
- Cluster 保留教学关系，逐步映射 Environment；旧 Vm 持 resource_id FK。
- 旧 PVE/OpenWrt 密文通过 credential_store 读取；先执行显式明文迁移，再建立 versioned secret ref，不复制凭据到 plan/operation 普通 JSON。
- 现存资源使用 register/adopt，不对其执行 create；cleanup responsibility 默认为 retain，除非人工确认。
- 每类资源切换前暂停新变更，处理内存任务、旧 worker、投票计时器和缓存写入者；无法恢复的外部结果人工核对。
- 相同资源类型始终只有一个实际执行路径；旧 API 转为创建新对象/Operation，不能双写或双执行。
- 外部副作用不在数据库事务内；恢复数据库不能撤销外部创建/删除，回退前必须对账。
- 切换 PVE 唯一键后，旧全局 vmid 版本不能无条件回退。

迁移工具必须 fail fast，输出不含凭据。生产 API/worker 启动时数据库配置错误应进入明确不可用状态，不能静默创建无绑定 session。

## 7. 验收矩阵

| 类别 | 必测行为 |
|---|---|
| API | POST/PATCH 只写对象；事务后返回；resourceVersion 冲突 409；status 不可由用户改写 |
| Watch | outbox 与对象同事务；丢通知后 full resync 仍收敛；旧游标重新 LIST |
| Scheduler | Filter/Score/Reserve/Bind 无外部调用；allocation 并发唯一；unknown 不释放 |
| Plan | 相同输入 digest 稳定；冲突产生新 PlanRevision；旧版不可变且可追踪 |
| Reconcile | 重复、并发和重启 reconcile 不重复创建 Operation；observedGeneration 不提前更新 |
| Operation | 固定 binding/connection/plugin；UPID 恢复 poll；未知提交不重发；旧 lease 写入失败 |
| Resource | provisional create、present/absent/unknown 可表达；跨 domain 身份隔离 |
| Finalizer | 删除失败/unknown 时保留；借用资源不删；allocation 确认后才释放 |
| 权限 | 每条新副作用再授权；撤权阻断；学生不能绕过投票；批量/日志/watch 逐范围过滤 |
| 状态 | 命令成功、资源观察和 Environment Ready 分离；K8s 节点 conditions 可诊断 |
| OpenWrt | 写操作跨进程 domain 串行；无 apply_mode=none 泄漏；删除不隐式清理别节 |
| K8s | 远端 job 断线和 worker 重启后继续查询；安装成功不等于节点 Ready |
| 模板 | 发布不可变、引用类型检查、能力缺失可诊断；模板升级不改旧 cleanup recipe |
| 迁移 | 可重跑、歧义停止、无重复外部创建、旧执行路径关闭、回退条件明确 |

技术测试使用模拟平台和独立 PostgreSQL。SQLite 只保留适合的安全单元测试；allocation、partial unique、lease、advisory lock、LISTEN/NOTIFY 和迁移必须用 PostgreSQL 验证。

## 8. 运维与排查

交接至少记录 API/worker 启动方式、controller resync/退避、插件/schema/迁移版本、连接/secret 管理、日志位置、Blocked/Unknown/finalizer 处理和备份恢复。

排查顺序：request/correlation → Environment generation → Placement/Allocation → PlanRevision/item → Operation → Resource/Binding/Connection → externalTaskRef。先核对外部事实，再决定重试、重规划或清理。禁止为了让界面变绿直接改 phase、observedGeneration、finalizer 或删除去重记录。

首版不承诺跨平台原子性、多 worker HA、任意自动补偿或默认自愈。扩大规模前补齐 lease fencing、每资源互斥、OpenWrt domain 锁、secret rotation 和故障注入验证。

## 9. 每阶段交接内容

每次交接注明：完成阶段、对象/schema 版本、修改入口、迁移是否执行、运行验证、真实平台范围、剩余差异和下一项工作。静态 JSON 校验、历史测试记录或模拟 provider 通过，均不能替代对应 PostgreSQL 与真实测试资源的发布验收。
