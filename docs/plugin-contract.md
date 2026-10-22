# Plugin API v1

状态：目标契约，尚未实现。入口：[架构](architecture.md)。相关：[模板](template-resource-contract.md)、[执行](execution-persistence-contract.md)、[安全](identity-security-contract.md)。

本文中的方法、Manifest 和帧是拟定的 v1 结构，不是现有 Python SDK。必须/禁止表示验收约束；传输实现与具体 Python 类型在实现时固化。

## 1. 加载与信任

插件是可信 Python 包，按配置目录在启动时发现，建议每个包包含 manifest.yaml、plugin.py 及私有辅助模块。插件标识重复、接口版本不兼容或 Manifest 无效时，该插件加载失败。

部署配置决定 required/optional：必需插件失败阻止启动，可选插件失败标记不可用而不丢弃原数据。插件自报的 required 不是平台启动依赖的权威来源。

v1 不支持热更新、在线安装、多版本并存或数据库中的可执行代码。平台记录实际加载的插件版本及代码摘要；未完成调用引用不一致时阻断恢复，不自动改用新代码。

平台不判断可信管理员命令是否危险，也不维护重复的动作白名单。Schema 校验、命名空间隔离和业务授权仍必须执行。插件不获取核心 DB Session、不执行 SQL/DDL；这不是 Python 沙箱承诺。

## 2. Manifest

基础字段：

| 字段 | 要求 |
| --- | --- |
| manifest_version | Manifest 结构版本，v1 为 1 |
| api_version | 平台插件接口版本，v1 为字符串 "1" |
| plugin_key | 稳定标识，同一运行时唯一 |
| plugin_version | 实现版本；与代码摘要共同识别已加载代码 |
| display_name | 展示名称 |
| entrypoint | 包内实现入口，不从 DB 读取可执行路径 |
| actions | 普通动作映射 |
| sessions | 可选交互会话类型映射 |

普通动作包含 display_name、input_schema、output_schema 和 execution。Schema 使用 JSON Schema 2020-12，发布及加载时校验 Schema 自身；引用只在已加载包内或当前文档中解析，不在执行时请求任意远程 Schema。

以下 JSON 也是合法 YAML，便于使用标准库直接解析；不改变模板和 Manifest 可用 YAML 编写的约定。它与模板规范中的完整示例配套，示例实例 ssh_lab 对应本 Manifest 的 ssh 插件。

```json
{
  "manifest_version": 1,
  "api_version": "1",
  "plugin_key": "ssh",
  "plugin_version": "1.0.0",
  "display_name": "SSH 执行插件",
  "entrypoint": "plugin:SSHPlugin",
  "actions": {
    "list_host_options": {
      "display_name": "列出允许的主机选项",
      "input_schema": {
        "type": "object",
        "additionalProperties": false,
        "properties": {}
      },
      "output_schema": {
        "type": "array",
        "items": {
          "type": "object",
          "additionalProperties": false,
          "properties": {
            "id": {"type": "string", "minLength": 1},
            "name": {"type": "string", "minLength": 1}
          },
          "required": ["id", "name"]
        }
      },
      "execution": {
        "effect": "read_only",
        "idempotency": "unsupported",
        "recovery": "unsupported",
        "cancellation": "unsupported"
      }
    },
    "execute_command": {
      "display_name": "执行命令",
      "input_schema": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "host_id": {"type": "string", "minLength": 1},
          "command": {"type": "string", "minLength": 1}
        },
        "required": ["host_id", "command"]
      },
      "output_schema": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "exit_code": {"type": "integer"},
          "stdout": {"type": "string"},
          "stderr": {"type": "string"}
        },
        "required": ["exit_code", "stdout"]
      },
      "execution": {
        "effect": "mutating",
        "idempotency": "unsupported",
        "recovery": "unsupported",
        "cancellation": "unsupported"
      }
    }
  }
}
```

list_host_options 返回候选项数组，每项 id/name 必填，id 在该结果内唯一，不包含凭据。selector 的 value_field/label_field 分别从每个数组项取值，未声明 input 时向该动作传空对象。execute_command 的成功输出必须有 stdout，可以为空字符串；例中 uname 的输出因而可作为必需资源映射。execute_command 对任意命令仍按 mutating 处理，不因示例命令看似只读而赋予盲目重试保证。

execution 的字段：

| 字段 | 值与语义 |
| --- | --- |
| effect | read_only / mutating；前者不得实施业务外部变更 |
| idempotency | unsupported / keyed；keyed 表示同一调用键与同一请求可可靠去重 |
| recovery | unsupported / queryable；queryable 表示能查询已接受调用的事实 |
| cancellation | unsupported / requestable；后者只代表能提出取消，不保证成功 |

这些是执行保证，不是功能审批。动作不能只因名称叫 observe 就自动被认定只读。prepare 可以收紧本次调用保证，不能无依据提升 Manifest 保证。SSH 任意命令默认不能声明为可盲目重试。

## 3. 普通操作接口

```text
describe() -> Manifest
prepare(action_key, input, context) -> PreparedCall
execute(prepared_call, context) -> ExecutionResult
poll(prepared_call, execution_ref, context) -> ExecutionResult
cancel(prepared_call, execution_ref, context) -> CancelResult
```

poll/cancel 仅在 Manifest 声明时实现。平台校验方法与声明一致；不将缺失方法自动替换为重新 execute。

### 3.1 prepare

prepare 校验输入、解析配置、固定目标及凭据引用，不能实施本次业务的外部变更。

```text
PreparedCall
  action_key
  resolved_input
  storage_refs[]       # key + 固定历史版本
  target_ref           # 插件解释的不透明 JSON
  secret_refs[]        # 具体凭据版本引用，无明文
  conflict_keys[]      # 可选，实例作用域内的稳定不透明互斥键
  execution_policy
```

conflict_keys 随 PreparedCall 固定，平台默认按插件实例命名空间比较。业务输入不能任意提供、清除或替换它们；平台必须在外部派发前事务取得相应保护，取得失败时不派发。键存在不代表核心理解或纳管了主机，也不承诺发现未声明的跨环境冲突。

平台检查结构可序列化、引用属于当前实例及调用允许范围，持久化后才允许外部执行。插件不能在 execute 中忽略快照、静默读取最新清单并换目标。密钥身份校验等协议职责由插件处理；SSH 主机密钥变化不得自动信任为原目标。

插件无需把主机清单中的每个项目注册为平台资源；target_ref 也不等于平台资产 Binding。

#### 跨步骤快照范围

| 层级 | 固定内容 | 后续读取规则 |
| --- | --- | --- |
| Environment | 模板版本、创建参数、成功 create_results、资源数据及已经明确固定的外部目标引用 | 后续独立动作可以固定新的运行配置，但不能把已有目标 ID 按新清单静默映射到另一对象 |
| Job | 模板/动作快照、涉及插件的版本与代码摘要、PluginInstance 配置 revision、各实例 Storage snapshot_id、可访问秘密的版本解析映射 | 从接受 Job 到全部后继 Step、恢复查询使用同一 Job 视图，不默认读取 latest |
| Operation | 从 Job 视图解析的具体 storage_refs、target_ref、secret_refs、输入、执行特性及冲突键 | PreparedCall 持久化后按其精确引用执行、查询或取消，不重新解析成当前版本 |

Job 在接受事务中固定模板涉及的插件实例和版本边界。插件可以在 prepare 时惰性选择实际读取的键，但必须在已固定的命名空间快照内选择。未显式带版本的 SecretRef 也按 Job 接受时的映射解析，不能在 Step B 查当前秘密版本。平台不解释私有 JSON；固定整实例命名空间使尚未解析的跨步骤依赖仍有确定视图。不同插件实例可以各有自己的 snapshot_id，不承诺它们共享一个全局物理时刻；需要联合一致的业务配置由管理员在发布/提交前确认。

后继步骤只有显式引用前序成功结果/受控检查点中的精确 Storage 或秘密版本，才能访问本 Job 开始后产生的数据；这种派生引用不会刷新整个 Job 的读视图，也不能偷偷替换原目标。缺失历史、停用实例、插件代码不匹配或秘密不可用时阻断，不跳到新配置或旧凭据。显式新 Job/关联恢复采用新快照时，必须保留原引用并核对已执行事实及目标连续性；不能把未决旧调用当成全新安全调用。

### 3.2 execute / poll

统一返回：

```text
ExecutionResult
  outcome              # succeeded / failed / pending_external / unknown
  output               # 仅确认成功且符合 output_schema 的正式输出
  external_execution_ref
  external_refs[]      # 已确认的部分外部引用，不等于成功输出
  error
```

- succeeded：确认该动作的完成条件满足，不自动意味着资源或环境 Ready。
- failed：确认失败，但可能产生部分副作用，不自动表示可以重试。
- pending_external：已被外部接受但尚未完成，必须能提供执行引用或已声明恢复查询机制。
- unknown：发送、连接或进程故障后无法确认结果。不得包装成可重试失败。

输出 Schema 错误需保存原始安全结果和协议错误状态，保留外部引用；不能因此重新发送变更。大体积 stdout/stderr 采用有界日志或受控产物引用，不能无限塞入结果和内存。

错误结构为 code、message、details、retry_hint。code 可以是 invalid_input、unavailable、authentication_failed、conflict、timeout、protocol_error 等通用类别；插件可提供命名空间化细节。message/details 必须脱敏。retry_hint 只是建议，不能覆盖 effect、未知结果、幂等与恢复规则。

### 3.3 cancel

返回必须区分：取消请求已提交、确认已取消、取消不支持、结果未知。请求取消不能提前将 Operation 标为 cancelled，也不证明远端进程停止。

## 4. Runtime Context

平台注入 plugin_instance_id、environment_id、job_id、step_instance_id、operation_id、invocation_key、lease_token、deadline，以及下列服务：

| 服务 | 用途 |
| --- | --- |
| storage | 当前插件实例的私有数据版本 |
| secrets | 当前调用获准的凭据版本访问 |
| logger | 有界、脱敏的结构化日志 |
| progress | 非权威进度信息，不决定成功 |
| cancellation | 读取取消请求，不保证能中止远端 |
| report_external_ref | 持久化已知外部引用 |
| report_checkpoint | 持久化恢复检查点 |

插件不能选择其他环境、用户或 Operation 作为回调归属。运行时根据当前调用填写关联，并在写入时检查执行资格。

report_external_ref(ref_key, reference) 和 report_checkpoint(checkpoint_key, data) 必须提供同步持久化确认；ref_key/检查点键稳定可去重。已确认引用保留历史，后续失败不能抹除。写库失败不表示外部对象不存在，应停止依赖该写入的后继操作并报告未决事实。

lease_token 可防止旧回调覆盖权威状态，不能停止旧外部调用。失去租约后的晚到证据只能在匹配原 Operation/目标后作为追加证据保存，由恢复流程核对，不能直接完成新执行尝试。

### 4.1 deadline 与不合作调用

prepare、execute、poll、cancel、prepare_session 和 open_session 都必须接受有限调用预算。插件使用剩余预算设置 DNS/连接、SSH banner/认证、读写、命令等待等可配置超时；重试不得重新获得完整预算，恢复查询或取消使用其各自明确的有限控制预算，不借恢复重新执行原变更。长任务使用 pending_external 和有界 poll，而不是无限阻塞等待。

插件内部的超时与取消是协作契约。平台等待 Future 超时、取消 Future 或 lease 到期都不能安全杀掉 Python 工作线程，也不能证明远端命令停止。插件应尽力关闭连接/释放本地句柄并返回已知事实；无法确认变更结果时返回 unknown，而不是保证可重试的失败。prepare 确认未派发的失败与已发出的执行超时必须区分。

未退出调用仍占真实执行 slot；平台不通过提前归还容量或无限补线程掩盖卡死。超期、实例并发耗尽和池饱和按执行规范降级，暂停受影响派发并等待管理员人工重启；不自动重启、不引入强制插件子进程。进程内限额只是逻辑隔离，不能隔离 GIL 长期占用、内存耗尽或插件导致的进程崩溃。

## 5. Storage API

```text
snapshot() -> snapshot_id + namespace_revision
get(key, version=None, snapshot_id=None) -> value + version
put(key, value, expected_version) -> version_ref
delete(key, expected_version) -> tombstone_version_ref
list(prefix=None, cursor=None, limit=..., snapshot_id=None) -> page
```

- 值为 JSONB 可保存的有限 JSON 对象，结构由插件解释；平台统一限制大小、深度和分页数量。
- 命名空间自动绑定 plugin_instance_id，不接受插件任意指定其他实例。
- 新建时要求键不存在；更新/删除要求 expected_version 匹配，否则冲突。
- 每次更新产生不可变历史版本，get(key, version) 必须实际读回该版本内容，不能只递增计数后覆盖旧值。
- 当前删除使用墓碑。被有效 Job 快照、未完成 Operation 或保留执行证据引用的历史版本不能清除。
- namespace_revision 是按插件实例递增的逻辑修订号，snapshot_id 固定一个 as-of 可见集合。每个键的值版本及墓碑必须可重建该集合，不能只覆盖当前行或用读取时的墙钟时间假装历史 list。
- Job 上下文中的 get/list 默认使用 Job 接受时固定的 snapshot_id，不允许插件为同一 Job 随意创建新快照切换到 latest。普通管理员配置管理在没有 Job 时可以创建新快照读取当前值。
- list 返回快照内未删除项及其精确版本；游标绑定 snapshot_id、prefix、稳定排序位置，跨页读取不会看到后来新增/修改的项，也不因后来删除漏掉原项。换快照/过滤条件复用游标必须拒绝。
- 显式 version 读取仅允许 Job 原视图已有版本，或由该执行组可信前序结果/检查点显式传递的派生 version_ref；不能通过猜版本读取其他实例或绕过目标快照。
- Job 内 put/delete 使用当前头版本进行 CAS，写入成功返回精确 version_ref，不自动改变本 Job 的 get/list 视图。后继步骤需要新值时必须显式引用该派生版本；冲突时不自动读取 latest 后覆盖。
- 普通 Storage 只存 SecretRef，不存明文密码、Token 或私钥。
- 插件可以保存主机清单，平台不解释它是否共享、是否独占及其业务合理性。必要的通用冲突键由模板/插件显式提供，平台不从 JSON 自动推导。

## 6. 凭据接口

SecretHandle 是运行时受控访问对象，不是 JSON 值。PreparedCall 和持久化数据只含凭据及版本引用。调用的句柄不能用于读取其他用户/环境或无关插件的秘密。

Job 接受时保存其允许秘密的版本解析映射。版本未指定的引用按该映射解析；Operation 再固定实际使用的版本。不能因 Step A 后管理员更新 Secret 当前版本，就让 Step B 自动获取新密码。未知执行恢复也使用原引用，缺失或不可用时阻断。

普通轮换保留历史版本，但旧版本只有仍被授权且远端仍接受时才能继续使用；密文存在不等于可用。紧急吊销和已知远端失效优先于快照，在派发与取得 SecretHandle 时重新检查，阻断未派发步骤，不回退更旧版本、不静默换新版本。已经交付给插件的明文无法追回，须按独立取消/远端撤权流程处理。

如模板需生成环境随机密码，平台凭据服务按环境/逻辑步骤/item_key/凭据槽位等稳定派生产物键生成并加密持久化，再返回精确 SecretRef。Job 内新凭据属于显式派生产物，不更新原秘密映射；后继步骤通过前序成功输出或受控检查点引用该版本，恢复不能再次生成。必须先通过模板步骤确认远端接受后，才允许学生查看。

显式关联恢复或新 Job 可以在核对目标和原外部事实后固定新凭据版本，记录替换原因；原 Operation 与凭据历史不改写。凭据轮换、失效与恢复策略见安全规范。

## 7. 通用交互会话

### 7.1 独立接口

普通 execute 的 JSON 结果不能携带双向终端流。插件另行声明 sessions，提供：

```text
prepare_session(session_type, input, context) -> PreparedCall
open_session(prepared_call, context) -> SessionOpenResult
send(session_handle, input_frame)
resize(session_handle, size)
receive(session_handle) -> EventStream
close_session(session_handle, reason) -> CloseResult
```

resize 为可选能力。open_session 是有持久化 Operation 的建立阶段；平台保存会话元数据并确认建立后，该 Operation 可成功，会话后续由 InteractiveSession 管理。建连遵守有限 deadline；active 后接收使用可检查取消/授权的有界等待和会话空闲策略，不给交互会话无限借用普通操作执行池。每个会话的缓冲、线程/任务和实例额度受限，失联关闭同样不能强杀不合作线程。学生入口来自模板 access/action，不从管理连接清单自动产生。

SessionOpenResult 返回运行时 session_handle 和可选的外部恢复引用。handle 不暴露给浏览器、不序列化为重启恢复依据。支持重连需另行声明恢复协议，不能默认重放输入。

### 7.2 帧与背压

客户端帧：input、resize、close；服务端帧：output、state、error、closed。每类帧有固定 Schema，输出带递增序号；平台拒绝未知帧和过大消息。

缓冲区和回放有界；慢客户端触发限速、丢弃回放或明确断开策略，不能无限堆积。输入控制仅允许一个当前控制者，接管后原控制者不能继续写；旁观者不可输入。重连不能自动重发已提交终端输入。

### 7.3 状态与恢复

```text
opening -> active -> closing -> closed
opening -> failed
opening / active / closing -> disconnected
```

opening 阶段失联或进程退出且无法证明未建立外部会话时，建立 Operation 进入 unknown，会话进入 disconnected；不能一律报 failed。只有确认未建立连接时才 failed。恢复查询确认会话已存在但不能重新附着时，仍记录断连并按关闭协议处置，不自动重建第二个会话。

closed 表示确认关闭当前通道，不表示远端后台命令或外部直连会话结束。disconnected 表示平台丢失连接事实，不能伪报远端停止。控制平面重启后，无恢复能力的会话标记 disconnected，不声称 SSH/TCP/PTY 可持久化恢复。

InteractiveSession 记录环境、可选资源、模板入口、建立 Operation、平台用户、插件实例、固定目标/凭据引用、状态、输入控制者、创建/结束/活动时间和原因。运行时句柄只存在内存中。

每次输入、输出、重连、旁观、接管及凭据查看均检查有效业务授权；接管和关闭有审计。默认不记录完整输入，不承诺通用脱敏器能消除所有终端输出秘密。

## 8. 最低插件验收

- 加载时验证 Manifest，optional 故障不删除原数据，required 故障阻止启动。
- 普通操作和交互会话有不同协议，不能用 JSON 输出假装终端流。
- Storage 实例隔离、历史读取、CAS 冲突、分页和被引用版本保护通过测试。
- prepare 固定目标，配置更新后 execute 不静默更换目标；Step A 后修改主机映射、增删 Storage 键或跨页读取，Step B 仍读 Job 原快照。
- Job 内写入的 Storage/新凭据只有显式派生版本可见；普通轮换不替换快照，紧急吊销阻断而不回退或偷换秘密。
- prepare/execute/poll/cancel 与会话建连超时、永久阻塞不返还 slot，饱和降级与人工重启恢复符合执行规范。
- SSH execute_command 在断线后不能无证据返回“无副作用且可重试”。
- 检查点和外部引用在失败后仍可读取，且不主动注册平台资源。
- 凭据不进入 Storage 明文、调用快照、普通结果或日志。
- 会话单输入控制、撤权、接管、背压及重启断连行为均可验证。
