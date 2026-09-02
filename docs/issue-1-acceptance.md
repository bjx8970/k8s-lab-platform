# Issue #1 独立验收记录

当前结论（2026-09-02）：Issue 原六项安全代码验收通过，依据为真实接口与隔离环境测试。本轮 canonical/default 完整发现各执行一次，均 152/152 通过，分别耗时 8.807 秒和 9.121 秒，失败/错误/跳过均为 0。已包含 D 新增 13 条真实 retry 验收；D 专项此前单次 13/13、4.666 秒通过，本次未单独重复。未执行生产迁移、部署、Issue 关闭或合并。

上一轮基线（本轮起点 HEAD `76ec3ce`）在补齐无副作用的测试包标记后，两种发现方式均 101/101 通过；本轮增加 51 条重试测试，最终实际总数为 152。以下保留上一轮首次测试加载失败及修正历史，不把测试打包错误当作生产安全修复失败，也不把历史 101 项混作本轮结果。

## 验证方法与范围

`tests/test_issue1_acceptance.py` 导入真实 `app`、`modules.k8s_manager`、`modules.db`，通过 Flask 和 SocketIO 内存客户端调用真实接口。数据库替换为每个用例独享的临时 SQLite；首次导入 DB 模块时只屏蔽仓库 `.db_config.json` 的存在性检查，避免读取真实连接配置。测试中的口令、Token、私钥内容均为虚构标记。

D 组夹具在应用导入期间同时屏蔽数据库启动初始化、状态监控和 `SSHManager._start_cleanup_thread`；该导入隔离保留在本轮 152 项中。重试夹具另外隔离三个私有容器，但不伪造可信描述、不替换真实授权或重试服务、不修改原安全断言。

队列入队边界被捕获，后台闭包由测试显式调用；PVE/OpenWrt 构造器及必要的执行函数被模拟。没有真实 PVE、OpenWrt、SSH、浏览器 E2E 或 PostgreSQL 运行验证。登录、当前用户、退出、Origin 三组合、跨 PVE 及旧 Socket 撤权专项由 A 组测试提供，不在本文件复制基础测试。

## 独立回归用例

| 用例 | 必须满足的预期 |
| --- | --- |
| 单个/批量创建 | 教师使用他人分组或课程返回 403；同一教师拥有的组课不匹配返回 400；管理员传入不存在的关联也返回 400；所有拒绝均不得入队或调用提供器 |
| 合法关联 | 教师自己的组课可创建及批量创建，返回 202，按预期入队 |
| 配置读取后保存 | 管理员 GET 服务器列表，仅改名称后 PUT 整个返回对象；Token 名称保持，数据库秘密列为密文且解密值不变 |
| 新建占位凭据 | 提交 `[REDACTED]` 返回 400，服务器表无新增行 |
| Job 所属教师 | 管理员通过真实部署接口为教师集群创建任务；该教师可列表、详情、日志、取消；其他教师列表不可见且详情、日志、取消返回 403，拒绝取消不调用执行函数 |
| 出队前撤权 | 教师入队后在临时数据库被禁用；执行真实队列闭包时不调用部署或提供器 |
| SQLAlchemy 异常 | 真实后台异常路径、日志写入及任务更新收到包含虚构多行 OpenSSH 私钥和密码的 StatementError/DBAPIError；任务 HTTP 响应及真实 task_update 接收内容不含秘密标记 |
| 审计实际输出 | root 为 WARNING 时，成功 HTTP 写操作仍到达实际 capture handler；Socket 越权拒绝记录操作者、动作、资源，不记录密码或输入内容 |
| 显式迁移 | SQLite 旧明文迁移后全部为可解密密文；再次运行密文保持不变；中途注入加密失败后所有数据回滚 |

## Issue 原六项验收映射

| 原验收项 | 证据来源 | 当前结论 |
| --- | --- | --- |
| 学生/教师原始提供器写操作返回 403 | `test_security_contract`、`test_socket_security`，跨 PVE 与拒绝后零执行用例 | 通过（真实接口、隔离提供器），两种全套一致 |
| 不可读取/取消/重试他人任务，无越权 WebSSH | `test_job_security`、`test_job_retry`、`test_socket_security`、D 新重试验收；当前执行者、通知边界、worker 再授权和资源指纹 | 通过（真实 retry，不再仅凭纯策略）；越权拒绝、一次直接 child、失败恢复均通过 |
| 浏览器 API、SocketIO、日志不含私钥或凭据 | `test_secret_boundaries`、D 原异常用例和新重试用例；SQLAlchemy/PEM、私有描述、入队失败与审计脱敏 | 通过（虚构秘密、真实输出边界）；临时 SQLite 加密及迁移回归通过 |
| 登录、退出、当前用户、CSRF、401/403、握手契约 | `test_http_security`、`test_http_retry`、`test_socket_security`、D retry 正反路径 | 通过（真实 HTTP/SocketIO 内存客户端）；不等同浏览器 E2E |
| HTTP/SocketIO 可配置 Origin 白名单 | HTTP/Socket Origin 组合、Engine.IO 检查、retry 非允许 Origin 拒绝 | 通过（隔离客户端），两种全套一致 |
| 权限矩阵及越权审计自动化测试 | `test_authz`、Job/Socket 用例和真实 audit handler；root WARNING 下 job.retry success/denied/failure | 通过；两种发现均 152 项，0 失败/错误/跳过，0 重复收集 |

六项安全代码验收均通过，证据范围为真实业务接口与隔离环境自动化；没有操作真实生产基础设施不等于本 Issue 代码未完成。生产凭据配置、备份与显式迁移另列为发布门槛，不混作代码验收的前置条件，也不声称已经生产部署验证。

## 本轮 Retry 范围及上一轮路由历史

本轮新增契约为 `POST /api/k8s/tasks/<task_id>/retry`：登录、CSRF、Origin 和真实服务授权共同保护；成功返回 202 及新 task_id/retry_of，权限不足 403，管理员访问不存在的源返回 404，其他角色可能先因 TASK_RETRY 授权失败返回 403；状态/描述/重复重试冲突 409。未知入队异常返回安全 500，释放重试占用且不留下虚假的运行中子任务；恢复队列后同源请求仍可 202。成功审计关联真实 child；拒绝/失败审计关联操作者与源，不要求失败必有 child。D 专项已验证实际 url_map 恰有一个该 POST 入口及上述正反路径，未把源码文本匹配作为验收。

安全支持范围：有服务端描述的 deploy/delete 在 error 或 cancelled 后可重试；create 已成功、随后 auto-deploy 失败时只重试 deploy。create 阶段失败返回 409，不重放 VM 创建，必须先检查已产生的资源并按补偿/清理流程处理。历史任务无安全描述、仍在执行或已完成、同源已有直接重试子任务等返回 409；不能通过客户端补传描述扩大执行范围。详见 [Job 安全重试](job-retry.md)。

Job 仍使用既有进程内队列，历史约保留 30 分钟；重试描述与源任务同生命周期。本 Issue 不新增持久 Job、跨进程或重启恢复能力，不能声称重启后能恢复历史任务。源记录已消失时管理员得到 404，其他角色可能先得到 403；通过授权且记录尚在而无可执行描述时返回 409。

上一轮历史：当时实际 `app.url_map` 为 108 条、retry 入口 0，故当时准确记录了“无可执行重试 API，只有 TASK_RETRY 纯策略用例”。该旧决策已由本轮真实 retry 工作流扩展替代，不能继续作为当前支持范围结论；上一轮的路由 404 也不应回溯描述为授权 403。

## 本轮独立 Retry 专项结果

新 TestCase 不继承原验收类，不重复计入旧测试。复制最小应用/临时 SQLite 初始化并复用无测试类导入的安全 DB/capture helper；源任务通过真实 HTTP/manager 入口创建并由捕获 worker 模拟失败，不直接伪造私有重试描述。`_task_retry_descriptions`、`_task_retry_reservations`、`_task_retry_pending` 逐用例替换为空容器，仅隔离残留状态，不预置可信描述。

验收覆盖：真实 route/login/CSRF/Origin/404；无关教师及已分组学生 403 且无入队；教师重试管理员任务后新执行者/所属教师正确；新任务详情、日志、列表及 task_update 隔离；同源重复 409 且仅一次入队；出队前禁用用户或变更 provider/VM 指纹不得执行；deploy/delete 的 error/cancelled；running/completed/历史无描述冲突；create 失败不得复制 VM；auto-deploy 失败只重试 deploy；root WARNING 下 job.retry 成功/拒绝真实审计；入队含秘密异常 500、失败审计且无成功审计、无僵尸子任务、恢复后可再次重试。

实际于本轮运行一次 `tests.test_issue1_retry_acceptance`：`Ran 13 tests in 4.666s`，`OK`；外层计时 4.666576 秒，进程退出码 0。失败 0、错误 0、跳过 0、预期失败 0、意外成功 0，无需派回 A/B 的失败。AST 解析通过，13 个独立测试方法，不含继承重复计数。

实际内存 runner 设置下列路径并加载专项，在加载前增加真实凭据文件、网络、非 SQLite 引擎及真实数据库连接拒绝护栏；以下为同一专项的常规复现入口（不含外层护栏代码）：

```powershell
$env:PYTHONPATH='D:\bjx897\Documents\code\k8s-lab-platform\tmp\security-test-deps;D:\bjx897\Documents\code\k8s-lab-platform'
python -B -c "import os, unittest, sys; os.chdir(r'D:\bjx897\Documents\code\k8s-lab-platform'); print(os.getcwd()); r=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromName('tests.test_issue1_retry_acceptance')); sys.exit(not r.wasSuccessful())"
```

此次全部护栏触发为 0，创建 13 个临时 SQLite 引擎。测试结束存活线程为 MainThread、worker-create、worker-delete、两个 worker-deploy；后四个为既有进程内队列的空闲线程，无 SSH 清理线程。没有连接真实 DB/PVE/OpenWrt/SSH，没有浏览器 E2E、生产迁移或部署。

上述 13 个独立测试随后全部纳入下面两次完整发现，三个私有容器隔离和入队 500 后恢复用例均未删减或放宽断言。

## 本轮最终完整回归（含真实 Retry）

在 A/B 最终兼容补丁完成后，规范发现与默认发现各运行一次，分别使用全新 Python 进程。既有依赖目录不变，未安装依赖、修改生产代码或真实配置。实际加载入口如下；内存 runner 在入口外增加下述安全护栏和统计，不改用例集合：

```powershell
$env:PYTHONPATH='D:\bjx897\Documents\code\k8s-lab-platform\tmp\security-test-deps;D:\bjx897\Documents\code\k8s-lab-platform'
$env:PYTHONIOENCODING='utf-8'
python -B -c "import os, unittest, sys; os.chdir(r'D:\bjx897\Documents\code\k8s-lab-platform'); print(os.getcwd()); r=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover('tests', top_level_dir='.')); sys.exit(not r.wasSuccessful())"
python -B -c "import os, unittest, sys; os.chdir(r'D:\bjx897\Documents\code\k8s-lab-platform'); print(os.getcwd()); r=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover('tests')); sys.exit(not r.wasSuccessful())"
```

| 发现入口 | 发现/实际运行/通过 | 失败 | 错误 | 跳过 | unittest 耗时 | 外层计时 | 退出码 |
| --- | --- | ---: | ---: | ---: | --- | --- | ---: |
| canonical：`discover('tests', top_level_dir='.')` | 152 / 152 / 152 | 0 | 0 | 0 | 8.807 秒 | 8.806918 秒 | 0 |
| default：`discover('tests')` | 152 / 152 / 152 | 0 | 0 | 0 | 9.121 秒 | 9.120765 秒 | 0 |

两次预期失败与意外成功也均为 0。原 101 项加新 `test_http_retry` 9 项、`test_job_retry` 29 项、D `test_issue1_retry_acceptance` 13 项，共增加 51 项；未重复继承原验收类。按去除可选 `tests.` 前缀后的完整用例 ID 检查，重复收集均为 0。

| 模块（canonical 加 `tests.` 前缀） | 两种发现各自实际用例数 |
| --- | ---: |
| test_authz | 16 |
| test_http_retry | 9 |
| test_http_security | 4 |
| test_issue1_acceptance | 10 |
| test_issue1_retry_acceptance | 13 |
| test_job_retry | 29 |
| test_job_security | 25 |
| test_secret_boundaries | 14 |
| test_security_contract | 10 |
| test_socket_security | 22 |
| 合计 | 152 |

两次护栏观测一致：对 `.db_config.json`、`.secret_key`、`.credential_key` 及旧提供器/集群配置文件的读取拦截触发 0；真实网络连接触发 0；非 SQLite 引擎及仓库内真实数据库连接触发 0。每次创建 24 个测试 SQLite 引擎。护栏覆盖加载和执行全过程，临时 SQLite 在测试后清理；不存在真实 DB/PVE/OpenWrt/SSH 访问或部署。

两次发现前均只有 MainThread；发现后均增加 worker-create、worker-delete、两个 worker-deploy，属于生产队列首次导入启动的 4 个空闲守护线程。测试结束仍为这五个线程，相对发现完成新增存活线程为 0，无 SSH 清理线程遗留。不能将此描述为“从未启动后台线程”。

canonical 只有 `tests.test_security_contract`、`tests.test_http_security`、`tests.test_issue1_acceptance` 这三种已检查 helper 名称。default 同时存在各自带与不带 `tests.` 前缀的模块别名，但没有重复收集测试，没有额外真实数据库连接或新增遗留线程；结果和用例数一致。优先使用 canonical，以避免双名称导入。

两次均检查实际 `app.url_map`：共 109 条路由，其中 retry 入口恰有一条 `/api/k8s/tasks/<task_id>/retry`，endpoint 为 `k8s_retry_task`，methods 为 POST/OPTIONS。不存在本轮“无 retry 接口”的结论；下方旧 108 条/0 retry 只保留为历史。

本轮无测试失败需要派回 A/B/C，未改安全断言。真实 PostgreSQL DDL/事务、PVE、OpenWrt、SSH、浏览器 E2E、生产迁移与部署未运行，属于另列环境/发布限制，不否定上述六项安全代码验收结论。

## 凭据迁移发布门槛

1. 安排停机窗口，停止 Web 服务及所有任务执行进程，确保迁移期间没有并发凭据写入。
2. 在迁移前备份数据库、应用版本和现有配置，并演练备份恢复。旧数据库备份可能含明文秘密，应按凭据材料限制访问。
3. 生成并安全配置 `K8S_LAB_CREDENTIAL_KEY`（Fernet 密钥），所有需要解密的服务进程使用同一密钥。不得提交到仓库、写入验收日志或前端配置。
4. 将密钥独立安全备份。丢失密钥会使已加密凭据不可恢复；恢复数据库备份并不会恢复密钥。禁止在密钥未知时重新生成一个密钥覆盖现有配置。
5. 在数据库副本上执行显式迁移入口 `modules.db.migrate_plaintext_credentials()`，确认失败回滚、成功解密和重复运行幂等。正常应用启动不能代替这一步。
6. 在受控生产维护窗口运行同一显式迁移，记录不含秘密的结果；完成授权人员的配置读取、普通字段回存和提供器连接核验后恢复服务。本文未执行这些生产步骤。

迁移审查检查点：迁移函数使用一个提交事务包住凭据变更；PostgreSQL 历史 `pve_servers.token_value` 和 `pve_servers.ow_password` 的短 VARCHAR 列在事务内扩为 TEXT，避免 Fernet 密文长度超限。C 组事务/DDL 模拟用例和本组 SQLite 幂等、故障回滚用例均已在本轮两次 152 项回归中通过。PostgreSQL DDL、锁与事务行为尚无服务实测，不能标为 PostgreSQL 运行验证通过。

## 上一轮已执行历史（本轮 Retry 不在该结果内）

按主会话通知，仅执行本组 10 条测试，实际命令如下：

```powershell
$env:PYTHONPATH='D:\bjx897\Documents\code\k8s-lab-platform\tmp\security-test-deps;D:\bjx897\Documents\code\k8s-lab-platform'
python -B -c "import os, unittest, sys; os.chdir(r'D:\bjx897\Documents\code\k8s-lab-platform'); print(os.getcwd()); suite=unittest.defaultTestLoader.loadTestsFromName('tests.test_issue1_acceptance'); r=unittest.TextTestRunner(verbosity=2).run(suite); sys.exit(not r.wasSuccessful())"
```

结果：进程退出码 0；`Ran 10 tests in 2.521s`，`OK`。创建关联用例另有 7 个拒绝分支和 2 个合法分支；异常脱敏覆盖 StatementError 与 DBAPIError。未修夹具、未修改或放宽断言，没有需要派回 A/B/C 的失败。SQLite 显式迁移的幂等和中途失败全量回滚均已实测通过。

集成后的首次执行顺序为：先规范发现，再原文档默认发现，各一个独立 Python 进程、各一次。首次规范发现失败后，主会话明确扩展 D 组写集合，允许新增只含模块文档字符串的 `tests/__init__.py`；确认文件原先不存在并新增后，按新授权再运行规范及默认发现各一次。实际内存执行脚本在以下发现入口外增加凭据文件读取、非 SQLite 引擎创建、真实网络连接的拒绝护栏，并统计线程及模块别名；未改变生产业务函数或安全断言。常规复现入口如下：

```powershell
$env:PYTHONPATH='D:\bjx897\Documents\code\k8s-lab-platform\tmp\security-test-deps;D:\bjx897\Documents\code\k8s-lab-platform'
python -B -c "import os, unittest, sys; os.chdir(r'D:\bjx897\Documents\code\k8s-lab-platform'); print(os.getcwd()); r=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover('tests', top_level_dir='.')); sys.exit(not r.wasSuccessful())"
python -B -c "import os, unittest, sys; os.chdir(r'D:\bjx897\Documents\code\k8s-lab-platform'); print(os.getcwd()); r=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover('tests')); sys.exit(not r.wasSuccessful())"
```

实际运行历史及最终结果：

| 阶段与发现方式 | 实际运行 | 通过 | 失败 | 测试错误 | 跳过 | 测试运行耗时 | 进程退出码 |
| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| 首次规范发现 | 0 | 0 | 0 | 未进入测试，发现阶段 1 个 ImportError | 0 | 未进入 runner，未产生测试运行耗时 | 1 |
| 首次默认发现 | 101 | 101 | 0 | 0 | 0 | unittest 3.910 秒；外层计时 3.910176 秒 | 0 |
| 补包标记后规范发现 | 101 | 101 | 0 | 0 | 0 | unittest 2.936 秒；外层计时 2.935777 秒 | 0 |
| 补包标记后默认发现 | 101 | 101 | 0 | 0 | 0 | unittest 2.918 秒；外层计时 2.918270 秒 | 0 |

默认方式按实际发现模块分组：`test_authz` 16、`test_http_security` 4、`test_issue1_acceptance` 10、`test_job_security` 25、`test_secret_boundaries` 14、`test_security_contract` 10、`test_socket_security` 22。预期失败 0、意外成功 0。

首次规范发现的实际堆栈（已通过补齐测试包标记解决）：

```text
File "<string>", line 48, in <module>
    suite = unittest.defaultTestLoader.discover('tests', top_level_dir='.')
File "C:\Program Files\Python314\Lib\unittest\loader.py", line 334, in discover
    raise ImportError('Start directory is not importable: %r' % start_dir)
ImportError: Start directory is not importable: 'D:\\bjx897\\Documents\\code\\k8s-lab-platform\\tests'
```

首次失败后的只读检查确认：`tests` 目录存在、`tests/__init__.py` 不存在，`find_spec('tests').origin` 为 `None`，当时属于命名空间包。此处是测试发现/包结构问题，不是生产安全断言失败。后续按主会话新授权新增 `tests/__init__.py`，其内容仅为一句模块文档字符串，没有导入、线程启动或其他副作用；未更改业务代码、发现规则或安全断言。

默认发现过程中实际同时出现 `tests.test_security_contract` / `test_security_contract`、`tests.test_http_security` / `test_http_security` 两组模块别名。补包标记后的规范发现仅出现 `tests.test_security_contract` 和 `tests.test_http_security`，不产生这两组双名称。两种发现的 101 项测试数量、通过结果和隔离护栏结果均一致，但不能声称默认发现没有重复导入；后续优先采用规范发现入口。

补包标记后两个独立运行的隔离观测均为：真实凭据文件访问、非 SQLite 引擎创建、真实网络连接的护栏触发为 0；各创建 11 个测试 SQLite 引擎。各进程在发现阶段由生产队列模块首次导入启动 4 个空闲守护工作线程（create 1、delete 1、deploy 2）；测试结束相对发现完成时额外存活线程为 0，没有新增遗留 SSH 清理线程。不是全进程从未创建任何线程，亦未实际连接真实数据库或提供器。

上一轮结束时两个进程均确认实际路由 108 条、retry 入口 0，规范发现包结构阻塞已解除，测试数量、结果及隔离观测一致。当时重试功能未实现，仅有 TASK_RETRY 策略；这段记录是历史，不代表本轮新增支持范围。真实 PostgreSQL、PVE、OpenWrt、SSH、浏览器 E2E 及生产迁移未执行；本轮按上述真实接口加隔离证据进行代码验收，发布门槛另行保留。
