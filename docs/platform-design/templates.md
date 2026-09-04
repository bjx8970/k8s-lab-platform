# 实验模板、调度与 PlanRevision

返回[接手指南](README.md)。状态：格式提案，解析器、Scheduler、PlanBuilder 与通用前端均待实现。

## 1. 四种对象不能混为一谈

| 对象 | 内容 | 管理者/生成者 |
|---|---|---|
| LabTemplate | 参数、能力要求、资源意图、状态配方、就绪条件、访问入口 | 管理员发布 |
| DeploymentProfile | 允许的 domain/connection/node、镜像映射、资源池、规格范围 | 管理员发布 |
| PVE VM 镜像模板 | 预装系统和软件的真实平台模板 | 平台运维人员 |
| PlanRevision | 某个 Environment generation 经 Placement 固定后的不可变实例计划 | PlanController/PlanBuilder |

模板描述“提供什么”，Profile 描述“这个站点允许怎样提供”，Scheduler 回答“放在哪里”，PlanBuilder 才把三者编译成 PlanRevision。用户参数不能覆盖 connection、secretRef、资源池边界或任意执行命令。

## 2. 首版模板格式

采用 JSON，`api_version=lab.template/v1`。示例见[Python 开发环境模板](examples/python-development.json)。这是待实现契约，当前应用不会读取该文件。

| 字段 | 必需 | 语义 |
|---|---|---|
| api_version、kind | 是 | 格式版本和 LabTemplate |
| metadata | 是 | id、不可变发布 version、名称和展示信息 |
| parameters_schema | 是 | 受限 JSON Schema；对象、基础类型、默认、枚举、范围、pattern |
| requirements | 是 | 资源类型、驱动、动作、观察和 evaluator 版本要求 |
| resources | 是 | 逻辑资源意图、created/adopted、cleanup responsibility |
| scheduling | 是 | Scheduler 使用的资源需求、约束和 placement rule，不含副作用 |
| reconciliation | 是 | Running/Stopped 及 deletion cleanup 的有限 plan items |
| readiness | 否 | Environment conditions 的 evaluator |
| failure_policy | 是 | 首版固定 `retain_and_block` |
| access | 否 | ssh/web 等受支持入口描述 |

模板发布内容不可原地修改。相同版本不同内容返回冲突；停用模板只阻止新 Environment，既有 Environment 继续使用固定模板和 PlanRevision。

## 3. Scheduler 输入与 PlanBuilder 输出

模板 `scheduling` 只表达需求，例如 VM 数量、CPU/内存、镜像别名、访问网络和必须能力。Scheduler 将其与 DeploymentProfile、容量观察组合，执行 Filter/Score/Reserve/Bind，输出 Placement：

```json
{
  "domainId": "pve-domain-a",
  "connectionId": "pve-connection-3",
  "bindings": {
    "workspace": {
      "node": "pve01",
      "vmid": 213,
      "ip": "10.20.1.13",
      "imageRevision": "python-base@2026-09-01"
    }
  }
}
```

Scheduler 不展开 clone/start/delete 条目。PlanBuilder 读取固定模板、Profile 和 Placement，输出 PlanRevision 中的完整 create request、运行状态配方、cleanup recipe、readiness 和 access。

规划必须是确定性的：相同规范化输入产生相同 contentDigest；任何改变 placement、模板、Profile 或资源身份的重规划都创建新 PlanRevision。旧 PlanRevision 不修改。

## 4. 参数和数据引用

引用采用仅含一个键的对象，例如 `{"$ref":"parameters.cores"}`，不是 JSON Schema 的 schema `$ref`。

| 命名空间 | 产生者 | 示例 |
|---|---|---|
| parameters | 参数 schema 校验并填默认值后的输入 | parameters.cores |
| placement | Scheduler 固定的绑定和 allocation | placement.workspace.vmid |
| plan | PlanBuilder 的标准化创建请求和访问值 | plan.workspace.create_request |
| resources | 运行时逻辑槽位到 resource_id 的持久绑定 | resources.workspace.resource_id |

解析器递归替换并保留数据类型，不执行 Python、shell、Jinja 或字符串插值。引用不存在、类型不匹配或指向未声明输出时发布/建 plan 失败，不能替换为空值继续。

PlanRevision spec 不回写运行时 resource_id；`resources.*` 在 reconcile 时从环境资源关联解析。引用解析结果和输入摘要写入 Operation，确保执行目标固定。

## 5. 最小 plan item 集合

| kind | 行为 | 执行者 |
|---|---|---|
| resource.create | 为逻辑资源创建 provisional Resource 和一次性 create Operation | EnvironmentController 创建；Executor 执行 |
| resource.register | 绑定已存在对象，保存 adopted/cleanup responsibility | EnvironmentController/资源框架 |
| resource.action | 为固定 resource 创建一次性 Operation | EnvironmentController 创建；Executor 执行 |
| wait.observation | 检查新鲜 Resource observation 的字段相等条件 | EnvironmentController/ObservationController |
| wait.endpoint | 受控 TCP endpoint evaluator；只更新 condition，不产生反向清理 | Controller evaluator |
| condition.set | 从明确输入设置 Environment condition | Controller |

plan item 有唯一 `item_key` 和有限 `dependsOn`；首版实现可以要求全序或简单静态依赖，不建设通用 DAG 调度器，也不支持运行时循环、任意表达式和用户脚本。拓扑重复由 PlanBuilder 按参数上限展开。

Controller 对每个需要副作用的 item 使用 `environment/intentGeneration/plan/item/attempt` 生成稳定 Operation request key。重复 reconcile 只能发现原 Operation；用户从 Stopped 再改为 Running 时 generation 改变，因此可产生新的 start Operation。Failed/Unknown 默认阻塞，新 attempt 必须来自显式恢复决策并记录依据。

## 6. 幂等检查与等待

`resource.action` 可以声明受限 `skip_if_observation={field,equals,max_age_seconds}`。Controller 只在观察新鲜且明确相等时将 item 标为 satisfied，不创建 Operation；观察失败或 stale 不算成功。

观察和命令之间仍可能发生外部变化，因此插件拒绝必须如实保存。skip 条件是减少不必要命令，不是并发锁或 exactly-once 保证。

`wait.endpoint` 的目标必须来自 Profile/Placement/PlanBuilder 的受信任输出，不能由普通用户直接指定任意 host。worker 网络出口应有 allowlist，避免模板把探测器变成内网扫描能力。TCP 可达不等于软件 Ready；HTTP/K8s 等检查使用版本化 evaluator。

## 7. Resource 创建和存在状态

执行 `resource.create` 前，Controller/Operation service：

1. 预分配 resource_id；
2. 保存 `registrationState=active`、`existenceState=pending`；
3. 外部 identity 可知时创建 provisional binding 并占用唯一键；
4. 创建固定 binding/connection/plugin 快照的 Operation；
5. 由 Executor 执行。

create 成功变为 present；确认未创建变为 absent；无法确认变为 unknown。resource_id 和历史不因失败删除。环境资源关联在 Operation 受理时保存，避免“外部已创建但关联丢失”。

## 8. Python 单 VM 路径

Python 示例要求预制 Python/SSH 镜像和已存在的可访问网络：

1. API 创建 Environment `desiredState=running`。
2. Scheduler 选择 PVE domain/node，预留 VMID/IP。
3. PlanBuilder 产生 workspace create、start、wait SSH 和 cleanup recipe。
4. EnvironmentController 依次创建 clone/start Operation；Executor 保存 PVE UPID 并 poll。
5. SSH evaluator 成功后设置 InfrastructureReady/Ready。
6. `desiredState=stopped` 时产生 graceful stop Operation 并等待 power_state=stopped。
7. `desiredState=running` 时仅在未运行时产生 start Operation。
8. deletionTimestamp 后 finalizer 按 stop→wait→delete→release allocation 清理。

Python 安装正确性由镜像构建验收保证；SSH 端口只作为此模板的最低 readiness。如果提供浏览器 IDE，需要新增受控 web access adapter 和软件级 evaluator，不增加 Python 专用 API。

## 9. K8s 模板路径

K8s Environment 使用同一控制面对象，但有更多 conditions：

| 阶段 | Scheduler/Controller 责任 | Resource 能力 |
|---|---|---|
| Placement | 选择 PVE/domain/node，预留 VMID/IP/VLAN/端口 | 只读容量/发现 |
| Plan | 展开 client/control-plane/worker、网络和安装条目 | 无副作用 |
| 网络 | 按 plan 创建 VLAN/interface/DHCP/zone member/forward | OpenWrt Operation |
| VM | clone/configure/start 并观察 power/SSH | VM/PVE Operation + observe |
| 集群 | register k8s Resource，以完整 inventory 创建 deploy Operation | K8s/kubeasz |
| 就绪 | list_nodes/API evaluator 更新 SoftwareReady/KubernetesReady | K8s observe |
| 删除 | finalizer 按 cleanup responsibility 清理并释放 allocation | 多条独立 Operation |

保留“基础设施完成后再安装”的交互时，可将 `spec.installationState=requested|deferred` 作为声明式意图；Controller 不使用一次性布尔 install。基础设施完成但尚未请求安装时，Environment 保持 `InfrastructureReady=True`、`SoftwareReady=False`、reason=WaitingForInstall。

K8s deploy 的远端执行必须有确定性 job id、状态文件、退出码和日志定位，SSH 断线后可 poll 原作业。安装退出 0 与节点 Ready 分开。

当前 OpenWrt `firewall_zone` 契约只有发现和成员调整。Profile 使用既有 zone，或先增加明确 zone create/delete 动作；模板不能假设不存在的能力。首版不支持 `apply_mode=none` 跨 Operation 累积 UCI 修改。

## 10. 删除和 cleanup recipe

cleanup recipe 在 PlanRevision 创建时固定，记录每个逻辑资源的 delete/unregister/retain 责任。它不是 create items 的自动反转：Controller 必须结合实际环境资源关联和 Operation 事实，只为确认已产生或可能存在的 created 资源创建清理 Operation。

Unknown 资源先观察/人工核对；借用资源默认 unregister 或 retain。任何外部删除未确认或 allocation 仍隔离时，Environment finalizer 保留。

模板升级不能改变既有环境的 cleanup recipe。重规划新 PlanRevision 时必须继承旧 plan 已产生资源的清理责任，直到这些资源确认清理完成。

## 11. 发布、权限与前端

发布流程：编辑草稿 → JSON Schema 校验 → 引用和输出类型检查 → 资源/驱动/动作/evaluator 版本检查 → 安全准入 → 固定版本发布。站点能力在绑定 Profile 和调度时再次校验。

模板发布者能间接请求受控基础设施副作用，应使用独立高权限并审计。普通用户只能填写 parameters_schema 允许的字段，不能指定 secret、connection、URL、命令或任意 endpoint。

通用前端展示模板目录、参数表单、Environment spec/status、conditions、Task View、Operation 日志和 access。按钮由模板支持能力、Environment 当前状态和实时权限共同决定。

access 首版支持 ssh；描述包含 kind、label、resource_id、host、port、username。私钥由 WebSSH 受控流程读取，不写入模板、PlanRevision、Environment 普通输出或浏览器 API。

## 12. 扩展验收

- 同一模板在不同 Profile 上产生不同 Placement/PlanRevision，资源身份不串用。
- PlanRevision v1 冲突后产生 v2；v1 immutable、Superseded，既有 Operation 可追踪。
- Controller 重复 reconcile 不重复 clone/reboot/delete。
- 发布模板 1.1 不改变运行于 1.0 的环境和 cleanup recipe。
- Python 环境仅依赖 VM/PVE 能力与镜像，无专用后端分支。
- K8s 安装成功、节点 NotReady 和 Environment Ready 三者状态分离。
- 删除遇到 Unknown 时 finalizer 与 allocation 保留。
- 直接资源 Operation 不要求存在 LabTemplate、Environment 或 Task。
