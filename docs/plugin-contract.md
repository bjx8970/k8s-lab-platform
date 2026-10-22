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

```yaml
manifest_version: 1
api_version: "1"
plugin_key: ssh
plugin_version: "1.0.0"
display_name: SSH 执行插件
entrypoint: plugin:SSHPlugin

actions:
  execute_command:
    display_name: 执行命令
    input_schema:
      type: object
      additionalProperties: false
      properties:
        host_id: {type: string, minLength: 1}
        command: {type: string, minLength: 1}
      required: [host_id, command]
    output_schema:
      type: object
      additionalProperties: false
      properties:
        exit_code: {type: integer}
        stdout: {type: string}
        stderr: {type: string}
      required: [exit_code]
    execution:
      effect: mutating
      idempotency: unsupported
      recovery: unsupported
      cancellation: unsupported
```

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

## 5. Storage API

```text
get(key, version=None) -> value + version
put(key, value, expected_version) -> new_version
delete(key, expected_version) -> tombstone_version
list(prefix=None, cursor=None, limit=...) -> page
```

- 值为 JSONB 可保存的有限 JSON 对象，结构由插件解释；平台统一限制大小、深度和分页数量。
- 命名空间自动绑定 plugin_instance_id，不接受插件任意指定其他实例。
- 新建时要求键不存在；更新/删除要求 expected_version 匹配，否则冲突。
- 每次更新产生不可变历史版本，get(key, version) 必须实际读回该版本内容，不能只递增计数后覆盖旧值。
- 当前删除使用墓碑。被未完成 Operation 引用的历史版本不能清除。
- list 默认只列当前未删除数据，并返回版本以供 prepare 固定引用。
- 普通 Storage 只存 SecretRef，不存明文密码、Token 或私钥。
- 插件可以保存主机清单，平台不解释它是否共享、是否独占及其业务合理性。必要的通用冲突键由模板/插件显式提供，平台不从 JSON 自动推导。

## 6. 凭据接口

SecretHandle 是运行时受控访问对象，不是 JSON 值。PreparedCall 和持久化数据只含凭据及版本引用。调用的句柄不能用于读取其他用户/环境或无关插件的秘密。

如模板需生成环境随机密码，平台凭据服务生成并加密保存，模板以 SecretRef 传给插件；必须先通过模板步骤确认远端接受后，才允许学生查看。凭据轮换、失效与恢复策略见安全规范。

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

resize 为可选能力。open_session 是有持久化 Operation 的建立阶段；平台保存会话元数据并确认建立后，该 Operation 可成功，会话后续由 InteractiveSession 管理。学生入口来自模板 access/action，不从管理连接清单自动产生。

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
- prepare 固定目标，配置更新后 execute 不静默更换目标。
- SSH execute_command 在断线后不能无证据返回“无副作用且可重试”。
- 检查点和外部引用在失败后仍可读取，且不主动注册平台资源。
- 凭据不进入 Storage 明文、调用快照、普通结果或日志。
- 会话单输入控制、撤权、接管、背压及重启断连行为均可验证。
