# Execution / Persistence Contract v1

状态：目标契约，尚未实现。相关：[架构](architecture.md)、[插件](plugin-contract.md)、[模板](template-resource-contract.md)、[身份安全](identity-security-contract.md)。

本规范替代旧进程内任务与手动重试说明，不描述现有 task_queue.py 的运行保证。PostgreSQL 负责持久事实，单体进程中的有界线程池负责领取和执行。

## 1. 执行对象与身份

| 对象 | 语义 |
| --- | --- |
| Job | 用户或系统提交的一次流程请求，如创建、资源动作、cleanup、恢复 |
| Step | 模板步骤展开后的具体实例，带稳定 item_key |
| Operation | 一次准备并提交的插件调用事实 |
| ExecutionEvidence | Operation 的检查点、外部引用和晚到结果等追加证据 |
| ConflictProtection | 未决副作用占用的冲突范围，不是 worker 租约 |
| InteractiveSession | 已建立双向通道及其平台身份，详见插件规范 |

用户幂等请求键与请求摘要、逻辑流程调用 ID、步骤实例 ID、Operation ID、外部幂等键分别存储，不能互相替代。相同用户作用域/请求键、相同请求摘要返回原 Job；同键不同摘要返回 409。重试有 parent/retry_of 关联，不接受用户重新提交底层插件/目标来改变原调用。

新 Operation ID 不能让未知旧副作用变成可安全重试。重复派发同一可恢复/幂等调用使用原 invocation_key；显式决定重新实施一个新动作才创建新的调用身份，并保留原事实。

## 2. Job 状态

| 状态 | 含义 |
| --- | --- |
| queued | 已持久化，等待领取 |
| running | 正在推进流程 |
| cancel_requested | 请求取消，外部停止尚未确认 |
| blocked | 因未知结果、插件缺失、代码/目标引用不兼容而阻断 |
| succeeded | 流程完成且结果已持久化 |
| failed | 确认流程失败，不保证无外部产出 |
| cancelled | 确认不再继续，需要停止的执行已经结束 |

合法转换：

```text
queued -> running / cancelled / blocked
running -> succeeded / failed / cancel_requested / blocked
cancel_requested -> cancelled / blocked / succeeded / failed
blocked -> running / failed / cancelled
```

blocked 迁出必须有恢复证据或显式处置，写明操作者、理由和关联 Operation。succeeded/failed/cancelled 是本次 Job 的终态；重试/恢复产生关联 Job，不抹掉历史。直接恢复同一 blocked Job 与创建关联恢复 Job 二选一执行，不允许两条恢复路径同时推进。

cancel_requested 后仍可能成功：远端先完成、取消未生效时保存真实成功和取消结果，不能为满足 UI 状态伪报 cancelled。

## 3. Step 与 Operation 状态

Step：pending、running、waiting_external、blocked、succeeded、failed、cancelled、skipped。

- 依赖满足、输入固定、执行资格有效时，pending 进入 running。
- 同一执行尝试的 Operation 必须与该转换同事务创建。
- 等待外部任务为 waiting_external，未知或不可恢复为 blocked。
- 默认失败停止后继变更并保留现场。skipped 必须说明原因，不能用来掩盖失败。
- 已确认成功的步骤不因恢复而退回 pending；显式重试通过新 Job 记录。

Operation：prepared、running、pending_external、succeeded、failed、cancelled、unknown。

| 转换 | 证据 |
| --- | --- |
| prepared -> running | 持久化派发意图，随后才允许外部调用 |
| prepared -> cancelled | 确认未派发 |
| running -> succeeded | 插件确认成功并通过输出验证 |
| running -> failed | 插件确认失败，仍保存部分引用 |
| running -> cancelled | 插件或外部协议明确确认本次执行已取消且不再继续；仅请求取消或关闭通道不足以成立 |
| running -> pending_external | 已被接受，具有外部引用或查询机制 |
| running -> unknown | 断线、崩溃等使结果无法确认 |
| pending_external -> succeeded/failed/cancelled/unknown | 查询或取消确认 |
| unknown -> pending_external/succeeded/failed/cancelled | 匹配原目标及执行身份的恢复证据 |

输出协议错误必须独立保存为 protocol_error。即使外部动作实际成功，平台不能伪造符合 Schema 的成功输出；Step/Job 进入阻断或协议失败处理，并保留原安全结果/引用，不自动重发。

取消请求单独记录。unsupported、requested、confirmed_cancelled、too_late、unknown 是不同取消结果。

## 4. 原子准备与派发

prepare 在事务外执行，只解析、校验和固定引用，不实施本次业务外部变更。随后：

```text
BEGIN
  校验 Job 当前租约及执行资格
  锁定 Step
  检查依赖、取消、冲突保护和固定引用
  创建 Operation(state=prepared)，保存 PreparedCall
  关联 Step.current_operation_id
  Step.state=running
COMMIT
```

约束：一个步骤执行尝试仅有一个初始 Operation；重复领取和事务重试不得重复创建。prepared Operation 在同一事务关联完整，不存在 running Step 却没有 Operation 的状态。

派发时用另一个短事务校验租约/冲突和取消，写 Operation.running 与派发意图，再提交并调用插件。任何数据库事务都不得跨越 SSH/PVE 等外部 I/O。

数据库原子性不是外部 exactly-once。running 后、真实发送前也可能崩溃；恢复不能从数据库状态推断是否发出。没有可证明未发送、可靠幂等或查询协议时，按 unknown 处理。

prepare 本身失败时保存准备错误，不伪造已派发 Operation。尚在 pending、prepared 且确认没有外部提交的调用可安全停止。

### 4.1 Job 级快照与跨 Step 引用

Environment 固定模板版本、创建参数、成功创建结果及资源目标；每个新 Job 另外持久化 execution_snapshot，至少包含插件实例到插件版本/代码摘要的映射、PluginInstance 配置 revision、各 Storage 命名空间 snapshot_id/revision、允许凭据的版本解析映射，以及所选候选项和数据摘要。

这些固定边界与 Job 接受、配额预留同事务关联。执行中不用数据库长事务持有历史，而由不可变版本记录实现 as-of get/list。平台不解释插件 JSON；后继 Step.prepare 只能从原 Job 视图读取，不因管理员修改当前配置、增删主机或分页而使用 latest。派发前校验实例未被禁止使用、历史版本存在、代码摘要一致、凭据未吊销，并保存 Operation 的实际依赖子集。

- 配置/Storage 在 Step A 后更新，Step B 保持原映射及列表快照。历史被删或不能解析时 blocked，而不是切到当前值。
- Job 内 Storage 写不刷新 Job 整体视图，新版本经前序成功结果/受控检查点显式传递；写入 CAS 冲突不能自动改读最新后重试覆盖。
- 新生成秘密按稳定派生产物键加密保存，返回精确版本引用；后继步骤显式引用，恢复不重复生成。任何快照、日志或结果只保存引用。
- 普通凭据轮换保留历史，旧版本仍获授权且远端接受时可继续；紧急吊销、远端旧密码失效或版本不可读取则阻断未派发步骤，不回退旧秘密或自动换新秘密。历史保留不是远端凭据有效性的保证。
- 独立资源动作 Job 可固定当前配置视图，但不能用它重新解释旧 resource.data/target_ref 为另一外部对象。新版本只能用于不改变目标的运行配置，不能绕过资源绑定快照。
- 如确需使用新配置/凭据恢复，显式建立关联恢复/新 Job，在相同冲突保护下核对原执行事实、目标连续性和所用新引用，保留旧快照及原因；未知旧操作不因此变为可安全重发。

延迟产生的 Step 输入和 for_each 集合来自已确认输出及已固定 Job 视图，展开结果持久化一次。引用来源可为新派生版本，但不得按字符串 ID 去查任意当前版本；完整 Storage/Secret 语义见插件及安全规范。

## 5. 部分引用、检查点与正式输出

三类数据独立保存：

| 数据 | 内容 | 可否视为成功输出 |
| --- | --- | --- |
| 已知外部引用 | 已存在对象或已接受任务的引用及确认时间 | 不可 |
| 检查点 | 插件可解释的恢复信息、阶段及配置引用 | 不可 |
| 成功输出 | 确认成功并符合 output_schema 的结果 | 可以供后继成功引用 |

插件取得关键外部 ID 后通过 report_external_ref/ref_key 和 report_checkpoint/checkpoint_key 立即报告。平台同步持久化确认、稳定键去重、追加历史；失败/unknown/日志清理不能抹掉引用。

同 key 内容变化保留版本，不静默覆盖原目标。写库失败不意味着外部不存在；插件不得依赖未确认持久化结果继续盲目实施后继变更。

成功结果、Step 成功和可供后继引用的 create_results 必须同事务提交；当下可确定的平台资源同时以稳定产出键写入。需等待后续步骤才能完成的资源，在该事务内记录未完成的本地生成项；最终所有必需本地生成项完成且 readiness 满足后才环境 ready。

本地生成项包含逻辑产出键、模板槽位、已确认输入引用及状态。恢复器只重放本地数据映射和写库，不重新 execute 已成功的外部动作；失败则阻断并保留原结果。逻辑产出身份为环境 + 逻辑流程调用 + 资源槽位 + item_key，Operation 只是来源。跨重试复用原产出需确认其外部事实；不能仅按名称合并。

进程在外部创建后、报告前崩溃仍有故障窗口：通过预先固定的调用键或插件恢复核对；不能承诺平台单靠数据库事务消除此窗口。

## 6. PostgreSQL 队列与租约

jobs 保存 type、固定输入、state、priority、available_at、attempts/max_attempts、lease_owner/token/expires_at、heartbeat_at、取消请求、父子关联及安全错误。Job 处理器由可信代码注册，不保存 Python 闭包或数据库可执行代码。

- 使用 FOR UPDATE SKIP LOCKED 与 UPDATE RETURNING 在短事务中原子领取。
- 领取后提交，线程池有上限，排队状态不得标为 running。
- 心跳、状态和完成写入必须匹配租约 token。
- worker 失去租约后不能继续提交权威状态，须协作停止；平台不声称远端命令因此终止。
- LISTEN/NOTIFY 只用于唤醒，启动和周期扫描保证通知丢失后仍能恢复。
- 到期扫描先检查 Operation 事实，不将所有 running Job 无条件改 queued。
- 仅确认未提交、只读调用或满足插件安全协议的调用可自动重试；设置退避和次数上限。
- pending_external 继续 poll 原任务，不能重复 execute。
- 变更结果 unknown 且不支持恢复/幂等时 blocked，等待显式处理。
- 启动失败插件、代码摘要变化或配置历史不可读取时保留 Job/Operation，不跳过或静默换目标。

### 6.1 调用预算与真实执行容量

prepare、execute、poll、cancel 及交互建连都有有限总 deadline。插件将剩余预算传给可控制的连接、认证、读写和命令等待超时；多次内部尝试不能刷新总预算。恢复查询/取消可有独立有界预算，但不得以此绕过原操作超时后重新提交变更。外部长期任务应返回 pending_external，由短时 poll 推进。

Python Future 等待超时、Future.cancel 和租约 fencing 都不能安全中断已经运行的不合作线程。没有远端证据时，关闭连接或命令等待超时不代表命令停止；已派发变更保存 unknown/blocked。确认未派发的准备阶段超时可以作为本地准备失败，但卡住的线程本身仍须计入容量。

执行池和应用排队缓冲必须有有限上限，按插件实例设置并发额度/公平领取，防止一个实例独占全部名额。协调扫描、健康查询和管理 API 不得等待普通插件执行池的可用线程；这些路径不直接执行无预算插件代码，会话接收也不能无限占据普通操作池。

上述只是逻辑隔离，不是子进程沙箱。插件持有 GIL、无限占内存或使整个进程崩溃时，API/扫描也可能不可用。v1 接受这一单进程可用性代价，不承诺在任意插件失控时健康接口仍能响应。

### 6.2 饱和、降级与人工重启

调用到期但尚未退出时：

1. 将调用标记为超期/疑似卡死，持久化能确认的事实，未知变更保持 unknown/blocked，保留环境及声明的额外冲突范围和配额。
2. 原调用继续占真实线程 slot；未退出前不返还执行额度、不无限补线程，不在其他线程并发重发或执行可能冲突的恢复变更。
3. 达到实例或全局有限阈值时，暂停受影响的 Job 领取/派发，未领取 Job 保持 queued；全局无安全容量时停止新变更准入，返回 503 和可重试提示，而不是接受无限内存排队。健康状态显示 degraded、占用/超期数量和需要人工处置，不宣称 ready。
4. 对仅受影响实例的新请求同样明确拒绝或保持已接受的持久队列；其他实例在确有剩余配额和容量时可继续。容量是否可用由真实活动调用统计，不由 Job.state 或 lease 是否过期推断。
5. 保留本地查询及管理能力仅是尽力保证。普通租约可以到期，但冲突保护不得因线程超期而释放；已有调用未退出时，恢复器只能追加事实、读取证据，不派发冲突变更。

管理员确认后人工重启整个控制面，v1 不根据阈值自动重启。退出先停止领取和新变更，尝试协作取消/关闭，持久化可确认进度并等待有限宽限期；若线程永久卡死，需要由部署操作停止整个进程，不能假设 daemon 或 Future 能完成优雅退出。不通过强制插件子进程替代已确定的单体边界。

重启会断开平台 WebSSH，不能证明远端命令或用户外部 SSH 已停止。启动恢复先取得唯一协调资格并扫描原 Operation：已派发且无法确认的变更保持 unknown/blocked，继续保存冲突与配额，先 poll/核对，不盲目重发；确认未派发才可继续提交。旧调用晚到结果只能作为匹配原执行身份的追加证据，不能覆盖恢复者的权威状态。

部署配置需明确有限的线程数、单实例并发、排队上限、各阶段超时、超期阈值、健康降级条件及退出宽限期；启动拒绝无界/不一致的配置。本文不伪造未经测量的生产数值，也不承诺仅靠超时能隔离不合作代码。

## 7. 持久冲突保护

### 7.1 与租约分离

执行租约决定谁可以推进 Job；冲突保护决定哪些新操作不能与未决副作用并发。blocked 后线程和租约可以退出，必要冲突保护必须保留。

v1 默认变更范围为 environment:{environment_id}。同一环境的资源变更、环境动作、创建和清理串行；只读查询仍经授权并遵守插件协议。

平台不分析命令或 IP 来判断不同环境是否操作了同一底层主机。需要跨环境互斥时由模板或 prepare 显式提供稳定的不透明冲突键；预检可提出键，必须在外部派发前事务校验并取得保护，不能作为已实现的全局资产隔离承诺。

### 7.2 执行处理组

原 Job 与关联恢复/清理 Job 归入一个 execution_group，持久记录 scope_key、组、当前推进 Job、revision、状态和原因。一个 scope 同时只能有一个未释放所有者；组 ID 由平台关联校验，不能让用户任意填写绕过互斥。

恢复接续在事务内锁冲突记录，确认旧推进者无有效执行资格，使用 revision CAS 取得推进权。恢复和新变更并发时只有一个合法推进者。

### 7.3 blocked 时的行为

| 请求 | 处理 |
| --- | --- |
| 查看事实、日志、状态 | 正常授权后允许 |
| 原调用恢复查询 | 关联同组恢复并串行推进 |
| 新启动/停止/重启等变更 | 409，返回安全阻断信息 |
| cleanup | 显式接续原事实，先核对未知变更，再执行模板清理 |
| 管理员人工核对 | 保存证据、理由和操作者，不能只改状态抹掉未知 |

只有确认没有未决外部变更或完成有效处置时，才释放/移交保护。已确认停止执行的失败现场可允许显式 cleanup 接续；保留配额不等于必须永久阻止所有清理。管理员不能用普通“强制成功”按钮把事实未知改成已清理。

## 8. 环境状态与清理

环境 lifecycle：creating、ready、create_failed、blocked、deleting、delete_failed、deleted。blocked 另存阻断阶段/原因和之前目标状态，恢复时按 Job/Operation 事实推进。

- 创建步骤和 readiness 满足后才 ready；resources 为空不自动失败。
- 普通启停重启不改变环境生命周期；执行状态与观测电源状态分离。普通动作 Job blocked 时保留环境原生命周期，单独展示阻断原因及冲突保护；环境 lifecycle=blocked 用于创建/清理等生命周期流程的阻断。
- 创建失败默认 create_failed，保留部分引用和占用，不自动补偿。
- cleanup 由教师/管理员显式发起，锁定相同环境冲突范围，使用原模板快照和确认的故障事实。
- 清理确认成功后才 deleted；失败为 delete_failed，未知为 blocked。
- 删除资源记录不证明外部资源删除，空 cleanup 也不能消除 unknown。
- 不因删除学生、移出组或删除课程请求而隐式销毁未知外部对象。删除仍有环境的课程或有未决事实的环境记录应阻断硬删除，先执行显式处理。

## 9. 教师配额事务

维度 environments、vms、vcpu、memory_mb，由模板参数快照和有限表达式计算，课程共享教师额度，不做二次隔离。

```text
BEGIN
  锁定教师配额作用域
  计算/校验已固定模板参数的需求
  检查：used + reserved + requested <= limits
  固定插件/实例配置版本、Storage 命名空间快照、秘密版本映射和候选引用
  固定请求幂等结果
  写 Environment、QuotaReservation、Job.execution_snapshot、初始冲突保护
COMMIT
```

纯 PostgreSQL 同事务写业务对象与 Job，不要求额外 Outbox；若以后有外部可靠消息需求再设计。相同幂等请求重复提交不能重复预留。

配额状态：reserved、allocated、quarantined、released。allocated 是确认占用；quarantined 是外部结果未知/清理未完成，仍计入限额。检查式中的 used = allocated + quarantined，reserved 单独统计，released 不再计入。统计必须互斥计量，不能同时在 used 和 reserved 重复计入同一笔。

- 未派发且确认没有外部副作用的取消可释放。
- failed 不自动释放；根据已确认占用及 cleanup 结果决定。
- 外部结果 unknown、删除失败或保留部分产出时保留/隔离相应占用。
- 只有模板清理完成条件确认后释放，保留释放审计，重试不得重复扣减/释放。
- 调低教师限额低于当前占用时，阻止新增超额申请，不自动删除已有环境。
- 模板成本不等于主机物理容量或独占约束，核心不强制插件清单资产纳管。

## 10. 交互会话与撤权

会话使用独立双向协议和持久元数据，不把字节流放在 execute JSON 返回。建立阶段与 Operation 关联，会话 active 后由会话服务管理；连接丢失与远端命令停止分开。

平台关闭会话只确认当前通道关闭。终端中用户任意执行及外部 SSH 客户端不受 Job 冲突锁控制。撤权后实时停止对应平台输入/输出，远端密码/直连会话处置单独记录，见安全规范。

## 11. 逻辑持久化对象与约束

这是职责清单，不是已执行的 DDL；不承诺所有职责一对一建表。

| 职责 | 建议对象 |
| --- | --- |
| 插件私有数据及历史 | plugin_instances、plugin_storage、plugin_storage_versions |
| 模板 | templates、template_versions |
| 课程/成员 | users、courses、groups、course_members |
| 环境与平台产出 | environments、experiment_resources、environment_assignments |
| 配额 | teacher_quotas、quota_reservations |
| 执行 | jobs、job_steps、operations、operation_evidence、job_logs |
| 互斥 | execution_groups、conflict_protections |
| 凭据 | secrets、secret_versions及受控关联 |
| 会话/审计 | interactive_sessions、audit_events |

必要唯一性/一致性：

- 用户域请求幂等键唯一，请求摘要不可变。
- 逻辑步骤路径与 item_key 唯一，初始执行尝试不会产生两个 Operation。
- 资源逻辑产出键稳定唯一，不以短暂 worker 身份生成。
- Storage 当前键按实例唯一，历史版本/墓碑不可变，expected_version 校验；snapshot_id 可重建 namespace_revision 下的 get/list 及分页集合。
- Job.execution_snapshot 关联不可变实例配置和秘密版本映射；精确派生引用与原读视图区分，紧急吊销可阻断交付但不重写历史。
- 一个冲突范围只有一个未释放所有者，恢复组接续有 revision 检查。
- 一个教师只有一个配额作用域行作为并发锁定点，预留关联 Job/环境并可去重。
- 外键显式定义删除策略，执行事实和审计不得被无意 CASCADE 清空。
- 全部持久时间用 TIMESTAMPTZ；状态、单位、JSON 对象形状和非负计数有约束。

## 12. 验收故障窗口

1. Step running 与 prepared Operation 同事务，任意写失败均回滚。
2. 派发意图前崩溃可确认未派发；派发意图后崩溃不假定未发出。
3. 创建外部对象后失败，检查点/引用仍可用于恢复和清理。
4. output Schema 错误不导致重复外部创建。
5. 旧租约回调不能覆盖新恢复状态，晚到证据保留来源。
6. blocked 后新变更被拒绝，两个恢复器只有一个推进者。
7. cleanup 不与未知原命令盲目并发。
8. 相同请求键并发不会重复创建环境或预占；两个申请不能超教师额度。
9. 清理失败/unknown 不提前释放，成功释放不重复计量。
10. 通知丢失、optional 插件失败、单进程重启不删除执行事实。
11. cancel_requested 后真实外部成功仍如实记录，不伪报 cancelled。
12. 交互会话断开不伪报远端进程及直连会话已结束。
13. 连接、认证、读写、命令等待及 poll/cancel 超时均消耗有限预算，重试不刷新总 deadline。
14. 不合作插件永久阻塞并 lease 到期时，真实 slot 仍占用；不得无限补线程，饱和时停止受影响派发、503 拒绝新增过载请求并降级，等待管理员人工重启。
15. 人工重启后保留 unknown/blocked、冲突和 quarantined 配额，不重发未决变更；旧结果晚到只能追加证据。
16. Step A 完成后改 host 映射、增加/修改/删除 Storage 键，Step B 的 get/list 及跨页读取仍使用同一 Job 原快照；历史丢失明确阻断。
17. 普通轮换后仍授权且远端接受的旧凭据可继续原 Job；紧急吊销/已知失效则阻断，不能读 latest、回退旧秘密或把历史密文存在当可用证据。
18. Job 内新 Storage/Secret 版本只有显式派生引用可进入后续步骤，恢复不重复生成、不刷新原命名空间视图。
19. 独立动作采用新的配置快照时，已有资源目标不静默改指其他主机/对象；需要替换时必须显式核对和关联恢复。
