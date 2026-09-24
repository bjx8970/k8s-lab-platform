# PVE VM 完整身份迁移

本次升级将旧 VM 定位从裸 `vmid` / `(node, vmid)` 改为 P1 **稳定过渡身份** `(pve_server_id, vmid)`。目标态业务身份是 `(domain_id, vmid)`；connection 只是访问端点。`node` 仅表示当前 locator，同一 VM 迁移节点后身份不变。

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

PostgreSQL 启动迁移依次执行冻结的 `v0001` 和新增的 `v0002_pve_vm_identity`。`v0002` 在一个事务中完成回填、预检、删除 `vmid` 全局唯一约束、增加外键/正整数检查和 `(pve_server_id, vmid)` 复合唯一约束；`clusters(id,pve_server_id)` candidate key 与 `vms(cluster_id,pve_server_id)` 复合外键持续约束两侧平台身份一致。SQLite 重建使用等价的复合外键。任一预检或 DDL 失败都会回滚整个版本。

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

P1 的 `pve_server_id` 是旧系统的 server/connection 作用域，**不是**最终 domain 身份。P3 中同一真实 PVE domain 可由多个 connection 访问，目标唯一键为 `(domain_id, driver_id, external_key)`，其中 PVE VM 的 `external_key = str(vmid)`、`driver_id = pve.qemu/v1`；`node` 仍是可变 locator。

本 PR 增加只读的 `modules.pve_domain_mapping.plan_pve_vm_mapping()` 契约和迁移测试：必须显式提供每个旧 server 的 `domain_id` 与 `connection_id`，以及每条旧 VM 的目标 `resource_id` 和 Cluster 所属。多个 connection 可归属同一 domain；若同 domain/VMID 的记录指向不同资源或 Cluster，则报告歧义并停止，不能依靠 URL、host、node 或默认服务器猜测。映射计划只生成供 P3 迁移审阅的结果，不创建目标表记录、双写路径或新旧执行器并行。
