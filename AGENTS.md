# K8s Lab Platform — Agent Guide

## 文档入口与实现状态

先阅读 [README](readme.md) 与 [架构边界](docs/architecture.md)，再按修改范围阅读：

- [插件接口](docs/plugin-contract.md)
- [模板与资源动作](docs/template-resource-contract.md)
- [执行与持久化](docs/execution-persistence-contract.md)
- [身份、授权与安全](docs/identity-security-contract.md)

这些是已确认的 v1 目标设计，不是现有功能说明。当前运行代码仍为旧 Flask/Jinja 单体，尚无新插件运行时、Alembic 或 PostgreSQL Job/Operation 内核。变更说明必须区分“设计”“实现”“实测”，不能把规范写成已交付功能。

## 不得偏离的产品边界

- 彻底重构，不默认维持旧 API、旧表、旧模板或旧运行结构兼容；这不等于授权删除真实数据库、外部资源或其他工作区文件。
- 按需模板工作流是 v1 基线，见[ADR-001](docs/architecture.md#11-adr-001采用按需模板工作流不以持续协调为-v1-基线)。历史 control-plane v0.2 持续协调路线不再是实施基线；恢复扫描不等于 Reconcile/漂移自动修复，cleanup 不等于自动 Finalizer。
- 控制平面单进程模块化，不拆 Web/Worker/Monitor 部署。后台线程显式启动和停止，禁止导入时启动线程、读库或外部连接。调用有有限预算，不合作线程未退出不返还 slot、不无限补线程；容量耗尽降级停止受影响派发，等待人工重启，不自动重启。
- 插件从可信目录在启动时加载，required/optional 由部署配置决定。v1 不做热更新、在线安装或多版本共存。
- 插件能力由 Manifest 声明。管理员和插件代码可信；不增加重复的插件能力审批、危险命令过滤或供应商功能白名单。
- 教师和学生只能通过管理员发布模板的环境/资源动作调用插件；仍须严格校验参数、角色和实时业务授权。
- 管理员创建模板；教师选择已发布版本、创建并分配环境；学生仅使用获授权环境及本人信息，无环境删除权限。
- 环境限同一课程内分配，可给多个组和/或学生。学生在同一课程最多一个组，也可未分组；可跨教师课程。
- 教师创建的学生初始唯一管理；加入其他教师所属课程后永久降为共享账号，创建教师失去编辑、重置、禁用和删除权限。账号创建者保留为历史事实。
- 教师是配额作用域，课程继承教师额度。模板声明固定或变量成本，核心事务内汇总、预占；不推断供应商物理占用。

## 插件与资源边界

- Operation API：平台按模板调用插件；插件解释动作、目标和供应商协议。
- Storage API：插件经平台保存私有 JSON 数据，按实例隔离、历史版本可读取、乐观并发控制；不得获取 DB Session、执行 SQL/DDL 或建立插件专用表。
- 普通配置与 Storage 不存明文秘密，只保存凭据引用；秘密由平台加密保存并按执行上下文提供。Job 固定实例配置、Storage 命名空间快照和秘密版本映射，跨 Step 不读 latest；新写入版本仅显式派生可见。普通轮换保留仍有效的旧版本，紧急吊销/失效阻断，不静默换新或回退旧秘密。
- 实验资源由平台按模板和执行结果生成。**没有插件 Resource Registration API。**
- 不把插件主机清单自动转换为平台资源，不强制资产身份、主机独占分配或外部对象纳管。
- 核心不硬编码 VMID、VLAN 等供应商字段，不强制所有平台资源具有同一种外部身份结构。目标引用和资源 data 可以是不透明 JSON。
- 资源与环境动作都来自模板快照，可以是多步流程。插件支持不等于普通用户可直接调用；前后端都以模板授权为边界。
- SSH 管理连接不自动等于学生 WebSSH 入口。交互会话独立于 execute() 的 JSON 结果，需双向流、输入控制权、背压和关闭语义。

## 执行与持久化边界

- PostgreSQL 是 Job/Step/Operation 与配额的事实来源；不用 Python 闭包或内存 dict 作为恢复依据。
- Step 进入 running 和创建 prepared Operation 必须同事务提交。派发意图持久化后才调用外部系统；外部调用不持有 DB 事务。
- 区分已知外部引用、检查点、成功输出和平台资源。失败不能抹掉部分产出；输出 Schema 错误不能触发盲目重发。
- 连接异常、进程崩溃、租约过期不证明远端失败。未知变更不自动重发；根据插件幂等/查询协议恢复或阻断。
- 租约 token 只拒绝旧执行器写库，不能阻止旧远端命令。执行租约和持久化冲突保护分离；blocked 保留必要互斥，恢复任务受同一范围约束。
- 创建失败保留现场，用户显式运行模板 cleanup。禁止默认逆序补偿、推断删除动作或清理未确认就释放配额。
- 只在确认没有外部执行或确认清理完成后释放对应配额；未知占用进入 quarantined。
- 允许学生查看环境密码并直接 SSH，不承诺删除分配或轮换密码会自动终止远端会话。平台撤权与远端撤权分别记录。

## 当前代码速查

| 路径 | 现状 |
| --- | --- |
| `app.py` / `run.py` | 旧 HTTP、SocketIO 和入口，存在导入副作用 |
| `modules/db.py` | PostgreSQL-only 连接，旧 models/CRUD，create_all + 手写补列 |
| `modules/k8s_manager.py` | PVE/OpenWrt 固定编排、内存任务状态/重试 |
| `modules/task_queue.py` | 导入时启动 create(1)、delete(1)、deploy(2) 守护线程 |
| `modules/pve_client.py` / `modules/openwrt_client.py` | 供应商协议实现素材，不是目标插件协议 |
| `modules/ssh_terminal.py` / `modules/status_cache.py` | 旧会话池和缓存 |
| `modules/authz.py` / `modules/security_service.py` | 旧授权策略与服务检查 |
| `modules/audit.py` / `modules/credential_store.py` | 旧脱敏与 Fernet 工具 |
| `templates/` | Jinja 业务页面，与实验环境模板不同 |
| `tests/` | 旧 unittest 安全/重试测试，确实存在 |

当前代码没有默认 SQLite 引擎；临时 SQLite 只用于隔离测试。不要恢复“支持 SQLite 在线切换”或“没有测试”的旧描述。

`SSHManager` 在旧应用 init_db 前读取配置表，全新配置/空库启动存在阻塞；不能声称旧 /db-config 向导已可用。不要以导入 app 的方式检查未知部署环境，避免启动迁移、连接和线程。

## 验证与操作约束

- 旧测试入口：`python -B -m unittest discover -s tests -t .`；优先在默认 WSL 中运行开发和测试，先验证 Windows 路径映射。git/gh 留在当前 Windows worktree 执行。
- 新规范必须增加接口、状态机、数据库事务、崩溃、并发、权限和模板驱动 UI 验收；旧测试不能代替这些测试。
- 文档更新通常无需运行真实设备、凭据迁移或生产部署；报告实际执行的检查，不复用历史测试次数。
- 凭据、私钥、密文和加密密钥不写日志或提交。API 使用 Session/CSRF/精确 Origin 白名单，敏感查看和接管须审计。
- 不主动恢复已删除的旧重构计划。新增规范只在用户明确要求文档时编写；删除过时文档直接删除，不做归档副本。
- 旧系统退役前按[架构第 9.1 节](docs/architecture.md#91-旧部署退役与硬切换前置条件)核对对象、未决事实、管理责任和可恢复的旧库/凭据/密钥，禁止新旧系统无协调修改同一对象；不兼容不等于授权销毁真实资源或证据。不实现双轨兼容层。
- 不擅自提交、推送、清库或调用真实资源。UI 实现完成前必须在浏览器验证；无法验证时明确说明。
