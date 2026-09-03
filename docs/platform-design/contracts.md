# 数据、接口与任务契约

返回[接手指南](README.md)。状态：接口提案，待实现。资源侧详细契约见[资源框架架构](../resource-framework/architecture.md)。

## 1. 标识与版本

| 标识 | 语义 |
|---|---|
| resource_id | 资源框架的 UUID，不能用裸 VMID 替代 |
| domain_id / connection_id | 外部身份作用域 / 连接端点与凭据引用 |
| environment_id | 通用实验实例 UUID，不等于 K8s 集群 ID |
| template_id + version | 不可变的已发布实验模板版本 |
| profile_id + revision | 管理员部署预设的固定版本 |
| task_id / step_id | 编排生命周期任务 / 已展开步骤 |
| operation_id | 一条资源命令的持久化记录 |
| request_id | 调用者范围内的请求去重键 |
| correlation_id | 跨 API、任务、步骤和资源操作的追踪标识，不授予权限 |

所有状态时间使用 UTC 存储，前端转换时区。可变记录使用 revision 做乐观并发控制；模板内容版本、环境 revision 和观察时间是不同概念。

## 2. 数据归属

以下表名为设计名称；首版可与旧业务表共用数据库，但由所属服务负责写入。

| 表/对象 | 关键字段与约束 | 所属模块 |
|---|---|---|
| users、roles、sessions 等 | 沿用或适配当前身份/角色数据 | 鉴权 |
| classes、groups、votes 等 | 课程、成员、投票及业务关联 | 应用适配 |
| orch_templates | template_id PK、名称、描述、展示信息、归档状态 | 编排/模板 |
| orch_template_versions | (template_id,version) 唯一、内容、摘要、发布状态、发布者、发布时间 | 编排/模板 |
| orch_profile_versions | (profile_id,revision) 唯一、连接/镜像/池引用、规格约束、摘要 | 编排/预设 |
| orch_environments | UUID PK、模板版本 FK、预设版本 FK、参数快照、owner/scope 引用、目标状态、phase、revision、outputs、conditions | 编排/环境 |
| orch_tasks | UUID PK、environment FK、生命周期动作、计划摘要、请求摘要、状态、创建主体、执行范围、lease_owner/lease_until/claim_revision、时间 | 编排/任务 |
| orch_task_steps | UUID PK、task FK、稳定逻辑键、顺序号、类别、固定输入、attempt、state、operation_id、结果/错误、时间 | 编排/任务 |
| orch_environment_resources | environment FK、logical_key、resource_id、created/adopted、所属创建任务、删除责任、登记状态 | 编排/关联 |
| orch_allocations | scope、kind、value、environment/task、reserved/assigned/released、revision；有效占用唯一 | 编排/规划 |
| orch_request_keys | 主体范围+入口+request_id 唯一、请求摘要、task/environment 引用 | 编排/受理 |
| rf_* 与插件扩展表 | 资源、绑定、连接、观察、单条操作；沿用已有设计 | 资源框架 |

一个环境的一个 logical_key 同时只能有一个当前资源关联；替换保留历史。分配作用域分别采用 PVE domain（VMID）、网络池（IP/子网）、路由器或网络域（VLAN/端口），不能全局混用。

模板版本、预设版本、计划快照及已执行步骤的输入不可原地修改。环境详情可以显示模板的新版本，但不能自动把旧环境的销毁流程切换到新版。

业务归属由环境/应用模型提供，rf_resources.metadata 中同名信息只能是检索投影。直接导入且不属于环境的资源，其访问范围由应用侧资源授权记录管理。资源层不存储业务权限。

## 3. 受理实验请求

拟新增 API（以下接口均不是已存在路由）：

```http
POST /api/environments
Content-Type: application/json
```

```json
{
  "template_id": "python-development",
  "template_version": "1.0.0",
  "profile_id": "teaching-lab",
  "profile_revision": 1,
  "parameters": {"name": "python-demo", "cores": 2, "memory_mib": 2048},
  "request_id": "create-python-001"
}
```

沿用现有 Session、CSRF 和 Origin 规则：浏览器先从 `/api/csrf-token` 获取令牌，变更请求携带 `X-CSRFToken`，Origin 按 `K8S_LAB_ALLOWED_ORIGINS` 校验。上面的 JSON 只展示业务载荷，不表示新路由豁免现有入口保护。

认证主体、业务归属授权和内部 caller_ref 由服务端建立。请求可以指定目标课程/组，但必须通过真实成员关系校验，不能据此直接获得权限。

通过授权和模板参数校验后，在同一数据库事务中写入 Environment、Task、去重记录，再返回：

```json
{
  "environment_id": "11111111-1111-4111-8111-111111111111",
  "task_id": "22222222-2222-4222-8222-222222222222",
  "state": "queued",
  "status_url": "/api/tasks/22222222-2222-4222-8222-222222222222"
}
```

HTTP 状态为 202；这只表示任务已持久化受理。规划或部署仍可能失败。事务未提交不得返回已受理，也不得先触发外部创建。

同一主体+入口+request_id 的相同规范化请求返回同一结果，正文不同返回 409 RequestConflict。正文摘要包含固定模板/预设版本和参数，不包含会话令牌或生成时间。

## 4. API 分工

| 接口 | 处理服务/语义 |
|---|---|
| POST /api/auth/login；POST /api/auth/logout | 鉴权模块的登录/登出适配 |
| GET /api/me | 当前身份与前端可用权限信息 |
| GET/POST /api/lab-templates | 已授权模板目录 / 创建模板草稿 |
| GET/PUT /api/lab-templates/{id}/versions/{version} | 读取 / 修改未发布草稿；修改需预期 revision |
| POST /api/lab-templates/{id}/versions/{version}/publish | 校验并发布不可变版本 |
| GET/POST /api/deployment-profiles | 查询 / 新建部署预设；修改形成新 revision |
| GET/POST /api/environments | 查询可见实例 / 受理创建 |
| GET /api/environments/{id} | 环境、资源摘要、任务状态和访问入口 |
| POST /api/environments/{id}/actions/{action} | start/stop/delete 等模板已声明生命周期；生成 task |
| GET /api/tasks/{id}；GET /api/tasks/{id}/logs | 编排任务和经授权的日志，支持游标 |
| POST /api/tasks/{id}/cancel | 请求停止后续调度，不等于回滚 |
| POST /api/tasks/{id}/resume | 解除可恢复阻塞；校验 revision，记录处理依据，不盲目重发未知命令 |
| GET /api/resource-types；GET /api/resources | 类型能力 / 可见范围内的资源列表 |
| POST /api/resources；POST /api/resources/register | 创建外部资源 / 登记已有资源 |
| POST /api/resources/{id}/actions/{action} | 明确资源命令；返回 operation_id |
| GET /api/resource-operations/{id} | 单条资源操作结果，与 Task 区分 |

资源 API 的 observe/unregister/cancel 等补充接口沿用资源框架设计。旧路由保留适配期，但相同类型只能有一个实际执行路径。

未登录为 401，权限不足为 403，未找到为 404，版本/身份/去重冲突为 409，格式或参数不符合 schema 为 400/422，必要插件暂不可用为 503。列表逐范围过滤；批量对象逐项授权；任务和操作 ID 本身不是访问凭证。

错误结构至少含 code、中文 message、correlation_id、可公开 details。外部错误可带 provider_code。敏感连接参数、密码、私钥和完整命令中的凭据不进入普通响应或日志。

## 5. 鉴权契约与执行范围

```text
authenticate(credentials_or_session) -> Principal | AuthenticationError
authorize(principal, action, target, scope, parameters) -> Decision
Decision = {allow, reason_code, visible_scope?}
```

现有 `authz.is_allowed/require_allowed` 与 `security_service` 是适配基线，以上接口为未来统一外观，不要求另建一套权限矩阵。

Principal 与目标归属从服务端可信信息构造。visible_scope 用于生成资源查询过滤或环境查询条件，不把未经验证的客户端过滤器当成授权范围。

实验创建被允许后，服务端生成限定于该环境、固定模板/预设和计划步骤的内部执行范围。用户只需拥有相应环境生命周期权限，不必同时拥有平台原始命令权限。编排适配在每次发送新的资源命令前核对执行范围以及发起主体当前是否仍有该生命周期权限；主动系统任务使用明确的服务身份和策略范围。

权限撤销或执行范围失效时，任务进入 blocked，reason=AuthorizationRevoked，停止提交新命令；已被外部平台接受的命令仍由资源执行器跟踪事实，不承诺撤回或回滚。恢复必须重新授权。凭据读取位于宿主连接适配中。

这些规则位于宿主鉴权/编排适配层。ResourceService 的输入仍不含角色、投票许可或授权回调。

## 6. 规划与分配

规划器读取固定版本及资源观察结果，按已注册的简单规划规则绑定 domain、connection、node、镜像和网络资源池。查询外部候选是读操作；真正的创建必须成为计划中的资源命令。

在短事务中为分配记录加唯一约束/行锁，保存计划和预留结果。不要持有数据库事务等待 PVE 或 SSH。平台外部管理员仍可能同时分配同一 VMID；数据库预留不等于外部平台锁，冲突应由平台返回并在编排层显式重新规划。

已发送可能产生资源的命令后，不能因为任务超时或 lease 到期就释放 VMID/IP。需确认未创建或已删除才释放；unknown 保留分配并记录待处理原因。借用的共享资源不取得默认删除责任。

## 7. 三层状态

| 层 | 状态 | 含义 |
|---|---|---|
| Environment | pending/provisioning/ready/stopped/deleting/deleted/error | 实验生命周期，带 conditions 与 observed_at |
| Task | queued/planning/running/waiting/blocked/succeeded/failed/cancelling/cancelled | 一次生命周期流程 |
| Operation | queued/running/pending/succeeded/failed/cancelled/unknown | 一条资源命令，沿用资源契约 |

步骤使用 queued/running/waiting/succeeded/failed/blocked/cancelled，具体命令结果通过 operation_id 查询。创建任务只有完成模板要求的就绪检查后，才能将环境标为 ready。K8s 安装退出成功并不自动满足节点 Ready 条件。

Task failed/cancelled 不表示所有资源已删除；Environment error 记录失败原因、已存在资源和待处理操作。delete 流程只有完成负责清理的资源确认及分配释放后才标 deleted。历史记录不随外部对象删除而消失。

## 8. 持久化执行与去重

1. TaskController 从数据库领取可推进任务，保存 lease_owner、lease_until 和递增 claim_revision；状态更新使用领取版本条件写入。
2. 规划后的逻辑步骤 ID、输入和 attempt 固定保存。调用资源服务时使用 caller_ref=orchestration，以及由 task_id/step_id/attempt 构成的稳定 request_id。
3. 资源服务在事务中保存 Operation 和可领取状态，再异步提交外部动作。事务外执行网络调用。
4. 编排保存 operation_id。若在资源受理后、保存关联前崩溃，用同一请求键再次调用可取回相同 Operation；这不等于重新执行后端命令。
5. pending 操作按已保存 external_task_ref 轮询；编排 waiting 定期查询结果并释放调度线程。
6. 只有当前步骤确认成功才推进后续步骤；失败或未知不推断成功。

请求去重记录至少保留到任务及其恢复/审计保留期结束；历史去重键不能在仍可重试时删除后重新执行。首版不承诺跨数据库和外部平台的 exactly-once。

lease/claim_revision 能防止旧 worker 覆盖数据库状态，不能阻止已经在途的网络调用。领取超时不证明旧 worker 未提交；对可能已经提交的变更标记 unknown 并核对事实，不能只因换了 worker 就重发。扩展 worker 时须同步实现资源 Operation 领取与 OpenWrt domain 互斥。

## 9. 恢复与失败决策

| 场景 | 处理 |
|---|---|
| API 在受理提交后断连 | 客户端用相同 request_id 取得原 task |
| Task 已保存但进程内唤醒丢失 | worker 定期扫描数据库继续领取 |
| Operation queued，尚未进入提交阶段 | 正常领取 |
| 已保存 PVE UPID 或远端安装作业 ID | 恢复查询，不再次启动操作 |
| 可能提交但未保存外部任务 ID | Operation unknown，Task blocked；查询身份/外部事实 |
| 外部创建成功，后续配置失败 | 保存已存在对象及部分结果；编排决定补配置或清理 |
| worker 租约丢失 | 拒绝旧领取者写状态；未知提交按 unknown 处理 |
| 插件版本缺失或不兼容 | 停止推进并保留历史，不能用不兼容驱动重放 |
| PostgreSQL 不可用 | 不受理新的变更、不提交未持久化命令；已有外部作业可能继续运行 |

当前应用已经提供 `/api/k8s/tasks/<task_id>/retry`，其安全重试限制见 [Job 安全重试](../job-retry.md)。它仍使用进程内记录；这里的持久化 Task/Operation 恢复为后续目标，迁移时保留现有授权、一次直接重试子任务及拒绝重放未确认 create 的约束，不将既有 retry 等同于通用 resume。

首版失败策略为保留资源并停止后续步骤。补偿是编排显式记录的新步骤/清理任务，不属于资源框架。失败的外部命令重新执行时分配新 attempt/request_id；读取旧操作结果和恢复轮询继续使用原记录。

resume 用于继续已确认可继续的任务，不允许把用户提交的 succeeded 字段当成外部执行事实。未知结果应通过查询得到足够证据并记录；无法确认时继续 blocked，由有权限的人员明确制定后续清理/重建操作。

cancel 先进入 cancelling，停止调度新步骤，并尝试取消支持取消的操作。无法取消的在途操作继续跟踪；结果确认后任务才结束为 cancelled。存在无法确认的在途结果时保持 blocked 并保留 cancel_requested，不报告已全部停止。取消不释放仍可能被资源使用的分配。

## 10. 事件与可观测性

持久化 task/operation 是状态事实来源。首版以前端轮询为完整路径，Socket.IO 仅用于提示刷新，断线重连后重新读取状态，不要求消息总线或完整事件溯源。

每个步骤关联 correlation_id、task_id、step_id、operation_id、resource_id 和适用的 external_task_ref。日志只输出必要且脱敏的错误和进度，复用现有 `modules/audit.py` 的安全审计与流式脱敏；凭据存储和迁移沿用[现有凭据设计](../security-credentials.md)。状态写入提交后才发通知，通知失败不改变操作结果。

状态缓存统一按 resource_id 定位，并附 observed_at/stale；过渡期使用完整服务器/node/VMID。禁止仅凭 VM 正在 running 就认定上一次 reboot 已执行完成。
