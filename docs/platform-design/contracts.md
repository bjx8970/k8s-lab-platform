# 数据、API 与并发契约

返回[接手指南](README.md)。状态：接口提案，待实现。资源侧详细契约见[资源框架架构](../resource-framework/architecture.md)。

## 1. 标识、版本与并发字段

| 字段 | 语义 |
|---|---|
| uid | 对象 UUID，删除后不复用 |
| resourceVersion | 任意对象写入后递增的数据库版本，用于乐观并发和 WATCH 游标 |
| generation | spec 发生语义变化时递增 |
| observedGeneration | 对应 controller 已完成处理的 generation；不能大于 generation |
| placement_id / revision | Scheduler 的固定绑定决定及其修订 |
| plan_id / revision | 不可变 PlanRevision；重新规划产生新对象/修订 |
| resource_id | 资源框架 UUID，不能用裸 VMID 替代 |
| domain_id / connection_id | 外部身份作用域 / 访问端点及凭据引用 |
| operation_id | 一次资源命令的持久化事实 |
| task_id | 用户请求的投影视图，不作为执行凭证 |
| request_id | 服务端作用域内的传输去重键 |
| correlation_id | 跨对象追踪标识，不授予权限 |

所有时间使用 UTC。更新 spec 必须携带 expected resourceVersion 或 If-Match；不匹配返回 409。status 使用独立 repository 方法和比较更新，不允许普通客户端写入。

## 2. 对象与数据归属

以下表名为目标设计名称。可以共用 PostgreSQL，但字段写入边界固定。

| 表/对象 | 关键字段与约束 | 主要写入者 |
|---|---|---|
| users、roles、sessions | 沿用当前身份和角色模型 | API/AuthN |
| classes、groups、votes | 课程、成员、投票 | 应用适配 |
| cp_templates / cp_template_versions | 模板、不可变发布版本、摘要、能力要求 | API/模板服务 |
| cp_profile_versions | 不可变部署预设、domain/connection/镜像/池引用 | API/预设服务 |
| cp_environments | uid、metadata、spec、status、generation、observed_generation、resource_version、deletion_timestamp | API 写 spec；Controller 写 status |
| cp_placements | environment、scheduling_input_digest、候选摘要、绑定结果、revision、phase | Scheduler |
| cp_allocations | scope/kind/value、environment、placement、reserved/assigned/quarantined/released | Scheduler/AllocationController |
| cp_plan_revisions | uid、environment、revision、固定输入摘要、spec、status；spec 不可变 | PlanController |
| cp_environment_resources | environment、logical_key、resource_id、created/adopted、cleanup_responsibility、历史区间 | EnvironmentController |
| rf_operations | 一等 Operation 对象：命令 spec、目标/版本快照、状态、lease、外部任务、结果、错误 | Controller/API 经资源服务创建；Executor 写执行状态 |
| cp_task_views | 请求、环境、generation、关联 plan/operation、聚合进度 | API 创建；ProjectionController 更新 |
| cp_request_keys | 服务端主体范围+入口+request_id、请求摘要、结果引用 | API/Operation service |
| cp_outbox | object_kind、uid、resource_version、event_type、投递时间 | 与对象变更同事务写入 |
| cp_conditions | 可内嵌 JSON 或独立表；type 唯一、status/reason/message/transition_time | 对应 Controller |
| cp_logs | operation/task、sequence、脱敏 chunk、时间、保留策略 | Executor/日志适配 |
| 其他 rf_* | Resource、Binding、Connection、Observation、插件描述 | Resource Executor/框架 |

Task View 不拥有步骤执行状态。Plan item、Resource 和 Operation 才是事实；Task 仅聚合展示，删除 Task 不得影响执行。

## 3. Environment 契约

`Environment.spec` 首版包含：

- 固定 templateRef 和 profileRef；升级需要明确的新 generation/策略，不能静默漂移；
- 参数快照；
- desiredState=`running|stopped`；删除使用 deletionTimestamp，不使用 `desiredState=deleted`；
- recoveryPolicy，首版默认 `retain_and_block`；
- owner/scope 引用和由服务端生成的 authorizationRef。

`Environment.status` 包含：

- observedGeneration、phase；
- activePlacementRef、activePlanRef；
- conditions；
- 逻辑资源摘要和访问入口引用；
- 最近错误、drift 和 reconciliationBlocked 原因。

phase 首版为 `pending|scheduling|provisioning|ready|stopping|stopped|deleting|blocked|error`。phase 只是摘要；Ready 必须由 `Ready=True` condition 表达。删除完成后保留 tombstone/历史记录，不把外部对象仍可能存在的环境显示为 deleted。

Condition 至少使用 `Scheduled`、`PlanReady`、`InfrastructureReady`、`NetworkReady`、`SoftwareReady`、`Ready`、`Drifted`、`ReconciliationBlocked`、`Deleting`。每个 type 在一个对象上最多一条当前 condition。

## 4. 创建和修改环境 API

```http
POST /api/environments
Content-Type: application/json
X-CSRFToken: ...
Idempotency-Key: create-python-001
```

```json
{
  "apiVersion": "lab.platform/v1",
  "kind": "Environment",
  "spec": {
    "templateRef": {"name": "python-development", "version": "1.0.0"},
    "profileRef": {"name": "teaching-lab", "revision": 1},
    "parameters": {"name": "python-demo", "cores": 2, "memoryMiB": 2048},
    "desiredState": "running"
  }
}
```

API 依次完成认证、CSRF/Origin、schema、模板/预设存在性、业务归属授权、配额/准入检查，然后在一个事务中保存 Environment、Task View、request key 和 outbox。事务提交后返回 202；不调用 Scheduler、Controller 或外部平台。

```json
{
  "environmentId": "11111111-1111-4111-8111-111111111111",
  "taskId": "22222222-2222-4222-8222-222222222222",
  "generation": 1,
  "resourceVersion": "18372",
  "statusUrl": "/api/environments/11111111-1111-4111-8111-111111111111"
}
```

更新 desiredState 使用 `PATCH /api/environments/{id}` 并携带 expected resourceVersion。删除使用 `DELETE /api/environments/{id}`：API 设置 deletionTimestamp、保留 cleanup finalizer 并返回 202，不同步执行清理。

同一服务端主体/租户+入口+Idempotency-Key 的相同规范化请求返回原结果，不同正文返回 409。resource API 的去重作用域必须由服务端生成，不能使用客户端可伪造的固定 `caller_ref=application-api`。

## 5. API 分工

| 接口 | 语义 |
|---|---|
| POST /api/auth/login；POST /api/auth/logout | 身份入口 |
| GET /api/me | 当前身份和前端权限摘要 |
| GET/POST /api/lab-templates | 模板目录 / 草稿创建 |
| PUT /api/lab-templates/{id}/versions/{version} | 只修改未发布草稿，需 expected resourceVersion |
| POST /api/lab-templates/{id}/versions/{version}/publish | 校验并发布不可变版本 |
| GET/POST /api/deployment-profiles | 查询 / 创建不可变预设 revision |
| GET/POST /api/environments | LIST 可见对象 / 提交新对象 |
| GET/PATCH/DELETE /api/environments/{id} | 读取 / 修改 spec / 设置 deletionTimestamp |
| GET /api/environments/{id}/watch | 从 resourceVersion 开始的授权事件流；首版可退化为轮询 |
| GET /api/tasks/{id}；GET /api/tasks/{id}/logs | 用户视图和游标日志 |
| POST /api/tasks/{id}/cancel | 修改相关 desired intent 或请求取消未完成 Operation，不等于回滚 |
| GET /api/resource-types；GET /api/resources | 能力描述 / 授权范围内的资源 |
| POST /api/resources；POST /api/resources/register | 创建 Resource intent / 登记已有对象 |
| POST /api/resources/{id}/actions/{action} | 经授权创建一次性 Operation |
| GET /api/resource-operations/{id} | 查询单条 Operation；ID 本身不是权限凭证 |
| POST /api/resource-operations/{id}/cancel | 请求取消，是否成功由后端事实决定 |

未登录 401，权限不足 403，不存在 404，resourceVersion/身份/去重冲突 409，JSON 语法错误 400，schema/语义校验失败 422，必要插件暂不可用 503。错误至少含 code、中文 message、correlationId 和可公开 details。

## 6. Scheduler、Placement 与 Allocation

Scheduler 观察满足以下条件的 Environment：当前 schedulingInputDigest 尚无有效 Placement、未处于删除、授权未阻塞、模板与预设可用。仅 desiredState 改变而 digest 不变时复用原 Placement。

调度周期：

1. Filter：能力、镜像、domain、connection、node、容量和预设约束；
2. Score：可用容量、站点偏好和分散策略；
3. Reserve：短事务内用唯一约束/行锁预留 VMID/IP/VLAN/端口；
4. Bind：保存 Placement，更新 Scheduled condition；
5. 失败：保存可诊断 reason，不执行任何资源命令。

VMID allocation 作用域是 PVE domain，过渡期是 pve_server；IP/子网按网络池，VLAN/端口按路由器或网络域。外部管理员仍可能抢占数据库预留值，因此 create 的 provider conflict 必须进入显式重规划判断。

已发送可能产生资源的 Operation 后，Allocation 只能在确认未创建或已删除后释放。Unknown 使用 `quarantined`，不得自动回池。重新规划创建新 Placement/PlanRevision，不修改旧记录。

## 7. PlanRevision 契约

PlanRevision spec 不可变，至少包含：

| 字段 | 语义 |
|---|---|
| environmentRef / sourceGeneration | 所属环境和生成该计划时的 generation；后续仅 desiredState 变化可复用 |
| templateRef / profileRef / placementRef | 固定输入版本 |
| parametersSnapshot | 校验并填充默认值后的参数 |
| resourceIntents | 逻辑资源、类型、驱动、连接、created/adopted 和外部 identity |
| planItems | 有限、确定性的 create/action/register/wait/observe 条目及依赖 |
| readiness | condition evaluator |
| access | 访问入口描述 |
| cleanupRecipe | finalizer 使用的明确逆向清理条目；不等同自动反转 |
| failurePolicy | 首版固定 `retain_and_block` |
| authorizationBounds | 此 plan 可创建的最大操作范围，不是永久授权 |
| contentDigest | 规范化内容摘要 |

status 为 `proposed|active|superseded|blocked`，可变但不改变 spec。一个 Environment 最多一个 active PlanRevision。PlanController 以 planInputDigest 决定复用或新建；仅 desiredState 变化通常复用。运行过的 plan item 输入不能修改；新 attempt 通过新 Operation 表达，重规划通过新 PlanRevision 表达。

运行时 resource_id 通过 `cp_environment_resources` 绑定逻辑槽位，不回写 immutable plan spec。Plan item 的全局稳定身份是 `plan_uid/item_key`。

## 8. Operation 受理、目标冻结与去重

Controller 或直接 API 创建 Operation 时，在同一事务中：

1. 通过 `ExecutionAuthorizationGate`；
2. 生成稳定 serverScope/requestId 和输入摘要；
3. 为 create 预分配 resource_id 和 provisional binding；
4. 固定 bindingId/bindingRevision、domain、connectionId/connectionRevision、secretVersionRef、plugin/driver version；
5. 保存 normalized input、phase=Pending、claimRevision=0 和 outbox；
6. 提交后才允许 Executor 领取。

同一 serverScope+requestId+digest 返回同一 Operation，不同 digest 返回 RequestConflict。对于计划操作，requestId 由 `environment/intentGeneration/plan/item/attempt` 确定；普通用户不能覆盖。

Resource/Operation 的绑定快照是强制内部字段，不能仅依赖调用者可选的 expectedBindingRevision。连接或 binding 在排队期间变化时，Executor 返回 BindingRevisionConflict/ConnectionRevisionConflict，不把旧命令发往新目标。

同一 Resource 默认只允许一个未结束的变更 Operation；OpenWrt 写操作还按 domain 跨进程串行。只读 observe 可并行，但旧 observation 不得覆盖更新 binding 后的新采样。

## 9. Operation 状态和恢复

状态：`pending|running|pending_external|succeeded|failed|cancelling|cancelled|unknown`。

Executor 使用 leaseOwner、leaseUntil、claimRevision 领取；所有状态更新带 claimRevision 条件。网络调用不在数据库事务内。

| 场景 | 处理 |
|---|---|
| Pending 且从未领取 | 正常领取 |
| 已保存 externalTaskRef | 恢复 poll，不再次提交 |
| 可能提交但未保存外部标识 | Unknown，阻止 plan 后续副作用 |
| create 外部 identity 可查询 | 按 provisional binding 核对；证据写入 status |
| 部分成功 | 保存阶段结果、resource existence 和错误，不自动补偿 |
| lease 丢失 | 旧 worker 不得写入；不能据此推断命令未发送 |
| 插件版本不可用 | 保留历史并 Blocked/Failed，不用不兼容版本重放 |
| PostgreSQL 不可用 | 不接受/执行未持久化新命令；已有外部作业可能继续 |

K8s 长时间 SSH 安装必须使用确定性的远端 job id、状态/退出文件和日志定位；仅保持 SSH streaming 连接不满足恢复契约。

## 10. 授权闸门与撤权

Environment spec generation 的 admission 保存创建主体和服务端 authorizationRef。authorizationBounds 只限制 controller 可产生的操作集合，不能单独授权。

每条新变更 Operation 创建前重新加载：主体是否有效、环境/课程/组归属、动作权限、模板/预设范围和对象未被撤权。失败时 Environment condition=`ReconciliationBlocked=True, reason=AuthorizationRevoked`，Plan 不再前进。

已经 PendingExternal 的 Operation 继续由 Executor 以系统身份查询实际结果。恢复授权后由 controller 在新 reconcile 中继续；不得接受客户端提交的 succeeded 或外部结果作为可信事实。

## 11. 删除、finalizer 与所有权

Environment 创建时加入 `lab.platform/environment-cleanup`。DELETE 只设置 deletionTimestamp。FinalizerController 依据固定 PlanRevision cleanupRecipe 和实际环境资源关联创建独立 Operation。

资源关联记录 `origin=created|adopted` 与 `cleanupResponsibility=delete|unregister|retain`。借用资源默认 retain。删除确认、provisional identity 核对、环境关联关闭和 allocation release 全部完成后，才能移除 finalizer。

Unknown、取消未确认、观察 stale 或外部对象仍存在时保留 finalizer，并通过 condition 告知人工处理。管理员强制移除 finalizer 是独立高权限 break-glass 操作，必须显示风险并审计；首版普通 API 不提供。

## 12. LIST/WATCH、事件和可观测性

对象事务和 outbox 同时提交。通知器读取 outbox 后发 LISTEN/NOTIFY 或 Socket.IO 刷新提示；通知失败不改变对象状态。客户端或 controller 从 resourceVersion LIST，再 WATCH；版本过旧时重新 LIST。

首版前端轮询是完整路径，Socket.IO 只提示刷新。Controller 启动和固定间隔执行 full resync，因此 watch 不是正确性依赖。

Operation 记录只保存结构化摘要和日志引用。大量安装日志写入有序、脱敏的 cp_logs 或外部日志存储；每 chunk 使用现有流式 SecretTextSanitizer，设置大小和保留期。凭据、私钥、kubeconfig、服务端 authorizationRef 和完整执行描述不进入普通响应或事件。

排查链路：correlation/request → Environment generation → Placement/Allocation → PlanRevision/item → Operation → Resource/Binding/Connection → externalTaskRef。

## 13. 首版不变量

- API 请求事务未提交前，不返回已受理，也不产生外部副作用。
- spec/status 分写；observedGeneration 不得提前追平 generation。
- Scheduler 不执行资源命令；Controller 不直接调用插件；Executor 不做业务规划。
- 同一确定性 plan item/attempt 最多一个 Operation。
- Operation 固定技术目标后才能执行；unknown 不自动重发或释放 allocation。
- finalizer 未完成时 Environment 不得消失或伪装成已删除。
- Task View、通知、缓存和日志都不是执行事实来源。
