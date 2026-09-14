# P0 运行与持久化契约

本文补充 [contracts.md](contracts.md) 中必须由数据库和 repository 强制执行的首版参数。实现不得把这些默认值解释为自动补偿、自动自愈或跨资源工作流。

## 写入 owner

| Repository | 可写列 | 禁止写入 |
|---|---|---|
| Environment API | metadata、spec、owner/scope、authorizationRef、generation、deletionTimestamp、finalizers、resourceVersion | status、observedGeneration |
| Environment Controller | status、conditions、observedGeneration、finalizers、resourceVersion | spec、generation、owner |
| Scheduler | Placement、Allocation 全部业务列及其 resourceVersion | Environment spec、Resource/Operation 技术事实 |
| Plan Controller | 新 PlanRevision immutable spec；既有 PlanRevision phase/status | 既有 PlanRevision spec |
| Operation admission | normalized input、serverScope/requestId/digest、固定 target snapshot、初始 Pending | 执行结果与 lease |
| Executor | Operation lease/phase/externalTaskRef/结果/错误/远端作业字段；Resource/Binding/Observation 技术事实 | Environment 任意列、Placement、Allocation、Plan spec |

所有 repository 接收调用方事务，不自行 commit。对象和对应 outbox 必须在同一事务写入；失败同时回滚。

## 版本与并发

- `resourceVersion` 来自数据库全局单调序列；任意对象变更都取新值并写 outbox。LIST 按 `(resourceVersion, uid)` 排序，WATCH 游标是最后确认的 resourceVersion；保留窗口外游标返回 `410 ResourceVersionExpired` 并要求重新 LIST。
- spec 规范化语义摘要变化时 `generation += 1`；相同语义的重放不递增 generation，但仍可因 metadata 变更递增 resourceVersion。
- API spec 更新必须带 expected resourceVersion/If-Match，比较更新失败返回 409。
- Controller 只能写 `observedGeneration <= generation`。仅当当前 generation 的全部必需 reconcile 判定完成时追平；Pending/Unknown、授权阻塞或清理未确认时不得提前追平。

## Operation 与资源状态

`serverScope` 由服务端按租户/主体/入口产生，客户端不能覆盖。相同 `(serverScope, requestId, requestDigest)` 返回原 Operation；同 scope/key 不同 digest 返回 409。计划 requestId 固定为 `environment/{uid}/intent/{generation}/plan/{planUid}/item/{itemKey}/attempt/{n}`。

Operation 在提交前固定 resource、binding/revision、domain、connection/revision、secret version、plugin/driver version 和 normalized input。Executor 用 `leaseOwner + leaseUntil + claimRevision` 领取；每次成功领取递增 claimRevision，全部后续更新比较 owner/revision，旧 claimant 更新为零行即 `LeaseLost`。已保存 externalTaskRef 只 poll；可能已提交但未保存外部标识进入 Unknown，不自动重发。

create admission 同事务预分配 Resource 与 provisional Binding，初始 existenceState=pending。确认创建后转 present/provisional=false；确认未创建转 absent；无法确认转 unknown。unknown 不能释放 allocation。Resource 登记状态 active/closed 与 existenceState 分离。

## 删除与 finalizer

Environment 创建时加入 `lab.platform/environment-cleanup`。DELETE 只设置 deletionTimestamp。关联记录保存 `origin=created|adopted` 和 `cleanupResponsibility=delete|unregister|retain`；adopted 默认 retain。删除/注销确认、Unknown 核对、关联关闭和 allocation release 全部完成后才可移除 finalizer。

break-glass 仅管理员专用运维入口可执行，要求二次风险确认、原因、操作者、correlationId 和删除前事实快照进入安全审计；普通对象 API 不提供此能力。强制移除不伪造外部对象已删除。

## Reconcile 与并发默认值

退避为 `min(maxBackoff, base * 2^attempt)` 并施加 `±20%` jitter；对象 resourceVersion 前进或人工恢复后 attempt 清零。通知只负责唤醒，以下 full resync 是正确性路径：

| Controller | reconcile key | base/max | full resync | 最大并发 |
|---|---|---:|---:|---:|
| Environment | `Environment/{uid}` | 1s/300s | 60s | 4 |
| Scheduler | `Environment/{uid}/{schedulingInputDigest}` | 1s/60s | 60s | 2 |
| Executor | `Operation/{uid}` | 1s/300s | 30s | 8 |
| Observation | `Resource/{uid}/{bindingRevision}` | 2s/300s | 300s | 8 |
| Finalizer | `Environment/{uid}/deletion` | 2s/600s | 60s | 2 |

默认值持久化于 `cp_controller_settings`；进程环境覆盖必须通过范围校验并记录启动配置。

## OpenWrt 与 K8s 协议

OpenWrt 写操作以 `(domainId, lockClass='openwrt-uci-write')` 领取 `rf_domain_locks`，字段为 leaseOwner、leaseUntil、fencingToken。每次领取递增 fencingToken，写回比较 token。首版一条 Operation 内完成 set/commit/apply，禁止 `apply_mode=none`，锁不表达跨 Operation 候选配置。

K8s 远端 job id 由 `sha256(operationUid + requestDigest)` 生成并使用固定目录。Operation 持久化 `remoteJobId`、`remoteStatusPath`、`remoteExitPath`、`remoteLogPath`；状态文件原子 rename，包含 jobId、phase、startedAt、updatedAt、pid、commandDigest，退出文件包含 exitCode/finishedAt。重连先核对 commandDigest，再读取状态/退出/日志游标；不以 SSH 连接断开判定失败，也不重发未知作业。

## 日志与保留

- `cp_logs.id` 是不透明游标；每 chunk 最多 64 KiB，每个 Operation/Task 最多 16 MiB，超限写明确 truncation 事件。
- chunk 入库前必须经过现有流式 `SecretTextSanitizer`；凭据、私钥、kubeconfig、authorizationRef、完整 target snapshot 不进入普通日志或事件。
- Operation/Task 日志默认保留 30 天；结构化 Operation、outbox 和对象历史不随日志清理。安全审计默认保留 365 天，普通用户不可删除。策略保存在 `cp_retention_policies`，缩短保留期属于管理员审计事件。
