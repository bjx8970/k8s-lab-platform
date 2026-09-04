# 实施、迁移与验收计划

本计划遵循[资源执行层边界](README.md)。框架实施与控制面接入分别验收，不包含 Environment/Scheduler/PlanRevision/finalizer。

平台层的实施顺序与交接清单见[平台实施计划](../platform-design/implementation-plan.md)。本文 M0–M6 仅描述资源子项目；M2 的持久化 Operation 纵向闭环先于完整 Controller。

实现基线已同步至 GitHub main `64891df`。Issue #1 的 authz/security_service、audit、credential_store 和安全重试已有代码及测试，M6 应复用这些能力；资源核心和持久化单操作执行仍待实现。

## 1. 阶段

| 阶段 | 工作 | 完成条件 |
|---|---|---|
| M0 设计修订 | 收缩模型/SDK/四插件，替换蓝图为指令示例 | 所有文档采用相同边界 |
| M1 Issue #2 | 旧 VM 服务器外键与 `(pve_server_id,vmid)` 唯一、node locator、缓存隔离；应用完成鉴权/投票/页面修复 | 两服务器同 node/VMID 隔离；同平台节点迁移身份不变 |
| M2 最小框架 | Resource/Binding/Connection、provisional create、类型/驱动注册、Operation 快照/lease/去重/恢复 | 不需要业务策略、Environment 或 Task 即可调用 |
| M3 VM/PVE | VM 类型、QEMU 驱动、节点/模板查询、外部任务返回、已有 VM 登记 | 明确指令正确作用于指定平台 |
| M4 OpenWrt | 独立连接、UCI 资源、独立服务 commit/reload/restart、逐命令结果 | 无隐式网络组合、地址分配或关联清理 |
| M5 K8s | 集群/API 查询、准备好的安装参数、kubeasz 命令执行与轮询 | 不查询 VM 业务归属或创建基础设施 |
| M6 控制面接入 | API/Controller 经授权创建 Operation；Executor 统一调用框架；业务模块保留权限和教学规则 | 原业务功能通过集成回归，新旧入口只有一个执行路径 |

M1 可独立发布，不必等待整个框架。每类资源切换时，旧入口也调用对应框架执行器；业务控制继续在调用层完成。

## 2. 原代码职责迁移

| 现有代码 | 进入框架/插件的部分 | 保留在其他模块的部分 |
|---|---|---|
| modules/db.py | 资源身份/绑定及查询 | Cluster、教师/课程/组归属、业务关系 |
| modules/pve_client.py | 平台查询、VM API、任务轮询 | 创建多少 VM、选择模板/服务器的规则 |
| modules/openwrt_client.py | UCI 节读写和服务命令 | 网络规划、共享使用者检查、删除顺序 |
| modules/k8s_manager.py | 可复用的 kubeasz 单指令执行适配 | VM/网络/K8s 的整套创建、回滚、批量业务 |
| modules/task_queue.py | 不复用导入即启动/吞异常行为；改为显式 Executor key queue | Controller reconcile 和跨资源 plan |
| modules/status_cache.py | 按 resource_id 的实际状态缓存 | 实验环境聚合可用性 |
| app.py | HTTP/旧 ID 到框架调用的适配 | 登录、鉴权、关机投票、用户可见列表 |
| 三个 VM 页面 | 使用 resource_id 和操作结果 | 用户动作入口、业务提示、投票界面 |
| modules/ssh_terminal.py | 可通过框架查到目标资源属性 | 交互会话、接管权限和学生会话规则 |

移出的职责仍然需要当前应用负责，不能因框架没有业务判断就删除现有应用判断。资源框架测试与应用业务测试分别组织。

## 3. 存储映射

| 旧数据 | 新数据/归属 |
|---|---|
| PVEServer | pve.platform、domain、connection；旧 PK 映射保存在应用侧 |
| PVEServer.ow_* | 独立 openwrt.router/connection；PVE 与 router 的业务配对在应用侧 |
| Vm | compute.vm、PVE 外部绑定；旧 Vm 保留资源 FK 和业务关联 |
| Cluster | 保留应用模型，通过 resource_id 引用 VM/网络/K8s；无需迁入内置 lab 控制器 |
| K8s 安装信息 | k8s.cluster 属性、安装器执行定位、API 连接 |
| Cluster 网络字段 | 分别登记实际 VLAN/interface/DHCP/转发等；地址 allocation 仍在应用侧 |
| SSH key/密码 | 通过宿主 credential_store 加密适配实现新的 SecretStore 引用；资源元数据不复制正文 |
| 内存业务任务 | 保留业务语义；新执行记录只代表单条资源指令 |

框架不创建 rf_relations、rf_operation_steps、资源删除策略或业务分配表。应用可自行实现这些概念，但它们不属于插件接入前提。

## 4. Issue #2 的职责拆分

原始问题见 [Issue #2](https://github.com/bjx8970/k8s-lab-platform/issues/2)。

框架/资源层：

1. 过渡期 Vm 增加 pve_server_id FK，回填后非空；撤销全局 vmid unique，新增 `(pve_server_id,vmid)` 唯一；node 仅为可变 locator。
2. 目标态使用 resource_id 和明确 domain/connection；PVE 插件保存域内 VMID 身份与当前 node。
3. 查询、创建、配置、启停、删除不选择模糊默认服务器；缓存以完整身份区分。
4. FK 保证绑定连接与 domain 对应，不能因为同名节点命中另一服务器。

应用层：

1. find_cluster_by_vm 与权限判断按完整身份匹配真实业务 Cluster。
2. 批量状态逐项鉴权；Socket.IO 验证提交对象与集群归属。
3. 投票以 resource_id（过渡期 pve_server_id+vmid，node 仅作请求定位校验）为对象，批准后再创建 stop Operation。
4. 首页/集群页/学生页按完整资源标识更新元素。
5. 是否允许删除被使用的 PVE 配置由应用决定；框架连接登记采用关闭记录以保留历史 FK。

用户没有要求取消这些应用检查；本修订只是将它们与资源执行框架分离。

## 5. 迁移方案

迁移是部署工作，不是运行时资源操作的业务审批。以当前代码实际使用的 PostgreSQL 为基线，SQLite 说明与代码的差异单独核实。

1. 在备份副本核对 schema、旧约束、server=0/null、无效引用和既有资源定位。
2. 明确旧 server→domain/connection、OpenWrt 连接、VM→resource 的映射。无法定位的记录报告迁移错误，不能猜默认服务器。
3. 应用暂停受影响类型的新请求，处理旧在途任务/投票计时器及旧状态写入者，避免切换期间双重执行。
4. 先新增可空列/核心表/插件表，再回填绑定和应用侧映射；验证唯一/FK 后设置非空约束。
5. 将完整类型的操作入口切到框架，应用业务字段仍由业务模型维护。
6. 原 API 保留响应适配；资源状态与绑定由框架维护，旧表需兼容的资源字段由同事务应用投影更新。
7. 迁移有版本和 checksum，重跑复用已分配 UUID；失败明确返回，不能吞异常后启用半迁移数据。

不连接实际数据库或外部平台来完成本轮设计修订。后续实施迁移时再读取必要配置，迁移输出不含凭据。

回退由部署模块处理：切换前可恢复对应数据库备份和版本；发生外部变化后应核对差异。允许跨服务器重复 VMID 后，不能只恢复旧的全局唯一约束。恢复数据库也不会撤销已执行的外部删除。

## 6. 框架/插件验收

| 场景 | 预期 |
|---|---|
| 两 domain 同 node/VMID | 两条资源分别查询和执行，无覆盖 |
| 相同 domain 的重复身份 | 数据库唯一约束拒绝重复登记 |
| 节点迁移、旧绑定版本 | 更新定位；旧版本请求返回技术冲突 |
| 无角色、投票、关系信息的内部调用 | 指令可正常提交，不要求额外业务对象 |
| VM 存在 K8s/学生使用的外部标签 | 不据此阻止 stop/delete，不推断后果 |
| 先提交删除接口、后删除 DHCP | 不重排指令；返回各次平台结果 |
| PVE/UCI 后端拒绝操作 | 返回后端原始原因，不自行解释成业务策略 |
| clone 任务 pending/failed | 保留外部任务 ID；不提前报告创建完成 |
| 部分成功或结果 unknown | 返回具体执行事实，不自动补偿、重试 |
| 同 request_id 重复发送 | 同内容复用执行记录，不同内容返回冲突 |
| OpenWrt 删除 interface | 只执行该指令范围，不自动扫描关联规则 |
| K8s 安装退出成功、节点不 Ready | 安装执行成功与查询事实分开，不做业务聚合判断 |
| 插件缺失/版本不兼容 | 可读历史记录；新调用返回 PluginUnavailable |
| unregister 被业务引用的资源 | 关闭登记，无依赖检查、无外部删除、无级联操作 |

技术测试使用模拟平台接口、独立 PostgreSQL，不加载生产配置。单条指令内部协议顺序可以验证，例如 clone 完成后才配置新 VM；这不引入跨资源工作流。

## 7. 应用集成验收

以下由应用模块验证：

- 教师/学生仍只能查看和操作允许访问的资源，包含新旧 HTTP、批量查询和 Socket.IO。
- 学生关机投票在调用框架前完成，不能通过新路由绕过原业务规则。
- 应用正确决定 VM/网络/K8s 指令顺序，显式等待单条执行结果。
- 资源分配、共享路由器、删除影响和补偿由 Scheduler/Controller/应用适配测试。
- 框架返回失败或 unknown 后，应用选择的处理方式是显式逻辑，不依赖插件隐藏动作。

## 8. 下一步实现顺序

先完成 M1 的定位修复，然后实现 register/list/get/execute + VM/PVE 的最小闭环，再接入 OpenWrt 与 K8s。核心可先用测试 handler 验证不依赖业务模块运行。框架实现不以完成新的工作流引擎、权限框架或网络规划器为前置条件。已有[安全验收](../issue-1-acceptance.md)和[重试约定](../job-retry.md)作为应用回归基线，不能因抽取资源框架而删除或降低原约束。
