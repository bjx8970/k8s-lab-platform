# 实验模板与扩展机制

返回[接手指南](README.md)。状态：模板格式提案，解析器、规划器与通用前端均待实现。

## 1. 三种配置不能混为一谈

| 对象 | 内容 | 管理者 |
|---|---|---|
| 实验模板 LabTemplate | 参数、能力要求、资源声明、生命周期步骤、就绪检查、访问入口 | 管理员 |
| 部署预设 DeploymentProfile | 允许的 domain/connection/node、镜像映射、网络池、存储和规格范围 | 管理员 |
| PVE VM 镜像模板 | 预装系统和软件的实际虚拟机模板 | 平台运维人员 |

模板描述“提供什么环境”，部署预设将它绑定到具体站点。用户选择模板与允许的预设，填写参数；不能在普通参数里任意覆盖连接凭据或扩大可分配范围。

新增 Python 环境可以复用 VM 插件及预制 Python 镜像。若模板需要尚未实现的容器平台、任意远程初始化、IDE 代理或新协议，就先增加对应插件/宿主能力；上传模板不能自动产生不存在的能力。

## 2. 首版格式

采用 JSON，api_version=lab.template/v1。示例见[Python 开发环境模板](examples/python-development.json)。这是后续实现的输入契约提案，当前应用不会读取执行该文件。

| 字段 | 必需 | 语义 |
|---|---|---|
| api_version、kind | 是 | 格式版本及 LabTemplate 类型 |
| metadata.id、metadata.version、metadata.name | 是 | 模板标识、发布版本、显示名称 |
| parameters_schema | 是 | 用户参数的 JSON Schema；首版支持对象、基础类型、必填、默认值、枚举、范围和字符串 pattern |
| requirements | 是 | 需要的资源类型、驱动和动作；对应已加载的能力描述 |
| resources | 是 | 逻辑资源名到类型/驱动/创建或借用方式的声明 |
| planning | 是 | 已注册规划规则及输入；输出固定计划 |
| lifecycle | 是 | 至少 create/delete；可选 start/stop，均为有限顺序步骤 |
| failure_policy | 是 | 首版固定 retain：失败保留已产生资源并停下 |
| access | 否 | 环境详情页可展示的访问描述，如 ssh、web |

资源框架 action 描述负责具体动作参数 schema，模板 schema 负责结构，规划器负责补齐实际参数。插件依赖范围还应进入发布校验；任务保存实际采用的插件版本和能力摘要，不让插件升级静默改变在途命令。

首版的元数据编辑不改变已发布内容。重复发布同版本不同内容返回冲突；停用模板阻止新建，既有环境继续使用固定版本执行已声明生命周期。

## 3. 参数引用

引用使用仅含一个键的对象，例如 {"$ref":"parameters.cores"}。这是本模板格式的数据引用，不是 JSON Schema 中解析 schema 的 $ref。

| 命名空间 | 产生者 | 示例 |
|---|---|---|
| parameters | 参数 schema 校验并填充默认值后的输入 | parameters.cores |
| plan | 规划器持久化输出 | plan.workspace.create_request |
| resources | 环境逻辑名到已登记 UUID 的绑定 | resources.workspace.resource_id |

解析器递归替换引用并保留原数据类型，不能把整个对象强行转成字符串；不执行 Python、shell、Jinja 或任意表达式。普通字符串保持原值，不提供隐式插值。引用不存在时返回明确错误，不能替换为空值继续调用插件。

发布时检查根命名空间、资源声明、可解析的计划输出路径和步骤先后关系；依赖运行结果的资源 ID 在执行时绑定并持久化。字段中的点号用作路径分隔，首版逻辑名使用字母、数字和下划线，不包含点。

## 4. 最小步骤集合

| kind | 字段/行为 | 所在层 |
|---|---|---|
| resource.create | resource 逻辑名、request；请求包含 type/driver_id/connection_id/parameters，调用 ResourceService.create | 编排→资源 |
| resource.register | resource 逻辑名、request；绑定已存在对象，记录 created/adopted 的来源 | 编排→资源 |
| resource.action | target resource_id、action、parameters；调用 execute | 编排→资源 |
| wait.observation | target、field、equals、timeout_seconds、interval_seconds；调用 observe 并比较指定观察字段 | 编排 |
| wait.endpoint | endpoint.protocol/host/port、timeout_seconds、interval_seconds；首版 TCP 探测 | 编排 |

resource.create/action 默认等待该 Operation 成功才推进下一步，返回 pending 时持久化 waiting。register 是数据库登记动作，返回资源引用后推进。调用上下文及稳定 request_id 由执行器产生，模板不能任意覆盖。

resource.create 接受时就产生 resource_id：编排应立即保存关联及操作引用，而非等整个创建成功后才保存。部分创建仍可追踪；resource.register 不意味着拥有外部对象的删除权，责任由编排声明。

resource.action 可显式声明 skip_if_observation={field,equals}。执行器先调用 observe；只有新鲜观察值等于模板给定值时，将该步骤记为 succeeded，结果带 skipped=true 和观察依据，不发送资源命令。观察失败不能当成匹配成功；默认阻塞并报告。这个受限的相等判断用于“已关机则跳过关机”等场景，不引入表达式引擎，也不成为资源插件的隐式行为。观察与命令之间仍可能发生外部状态变化，平台拒绝时如实报告。

wait.observation 从对应资源类型的观察结果中取字段，例如 VM 的 power_state。wait.endpoint 超时只说明就绪条件未满足，不反向删除资源。探测是编排判断能力，不能变成资源层的强制准入检查。

扩展 HTTP 或软件特定的就绪判断时注册新的 evaluator/版本化步骤能力，不能默默让 TCP 成功代表应用完整可用。为访问入口生成地址也不代表入口已经满足就绪条件。

每个步骤 ID 在生命周期内唯一；执行时加入 task_id 形成全局稳定身份。首版不支持任意 DAG、用户脚本或运行时循环。节点数量扩展由规划器展开有限资源集合与顺序步骤，具体上限在模板参数与部署预设中共同校验。

## 5. 部署预设及规划输出

预设引用已有资源连接，包含以下非敏感配置：

- PVE domain、connection、允许 node/storage/bridge，及逻辑镜像名到模板 VMID/镜像修订的映射。
- 可分配 VMID/IP/VLAN/端口范围、可借用网络和 OpenWrt router 资源引用。
- 默认规格、允许规格上限、SSH 用户与公钥/连接凭据引用。
- 访问地址生成规则，以及对应访问网络是否需要端口转发。

connection/secret 的生命周期属于资源接入层；镜像用途、池分配和 PVE/OpenWrt 配对属于编排预设。修改预设生成新 revision。规划保存实际使用的镜像绑定/修订，运行任务不重新查询一个可能已经指向别的 VM 的浮动镜像别名。

Python 示例采用规划器 pve.single_vm/v1：给定逻辑资源名、镜像别名、规格与访问网络，选择预设允许的节点并预留 VMID/IP，输出 plan.workspace：

| 输出 | 内容 |
|---|---|
| create_request | type=compute.vm/v1、driver_id=pve.qemu/v1、connection_id，以及完整 parameters |
| create_request.parameters | name、cpu.cores、memory_mib、nics、initialization、provider_options |
| provider_options | node、template_vmid、vmid、storage、clone_mode 等驱动必需值 |
| access | host、port、username；来自已分配地址和镜像约定 |

nics/initialization 的完整结构由 VM/PVE schema 定义。该示例要求预设提供已存在、客户端与 worker 均可访问的网络，预制镜像包含 Python 运行环境及 SSH 服务，地址经 cloud-init 明确设置；规划器不隐式创建路由或端口转发。站点需要新网络时，在模板中显式增加 OpenWrt 资源步骤。

## 6. Python 环境完整路径

[示例模板](examples/python-development.json)包含：

1. create：克隆预制 Python VM → 启动 → 等待 SSH 端口。
2. stop：若尚未停止，明确 graceful stop → 等待 power_state=stopped。
3. start：启动 → 等待 SSH 端口。
4. delete：若已停止则按模板跳过关机，否则明确关机 → 等待停止 → 删除这台 VM。
5. access：输出 SSH 主机、端口、用户名和 VM resource_id，通用页面可接入 WebSSH。

这个模板提供终端式 Python 开发环境。SSH 端口可达仅是此模板的最低就绪约定；Python 安装正确性由受控镜像验收保证。如果要提供浏览器 IDE，需在镜像中预装服务，并具备相应的访问转发、鉴权和就绪探测能力后添加 web 入口。

销毁步骤针对该模板创建的资源。若创建中途失败，编排依据真实关联和操作结果生成针对已产生资源的清理任务；已确认从未产生的资源不用删除，结果不明的资源先核对。通用执行器不自动反转 create 步骤，也不自动删除借用资源。

## 7. K8s 模板规划

K8s 实验使用同一 Environment 模型及前端，内部有基础设施与软件两段生命周期。首版迁移可保留“创建基础环境后，用户再请求安装”的现有交互，以明确的 install 扩展生命周期表示，不把 VM 开机等同于 K8s 已就绪。

| 阶段 | 编排职责 | 调用的资源能力 |
|---|---|---|
| 规划 | 固定预设/镜像，预留 VMID、地址、VLAN、端口，展开 client/control-plane/worker 节点 | 平台/资源只读查询 |
| 网络配置 | 按模板顺序创建 VLAN、接口、DHCP，处理明确的 zone 关联 | OpenWrt 单资源指令 |
| VM 配置 | 为每个节点给出 clone、规格、网卡、cloud-init 参数 | VM/PVE create |
| 网络绑定与应用 | 写入静态租约/转发，显式 reload/restart | OpenWrt 指令 |
| 启动与等待 | 启动所有节点，确认安装执行节点及目标地址达到模板条件 | VM start、编排等待 |
| 集群登记与安装 | 提供固定安装源、版本、inventory 和 execution_connection | K8s register/deploy |
| 就绪判断 | 读取 API 和节点条件，按模板判定实验可用 | K8s observe/list_nodes、编排 evaluator |
| 访问输出 | 生成 WebSSH、API 地址和受权限保护的 kubeconfig 访问引用 | 通用访问描述 |
| 销毁 | 根据实际关联明确关机/删除 VM、移除自建网络配置、关闭登记、释放分配 | 多条独立资源操作 |

保留分段安装交互时，基础设施创建任务可以 succeeded，但环境在完成软件就绪检查前保持 provisioning，并通过 InfrastructureReady=true、SoftwareReady=false、WaitingForInstall 条件展示进度；install 成功且就绪条件满足后才标 ready。API 返回这些条件，避免旧的“运行中”混合表示 VM 开机与 K8s 可用。

该表是迁移配方要求，不是可直接执行的模板。K8s 专用规划器的输出 schema、list_nodes 就绪 evaluator 和各 OpenWrt 参数 schema 在相应实施阶段固化。Python 示例不依赖这些 K8s 专用能力。

当前 OpenWrt 基础设计的 firewall_zone 只支持发现和成员调整，尚无 zone create/delete。迁移时应明确选择预设中的既有 zone，或先补充对应的单资源动作及契约，再用模板新建 zone。不能假设模板已经具备该能力，也不能保留旧客户端隐式创建/删除 zone 的组合行为。

K8s 基础插件当前没有通用访客系统执行、升级、扩缩容和卸载能力。需要准备密钥/账号等动作时，优先使用镜像/cloud-init 可表达的能力；无法表达的工作保留明确的宿主适配步骤，后续再抽取受控能力，不能在模板中调用任意 Python 函数。

## 8. 发布与前端

发布流程：编辑草稿 → 校验结构和参数 → 校验类型/驱动/动作及规划/等待能力 → 固定版本发布。站点绑定在使用某预设或创建实例时校验，不能因模板结构合法就认为所有站点均可部署。

通用前端只需模板目录、参数表单、环境列表/详情、任务日志和访问入口。按 parameters_schema 渲染控件，按 lifecycle 展示允许的动作，再结合实际用户权限生成按钮。插件支持某动作并不意味着当前用户拥有该动作权限。

access 类型首版支持 ssh；web 可由后续访问适配注册。SSH 描述含 kind、label、resource_id、host、port、username；真实私钥由 WebSSH 的受控连接流程获取，不写进模板或普通环境输出。新入口类型需要对应渲染/访问适配，不能只靠模板让浏览器理解未知协议。

模板只新增现有能力的组合时无需后端专用路由或类型分支；如果缺少能力，发布或实例化返回具体缺失项，管理员补齐后再使用。

## 9. 扩展验收

- 同一模板使用不同部署预设，生成不同 domain/连接的资源，身份不串用。
- 发布 1.1.0 不改变使用 1.0.0 的运行环境、计划或销毁步骤。
- 参数和引用在外部变更前校验；未知动作、规划器或入口类型可明确报告。
- Python 通过已有 VM/PVE 能力和预制镜像实现，无 Python 专用数据库表/API/page 分支。
- 环境 phase、任务结果与资源事实分别展示；失败后能找回已创建资源。
- 直接资源命令不要求存在任何 LabTemplate 或 Environment。
