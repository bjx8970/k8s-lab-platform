# Issue #1 独立验收记录

当前状态：补齐无导入副作用的 `tests/__init__.py` 后，两种全套发现方式均运行 101 项且全部通过：规范发现耗时 2.936 秒，默认发现耗时 2.918 秒；两者失败、错误、跳过均为 0，进程退出码均为 0。首次规范发现因缺少包标记而加载失败的历史记录保留如下，这是测试打包问题，不是生产安全断言失败。此记录不表示生产数据已迁移、Issue 已关闭或变更已合并。

## 验证方法与范围

`tests/test_issue1_acceptance.py` 导入真实 `app`、`modules.k8s_manager`、`modules.db`，通过 Flask 和 SocketIO 内存客户端调用真实接口。数据库替换为每个用例独享的临时 SQLite；首次导入 DB 模块时只屏蔽仓库 `.db_config.json` 的存在性检查，避免读取真实连接配置。测试中的口令、Token、私钥内容均为虚构标记。

D 组夹具已在应用导入期间同时屏蔽数据库启动初始化、状态监控和 `SSHManager._start_cleanup_thread`；本次夹具修正未替换业务函数或修改安全断言，已包含在默认发现方式通过的 101 项之中。

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
| 学生/教师原始提供器写操作返回 403 | 原安全契约测试、A 组跨 PVE 用例 | 仅隔离验证通过，包含拒绝前不得调用提供器 |
| 不可读取/取消/重试他人任务，无越权 WebSSH | 本组 Job 所属教师与出队撤权；A 组 WebSSH 撤权与通知隔离；TASK_RETRY 纯策略矩阵 | 已实现的读取、取消、WebSSH 边界仅隔离验证通过。当前无可执行重试 API，不能声称重试功能完成 |
| 浏览器 API、SocketIO、日志不含私钥或凭据 | 本组异常/任务输出与配置回存；C 组秘密文本/加密专项；A 组终端输出脱敏 | 仅隔离验证通过，覆盖 SQLAlchemy 异常、任务及终端输出；未进行真实终端/浏览器 E2E |
| 登录、退出、当前用户、CSRF、401/403、握手契约 | A 组真实认证接口契约及原测试 | 仅隔离验证通过，使用真实路由与内存用户/客户端 |
| HTTP/SocketIO 可配置 Origin 白名单 | A 组白名单为空、非空、拒绝来源及转发头伪造用例 | 仅隔离验证通过，包含真实 Engine.IO 传输入口 |
| 权限矩阵及越权审计自动化测试 | 原 authz 矩阵、本组真实审计输出、A/B/C 专项 | 补齐测试包标记后，规范及默认发现均 101 项通过；测试数量与结果一致，全部属于隔离验证 |

全部自动化验证即使通过，也仅代表上述隔离环境通过；不能据此声明真实提供器、浏览器或生产部署验收完成。

## Retry 范围与实际路由核查

已导入隔离的真实应用并遍历实际 `app.url_map`：共 108 条路由，路由地址或 endpoint 中包含 `retry` 的入口为 0（`RETRY_ROUTES=[]`）。这次只读检查在导入期间禁用线程启动，线程清单确认 `ADDED_THREADS=[]`；未执行测试套件、未读取真实 DB 配置或访问提供器。

当前无可执行重试 API。`TASK_RETRY` 角色/所有权纯策略已有矩阵验证用例，包括教师作为创建者、所属教师及无关教师等边界；这些策略用例不能证明重试工作流存在或完成。本次按协调决策不新增队列重试工作流，它属于后续任务生命周期范围。不存在的 retry URL 返回 404 属于路由不存在，不能表述为权限检查返回 403，也不以取消任务用例替代重试功能验收。

## 凭据迁移发布门槛

1. 安排停机窗口，停止 Web 服务及所有任务执行进程，确保迁移期间没有并发凭据写入。
2. 在迁移前备份数据库、应用版本和现有配置，并演练备份恢复。旧数据库备份可能含明文秘密，应按凭据材料限制访问。
3. 生成并安全配置 `K8S_LAB_CREDENTIAL_KEY`（Fernet 密钥），所有需要解密的服务进程使用同一密钥。不得提交到仓库、写入验收日志或前端配置。
4. 将密钥独立安全备份。丢失密钥会使已加密凭据不可恢复；恢复数据库备份并不会恢复密钥。禁止在密钥未知时重新生成一个密钥覆盖现有配置。
5. 在数据库副本上执行显式迁移入口 `modules.db.migrate_plaintext_credentials()`，确认失败回滚、成功解密和重复运行幂等。正常应用启动不能代替这一步。
6. 在受控生产维护窗口运行同一显式迁移，记录不含秘密的结果；完成授权人员的配置读取、普通字段回存和提供器连接核验后恢复服务。本文未执行这些生产步骤。

迁移审查检查点：迁移函数使用一个提交事务包住凭据变更；PostgreSQL 历史 `pve_servers.token_value` 和 `pve_servers.ow_password` 的短 VARCHAR 列在事务内扩为 TEXT，避免 Fernet 密文长度超限。C 组事务/DDL 模拟用例和本组 SQLite 幂等、故障回滚用例已包含在本次通过的 101 项中。PostgreSQL DDL、锁与事务行为尚无服务实测，不能标为 PostgreSQL 运行验证通过。

## 已执行记录与剩余集成验证

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

最终两个进程均再次确认实际路由共 108 条，retry 入口 0。规范发现包结构阻塞已解除，两种发现的测试数量、结果及隔离观测一致。没有真实 PostgreSQL、PVE、OpenWrt、SSH 或浏览器 E2E 验证；未执行生产迁移、提交或推送。重试功能未实现，不能从 TASK_RETRY 策略测试推断已有队列重试工作流；这项明确范围限制及真实环境验证仍保留。
