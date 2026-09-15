# Issue #5 实施进度检查点

记录时间：2026-09-15（Asia/Hong_Kong）  
工作分支：`codex/issue-5-p0-contracts`  
同步基线：`origin/main` / `8fd10d0`

> PR #16 审查反馈已逐项修复。快速测试 9/9 通过。PostgreSQL 集成测试已扩展至 24 项，待提供 `K8S_LAB_TEST_POSTGRES_URL` 后执行。

## 审查反馈修复

### Blocker 1：v0001 与运行时 ORM 解耦
- `v0001_control_plane_p0.py` 不再 import `modules.db.Base`
- 新增 `v0001_legacy_baseline.sql` 冻结 legacy 表结构（users/classes/groups/group_members/pve_servers/clusters/vms/config）
- `checksum_files` 包含两个 migration-local SQL 文件
- 新增 `test_migration_checksum_independent_of_runtime_orm` 证明 runtime ORM 变化不影响 checksum

### Blocker 2：旧 PVE/OpenWrt 数据迁移恢复
- `_migrate_pve_config(connection)` 冻结原 `migrate_pve_config()` 语义：空 pve_servers 时从 config.pve 创建 default server 并删除 config
- `_migrate_openwrt_to_pve_servers(connection)` 冻结原语义：回填空 ow_host 的 server，保留 config.openwrt
- `_validate_secret()` 冻结 `enc:v1:` + Fernet fail-closed 验证，不 import 运行时 credential_store
- 所有 DML 使用绑定参数，错误不泄露 secret/JSON 内容
- `modules/db.py` 移除 7 个已失效的结构迁移 helper（`_ensure_db_indexes`、`_migrate_user_name` 等），保留 `migrate_pve_config` 和 `_migrate_openwrt_to_pve_servers` 供既有安全测试使用

### Blocker 3：Binding fencing 穿透修复
- `update_resource_fact()` 改为单一相关 EXISTS + JOIN：`operations JOIN bindings ON binding.uid=op.binding_uid AND binding.revision=op.binding_revision AND binding.resource_uid=op.resource_uid AND binding.active=TRUE WHERE op.uid=operation_uid AND op.lease_owner=worker_id AND op.claim_revision=claim_revision AND op.lease_until>now()`
- 新增 `test_old_binding_cannot_write_resource_fact`：O1/B1 claim → rebind B2 → O2/B2 claim → O1 拒写 → O2 成功写

### Major 1：Operation 契约统一
- Schema 新增 `sourceType`（plan/direct）和 `correlationId` 为公共必填
- `oneOf` 条件分支：plan 要求 operationKey+attempt，direct 禁止二者
- DDL 新增 `source_type VARCHAR(16) NOT NULL`、`correlation_id VARCHAR(255) NOT NULL`
- 命名约束：`ck_rf_operation_source_type`、`ck_rf_operation_source_fields`、`ck_rf_operation_attempt_nonnegative`、`ck_rf_operation_lease_pair`、`ck_rf_operation_pending_external_ref`
- `resource_uid`、`binding_uid`、`binding_revision`、`connection_uid`、`connection_revision` 改为 NOT NULL
- Repository `create()` 新增 `source_type`、`correlation_id` 必填参数，plan dedup 命中时校验 digest 一致性
- 新增 `test_plan_operation_requires_key_and_attempt`、`test_direct_operation_rejects_plan_fields`、`test_correlation_id_persisted`、`test_transport_dedup_different_digest_raises_conflict`

### Major 2：PG 测试假阳性修正
- 日志测试使用 `_insert_log()` helper 满足所有非目标约束（含 `expires_at`），断言 SQLSTATE `23514` 和具体 constraint name
- `test_observed_generation_cannot_exceed_generation` 使用 `create()` 返回的真实 resourceVersion，失败后验证对象未变化
- `test_allocation_unique_and_lease_fencing` 使用完整 direct Operation（含 resource/binding/connection）
- 所有 Operation 创建通过 `_create_direct_op()` helper 提供完整必填字段

### Major 3：日志 quota 并发硬保证
- 新增 `cp_log_owner_counters` 表：`(owner_kind, owner_uid)` PK，`used_bytes`/`max_bytes`，`ck_cp_log_owner_quota` CHECK
- BEFORE INSERT trigger：`INSERT ... ON CONFLICT DO NOTHING` 初始化 counter → 条件原子 `UPDATE ... WHERE used_bytes <= max_bytes - NEW.byte_count RETURNING` → 无返回行则 RAISE EXCEPTION（SQLSTATE 23514，constraint ck_cp_log_owner_quota）
- AFTER DELETE trigger：`GREATEST(used_bytes - OLD.byte_count, 0)` 原子扣减
- 日志表约束改为命名约束：`ck_cp_log_exactly_one_owner`、`ck_cp_log_byte_count`、`ck_cp_log_redacted`
- 新增 `test_log_quota_exceeded`

## 验证证据

- `python -m py_compile` 全部通过
- P0 快速测试：9/9 通过
- PostgreSQL 集成测试：24 项待执行（需 `K8S_LAB_TEST_POSTGRES_URL`）
- `git diff --check`：待执行

## 待办

1. 提供 `K8S_LAB_TEST_POSTGRES_URL` 执行完整 PG 集成测试（24 项）
2. 修复/重建与当前 Python ABI 匹配的依赖环境，执行全仓回归
3. PG 验收通过后更新 PR 描述，逐项回复审查意见，请求 re-review