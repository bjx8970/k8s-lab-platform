# Issue #9：PVE 7.4-20 测试设计与验收基线

- 案例版本：1.0（设计基线，含待决契约；不是已批准的实现验收标准）；设计日期：2026-10-16。
- 需求代码基点：`e58f1472d9e8ee05348f4062090c554f6215e8f5`。
- 工作分支：`test/issue-9-pve-7.4-20-acceptance`。
- 目标：`compute.vm/v1` / `pve.qemu/v1`，PVE 7.4-20。
- 版本事实：用户确认部署版本；本次没有连接 PVE，尚未取得实际包版本、API schema、存储类型或权限证据。
- 当前结论：测试设计已形成；功能验收未完成。53 条案例全部未运行，不存在业务断言失败、通过或跳过证据；文档结构/哈希校验不计入功能测试通过数。
- 本次交付仅为测试设计和可校验基线，不包含驱动、FakePVE、可执行验收测试、部署或真实平台操作。

## 1. 需求和版本证据

| 标识 | 来源及需求 |
| --- | --- |
| U1 | 本次用户要求：实际环境 PVE 7.4-20，新分支设计需要完成的测试案例。 |
| R1 | [Issue #9](https://github.com/bjx8970/k8s-lab-platform/issues/9)，读取时 updatedAt=`2026-09-04T07:25:20Z`：domain/connection、动作、UPID、重启、存在性、固定快照、部分失败、不重发、Fake 与隔离真实 smoke。 |
| R2 | [PVE 插件设计](resource-framework/plugins/pve.md)：身份作用域、明确 clone 输入、clone→配置、单项动作、unknown、不回滚。 |
| R3 | [VM 插件设计](resource-framework/plugins/virtual-machine.md)：字段校验、graceful/force、configure 不重启、部分结果、实际配置/电源观察。 |
| R4 | [资源框架契约](resource-framework/architecture.md)：provisional 身份占用、传输去重、lease/claim、陈旧采样、固定目标、IdentityMismatch、无业务策略。设计文档中未实现项不能当作当前能力。 |
| R5 | [凭据存储契约](security-credentials.md#控制面目标适配)：版本化 secret 引用、旧版保留、缺版失败、不回退与脱敏。 |
| V1 | [官方 7.4 文档归档](https://pve.proxmox.com/pve-docs-7/)，索引日期 2023-03-22；[API schema](https://pve.proxmox.com/pve-docs-7/api-viewer/apidoc.js) 的定向解析，记录在 [接口证据摘录](issue-9-pve-7.4-api-evidence.json)。归档不是精确 7.4-20 安装包的证明。 |
| V2 | [官方 7.4 qm 手册](https://pve.proxmox.com/pve-docs-7/qm.1.html)：clone、shutdown、stop、reboot、destroy 和参数边界。 |
| V3 | [官方 API 认证说明](https://pve.proxmox.com/wiki/Proxmox_VE_API)：API token 请求头及无需 PVE ticket CSRF token。此页面不是历史版本冻结证据，仍以 E02 实测核对。平台自身 HTTP 入口的 CSRF/Origin 规则不因此取消。 |
| T1 | [现有 CI runner](../scripts/run_ci_tests.py)：真实一次性 PostgreSQL、Node、零测试/失败/跳过均不通过；[既有运行时测试](../tests/test_issue3_runtime.py)、[身份 PostgreSQL 回归](../tests/test_vm_identity_postgres.py)、[映射测试](../tests/test_pve_domain_mapping.py)仅作为后续回归集合，不作为本次执行证据。 |

### PVE 7.4 特别需要防止的误判

1. POST clone/start/stop/shutdown/reboot 与 DELETE VM 返回任务引用，不等于外部完成。
2. PUT config 同步成功可返回 `null`；POST config 可返回 UPID，指定 `background_delay` 时也可在等待内完成并返回 `null`。不能一律要求每次 configure 都有 UPID。
3. GET config 默认含 pending 值。当前生效配置使用 `current=1`，结合 GET pending 分层报告；不能拿合并值当运行事实。
4. graceful 对应 shutdown，`forceStop` 默认 0；force 对应 stop。reboot 没有该归档声明的 `forceStop` 参数，不偷换成 reset 或 stop/start。
5. `full` 不存在对所有源统一适用的默认值；模板默认尝试链接克隆，普通 VM 克隆为完整复制。基线输入明确 clone_mode。
6. `memory` 单位 MiB、最小 16；cores 最小 1；VLAN tag 为 1..4094。归档的 VMID schema 最小 1，不能凭经验硬编码最小 100，也不能由此猜测未公开的上限。
7. `sshkeys` API 字段是 URL 编码值；必须在实际 SDK/HTTP 边界确认解码后仍是原 OpenSSH 公钥集合，不能重复编码或丢换行。
8. task `stopped` 本身不是成功；任务查询 403、日志丢失或连接失败不是 VM 不存在的证明。成功 `exitstatus=OK` 须由实际任务记录佐证。
9. DELETE 自身包含使用/拥有卷及 VM 权限、防火墙规则的原生清理。禁止的是隐式 stop/扩展 purge/额外未引用磁盘清理，不是要求 DELETE 完全无原生副作用。

## 2. 测试层次、夹具与通用断言

- **U**：契约/纯函数测试，假凭据，无网络。
- **F**：真实 PVE 驱动与 HTTP/SDK transport 边界测试，对端为有状态 FakePVE；不 mock 驱动自身结果。必要时使用本地 HTTP 服务观察方法、路径、认证/编码和返回 envelope。Fake 不证明真实 PVE 的行为。
- **D**：真实 ResourceService/OperationWorker/repository + 一次性 PostgreSQL + FakePVE。默认 WSL，Podman 提供一次性 PostgreSQL；禁止 SQLite 代替并发、约束与事务验收。
- **L**：隔离 PVE 7.4-20 与测试数据库上的显式启用验收。日常 CI 不自动连接真实 PVE。
- **P0**：安全、身份、重复副作用、恢复等合并/验收门槛；**P1**：必要正确性和边界，仍需在 #9 关闭前完成。条件不足的关键案例标记受阻，不能改成通过或静默跳过。

公共夹具：独立 domain A/B，可有相同 node 名和相同 VMID；明确 source_node/target_node/template_vmid/vmid/storage/clone_mode；新任务与 VM 当前节点分别保存；可区分的旧/新秘密测试标记；每例独立数据库 schema 和 Fake 状态。示例 VMID 为测试参数，不预占用户任何真实编号。

FakePVE 必须提供 VM 状态、任务状态和调用台账，按测试显式 barrier 推进，不依赖任意 sleep。支持四个不同失败点：确定未发送、平台明确拒绝、已接受但响应丢失、已有 UPID 后查询失败。支持 current/pending、部分创建、VMID 冲突、任务错误、身份更换和 locator 改变。Fake 状态必须独立于被重建的 worker；子进程故障测试使用独立 Fake 服务或可持续台账，不能随 worker 一起清空“平台”。

所有案例的公共副作用断言：没有未要求的 start/stop/reset/unlock/delete/换号重试/跨域回退；没有访问真实配置或默认真实网络；秘密不出现在 Operation 快照、exec_data、outbox、日志和错误。只对本例允许的动作计数，外部提交次数、查询次数分别记录。数据库 claim fencing 不等于外部 PVE fencing，禁止宣称 exactly-once。

证据代号：**Q**=不含秘密的请求方法/路径/参数与调用次数；**B**=Operation/Resource/Binding 前后及 outbox 事务证据；**P**=Fake 或真实平台任务状态与 VM/config/current/pending；**J**=worker/进程/lease 故障点与脱敏日志；**H**=版本、安装 schema 与包版本记录。每例表内证据列均为执行时必须产生的材料，不表示本次已经存在。

## 3. 案例目录

每行包含需求来源、前置及输入/步骤、预期、副作用检查、优先级、层级、所属 PR 与执行证据。当前 53 条统一状态为 **未运行**。

### E：目标环境及版本核验（4 条）

| ID / 来源 | 前置条件、输入与步骤 | 预期结果及副作用检查 | 优先级 / 层级 / PR | 证据 |
| --- | --- | --- | --- | --- |
| E01 / U1,V1 | 经授权只读连接指定测试端点；GET /version；由操作者在目标节点采集 `pveversion -v` 和测试节点包清单。 | 记录实际 pve-manager=7.4-20、release、repoid、qemu-server、pve-qemu-kvm、内核及各参与节点版本；不由 manager 版本猜测其他包。版本不符时阻断“7.4-20 已验收”结论，不升级平台。 | P0 / L / PR3 前置 | H,Q |
| E02 / V1,V3 | 有效测试 token 和有效 TLS CA；对安装环境读取相关 API 帮助/schema，以已授权动作验证 token 调用。 | 固定安装 schema 和版本证据，核对归档差异；API token 使用 `PVEAPIToken=USER@REALM!TOKENID=SECRET`，不依赖浏览器 ticket/CSRF；不记录请求头秘密，不降低 TLS 验证。 | P0 / L / PR3 前置 | H,Q |
| E03 / R1,R2 | 操作者确认 domain、node、只读模板、存储、bridge/VLAN、测试 VMID、最小权限、可用空间、测试窗口和清理归属。 | 形成允许清单；未授权资源、HA/复制/备份对象、生产 VM 均排除；不自动挑默认服务器或修改网络/存储配置。 | P0 / L / PR3 前置 | H,J |
| E04 / R1,V1 | 收集节点数量、第二独立 domain、linked/full 支持、共享存储、Cloud-init 模板/客户机 ACPI能力。 | 标记能力覆盖矩阵。单节点可跑基本 smoke，不能声称验证真实节点迁移；单 domain 不能声称验证真实跨域；无资源者保留 F/D 证据并显式记录 L 缺口，不能将其当作全部真实通过。 | P1 / L / PR3 前置 | H,J |

### C：连接、凭据和输入契约（8 条）

| ID / 来源 | 前置条件、输入与步骤 | 预期结果及副作用检查 | 优先级 / 层级 / PR | 证据 |
| --- | --- | --- | --- | --- |
| C01 / R1,R2 | domain A 与 B、两个 connection；尝试给 A 的 binding 使用 B 的 connection；另设 A 的第二合法 connection。 | 跨域映射在副作用前拒绝；合法同域连接不产生另一资源身份；拒绝时平台提交数=0。 | P0 / U,D / PR1 | Q,B |
| C02 / R1,R5 | 分开测试受理时前置版本冲突、受理后当前connection revision变化、仅secret轮换，以及已有UPID后的轮换。 | 受理时冲突拒绝且提交=0；受理后绝不静默改投新host/token/plugin。未提交者因当前revision变化而拒绝，还是解析保留的旧版执行，属于D5待决，不能同时断言两者；已有任务仅查询原冻结目标，原版不可用时保留任务且不重发。 | P0 / F,D / PR1+2，D5待决 | Q,B,J |
| C03 / R5 | 旧 secret 保留、缺失、撤销、密文损坏、解密密钥缺失分别注入；排队及 poll 各测。 | 解析器对可用旧版返回精确版本，不回退最新或明文；不可用版显式安全错误。排队Operation是否获准执行由C02/D5决定，不把解析成功等同执行许可；不伪造成功/不存在，阶段状态遵守恢复不变量。 | P0 / U,F,D / PR1+2 | Q,B,J |
| C04 / R2,R3,V1 | 分别缺 node/template_vmid/vmid/clone_mode、传错误类型/未知字段/路径分隔与控制字符；提交创建或配置。 | 必填/类型/编码/支持字段在外部变更前拒绝；bool 不冒充整数；不将输入拼成另一 API 路径；提交数=0。精确 schema 在 PR1 固定，不由实现输出反推。 | P0 / U,F / PR1 | Q,J |
| C05 / R3,V1 | 参数化 cores=0/1、memory_mib=15/16、tag=0/1/4094/4095；VMID=0/1、合法测试 VMID及非整数。 | 在 U/F 层核对公开边界及 MiB 单位；合法值不被凭经验的 VMID>=100 规则拒绝；上限遵循安装 schema，不猜测；边界值不发到真实低编号 VM。 | P1 / U,F / PR1 | Q,H |
| C06 / R3,V1 | 配置两个带换行、空格、`+`/`/`字符的虚构合法公钥；通过真实 transport 编码到 Fake，并读回。 | 经过 HTTP/API 所需解码后公钥集合逐项相同，无双重值编码、丢换行；未传字段不被补默认覆盖，非目标配置不变。 | P1 / U,F / PR1+2 | Q,P |
| C07 / R1,R5,V3 | 注入有效/错误 token、缺权限、错误 CA、TLS 主机名不匹配、连接超时；分开测试提交前及已知任务查询。 | 成功认证按契约；确定拒绝有安全分类，未知提交不当失败重试；TLS 失败不降级 verify=false、不改账号/端点；已知任务查询错误不抹 UPID。 | P0 / F,D / PR1+2 | Q,B,J |
| C08 / R4,R5 | 使用秘密哨兵串制造 SDK/HTTP 异常和跨 chunk 日志；检查结果、数据库普通 JSON、outbox、审计与日志。 | 无 token/认证头/密码/私钥/连接串秘密；凭据仅在专用秘密存储密文或瞬时 transport 中；provider 原因经脱敏保留，不以吞掉所有诊断代替脱敏。 | P0 / U,F,D / PR1+2 | B,J |

### I：身份、登记与目标定位（6 条）

| ID / 来源 | 前置条件、输入与步骤 | 预期结果及副作用检查 | 优先级 / 层级 / PR | 证据 |
| --- | --- | --- | --- | --- |
| I01 / R1,R2 | A/B 两 domain 均有 node=pve、vmid=X；分别 register/observe/start其中一个。 | 两资源、缓存、任务、结果独立；仅目标 domain 有变更提交；A 故障不去 B 查询同号 VM。 | P0 / F,D / PR1+2 | Q,B,P |
| I02 / R1,R4 | 同 domain 的两个 connection、同 VMID；并发登记/创建；跨 domain 再使用该 VMID。 | 同域只能一个有效身份占用，竞争失败者外部提交=0；跨域允许独立占用；创建失败身份释放须满足 R11，不能一律释放。 | P0 / D / PR1+2 | Q,B |
| I03 / R2,R4 | 登记已有 VM，随后 unregister；已有任务另测，不发 delete。 | register 不 clone/配置，unregister 不物理删除且保留历史；新旧任务按固定目标/可用性契约处理，不把关闭展示登记当平台销毁。 | P1 / F,D / PR2 | Q,B,P |
| I04 / R1,R2 | A 同 domain 中 VM 从 node1 变为 node2；保留 node1 发起的任务 UPID；观察并明确更新 locator。 | resource/domain/VMID 不变；新采样使用有效 locator，旧绑定采样被拒；原任务仍按任务所属 node poll，不能随 VM locator 改写任务路径。 | P0 / F,D / PR2 | Q,B,P |
| I05 / R4 | 删除原外部实例、用同 VMID 建替代实例；身份依据不同；再测依据不足。 | 确证替换时 IdentityMismatch，后续变更提交=0；名称/VMID相同不足以认领。证据不足不宣称身份已确认，不静默继承旧 binding。具体依据是第 5 节未决项 D1。 | P0 / F,D / PR1 契约+PR2 | Q,B,P |
| I06 / R2,V1 | nextid 查询返回 X；外部随后占用 X；或源模板不存在、存储不支持所选 clone_mode。 | 原样报告安全的冲突/不支持原因，不换号、不自动 full 回退、不认领占用者；查询不是平台 reservation；不修改已有 VM 或模板。 | P0 / F,D / PR2 | Q,B,P |

### A：单项动作与 7.4 语义（12 条）

| ID / 来源 | 前置条件、输入与步骤 | 预期结果及副作用检查 | 优先级 / 层级 / PR | 证据 |
| --- | --- | --- | --- | --- |
| A01 / R1,R2,V1 | stopped 模板、明确 source/target/storage/full 或 linked；执行 create；Fake clone task 先 running 再 OK。 | clone endpoint 使用源模板 locator 与目标 newid；先保存 UPID，不先 configure；clone OK 后按冻结参数配置，最终确认 present；整例 start=0、源模板不变。 | P0 / F,D / PR2 | Q,B,P |
| A02 / R2,V1 | 模板链接克隆可用及不可用、full可用、跨节点存储条件不足分别执行。 | clone_mode精确映射，不依赖模糊默认，不支持时保留真实错误；不自动改模式、存储、node；源 VM/template 与其他卷不变。 | P1 / F,D / PR2 | Q,B,P |
| A03 / R1,R2 | clone task报错；分别模拟确定未创建、已产生VM/卷、存在性查不清。 | Operation failed 或 unknown与证据一致；Resource为 absent/present/unknown真实事实，不因failed一律absent；保留UPID、已知身份/阶段/部分结果；不自动清理或释放未知身份。 | P0 / F,D / PR2 | Q,B,P |
| A04 / R1,R2 | clone OK，后续 configure 明确失败；VM已经存在。 | 保存 clone 完成、配置失败、VMID/UPID/部分配置事实；Resource present，原create不得再clone；没有自动delete、重建、start。 | P0 / F,D / PR2 | Q,B,P |
| A05 / R3,V1 | configure分别收到PUT成功null、POST任务UPID、POST background_delay内完成null及明确错误。 | 有效null不被当空响应失败；UPID持久化并poll；任务/同步完成与配置采样分开；不得伪造UPID、自动换HTTP方法重试或隐式reboot。 | P0 / F,D / PR2 | Q,B,P |
| A06 / R3,V1 | running VM关闭CPU/内存热插拔；修改配置，再取默认config、current=1和pending。 | 返回current与pending差异，不把合并配置当生效事实；configure可以已完成但配置待应用；boot/power状态不因提交被乐观修改；reboot提交=0。 | P0 / F,D / PR2 | Q,B,P |
| A07 / R3,V1 | 带旧digest配置；平台当前digest已改变；另测保护/锁/权限明确拒绝。 | 报真实技术冲突/拒绝，不清锁、不加skiplock、不去掉digest重发；VM及其他配置不被覆盖；外部错误经脱敏可定位。 | P0 / F,D / PR2 | Q,B,P |
| A08 / R1,R3,V1 | stopped VM执行start；任务running、OK、error三个分支。 | 仅POST start一次；外部接受时pending_external，不提前成功或写running；终态结合任务证据与观察分别记录；错误不自动reboot/reset。 | P0 / F,D / PR2 | Q,B,P |
| A09 / R2,R3,V1 | running VM执行stop(mode=graceful)，分别正常关机、客户机不响应而超时。 | 只发shutdown，forceStop为false/0；等待原UPID；超时不调用stop、不改forceStop；保存任务错误与实际仍running事实。 | P0 / F,D / PR2 | Q,B,P |
| A10 / R2,R3,V1 | running VM执行stop(mode=force)；任务先running后OK。 | 只POST stop，保持UPID；完成后观察stopped；不发shutdown/reboot、不混淆graceful。仅专用可丢弃VM可执行L扩展。 | P0 / F,D / PR2 | Q,B,P |
| A11 / R1,R3,V1 | running VM执行reboot，任务running时观察仍running，再OK；另测stopped VM被平台拒绝。 | 仅POST reboot一次；running不足以宣布重启完成；不替换reset/stop+start，不为stopped VM偷偷start；任务拒绝原样保留。 | P0 / F,D / PR2 | Q,B,P |
| A12 / R1,R2,V1 | 分别对stopped、running、受保护、锁定测试VM执行delete；显式purge/额外磁盘参数各测。 | stopped正常返回UPID并最终确认absent/关闭当前绑定、保留历史；running/保护/锁按平台拒绝；stop/unlock=0；不擅自增加purge或destroy-unreferenced-disks；仅允许参数指定的原生清理。 | P0 / F,D / PR2 | Q,B,P |

### R：持久化、崩溃与并发（12 条）

| ID / 来源 | 前置条件、输入与步骤 | 预期结果及副作用检查 | 优先级 / 层级 / PR | 证据 |
| --- | --- | --- | --- | --- |
| R01 / R1,R4 | Service创建provisional和Operation，事务提交前/回滚时尝试唤醒worker；正常提交后再驱动。 | 提交前无外部调用；回滚无残留对象/事件；提交后可领取且唯一身份已占用。无需Environment/PlanRevision即可内部执行。 | P0 / D / PR2 | Q,B,J |
| R02 / R1,R4 | Operation pending且尚未claim时退出worker；用同数据库和独立Fake重启。 | 重新领取并仅提交一次；不把pending当已发Unknown；输入/目标与原记录一致。 | P0 / D / PR2 | Q,B,J |
| R03 / R1,R4 | claim后Running、外部调用前崩溃；无持久化“确定未发”证据；等待租约过期再恢复。 | 保守Unknown，不推测必然未发送而重发；保留provisional身份及阻断；若未来增加可靠提交checkpoint，应升级案例基线而非私改此预期。 | P0 / D / PR2 | Q,B,J |
| R04 / R1,R2 | Fake接受clone或reboot后丢响应；或返回UPID后在保存前退出worker。 | Unknown、保留已知事实/身份占用；外部提交次数=1；同request重放及新request均不能绕过未核对阻断；不凭running或VM存在认定reboot/create全部成功。 | P0 / F,D / PR2 | Q,B,P,J |
| R05 / R1 | UPID和阶段已提交，worker崩溃；重建worker并在原任务running/OK/error下继续；clone OK后下一配置阶段分别返回null/UPID。 | 恢复原domain/connection/secret/plugin/任务node和UPID，从handler.poll入口继续，不能再次调用execute重放原clone/reboot。poll可按D3推进被可靠证明尚未提交的配置阶段，台账中该阶段恰好一次；原任务失败则不推进，阶段/部分结果不丢。 | P0 / D / PR2 | Q,B,P,J |
| R06 / R1,R2,V1 | 已有UPID；poll断线、超时、403、任务历史查不到、异常格式、stopped缺exitstatus分别注入，再恢复可查。 | 保留引用和不确定诊断；不宣称成功/VM不存在、不再提交动作；可查询恢复后继续原任务。缺历史的最终Unknown核对路径按D2执行，不硬编码所有错误同一终态。 | P0 / F,D / PR2 | Q,B,P,J |
| R07 / R1,R2,R4 | clone完成；分别在配置提交前checkpoint后、配置已接受但结果未保存时崩溃；配置可null或有UPID。 | 只有持久化证据证明下一阶段未提交且合法claim持有者才能发配置；不明确窗口转Unknown，不重复configure；保持clone及config任务引用/阶段，不自动回滚。 | P0 / D / PR1 契约+PR2 | Q,B,P,J |
| R08 / R1,R4 | 平台已完成create/delete/reboot但最终数据库写入前退出；保留已有任务/阶段信息后恢复。 | 有足够任务和阶段证据则恢复确认终态，无证据保留Unknown；原外部提交次数不增加；create/delete存在性及Binding变更同事务，结果/事件不部分提交。 | P0 / D / PR2 | Q,B,P,J |
| R09 / R4 | 两worker并发claim；旧响应延迟到达；另在旧worker即将发后续configure前暂停并使租约过期，再恢复；最后注入检查claim后、发请求前失租窗口。 | 只有有效claim可保存事实/事件；旧写入失败。已确认失租的旧worker不得开始下一阶段；检查后失租且可能已发送的请求必须保留不确定事实，恢复者不重发该阶段。检查外部台账，不将DB拒写当外部fencing，不宣称消除了所有检查/发送竞争窗口。 | P0 / D / PR2，D3定协议 | Q,B,P,J |
| R10 / R4 | 并发提交同request相同/不同内容，和同资源两个变更；observe并行另测。 | 相同请求回同Operation不再发；不同内容RequestConflict；变更遵守每资源互斥，observe不能伪装只读变更；失败/Unknown不借新request绕过去重和准入。 | P0 / D / PR2 | Q,B,J |
| R11 / R1,R4 | create校验/插件缺失/版本冲突/取消发生在确定提交前；另测可能已发和已返回UPID后的取消。 | 确定未发才可按契约释放provisional占用；可能已发/有UPID不得假报未创建、释放占用或回滚；取消请求不是平台成功取消，原任务事实保持可追踪。 | P0 / D / PR2 | Q,B,P,J |
| R12 / R1,R4 | 制造Unknown并尝试普通新变更；D2批准恢复协议后，再测试可信/不足/矛盾证据、显式批准新attempt及重复恢复请求。 | 普通新request不能绕过现有Unknown阻断；禁止自动重发/补偿或以直接SQL冒充恢复。恢复入口、是否必须先核对及新attempt的批准规则由D2决定，批准后固定具体断言；保存审计和原历史。此扩展未获批准前标记待决，不强制新增产品能力。 | P0 / D / 恢复扩展归属待D2决定 | Q,B,P,J |

### O：观察、缓存与回归（5 条）

| ID / 来源 | 前置条件、输入与步骤 | 预期结果及副作用检查 | 优先级 / 层级 / PR | 证据 |
| --- | --- | --- | --- | --- |
| O01 / R1,R3 | observe present/running、present/stopped、确认absent、无权限/断线分别返回。 | 分开存在性、电源、采样时间与错误；403/断线不当absent；旧事实保留并stale，不清空其他资源；所有变更提交=0。 | P0 / F,D / PR2 | Q,B,P |
| O02 / R1,R4 | 采样A阻塞，binding revision改变后采样B先回，再放行A；同binding反序采样另测。 | 旧binding及更旧采样不能覆盖B；domain/resourceId定位无串号；保存可核对的revision/序号/时间证据，不能仅靠到达顺序。 | P0 / D / PR2 | Q,B,J |
| O03 / R1,R4 | Operation受理、外部running、外部OK/失败、observe分别停在barrier读取数据库和结果。 | 四层事实不混淆：已受理≠外部已接收≠完成≠新观察；outbox与对应数据库提交一致；不使用旧状态缓存或乐观running作为完成证据。 | P0 / D / PR2 | B,P,J |
| O04 / R4,T1 | 不传教师/学生/Environment/PlanRevision；metadata含K8s角色；通过可信内部Service执行技术有效动作。 | 驱动不读取业务策略、不因metadata拒绝或生成跨资源动作；正式HTTP鉴权仍在应用边界，本例不授权新增无鉴权路由。 | P1 / F,D / PR2 | Q,B,J |
| O05 / R1,T1 | 在实现后的固定代码快照运行现有身份、映射、运行时、安全测试及全套CI runner；检查模块导入。 | 原有授权/CSRF/Origin/脱敏/身份/去重断言不放宽；新增插件模块导入不访问真实配置、迁移或启动worker；全套零失败零跳过。不是要求本PR提前消除全部旧app导入副作用。 | P0 / U,D / PR1+2+3 | J,B |

### L：真实 PVE 7.4-20 验收场景（6 条）

本组逐项通过安全准入后才执行；E01–E03先完成，测试数据库与PVE都必须隔离。每个场景包含多个动作断言，不把场景数冒充自动化测试方法数。

| ID / 来源 | 前置条件、输入与步骤 | 预期结果及副作用检查 | 优先级 / 层级 / PR | 证据 |
| --- | --- | --- | --- | --- |
| L01 / R1,R2,V1 | 专用模板和已确认空闲VMID；按创建→observe→configure→start→reboot→graceful stop→delete顺序，分别等终态。 | 每次动作保存真实任务/同步null证据；clone未自动开机、configure未重启；reboot有原任务终态；配置current/pending真实；delete最终absent；源模板及非本次VM/卷不变。full/linked各按实际支持执行并记录覆盖。 | P0 / L / PR3 | H,Q,B,P,J |
| L02 / R1 | 专用clone或reboot已保存UPID；仅停止测试worker，再以同测试数据库重启；不重启PVE或生产worker。 | 继续原任务poll，原UPID/node不变，原clone/reboot提交次数不增加；可靠证明尚未提交的后续configure可按R05/D3推进并单独记录；结果与PVE任务记录一致。至少一次进程级恢复，不以同进程对象重建代替全部证据。 | P0 / L / PR3 | Q,B,P,J |
| L03 / R1,R5 | 在有UPID阶段，通过仅作用于测试执行端的transport故障开关阻断API，再恢复；轮换测试token版本另测。 | 断线期不假成功、不重复提交；恢复后用原冻结版本查询；旧token失效时报告不可查询而非改用新token；新操作使用新版本。禁止改生产网关、防火墙或撤销生产token。 | P0 / L / PR3 | Q,B,P,J |
| L04 / R1,R2,V1 | 对本次测试VM复用已占用VMID、运行中delete、只读/不足权限测试token分别发明确动作；不制造宿主存储耗尽。 | 捕获真实冲突/状态/权限拒绝，无换号、隐式stop、unlock、自动重试或不相关删除；错误后能观察到真实VM仍在。危险资源不足/残卷窗口只用F/D模拟。 | P0 / L / PR3 | Q,B,P,J |
| L05 / R1,R3,V1 | 专用VM关闭热插拔；配置产生pending，后显式reboot应用；可丢弃VM另测force stop和graceful超时。 | current/pending分层正确；只有显式reboot应用对应配置；graceful超时无force回退；force只调用stop。客户机控制/ACPI条件不足时标记受阻，不篡改预期成正常关机。 | P1 / L / PR3 | Q,B,P,J |
| L06 / R1,R2 | 若已授权第二独立PVE domain，用相同VMID验证隔离；若有同域第二节点/迁移条件，由操作者明确迁移专用VM。 | 真实跨域互不影响，同域迁移resource identity不变、旧任务node不被改写。未具备条件分别记录受阻，不能用两个连接冒充两个domain。此项环境不足不否定已完成的单平台smoke，但整体覆盖结论必须保留缺口。 | P1 / L / PR3 条件场景 | H,Q,B,P,J |

## 4. 真实环境安全准入与清理

- 本设计不构成执行真实创建、启停、删除、迁移或凭据轮换的授权。执行前由用户确认目标允许清单与维护窗口，秘密通过安全方式注入，不能放在案例、命令参数、仓库或聊天中。
- 专用测试token按实际动作赋权，先记录源模板VM.Clone、目标VM.Allocate/VM.PowerMgmt/配置所需权限、存储Datastore.AllocateSpace及读取任务所需权限。任务所有者与非所有者分别检查；不为了poll失败把账号升级成root或全局管理员。
- 每次运行需要run_id、测试数据库标识、domain/connection、模板、允许VMID、node/storage、预期卷清单和参与者确认；VM名称只能辅助定位，不作为独占删除证据。
- linked clone仅在可用存储/模板上执行，full clone单独限制空间；不测填满磁盘、不重启宿主、不改PVE全局网络、不停止生产集群、不中断生产worker。
- 注入崩溃仅终止本次启动且记录PID/所属身份的测试进程；禁止按进程名批量杀进程。租约/时间采用测试可控机制，不修改宿主系统时钟。
- 清理只对本次台账且身份匹配的资源执行显式stop后delete，并分别保存Operation；不得使用VMID范围扫描、模糊名称匹配或通用finally无条件删除。模板、未知归属卷、HA/备份/复制配置永不作为顺手清理目标。
- Unknown、IdentityMismatch、部分创建及清理失败先保留现场和身份占用，由受控核对决定；不得为恢复环境整洁而重发clone/强删。结束报告必须列残留VM/卷和人工接管人。

## 5. 尚需在编码前明确的契约

以下不是本次替用户决定的新业务规则；案例先固定安全不变量，协议形式未定的部分不得宣称已可执行。

| 标识 | 待定内容 | 已固定不变量 / 对应案例 | 决定时点 |
| --- | --- | --- | --- |
| D1 | PVE实例身份依据：可用字段、创建/登记时采集方式、clone/迁移后的稳定性、证据不足的状态及显式重新绑定流程。SMBIOS UUID可能被克隆或修改，不能未经验证当唯一真相。 | 确认替换则IdentityMismatch；不向未确认实例静默发送旧变更，见I05。 | PR1接口冻结前；由维护者确认方案。 |
| D2 | 是否把受控Unknown核对/解除阻断纳入#9，还是留给控制面；显式批准新attempt是否必须先核对、可接受证据、权限/审计和历史保留；任务历史缺失后的处理。 | 已确定不自动重发、普通新request不绕过当前阻断；不能仅凭running推断reboot成功。正式恢复入口和前置条件为候选协议，见R06/R12。 | PR1前维护者决定范围；推荐PR2最小审计入口，但本设计未替用户批准新增范围。 |
| D3 | clone→configure各副作用checkpoint、持有claim检查、同步null和配置UPID的持久化格式，以及旧版本连接/secret解析方式。 | 不确定的配置提交不重发，见C02/R07/R09；不要求通过“幂等看起来没事”替代提交证据。 | PR1定协议，PR2实测。 |
| D4 | 精确环境证据和资源可用性：包清单、安装API、存储/模板/客户机、第二domain/节点、真实测试允许清单。 | 当前仅用户报告版本；未测的集成能力不能记通过，见E系列/L系列。 | 可提前只读采集；任何真实写动作之前确认。 |
| D5 | Issue的“版本不匹配即失败”与已受理指令使用冻结旧连接/秘密版本的边界：分别定义connection revision改变、secret轮换、停用/撤销、未提交和已有UPID。 | 所有路径不改投新目标/秘密、不自动重发；受理前版本冲突必须拒绝。未提交时拒绝还是保留旧版执行存在文档歧义，不能依据当前实现自行决定，见C02/C03。 | PR1前维护者确认并升级可执行验收预期。 |

已有执行框架只提供骨架不等于上述协议完成。此文不规定尚不存在的HTTP路径、checkpoint类名或状态字段名；实现后必须让可执行测试调用真实公开边界，而不是文本搜索或测试自己构造的结果。

## 6. 三个 PR 的门槛与自动化落地

| 阶段 | 必须完成 | 不能冒充完成的内容 |
| --- | --- | --- |
| PR1 连接/契约/Fake | C01–C08适用契约、I01/I02/I05身份契约；Fake的状态机、接受后丢响应能力；D1/D3/D5协议及D2范围确认；为A01/R04/R05/R07建立可运行测试并取得至少关键业务断言失败证据。 | 不把缺模块ImportError、缺数据库或runner报错当驱动业务红测；不把Fake自测当真实驱动验收。 |
| PR2 持久化执行/恢复 | 全部已批准U/F/D动作与恢复案例；R12新增恢复能力按D2批准范围归属；测试参数覆盖sync-null/UPID、每个提交窗口及同资源并发；身份、安全、issue3回归。 | 只有clone正常路径或只存UPID不等于完整create恢复；只重建Python对象不等于所有进程故障均测过；不能把D2待决扩展当已批准需求。 |
| PR3 真实验收/交接 | E与L场景、完整回归、残留清单、真实7.4-20证据；独立执行固定快照测试并记录覆盖缺口。 | 无第二domain/节点时不声称全拓扑实测；本地F/D通过不能替代真实L证据。 |

后续可执行测试建议依现有unittest组织到`tests/test_pve_driver_contracts.py`、`tests/test_pve_driver_runtime.py`及独立显式启用的smoke入口；这些是建议的未来文件，不在本次清单中，也不能现在运行不存在的模块。smoke不纳入默认发现产生skip，默认CI仍维持零跳过；smoke入口另行要求明确配置、零测试或缺环境时非零退出。

开发/测试在默认WSL中进行，先核验Windows路径映射；git/gh仍在Windows Git Bash执行。D层使用一次性PostgreSQL，完整回归入口为既有`K8S_LAB_TEST_POSTGRES_URL=... python scripts/run_ci_tests.py`；此处是未来执行说明，不是本次已执行命令。不得输出实际数据库URL。

当前暂不编写/运行业务验收代码的原因：本任务范围为设计案例，PVE驱动、transport注入和阶段checkpoint协议尚未交付，D1/D2/D3/D5也未定。此时编造接口再取得导入失败不能形成有效红测。例外边界是“只冻结设计，不允许据此认定功能完成”；PR1开始功能编码前仍必须补独立审查的可执行关键测试与业务红测证据。若维护者要绕过该门槛，需明确决定并升级需求/基线版本。

## 7. 固定基线与执行报告要求

- 验收文件清单为本文件及`issue-9-pve-7.4-api-evidence.json`；其SHA-256记录在`issue-9-acceptance.sha256`。哈希文件不包含自身。
- 案例版本和预期先于功能实现固定；后续测试实现必须逐例关联ID及子场景，不得通过删案例/放宽断言/更新快照来获得通过。
- 接口/测试代码错误可修正，但需独立复核；需求预期变化必须有维护者决定、变更理由及新的案例版本/哈希。安装7.4-20与归档差异归为版本事实，不能暗改业务不变量。
- 独立审查与交付校验记录另见`issue-9-acceptance-review.md`，它不是业务通过证据，也不改变这53条预期。
- 实现后在固定commit/工作树摘要下执行，记录脱敏命令、Python/SDK/PG/PVE版本、测试ID、开始结束时间、退出码、通过/失败/错误/跳过/未运行数、证据路径与hash、残留资源及限制。
- 失败分类：实现缺陷、测试缺陷、环境受阻、需求矛盾。关键案例未运行时结论为受阻/未完成，不使用“全部通过”。

当前功能统计：设计53条；已运行0、通过0、业务失败0、测试错误0、跳过0、未运行53。没有连接真实PVE、没有启动测试数据库或测试进程、没有修改生产代码、没有提交或推送。
