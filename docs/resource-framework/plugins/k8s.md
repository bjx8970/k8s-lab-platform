# K8s 插件设计

插件 ID：`k8s`；资源类型：`k8s.cluster/v1`。遵循[框架契约](../architecture.md)。

## 定位

提供 Kubernetes 集群的登记、查询、安装器指令和结果。基础插件不创建 VM/网络，不规划节点，不检查一台 VM 属于哪个实验环境，也不决定多个资源如何先后操作。

K8s 集群无论由本平台安装还是外部创建，都可以登记查询。是否向特定调用方开放某项操作由应用决定；插件只声明实际具备的技术能力。

## 连接和资源属性

| 字段 | 含义 |
|---|---|
| api_connection_id | Kubernetes API 端点/TLS/kubeconfig secret_ref |
| installer_driver | 可选，首版 kubeasz/v1；提供部署动作时使用 |
| execution_connection_id | 可选，执行安装命令的 SSH 连接 |
| installation_locator | 安装器工作目录/集群名等定位 |
| attributes | 已知版本、API 地址、安装器配置摘要 |
| metadata | 调用方不透明业务关联，不用于判断是否允许操作 |

安装器输入由调用方完整提供：版本化安装参数、离线包/仓库信息、inventory 中的节点地址/名称/角色、安装目录。插件验证格式和安装器支持的参数，不向 VM 插件询问这些机器是否已开机或是否能被分配给另一个集群。

接入 K8s 不依赖 PVE/OpenWrt 插件。execution_connection 可以访问任意适当的执行机器，不要求先登记为 client VM。

## 首版动作

| 动作 | 内容 | 完成结果 |
|---|---|---|
| observe | 查询 API 版本、可达性和集群状态 | 实际查询数据或平台错误 |
| list_nodes | 列出节点及 Kubernetes 返回的 conditions | 节点列表，不判断实验环境是否合格 |
| deploy | 将完整安装输入交给 kubeasz 驱动执行 | 安装器退出码、日志、外部作业标识 |
| poll | 查询已知远端安装作业 | 执行中/完成/失败/unknown |
| read_installation | 查询指定安装目录中的安装状态/产物信息 | 已知事实，凭据正文通过专门连接适配保存 |

通用 register/unregister 由框架提供。独立卸载、升级、扩缩容在对应驱动方法实现后注册为新动作，不以业务“是否危险”决定是否注册。首版未实现的方法返回 UnsupportedAction。

deploy 只安装这一项 K8s 集群软件，不会先发送 VM start，也不会创建 OpenWrt 端口转发。机器不可达时返回执行错误；调用方决定先启动机器、补建网络还是终止流程。

## 安装器协议

```python
class KubernetesInstaller:
    def describe_actions(self) -> list[ActionDescriptor]: ...
    def execute(self, ctx, target, action, parameters) -> ExecutionResult: ...
    def poll(self, ctx, external_task_ref, exec_data) -> ExecutionResult: ...
```

kubeasz 驱动在一条 deploy 指令内部可按安装器协议准备文件、生成 inventory、启动安装命令。这是安装一个软件资源的底层执行，不包含跨资源编排、环境分配或补偿操作。框架不拆成 DAG，也不按节点业务归属设置前置检查。

实际命令来自驱动模板，参数正确编码；不将教学账户创建、学生 kubeconfig 分发加入 deploy。这些属于业务模块另外安排的动作。

## 执行结果与观察结果分离

安装命令退出 0 时，operation 可为 succeeded；节点是否全部 Ready、哪些组件符合课程要求由另外的 observe/list_nodes 查询及业务判断完成。

插件返回实际版本、各节点 Ready conditions 和 API 错误，不自动聚合成“实验环境可用”。kubeconfig 文件存在也只是一个观察事实，不替代安装器执行记录。

Kubernetes 对象的状态和名称/UID 语义可参考 [对象模型](https://kubernetes.io/docs/concepts/overview/working-with-objects/)和[名称与 UID](https://kubernetes.io/docs/concepts/overview/working-with-objects/names/)。未来管理集群内对象时使用明确 cluster domain 和对象 UID/定位；这不引入框架级依赖管理。

## 长时间命令与失败

远端安装作业以 operation_id 对应的日志、执行标识和退出记录跟踪，避免只依赖一次 SSH streaming 连接。SSH 断开时返回 pending 或 unknown；存在作业标识则继续查询，不自动启动第二次安装。

失败时返回已知的安装目录、进度、日志摘要和退出码，不删除 VM、清理网络、恢复节点或重新安装。调用方需要重试时显式提交新的 deploy；插件不推断重试是否符合业务意图。

unregister 仅关闭集群登记，不检查是否有工作负载或其他资源使用它，也不卸载 Kubernetes。销毁 VM 和由此产生的后果完全由发出相关命令的外部模块管理。

## 当前实现拆分

`k8s_manager.py:deploy_k8s` 中的平台/集群运行状态检查、从 PVE 配置推导 OpenWrt 地址、按集群名推算端口、学生账户处理移到应用编排模块。

可复用的 SSH 文件传输、kubeasz 参数生成、命令执行与日志采集作为安装器适配。硬编码的安装源和账号由调用方配置提供，插件不选择全局业务默认值。

## 验收

- 外部已存在的 K8s 可登记查询，不要求存在 PVE/VM 资源。
- deploy 接受准备好的 inventory，不调用 VM 或 OpenWrt 插件。
- 安装命令成功与节点不 Ready 可以同时如实返回，不混为一个业务结论。
- 节点被其他集群使用的业务关系不由插件校验。
- 失败、部分成功、SSH 断线只返回结果，无自动清理或重试。
- unregister 不需要依赖图或删除计划，也不卸载软件。
