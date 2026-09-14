# 通用实验平台控制平面设计与接手指南

状态：控制面设计基线，尚未实现。版本：0.2。日期：2026-09-04。

本文档面向后续开发与维护人员，记录从当前 K8s 专用应用演进为通用实验平台的目标设计。目录、对象、表名、API 和模板格式均为目标契约；当前代码现状见[实施、迁移与交接计划](implementation-plan.md)。文档存在不代表功能已经可用。

当前实现基线已包含统一授权、凭据加密、脱敏审计和受约束的 Job 安全重试；Environment、Scheduler、Controller、PlanRevision、持久化 Operation 与资源框架仍待实现，不能把两部分混作已经完成或全部未完成。

## 1. 目标模型

平台借鉴 Kubernetes control plane 的运行机制，而不是复制组件名称或部署形态：

1. Platform API Server 只负责对象入口、认证授权、校验、准入、CRUD、LIST/WATCH 和响应，不在 HTTP 请求中调用规划器或外部平台。
2. PostgreSQL 是控制面对象和状态的唯一事实来源；事务通知只降低延迟，定期全量扫描保证正确性。
3. Scheduler 只做 placement、allocation、reserve 和 bind，不执行资源命令。
4. Controller Manager 持续读取 spec/status，生成不可变 PlanRevision 和必要的一次性 Operation，更新 conditions，并通过 finalizer 完成可靠清理。
5. Resource Executor 只领取持久化 Operation，通过资源框架及插件产生真实外部副作用；一次性动作不会因为 reconcile 被重复发送。

首版仍是模块化单体：一个 API 进程和一个 control-plane worker 进程，共用 PostgreSQL，不要求部署 Kubernetes、etcd、消息总线或微服务。

## 2. 五个控制面角色

| 角色 | 负责 | 不负责 |
|---|---|---|
| Platform API Server | AuthN/AuthZ、请求规范化、schema/admission、对象 CRUD、spec 更新、status 展示、LIST/WATCH、CSRF/Origin、审计 | 选择 PVE/node、生成步骤、等待部署、直接调用插件 |
| PostgreSQL | Environment、Placement、Allocation、PlanRevision、Resource、Operation、Task View、resourceVersion、lease、outbox | 通过数据库触发器执行业务或外部命令 |
| Scheduler | Filter/Score/Reserve/Bind，分配 domain/node/VMID/IP/VLAN/端口 | clone VM、配置 OpenWrt、安装 K8s、生成业务补偿 |
| Controller Manager | Environment/Plan/Resource/Allocation/Observation/Finalizer reconcile，比较 spec/status，创建确定性 Operation | 直接调用 PVE/UCI/SSH，不重放一次性命令，不绕过授权闸门 |
| Resource Executor | 领取 Operation、冻结目标、解析凭据、调用资源框架和插件、轮询外部任务、保存事实 | 业务授权、placement、跨资源依赖、自动补偿、教学投票 |

课程、班级、组、关机投票和 WebSSH 接管规则继续由 API 下的应用适配组件处理。它们可以更新对象 spec 或创建受控 Operation，但不进入资源插件。

## 3. 核心对象

| 对象 | 类型 | 作用 |
|---|---|---|
| Environment | 声明式 | 用户期望的实验实例，采用 metadata/spec/status、generation、observedGeneration 和 conditions |
| Placement | 声明式 | Scheduler 对站点、domain、connection、node 和资源位置的绑定决定 |
| Allocation | 声明式 | VMID、IP、VLAN、端口等占用；unknown 外部结果期间不得释放 |
| PlanRevision | 不可变计划 | 固定模板/预设/placement 后的实例化方案；新规划产生新 revision，旧版保留并标记 superseded |
| Resource | 声明式记录 | 外部对象身份、绑定、已知配置和观察状态；业务归属不在资源核心 |
| Operation | 命令型 | reboot、clone、delete、reload、deploy 等一次性副作用的唯一执行事实 |
| Task | 投影视图 | 面向用户聚合一次请求涉及的计划、资源和 Operation，不是控制面调度核心 |

Environment、Resource、Allocation 等适合通过 spec/status reconcile。reboot、重装、reload 等一次性动作必须建模为 Operation，禁止使用会被控制循环反复解释的 `spec.reboot=true`。

## 4. 首版控制循环

```text
用户更新 Environment.spec
          ↓
API 事务提交并返回 202
          ↓
Scheduler 发现未绑定 generation，保存 Placement/Allocation
          ↓
PlanController 生成不可变 PlanRevision
          ↓
EnvironmentController 比较 plan 与当前 status，创建确定性 Operation
          ↓
Resource Executor 执行并更新 Operation/Resource.status
          ↓
EnvironmentController 更新 conditions 与 observedGeneration
```

组件通过数据库对象协作，不存在“编排生成文件后回调 API，再由 API 分发执行”的跨进程调用链。通知丢失时，controller 依靠定期 LIST/resync 再次发现未收敛对象。

首版采用受控 reconcile，不默认自愈所有漂移：已经 Ready 的外部资源被人工删除时，Environment 标记 Drifted/Blocked，默认 `recoveryPolicy=retain_and_block`，不会静默重建。管理员明确更新 generation 或发出恢复请求后才能产生新的 PlanRevision/Operation。

## 5. 阅读顺序

| 文档 | 阅读目的 |
|---|---|
| [整体架构与控制循环](architecture.md) | 理解五个角色、对象流、spec/status、watch/resync 和首版部署 |
| [数据、API 与并发契约](contracts.md) | 实现对象表、状态转换、授权闸门、lease、去重、finalizer 和恢复 |
| [P0 运行与持久化契约](p0-runtime-contracts.md) | 核对 repository owner、并发默认值、domain lock、远端作业和日志保留参数 |
| [实验模板与扩展机制](templates.md) | 实现模板发布、Scheduler 输入、PlanBuilder、PlanRevision 和访问入口 |
| [Python 模板示例](examples/python-development.json) | 阅读单 VM 纵向切片；示例不是当前可执行配置 |
| [实施与交接计划](implementation-plan.md) | 按阶段推进并核对当前代码、迁移和验收 |
| [资源框架总览](../resource-framework/README.md) | 理解 Resource Executor 下方的独立资源执行库 |
| [资源框架与插件契约](../resource-framework/architecture.md) | 实现 resource_id、绑定快照、Operation 和插件协议 |

## 6. 平台层与资源层的边界

Environment、Placement、Allocation、PlanRevision、finalizer 和教学归属属于控制面。Resource、Binding、Observation 与单条 Operation 的技术执行属于资源执行层。Resource Executor 是资源框架的宿主；API 直接资源命令也先持久化 Operation，不同步调用插件。

控制面决定是否需要创建、停止或删除某个资源；资源框架只判断指令能否解析、目标能否唯一定位、动作是否受支持，并如实保存外部事实。不能把投票、权限、跨资源顺序、自动补建或级联清理藏入插件。

## 7. 首版完成标准

1. 复用现有授权、凭据加密和审计，保留课程/组权限、WebSSH 和关机投票行为。
2. 修复多 PVE 平台相同 node/VMID 的定位问题；过渡期 `(pve_server_id,vmid)` 唯一，目标态 `(domain_id,vmid)` 唯一，node 仅为 locator。
3. API 只提交对象；进程重启或通知丢失后，Scheduler/Controller/Executor 可由 PostgreSQL 恢复。
4. 每个外部副作用均有持久化 Operation、稳定请求键、固定 binding/connection/plugin 快照和明确的 unknown 语义。
5. K8s 实验通过 Environment、PlanRevision 和 finalizer 创建、观察、停止与销毁。
6. 新增 Python 模板及预制镜像即可提供 Python 环境，无 Python 专用路由、表或页面分支。
7. 删除时 finalizer 在外部结果和 allocation 释放全部确认前保持；数据库状态不能伪装成已经删除。

## 8. 后续扩展而非首版前提

多 worker 高可用、自动自愈、复杂滚动更新、任意 DAG、用户脚本、第三方插件隔离、跨站点调度和完整事件流均可后续增加。首版先建立可靠对象状态、确定性 reconcile 和一次性 Operation 边界。
