# PVE VM 完整身份迁移

本次升级将 VM 永久身份从裸 `vmid` / `(node, vmid)` 改为 `(pve_server_id, vmid)`。`node` 仅表示当前定位信息，VM 在同一 PVE server 内迁移节点后身份不变。

## 上线前

1. 停止应用写入，并备份 PostgreSQL 或 SQLite 数据库。
2. 使用当前应用配置执行只读审计：

   ```bash
   .venv/bin/python scripts/audit_pve_vm_identity.py
   ```

3. 也可使用 `--url-env K8S_LAB_TEST_POSTGRES_URL` 只读审计一次性 PostgreSQL 环境；URL 只通过环境变量传入，不写入脚本或仓库。输出包含 `vm_total`、`backfillable`、逐条映射和问题行 ID。
4. 只有输出中的 `ready` 为 `true` 时才能继续。以下情况必须人工修复，迁移不会猜测默认平台：
   - VM 没有 Cluster；
   - Cluster 的 `pve_server_id` 为空、为 `0` 或引用不存在的 server；
   - 同一 PVE server 内存在重复 VMID；
   - 已填写的 VM 平台 ID 与 Cluster 冲突、VMID 或 node 定位信息无效。

## 迁移行为

PostgreSQL 启动迁移依次执行冻结的 `v0001` 和新增的 `v0002_pve_vm_identity`。`v0002` 在一个事务中完成回填、预检、删除 `vmid` 全局唯一约束、增加外键/正整数检查和 `(pve_server_id, vmid)` 复合唯一约束。任一预检或 DDL 失败都会回滚整个版本。

开发或测试使用的旧 SQLite 表通过 `modules.db.upgrade_sqlite_vm_identity()` 事务化重建；失败时原表、索引和触发器保留。上线前先在数据库备份上演练，不要让旧版应用与新身份格式并行写入。

一次性 PostgreSQL 验收示例：

```bash
podman run -d --rm --name k8s-lab-issue2-pg -e POSTGRES_PASSWORD=test-only \
  -e POSTGRES_USER=issue2 -e POSTGRES_DB=issue2 -p 127.0.0.1::5432 postgres:16-alpine
podman port k8s-lab-issue2-pg 5432
# 用显示的临时端口组成 K8S_LAB_TEST_POSTGRES_URL，再运行：
K8S_LAB_TEST_POSTGRES_URL=... .venv/bin/python -m unittest discover -s tests -t . -p 'test_*.py'
podman stop k8s-lab-issue2-pg
```

禁止修改已发布的三个 v0001 文件，否则 migration runner 会报告 checksum drift 并停止启动。

## API 兼容期

规范 VM 路由为：

```text
/api/pve/servers/{pve_server_id}/vms/{vmid}/status
/api/pve/servers/{pve_server_id}/vms/{vmid}/config
/api/pve/servers/{pve_server_id}/vms/{vmid}/start
/api/pve/servers/{pve_server_id}/vms/{vmid}/stop
/api/pve/servers/{pve_server_id}/vms/{vmid}/reboot
/api/pve/servers/{pve_server_id}/vms/{vmid}
```

旧 `/api/pve/vms/{node}/{vmid}/...` 路由暂时保留，但必须显式提供 `pve_server_id`，并返回 `Deprecation: true`。缺少平台身份时返回 400，且不会调用任何 PVE client。

批量状态请求项包含 `pve_server_id` 和 `vmid`，响应与 Socket.IO 状态事件包含平台身份、VMID 和当前 `node`；客户端 key 使用 `pve-{pve_server_id}-vm-{vmid}`。规范 URL 不包含 node，服务端从授权后的数据库资源解析当前 locator。集群关机投票必须提交平台 ID，执行前按当前 VM/Cluster 重新授权并更新 node。

## 回滚

迁移后允许不同 PVE server 保存相同 VMID，旧应用无法安全读取这种数据形态。不要只回滚应用或尝试自动恢复 `vmid` 全局唯一；应停止写入，恢复迁移前数据库备份，再回滚应用。

## 下一阶段映射

未来 Resource Framework 中，`pve_server_id` 对应 domain/provider 边界，`vmid` 对应外部 key，`node` 对应可变 locator。本次不创建 `(domain_id, driver_id, external_key)` 双写，也不引入新旧执行器并行。
