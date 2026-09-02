# 凭据存储与迁移

平台的 PVE Token、OpenWrt 密码、旧版配置中的秘密字段以及集群 SSH 私钥使用 Fernet 加密后存储，密文格式为 `enc:v1:<token>`。解密密钥只从环境变量 `K8S_LAB_CREDENTIAL_KEY` 读取，和 Flask Session 密钥相互独立。代码不会生成默认密钥，也不会把密钥写入仓库或配置文件。

## 生成、备份和恢复密钥

在安全的密钥管理环境中使用 `Fernet.generate_key()` 生成一个密钥，并将其作为 `K8S_LAB_CREDENTIAL_KEY` 注入服务进程。例如，生成动作应在受控 Python 环境中完成：

```python
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode("ascii"))
```

密钥是恢复凭据的必要条件。应通过受控密钥管理系统备份，并限制读取权限；数据库备份和密钥备份应分开保存。恢复时先恢复完全相同的密钥，再启动应用并验证 PVE/OpenWrt/集群内部读取；更换密钥不能直接解密旧密文，必须制定重新加密迁移方案。

缺少密钥、密钥格式错误或密文损坏时，读取会失败并返回固定中文错误，绝不会把数据库中的旧值当作明文回退。日志和错误响应不得包含原始密码、Token 或私钥。

## 明文迁移

`migrate_plaintext_credentials()` 是显式迁移函数，初始化数据库时不会自动运行。执行前应：

1. 停止会写入凭据的应用实例并备份数据库。
2. 配置 `K8S_LAB_CREDENTIAL_KEY`，确认密钥已完成备份和恢复演练。
3. 在隔离环境先运行迁移和读取验证，再在维护窗口手动调用迁移函数。
4. 检查凭据列中不存在原始值，验证服务端连接，再恢复流量。

完成备份、停止应用并向当前进程注入密钥后，在项目根目录手动执行以下命令。命令仅导入数据库模块，不导入会触发应用初始化的 `app`：

```bash
python -B -c "from modules.db import migrate_plaintext_credentials; migrate_plaintext_credentials(); print('凭据迁移完成')"
```

本次代码修复没有执行生产凭据迁移。上述命令是维护窗口操作说明，不代表部署后已经迁移。不要在终端、日志或工单中输出原始凭据来验证；验证应只报告迁移是否成功、密文格式是否正确、内部读取或连接检查是否通过。

迁移在单个事务内进行；任一凭据加密、解密验证或数据库操作失败会回滚全部修改。已带 `enc:v1:` 前缀的值必须先用当前密钥验证可以解密，再原样保留，不重复加密。密钥不匹配或已有密文损坏会使整个事务失败，防止新旧密钥混用。PostgreSQL 迁移会在同一事务中将 PVE `token_value` 和 `ow_password` 调整为 `TEXT`；SQLite 测试不会执行 PostgreSQL 专属 DDL。

旧版 PVE/OpenWrt 配置搬迁属于结构迁移，读取到明文会拒绝继续。应先执行上述显式凭据迁移，再启动应用进行结构迁移；搬迁时复制经过验证的密文。对于原有 PostgreSQL 表，必须先完成 `TEXT` 列类型迁移再恢复写入。

安全审计通过独立的 `security.audit` INFO handler 输出到标准错误流，可在服务进程的 stderr 或其服务管理器收集的日志中查看 JSON 记录，不依赖 root logger 的级别。审计处理完整私钥块、结构化秘密字段和带密码 URI。`token_name` 是非秘密标识，保留用于定位配置；`token_value`、`ow_password` 等对外仍返回 `[REDACTED]`。更新时省略、空值、`****` 或 `[REDACTED]` 均表示保留原凭据；新建不接受脱敏占位符作为凭据。

流式日志必须在同一输出流内复用 `SecretTextSanitizer`，对每个 chunk 调用 `feed()`，结束时调用 `flush()`，以跨越逐行或 chunk 边界抑制私钥块。将输出继续交给 `sanitize_text()` 处理完整的秘密字段和 URI。无状态函数无法识别完全脱离 BEGIN 标记的私钥正文；不要对每行重新创建流式实例。未闭合的私钥块到流结束仍保持抑制。
