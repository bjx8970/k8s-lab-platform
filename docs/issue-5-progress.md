# Issue #5 实施进度检查点

记录时间：2026-09-15（Asia/Hong_Kong）  
工作分支：`codex/issue-5-p0-contracts`  
同步基线：`origin/main` / `8fd10d0`

> 第二轮复审（基于 `48d0ddf`）仍为 Request changes。本轮修复进行中：canonical target snapshot、pre-P0 升级验收、日志 append-only 与并发 quota、correlationId 测试。真实 PostgreSQL 与全仓回归证据补齐前，不得声称 #5 完成。

## 第二轮复审已确认解决

- v0001 与 runtime ORM 解耦（migration-local `v0001_legacy_baseline.sql`，纳入 checksum）
- `update_resource_fact()` Binding fencing 穿透（单一相关 EXISTS + JOIN）
- `sourceType` / `operationKey` / `attempt` 主体契约
- 约束测试假阳性（SQLSTATE + constraint name、真实 resourceVersion）
- INSERT 路径日志 quota 并发 race（owner counter 行锁）

## 本轮修复内容

### 1. Canonical target snapshot 与 idempotency

- `OperationAdmissionRepository.create()` 删除公开的 `target_snapshot` 和 `secret_version_ref` 参数，改由新增 `_resolve_target()` 在同一事务内读取并校验 Resource / Binding / Connection 后构造。
- snapshot 字段全部来自权威行：`resourceId`、`bindingId/revision`、`domainId`（取自 Binding）、`connectionId/revision`、`secretVersionRef`（取自 Connection）、`pluginId/pluginVersion/driverId`、`externalIdentity`（由 `external_key` 构造）、`locator`。
- digest 改为基于 canonical snapshot 计算，且在构造之后、dedup 之前。
- 先按 UID/revision 校验历史关系，dedup 未命中后才要求 Binding/Connection active，因此 retired Binding 的历史请求仍可幂等重放但不能新建 Operation。
- 唯一约束显式命名 `uq_rf_operation_transport` / `uq_rf_operation_plan_item`；并发插入在 savepoint 内按 constraint name 分类处理，失败后回查并比较 digest/source，不再使用无目标 `ON CONFLICT DO NOTHING`。
- Schema：新增 `domainId` 业务字符串定义并用于 Binding/Connection/targetSnapshot；Connection 的 `secretRef` 明确为 `secretVersionRef`。
- DDL：`rf_connections.credential_ref` 改名 `secret_version_ref TEXT NOT NULL`；`rf_operations.secret_version_ref` 改 `NOT NULL`；`tables.py` 补齐 Binding locator/external_identity 与 Connection secret 字段。

### 2. Pre-P0 数据库升级验收

- 新增 `PreP0MigrationTests`：独立 schema，在 `run_migrations()` 前手工建旧 `config` / `pve_servers`（不含 `template_vmid`/`ow_*`）/ `users`。
- 覆盖 PVE default server 创建、字段映射、port 字符串转换、`config.pve` 删除、OpenWrt 回填、`config.openwrt` 保留、密文逐字节保持、legacy 业务数据保留、rerun 稳定。
- 覆盖已有 PVE server 时不创建 default、不删除 `config.pve`。
- 覆盖错误 Fernet key 与明文凭据 fail-closed，并验证整事务回滚（无 revision、无 P0 表、无半迁移数据）与修正后重新成功。

### 3. 日志 append-only 与并发 quota

- 新增 `cp_reject_log_update()` 与 `trg_cp_log_append_only`，任何 `cp_logs` UPDATE 以 SQLSTATE `55000` / `ck_cp_log_append_only` 拒绝。
- quota 测试改为用服务端 `INSERT ... SELECT generate_series` 预填至恰好 16 MiB - 1。
- 新增真实并发测试：两个独立事务同时越界写入，恰好一个成功、一个命中 `ck_cp_log_owner_quota`，最终 SUM 与 counter 均为 16 MiB。
- 新增 UPDATE 绕过回归与 DELETE 释放配额一致性测试。

### 4. correlationId

- `_create_direct_op()` 支持显式 `correlation_id`；测试直接断言 repository 返回值与新 Session 查询值，删除原先的直接 SQL UPDATE。
- 新增重放测试：同 requestId 重放即使传入不同 correlationId，也返回原 Operation 与原 correlationId。

## 验证证据

环境：Python 3.14.4、SQLAlchemy 2.0.49、pg8000 1.31.5、cryptography 48.0.0、PostgreSQL 17.11（一次性本地实例，仅用于验收）。

命令与结果：

```text
python -m unittest tests.test_p0_contracts
  Ran 12 tests ... OK

K8S_LAB_TEST_POSTGRES_URL=... python -m unittest tests.test_p0_postgres_integration -v
  Ran 34 tests ... OK        # 0 skip / 0 failure

K8S_LAB_TEST_POSTGRES_URL=... python -m unittest discover -s tests -t .
  Ran 198 tests ... OK       # 含既有安全、授权、HTTP、Socket.IO、Job、WebSSH 回归
```

PG 覆盖重点：空库 baseline 与 rerun、legacy pre-P0 升级（含错误密钥整事务回滚与明文 fail-closed）、resourceVersion/outbox 原子性、allocation unique、Binding 身份唯一、canonical snapshot 与拆分列一致、transport/plan 双去重与同 requestId 不同目标冲突、lease/claimRevision fencing、旧 Binding 穿透回归、日志 byte_count/redacted/owner 约束、append-only 拒绝 UPDATE、临界 16 MiB 并发 quota、DELETE 释放配额。

执行期间同时修复了两处仅在真实 PostgreSQL 下才暴露的问题：

1. `RAISE EXCEPTION USING` 不支持 `CONSTRAINT_NAME`，已改为 `CONSTRAINT`（`ck_cp_log_owner_quota`、`ck_cp_log_append_only` 可被稳定识别）。
2. 测试 `SET search_path` 处于隐式事务中，会被期望回滚的用例一并回滚；已改为设置后立即 commit，避免连接复用后表解析失败。

## 待办

1. 更新 PR 描述并逐项回复复审，请求 re-review。不执行合并。
2. 真实 PVE/OpenWrt/K8s 的完整 smoke test 属于后续阶段（#9/#12/#15），不在 P0 范围。