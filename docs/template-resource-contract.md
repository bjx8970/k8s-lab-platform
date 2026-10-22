# Template / Resource Contract v1

状态：目标契约，尚未实现。相关：[架构](architecture.md)、[插件](plugin-contract.md)、[执行](execution-persistence-contract.md)、[安全](identity-security-contract.md)。

模板是管理员定义的业务执行边界。平台根据模板调用插件、生成实验资源、绑定动作；不存在插件主动注册资源的接口。本文示例定义结构，不代表当前已有解析器或 Schema 文件。

## 1. 模板身份和发布

平台维护 templates 与 template_versions：稳定模板 ID、版本号、名称、说明、内容摘要、创建/发布者、发布时间、状态及完整定义。

- 草稿允许修改，发布前进行结构、引用、能力和参数校验。
- 已发布版本内容不可变。修改需要发布新版本，已有环境继续使用原版本和参数快照。
- 停用版本阻止新的环境创建，不自动破坏已有环境动作与清理能力。
- 被环境或执行事实引用的版本不得硬删除。
- 发布不是外部执行，也不表示插件将一直可用；每次调用仍检查插件和执行引用。
- v1 不实现模板相互引用，不加入一个看似可用但无语义的 template_ref。

## 2. 顶层字段

| 字段 | 要求 |
| --- | --- |
| schema_version | 模板格式版本，v1 为 1 |
| metadata | 名称、说明、标签等展示信息，非执行身份 |
| parameters | 教师创建环境时可输入的业务参数 |
| quota | 教师配额需求，固定值或受限变量计算 |
| create | 创建流程 |
| resources | 平台实验产出定义，可为空 |
| actions | 环境级普通动作 |
| cleanup | 教师/管理员显式触发的删除清理流程 |
| readiness | 创建就绪条件 |
| access | 可选环境交互入口 |

resources 中每项可声明 key、for_each、name、type_label、data、display、actions、access。平台生成 resource_id，教师不能传入底层插件调用配置替换它。

## 3. 参数、选择器与数据引用

### 3.1 参数

参数声明 key、类型、必填、默认值、约束及展示信息。使用有限 JSON 类型，明确字符串长度、数值范围、数组长度和唯一性。未知用户字段默认拒绝；前端表单不替代后端校验。

需要插件候选项时，参数可提供 selector：plugin_instance、action、value_field、label_field。选择器动作必须声明 read_only，由后台按模板调用，不能成为用户任意插件动作入口。v1 选择器返回候选项数组，value_field/label_field 从每个数组项读取，键必填且值唯一；未配置 input 时传空对象，不能调用需要未提供必填输入的动作。

候选项由插件解释，平台只验证稳定键和模板许可。提交时在即将固定的 Job 配置/Storage 视图中重新校验所选项合法，并将实际清单/配置版本固定到 Job；界面早先展示的候选快照不自动视为提交依据。管理员也可直接在模板中固定选择项，不允许教师修改。

### 3.2 引用

结构化引用形式为 `{ $ref: params.target_host }`，不能把任意字符串解析成代码。命名空间：

| 命名空间 | 来源 |
| --- | --- |
| params | 环境创建参数快照 |
| action_params | 当前动作经过校验的用户参数 |
| steps | 当前流程中已经确认成功的步骤输出 |
| resource | 当前资源的固定数据，仅资源动作/access 可用 |
| environment | 允许引用的环境 ID、名称等字段及创建结果快照 |
| evidence | 仅 cleanup/恢复可用，平台固定的执行证据快照 |
| item | 当前有界展开项 |

当前流程可用的环境创建结果保存为不可变 create_results，只有完成输出 Schema 校验的成功输出进入此处。已知部分引用/checkpoint 通过 evidence 提供，不能伪装为成功 steps 输出。

Environment 的模板、创建结果和资源目标快照不等于所有以后动作都固定创建时的最新凭据；每个独立 Job 接受时另行固定 plugin_instance 配置 revision、Storage 命名空间 snapshot_id、插件代码摘要和允许秘密版本映射。多 Step 共用该 Job 视图，Step B 不能读取 A 之后更新的 latest 主机映射。资源 data 中已固定的外部目标不会被新的 Job 配置重新解释成另一对象。

后继步骤若要使用前序写入的新 Storage 或新生成秘密，必须显式引用精确派生版本；原 Job 的读视图不自动刷新。秘密普通轮换保留历史但不保证远端仍接受；紧急吊销或版本失效时阻断调用，不自动换新凭据。详见插件“跨步骤快照范围”和执行“Job 级快照与跨 Step 引用”。

### 3.3 cleanup / 恢复证据引用

平台根据当前环境和执行处理组生成 evidence，业务用户不能提交其他 Job/Operation 作为证据来源。结构至少包括 snapshot_id、execution_group_id、source_job_ids，以及 steps 下按步骤定义 ID / 稳定 item_key 索引的执行事实。每个步骤项包含按尝试序号排列的 operations；每个 Operation 含 operation_id、state、target_ref、external_refs、checkpoints 及其版本和确认时间。未展开步骤或无引用时显式给空集合，不伪造成功 output。

模板可以有界遍历这些集合，再用普通引用读取已知项，例如 `{ $ref: item.external_refs.vm.reference }`；evidence 中 external_refs 以稳定 ref_key 映射到 reference、revision、observed_at，checkpoints 同理。不以“最后一个 Operation”隐式替代明确尝试选择；同键跨尝试冲突时先恢复核对，不能自动合并外部对象。

接续流程提交时固定证据快照，派发前在相同冲突保护下核对证据 revision，防止新结果改变目标。快照只能提供当前环境的安全信息及 SecretRef；不能读取其他环境秘密。evidence 是执行依据，不是解除 unknown 的授权：原操作未知且与清理冲突时，先查询或显式人工核对，不能仅因为存在一个外部引用就立即执行删除。

引用不存在、引用失败步骤或未完成步骤的 output 时阻断该步骤，不以 null/空字符串静默代替。动作不重新运行创建流程，引用创建数据时使用 environment.create_results 或 resource.data。凭据引用是单独类型，不能通过普通引用取得明文。

### 3.4 有界表达式

配额等计算采用明确算子树，例如：

```yaml
vcpu:
  $expr:
    multiply:
      - {$ref: params.vm_count}
      - {$ref: params.cpu_per_vm}
```

v1 仅需要参数引用、长度及有限算术等明确实现的算子，不执行 Python eval、动态属性/函数调用或本地 Shell。输入、结果、运算深度和最大展开数量有界；检查非负、单位、整数溢出、除零和类型错误。不支持的表达式在发布时拒绝。

SSH 命令正文由管理员发布。业务用户不能覆盖正文。带用户参数的命令应使用插件明确提供的参数化传递或上下文对应的转义方式，不能把类型校验等同于 Shell 安全；核心也不能对所有命令做一次字符串替换便承诺无注入。

## 4. 步骤、依赖与展开

普通步骤字段：id、plugin_instance、action、input、depends_on、timeout_seconds、可选 for_each。未声明依赖的步骤只依赖输入引用所要求的已完成事实，不能靠文件排列掩盖缺失依赖；发布器必须校验引用的先行关系。

- 步骤 ID 在流程内唯一；依赖必须存在且无环。
- 步骤输出必须通过插件 output_schema，才允许作为后续成功输入。
- 首版可以按拓扑顺序串行执行，不要求并行 DAG 引擎。
- 一个步骤失败后默认停止后继变更、保留现场，不自动执行逆序补偿。
- 超时不等于远端失败；执行协议见 Operation 规范。

```yaml
for_each:
  items: {$ref: params.target_hosts}
  item_key: {$ref: item}
  max_items: 5
```

for_each 输入是有限列表，稳定 item_key 在该集合内唯一。展开步骤实例及列表快照持久化，恢复使用原展开集合，不读取新清单重新展开。来自前序成功输出的列表只展开一次。资源展开同样生成稳定 item_key，与步骤结果明确对应；不能按重试后的临时序号重新编号产出。

## 5. 平台资源生成

插件返回执行结果，平台从模板 resources 定义映射名称、类型展示、data 及动作绑定，生成通用资源记录。一个资源可表示一个 VM、逻辑集群或外部对象组合，不要求统一 external_key，也不为 VM/VLAN 增加专用表。

平台保存环境、模板资源槽位、稳定 item_key、来源 Operation、生成数据与动作快照；其逻辑产出键至少包含：环境 + 逻辑流程调用实例 + 资源槽位 + item_key。执行恢复和明确重试必须保持原调用谱系，不能因 Operation ID 变化重复登记同一产出。

- 插件内部主机、节点、镜像等不自动变成实验资源。
- 模板 resources 可以为空，环境仍按 readiness 决定是否就绪。
- 已知外部引用必须在 Operation 中及时持久化，不等待完整 output 或平台资源生成。
- 同一成功结果、Step 成功和后继可引用输出必须同事务持久化；当下可确定的资源同时幂等生成。依赖后续结果的资源先记录本地生成项，完成所有必需生成后才允许 ready。
- 恢复只重放本地生成项的数据映射和写库，不重新执行已经成功的插件动作。具体持久化规则见执行规范。
- 不完整结果只进入故障现场，不强行包装成可用实验资源。业务资源生成失败不证明外部没有副作用。

## 6. 环境与资源动作

环境 actions 和资源 actions 使用同一种流程结构，每个动作包含 key、label、roles、parameters、steps，可选安全的状态映射。动作可以有界展开或多步依赖，不限于一次插件调用。

拟定业务入口：

```text
POST /api/environments/{environment_id}/actions/{action_key}
POST /api/resources/{resource_id}/actions/{action_key}
```

用户只提交该动作 parameters 允许的参数及请求幂等键，不能提交任意 plugin_key、插件动作、目标引用或覆盖固定输入。后端从模板/资源快照解析并授权，长操作返回 202 + job_id。

角色可见性由模板定义，但不提升业务权限：学生不能被模板授予环境创建、删除、分配或插件管理权限。cleanup 与普通学生动作分离。

UI 只显示模板向角色开放且插件可执行的动作。插件不可用可显示故障原因，但 UI 显隐不替代后端授权。核心不推断“start”“reboot”的供应商语义，不自动批量操作所有子资源。

## 7. 完整结构示例

本示例与插件规范第 2 节 Manifest 联合使用：由管理员预先建立实例 ssh_lab，其 plugin_key 为 ssh；该实例身份映射属于部署/配置前置条件，不是模板可伪造的字段。Manifest 同时声明只读 list_host_options 和 execute_command。这里的命令不创建学生账号，也不自动产生学生终端入口。

以下 JSON 也是合法 YAML，方便用标准库直接解析；其他 YAML 表达方式仍支持。selector 返回数组项的 id/name，必需的 stdout 已在配套 Manifest output_schema 中声明，可为空但不能省略。

```json
{
  "schema_version": 1,
  "metadata": {
    "name": "SSH 检查实验",
    "description": "对教师选择的执行主机完成一次检查，并生成可再次检查的实验记录"
  },
  "parameters": {
    "target_host": {
      "type": "string",
      "required": true,
      "selector": {
        "plugin_instance": "ssh_lab",
        "action": "list_host_options",
        "value_field": "id",
        "label_field": "name"
      }
    }
  },
  "quota": {"environments": 1, "vms": 0, "vcpu": 0, "memory_mb": 0},
  "create": {
    "steps": [
      {
        "id": "inspect",
        "plugin_instance": "ssh_lab",
        "action": "execute_command",
        "input": {
          "host_id": {"$ref": "params.target_host"},
          "command": "uname -s"
        },
        "timeout_seconds": 30
      }
    ]
  },
  "resources": [
    {
      "key": "report",
      "name": "系统检查记录",
      "type_label": "检查结果",
      "data": {
        "host_id": {"$ref": "params.target_host"},
        "system": {"$ref": "steps.inspect.output.stdout"}
      },
      "actions": {
        "inspect_again": {
          "label": "再次检查",
          "roles": ["teacher", "student"],
          "parameters": {},
          "steps": [
            {
              "id": "inspect",
              "plugin_instance": "ssh_lab",
              "action": "execute_command",
              "input": {
                "host_id": {"$ref": "resource.data.host_id"},
                "command": "uname -s"
              },
              "timeout_seconds": 30
            }
          ]
        }
      }
    }
  ],
  "actions": {},
  "cleanup": {
    "steps": [],
    "completion": {"no_external_cleanup_required": true}
  },
  "readiness": {"require_steps": ["inspect"]},
  "access": {}
}
```

此模板登记的是平台检查产出，不是自动登记 SSH 管理主机资产。一个不需要产出的模板可以将 resources 设为空。空 cleanup 只适用于管理员明确声明无外部清理需求的流程，不能据此消除 unknown 调用。

VM 模板可将创建结果的 vm_ref 放入 resource.data，并将启动、停止、重启动作绑定到插件；核心无需改变类型逻辑。这是后续插件用例，不表示首版已有 PVE 插件。

## 8. readiness、状态与访问

readiness 显式指定必需步骤、结果条件和可选必需资源。不能把“有资源记录”或“Job 已完成”直接等同环境 Ready；无资源模板也能就绪。

动作成功不自动表示 VM 已运行。资源展示状态由模板对已确认输出或只读查询结果映射，并附 observed_at/有效期；过期或不可确认时显示 unknown。

access 显式声明交互入口、允许角色、会话类型、插件实例、固定连接引用及凭据引用，参数按 session Schema 校验。只有经过模板验证的入口与凭据才能开放。管理主机清单、SSH 执行连接或 SecretRef 不自动变成学生入口。

会话建立及每帧授权、教师旁观接管和远端直连限制按插件/安全规范执行。

## 9. cleanup 与失败现场

- 创建部分失败默认保留，不自动逆序删除。
- 教师/管理员显式触发环境 cleanup Job，使用原模板版本、参数、已确认输出、部分引用和检查点。
- 只有外部未决事实已核对且前置条件满足时，清理变更才能执行，不能与未知原命令并发。
- cleanup 显式定义目标、步骤和完成条件，核心不推断外部删除方式。
- cleanup 失败或未知保留环境/引用/冲突和对应配额；只有确认已释放才改变占用。
- 无 cleanup 的模板不得悄悄当成“已清理”。明确空 cleanup 的声明也不能自动解决未知副作用。

## 10. 配额定义

维度为 environments、vms、vcpu、memory_mb。模板声明固定值或受限变量，可按资源展开计算再汇总；核心不从 SSH 清单、外部 CPU 或生成资源行数猜测占用量。

这些值是管理员模板定义的教师额度计量，不是物理资产隔离保证。各维度允许由管理员声明为 0，核心不能擅自按实验资源行数改写成本；通常新环境的 environments 成本声明为 1。所有值发布/实例化时检查非负、范围和单位。执行中任意新参数不得绕过已预占额度；如未来动作允许扩容，需另定增量预占协议，v1 不默认开放绕过配额的可变动作参数。

模板声明成本不要求平台维护主机占用表。教师限额、原子预占和释放规范见执行文档。

## 11. 发布与调用验证

发布时验证：格式版本、未知字段、参数 Schema、插件动作/会话类型存在、引用、依赖 DAG、展开上限、配额表达式、资源槽位、动作角色、cleanup/readiness、SecretRef 的用途；不审查管理员命令内容是否合理。

调用时验证：账号有效、课程/环境归属、实时直接/组授权、模板角色策略、动作输入、候选项、配额和冲突保护。请求幂等键相同且请求摘要相同返回原 Job；同键不同请求返回冲突。配置或插件不可用时明确报错，不静默换目标或忽略步骤。
