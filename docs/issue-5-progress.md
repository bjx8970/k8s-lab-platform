# Issue #5 实施进度检查点

记录时间：2026-09-14（Asia/Hong_Kong）  
工作分支：`codex/issue-5-p0-contracts`  
同步基线：`origin/main` / `8fd10d0`

> 全部 9 项已确认的审查问题已完成修复。快速测试 8/8 通过。PostgreSQL 集成测试已扩展至 20 项，待提供 `K8S_LAB_TEST_POSTGRES_URL` 后执行。

## 已修复项

### 1. Schema/DDL/Table 字段对齐
- Binding active identity 统一为 `(domain_id, driver_id, external_key)`，DDL 新增 `driver_id VARCHAR(128) NOT NULL`、`external_key VARCHAR(255) NOT NULL`、`locator JSONB NOT NULL DEFAULT '{}'`，唯一索引改为 `uq_rf_binding_identity ON (domain_id, driver_id, external_key) WHERE active`
- Resource Schema 移除 `connectionId`（connection 是 Binding 的访问端点，不是 Resource 的身份组成部分）
- Allocation value 统一为规范化字符串
- Operation DDL/tables 新增 `operation_key VARCHAR(255)`、`attempt BIGINT` 及 `UNIQUE(operation_key, attempt)`
- Condition Schema 新增 `x-uniqueBy: "type"` 扩展声明
- Connection 表的 `resource_version` 列依赖 DEFAULT，不再在 helper 中显式插入

### 2. 数据库约束
- `pending_external` 必须存在 externalTaskRef: `CHECK(phase <> 'pending_external' OR external_task_ref IS NOT NULL)`
- 日志 exactly-one 归属: `CHECK((operation_uid IS NOT NULL) <> (task_uid IS NOT NULL))`
- 日志 redacted 强制: `CHECK(redacted)`（而非仅 DEFAULT TRUE）
- 日志 byte_count 校验: `CHECK(byte_count = octet_length(chunk))`
- 每 Operation/Task 16 MiB 总量通过 trigger function `cp_enforce_log_quota()` 校验

### 3. Environment 写入边界
- `remove_cleanup_finalizer()` 改为 `func.array_remove(finalizers, FINALIZER)`，仅移除指定 finalizer，保留其他 finalizer

### 4. Operation admission 与双层去重
- `OperationAdmissionRepository.create()` 新增 target snapshot 一致性校验：在同事务中验证 Resource 存在、Binding active 且匹配 resource/binding revision、Connection active 且匹配 binding/connection revision、domain 一致
- 新增 `operation_key` / `attempt` 参数及 plan-item 去重逻辑
- Repository 导入 `bindings`、`connections` 表用于校验

### 5. Executor fencing、恢复与取消原语
- `claim()` 排除 `cancelling` phase；`pending_external` 接管后保留 phase；仅首次 claim 设置 `started_at`（`func.coalesce`）
- `update_execution()` 使用 `_UNSET` sentinel 区分"未提供"与"显式清空"，未提供时保留 externalTaskRef、execData
- `update_resource_fact()` 新增 `binding_fresh` 条件：验证 Operation 固定的 Binding 仍是 Resource 当前 active Binding 且 revision 匹配

### 6. 测试扩展
- 快速契约测试新增 `test_field_alignment_schema_ddl_table`：校验 Schema、DDL、tables.py 核心字段一致性
- PostgreSQL 集成测试从 3 项扩展至 20 项，覆盖 migration baseline、environment boundary/finalizer、allocation unique、lease fencing、binding identity、target snapshot 校验、transport/plan-item dedup、cancelling 安全性、pending_external phase 保留、started_at 保留、externalTaskRef 保留、binding fencing、log byte_count/owner/redacted 约束

## 待办

1. 提供 `K8S_LAB_TEST_POSTGRES_URL` 执行完整 PG 集成测试
2. 修复/重建与当前 Python ABI 匹配的依赖环境，执行全仓回归
3. PG 验收通过后创建 Draft PR，最终 Ready 后使用 `Closes #5`