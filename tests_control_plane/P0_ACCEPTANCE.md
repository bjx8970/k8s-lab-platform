# P0 运行基线验收案例

状态：验收基线 v1.0，冻结日期 2026-11-06。

本文件只覆盖 P0“无副作用启动骨架”：配置校验、显式 PostgreSQL 连接、唯一协调资格、显式启停和进程边界。它不包含领域表、Alembic 业务迁移、插件运行时、Job/Step/Operation、HTTP API、模板 UI 或旧系统迁移；这些属于后续阶段。P0 不修改旧 Flask/Jinja 代码，也不建立兼容层。

依据：

- [架构与产品边界](../../docs/architecture.md)：单进程模块化、显式后台组件、唯一协调实例、PostgreSQL 持久事实。
- [执行与持久化规范](../../docs/execution-persistence-contract.md)：调用容量、失败关闭、恢复边界。
- [身份与安全规范](../../docs/identity-security-contract.md)：错误和日志不得泄露秘密。
- [AGENTS 验证约束](../../AGENTS.md)：测试优先在默认 WSL 运行；导入不得读库、连接外部系统或启动线程。

## 运行契约

入口：`python -m control_plane start`

### 环境变量

| 变量 | 必需 | 默认值 | 约束 |
| --- | --- | --- | --- |
| `CONTROL_PLANE_DATABASE_URL` | 是 | 无 | 仅 PostgreSQL；接受 `postgresql://`（内部归一化为 pg8000）或 `postgresql+pg8000://`；拒绝其他驱动 |
| `CONTROL_PLANE_CONNECT_TIMEOUT_SECONDS` | 否 | 5 | 0.5～30，包含小数；整个启动连接过程的总预算，同时作为 pg8000 socket 超时 |
| `CONTROL_PLANE_LOCK_POLL_SECONDS` | 否 | 1.0 | 0.05～60；仅用于已取得锁之后的存活探测周期，不用于等待获取锁 |

`CONTROL_PLANE_CONNECT_TIMEOUT_SECONDS` 在支持 SIGALRM 的 POSIX 平台同时作为整个启动握手总预算；无 SIGALRM 平台只能保证单次 socket 超时。P0 部署与测试基线为 Linux/WSL。

### 启动与协调语义

- 启动时只做一次 `pg_try_advisory_lock`；失败立即以 3 退出，不轮询等待锁。
- 锁由专用数据库连接持有；`CONTROL_PLANE_LOCK_POLL_SECONDS` 只控制该连接执行 `SELECT 1` 的存活探测周期。
- P0 使用固定锁 key 常量 `COORDINATOR_LOCK_KEY`，与后续持久租约行分开。advisory lock 是 P0 的已确认实现选择，不是 P1 的持久租约；它不能识别前任协调者，P1 引入持久租约/revision 后再承担该职责。
- 连接丢失、探测失败或数据库终止锁会话即失去资格：进程以 4 退出，不自动重新获取锁，等待人工重启。
- 部署侧不得对退出码 4 配置自动重启循环；P0 不提供自动恢复。
- 取得锁之前收到 SIGTERM/SIGINT：进程在开始获取协调锁前检查停止意图；若尚未开始连接则直接以 0 退出。若信号在连接/获取锁过程中到达，允许当前有界连接结束，取得锁后不发布 ready，立即释放并以 0 退出；不保留协调锁、连接或线程。
- 停止阶段再次收到 SIGTERM/SIGINT：幂等，不改变停止流程；最终仍以 0 退出（除非已因失锁进入 4）。
- 所有 stdout 帧写出一行后立即 `flush`；不得把 URL、密码或连接串写入 stdout/stderr。

### 帧契约

成功取得协调资格后向 stdout 写一行：

```json
{"event":"ready","lock_backend_pid":12345}
```

干净停止后写一行：

```json
{"event":"stopped"}
```

失去协调资格时向 stderr 写一行：

```json
{"event":"leadership_lost"}
```

### 退出码

| 退出码 | 含义 |
| --- | --- |
| 0 | 收到停止信号并干净退出，或启动未完成时收到停止信号 |
| 2 | 配置缺失或无效 |
| 3 | 已有协调者，拒绝重复启动 |
| 4 | 数据库不可用、启动失败或协调资格丢失 |

P0 不创建任何表，不执行 DDL，不调用 `create_all`。

## 验收案例

| ID | 案例 | 等级 | 优先级 |
| --- | --- | --- | --- |
| P0-01 | 顶层包导入无副作用 | 进程 | 阻塞 |
| P0-02 | 配置失败关闭且不泄露秘密 | 单元/进程 | 阻塞 |
| P0-03 | 空库启动并取得唯一协调资格 | 集成 | 阻塞 |
| P0-04 | 重复或并发启动只有一个协调者 | 进程 | 阻塞 |
| P0-05 | 信号停止释放资格并干净退出 | 进程 | 阻塞 |
| P0-06 | 协调连接丢失立即失去资格 | 进程/集成 | 阻塞 |
| P0-07 | 数据库不可达或黑洞连接有界失败 | 进程 | 阻塞 |
| P0-08 | 启动不执行 DDL | 集成 | 阻塞 |
| P0-09 | 测试守卫拒绝非一次性数据库 | 单元 | 阻塞 |
| P0-10 | 启停可重复且 stop 幂等 | 进程/单元 | 阻塞 |
| P0-11 | 事务时限下协调连接持续存活 | 集成 | 阻塞 |

### P0-01 顶层包导入无副作用

- 需求来源：架构第 7 节“后台组件显式启动、停止；模块导入不得读库、连接外部系统或启动线程”。
- 前置条件：设置一个不可达的数据库 URL，确保任何连接尝试都会失败或阻塞。
- 步骤：新进程执行 `python -c "import control_plane, sys, threading; ..."`。
- 预期：进程在 5 秒内以 0 退出；`sqlalchemy` 不在 `sys.modules`；除主线程外无线程；不读取旧 `.db_config.json`，不创建文件。
- 边界：本案例只约束顶层 `control_plane` 包；显式导入子模块允许加载 SQLAlchemy，但不允许建立连接。
- 副作用检查：无数据库连接、无子进程、无文件写入。
- 自动化：`ImportBoundaryTests.test_top_level_import_does_not_load_database_or_start_threads`。

### P0-02 配置失败关闭且不泄露秘密

- 需求来源：架构第 7 节；执行规范第 6.2 节；安全规范第 8 节。
- 场景：缺少 URL；使用 `sqlite:///...`；轮询或连接超时越界；URL 含密码；使用不支持的 PostgreSQL 驱动。
- 步骤：直接构造 `RuntimeSettings.from_env({...})`；并通过 CLI 以无效配置启动。
- 预期：抛出或返回配置错误，CLI 退出 2，不尝试连接、不输出 ready；错误和 `repr(settings)` 不包含 URL、用户名、密码或数据库名；`postgresql://` 被归一化为 pg8000，`postgresql+psycopg://` 被拒绝。
- 自动化：`ConfigurationTests`、`CliConfigurationTests`。

### P0-03 空库启动并取得唯一协调资格

- 需求来源：架构第 7 节“只允许一个有效协调实例”；执行规范第 6 节。
- 前置条件：一次性 PostgreSQL 数据库，`public` schema 无业务表；URL 由受守卫工厂生成。
- 步骤：启动 CLI，等待 ready 帧；用独立连接尝试获取同一 `COORDINATOR_LOCK_KEY`。
- 预期：ready 帧包含正整数 `lock_backend_pid`；独立连接无法获取锁；进程保持运行。
- 副作用检查：表数量不变；无额外子进程；日志不含 URL。
- 自动化：`StartupTests.test_empty_database_acquires_coordination`。

### P0-04 重复或并发启动只有一个协调者

- 需求来源：架构第 7 节；AGENTS“重复启动、Web 多 worker 或开发重载配置必须被拒绝”。
- 场景 A：已有协调者时启动第二个实例。
- 场景 B：三个实例几乎同时启动。
- 预期：场景 A 第二实例在 10 秒内退出 3，不产生 ready；第一个实例保持 ready。场景 B 恰好一个 ready，其余退出 3；无挂起进程。
- 副作用检查：协调锁始终只有一个持有者；失败实例不留下连接。
- 自动化：`StartupTests.test_duplicate_start_is_rejected`、`StartupTests.test_concurrent_start_has_single_leader`。

### P0-05 信号停止释放资格并干净退出

- 需求来源：架构第 7 节；AGENTS“后台线程显式启动和停止”。
- 步骤：对 ready 实例发送 SIGTERM；等待退出；再尝试获取同一锁；随后重新启动。
- 预期：退出码 0，stdout 出现 `{"event":"stopped"}`；锁可被测试连接获取；新实例可再次 ready。
- 边界：启动未完成时收到 SIGTERM/SIGINT 应退出 0；该黑盒用例使用未完成握手的 TCP 端点，未取得协调锁，因此锁释放由 P0-05 正常停止和 P0-06 失锁用例覆盖；停止阶段重复信号不改变退出路径；对持续发送合法 Notice 的握手端点，SIGTERM 也必须中断启动并有界退出。
- 副作用检查：无残留线程/进程；连接池已释放。
- 自动化：`StartupTests.test_sigterm_releases_coordination_and_allows_restart`、`StartupTests.test_stop_signal_before_ready_is_bounded`、`StartupTests.test_stop_signal_interrupts_chatty_handshake`。

### P0-06 协调连接丢失立即失去资格

- 需求来源：执行规范第 6.2 节“重启恢复、失去租约”、第 6.1 节“线程和容量不能靠超时强杀”；架构第 7 节唯一协调。
- 步骤：从 ready 帧读取 `lock_backend_pid`，使用独立管理连接执行 `pg_terminate_backend`，等待主进程退出。
- 预期：进程在 `CONTROL_PLANE_LOCK_POLL_SECONDS + 5s` 内以 4 退出，不输出新的 ready，不自动重新获取锁；锁已释放；错误信息不含数据库凭据。
- 自动化：`LeadershipTests.test_terminated_lock_backend_forces_shutdown`。

### P0-07 数据库不可达或黑洞连接有界失败

- 需求来源：执行规范第 6.1 节有限调用预算；安全规范第 8 节错误脱敏。
- 场景 A：回环地址上的未监听端口和包含唯一哨兵密码的 URL。
- 场景 B：本地 TCP 监听端接受连接但不完成 PostgreSQL 握手，验证 `CONTROL_PLANE_CONNECT_TIMEOUT_SECONDS` 生效。
- 场景 C：本地端点持续发送合法 PostgreSQL NoticeResponse 但不完成握手，验证总启动预算而非单次 socket 读超时。
- 预期：均在 `connect_timeout + 5s` 内以 4 退出，不输出 ready；stderr 不含哨兵密码、完整 URL 或驱动堆栈中的凭据。
- 自动化：`StartupTests.test_unreachable_database_fails_closed_without_secret_leak`、`StartupTests.test_silent_tcp_endpoint_respects_connect_timeout`、`StartupTests.test_chatty_handshake_respects_startup_deadline`。

### P0-08 启动不执行 DDL

- 需求来源：架构第 8 节“Schema 变更使用版本化 Alembic migration”；P0 范围约定。
- 步骤：在空库启动前和停止后查询 `information_schema.tables WHERE table_schema='public'`。
- 预期：两次均为 0；P0 不调用 `create_all`、不创建迁移表。
- 自动化：`StartupTests.test_startup_does_not_create_schema`。

### P0-09 测试守卫拒绝非一次性数据库

- 需求来源：AGENTS 验证约束；项目测试隔离要求。
- 场景：非 PostgreSQL URL、远程主机、数据库名不符合 `control_plane_test_` 前缀、非回环主机、带 `host/port/database` 等 query 覆盖参数。
- 预期：受守卫工厂拒绝连接，防止误用真实或共享数据库；合法的一次性 URL 通过；每次 P0 运行使用唯一数据库名；正常集成用例的 CLI URL 来自受守卫工厂，故障注入 URL 只指向回环未监听/黑洞端口或未创建数据库，不会连接共享数据库；测试脚本设置 `CONTROL_PLANE_REQUIRE_TEST_DATABASE=1`，缺少测试库时集成用例失败而不是静默跳过。
- 主机白名单：`127.0.0.1`、`localhost`、`::1`，或 Unix socket（无主机）；不接受任何 query 参数。
- 自动化：`TestDatabaseGuardTests`。

### P0-10 启停可重复且 stop 幂等

- 需求来源：架构第 7 节；AGENTS 显式启停约束。
- 步骤：单元级创建 runtime，连续 start/stop 两轮；对已停止实例再次 stop；进程级验证第二次启动成功。
- 预期：不抛异常、不重复持有锁、不残留连接；配置对象不可变。
- 自动化：`LifecycleUnitTests.test_runtime_restart_and_idempotent_stop`、`StartupTests.test_sigterm_releases_coordination_and_allows_restart`。

### P0-11 事务时限下协调连接持续存活

- 需求来源：执行规范第 6.2 节长期运行与失锁边界。
- 前置条件：一次性 PostgreSQL 17，使用 `ALTER ROLE postgres SET transaction_timeout = '2s'`。
- 步骤：启动 leader，锁存活探测周期 0.1 秒，等待 2.6 秒，再正常停止。
- 预期：进程保持运行，不出现 `leadership_lost`，退出 0。协调专用连接必须处于自动提交状态，心跳不得长期停留在 `idle in transaction`。
- 自动化：`StartupTests.test_heartbeat_survives_transaction_timeout`。

## 明确不纳入 P0

- 业务表、Alembic 业务 revision、持久协调租约行（P1）。
- 插件加载、Job/Step/Operation、恢复扫描、线程池（P2）。
- 模板、资源、配额、课程授权（P1/P2/P3）。
- HTTP API、模板驱动 UI、WebSSH（P5）。
- 旧系统数据迁移、旧库改表、真实 PVE/OpenWrt/SSH 调用。

## 执行证据

执行命令：

```sh
bash scripts/run_p0_tests.sh
```

默认 WSL，使用 Podman 一次性 PostgreSQL 17；脚本生成唯一数据库名，并在结束时删除容器。

- 2026-11-06 实现前基线：26 项中 2 项通过（顶层导入、测试守卫），24 项因 P0 骨架未实现或退出码为 99 失败；失败来自业务断言，不是导入、依赖或数据库环境失败。
- 实现后：32 项通过，0 失败、0 错误、0 跳过、0 未运行；使用默认 WSL、Python 3.12.15、Podman PostgreSQL 17。
- 独立复核后又收紧了失锁/连接超时上界、未就绪信号同步点、query 参数隔离和必需测试库守卫；未放宽任何业务预期。
- PR 复审后修复两项 P2：协调连接改为 AUTOCOMMIT，避免事务时限终止长期心跳；启动握手增加总预算和可中断信号，覆盖持续 Notice 但不完成握手的端点。新增 P0-11 与持续握手用例。
- 冻结哈希见 `p0_baseline.sha256`。
