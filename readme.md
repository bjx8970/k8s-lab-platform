# K8s Lab Platform

基于 Flask 的 Web 管理平台，自动化完成 PVE 虚拟机克隆 → OpenWrt 网络配置 → K8s 集群部署的全流程。支持多用户 RBAC 权限管理（管理员/教师/学生）。

通用实验平台的四模块架构、控制平面、模板机制及实施交接见 [平台设计与接手指南](docs/platform-design/README.md)（设计稿，尚未实现）。

资源管理框架及 PVE、OpenWrt、K8s、虚拟机插件的规划见 [统一资源管理框架设计](docs/resource-framework/README.md)（设计稿，尚未实现）。

## 架构

```
用户浏览器 → Flask Web + SocketIO (app.py)
                ├── PVE API (proxmoxer)           → Proxmox VE 虚拟化 (支持多服务器)
                ├── OpenWrt SSH (paramiko)        → OpenWrt 路由器 (UCI 配置)
                ├── SQLite / PostgreSQL           → 集群数据持久化 (SQLAlchemy)
                ├── WebSSH (xterm.js + SocketIO)  → 浏览器终端 SSH 到 client VM
                ├── VM 状态缓存 (status_cache)     → 后台定时轮询 PVE VM 状态
                └── 异步任务队列 (task_queue)      → 创建/删除/部署异步执行
```

## 功能

- **用户与权限管理** — 管理员/教师/学生三级 RBAC，支持 CSV 批量导入用户
- **课程与组管理** — 教师创建课程与组，学生分配到组，可按组批量创建集群
- **PVE 连接管理** — 支持多 PVE 服务器，配置 API Token，查看节点/虚拟机/模板
- **OpenWrt 管理** — 通过 SSH + UCI 管理 VLAN、接口、DHCP、dnsmasq、防火墙
- **K8s 集群创建** — 一键完成：
  - 自动编号集群名称 (`k8s_1`, `k8s_2` ...)
  - VLAN 规划 (ID 101+，子网 `10.100.N.0/24`)
  - OpenWrt 网络配置 (VLAN 设备、接口、DHCP、dnsmasq、防火墙)
  - PVE linked clone (cloud-init + 随机 MAC + SSH 密钥注入)
  - 生成并上传 Ed25519 SSH 密钥对到 client VM
  - OpenWrt 端口转发 (WAN:N+50000 → client:22)
  - OpenWrt 静态 DHCP 绑定
- **K8s 部署** — 基于 kubeasz 在 client VM 内自动部署 Kubernetes 集群
- **异步任务** — 创建/删除/部署均异步执行，前端实时显示进度条 + 详细日志页面，支持取消运行中的任务
- **集群删除** — 释放所有虚拟机并清理 OpenWrt 配置，支持强制删除异常集群
- **浏览器 WebSSH** — 基于 xterm.js + SocketIO + paramiko 的浏览器终端，教师/管理员可接管或查看学生会话
- **用户在线追踪** — 通过 SocketIO 实时追踪用户在线状态
- **VM 关机投票** — 学生组成员可投票决定是否关闭共享集群的 VM
- **数据库切换** — 支持 SQLite ↔ PostgreSQL 在线切换和数据迁移

## 前置条件

1. **Proxmox VE** 7.x+
   - 已配置 API Token（无需 root 密码）
   - API Token 用户需要以下最小权限（可在 Datacenter → Permissions 中为用户或 API Token 分配自定义角色）：

     | Privilege | 用途 |
     |---|---|
     | `VM.Audit` | 查看虚拟机、模板列表与状态 |
     | `VM.Clone` | 克隆模板创建新虚拟机 |
     | `VM.Config.Network` | 配置虚拟机网络 (net0) |
     | `VM.Config.Cloudinit` | 注入 cloud-init 配置 |
     | `VM.Config.Options` | 修改虚拟机其他配置选项 |
     | `VM.PowerMgmt` | 启动 / 停止 / 重启虚拟机 |
     | `VM.Allocate` | 创建和删除虚拟机 |
     | `VM.Monitor` | 通过 QEMU Guest Agent 执行命令 |
     | `Sys.Audit` | 查看节点列表、集群信息 |
     | `Datastore.AllocateSpace` | 分配磁盘空间（克隆时需要） |

     也可以直接使用内置角色 `PVEAdmin`（包含全部权限）或 `PVEVMAdmin`（需额外补 `Datastore.AllocateSpace`）。
   - VM 模板需预装 `cloud-init` + `qemu-guest-agent`
   - 模板需有 `k8s` 用户（cloud-init 配置 `ciuser: k8s`）
   - 模板需预装必要系统包：`python3-venv`、`curl` 等
2. **OpenWrt** 路由器
   - 已启用 SSH
   - `eth1` 作为 VLAN 上行口（可在代码中修改）

## 安装

```bash
# 克隆项目
git clone <repo-url> k8s-lab-platform
cd k8s-lab-platform

# 创建虚拟环境
python -m venv venv
# Linux/Mac
source venv/bin/activate
# Windows
venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
```

## 配置

首次启动会自动跳转数据库配置页面。完整配置流程如下：

1. **数据库配置** (`/db-config`) — 首次启动时配置 PostgreSQL 连接（或使用默认 SQLite）
2. **初始化管理员** (`/setup`) — 创建默认管理员账号
3. **PVE 配置** (`/pve`) — 添加一个或多个 PVE 服务器
   - 名称、Host、User、Token Name、Token Value、Verify SSL、Port
4. **OpenWrt 配置** (`/openwrt`) — 填写路由器 SSH 信息
   - Host、Username、Password、Port
5. **数据库管理** (`/db`) — 管理员可切换 SQLite / PostgreSQL 模式

PVE 和 OpenWrt 配置保存在数据库的 `config` 表或 `pve_servers` 表。

## 启动

```bash
# 开发模式 (Flask dev server)
python app.py

# 生产模式 (flask-socketio threading + Werkzeug)
# 自动生成 .secret_key 文件，或设置环境变量
set FLASK_SECRET_KEY=your-secret-key   # Windows
export FLASK_SECRET_KEY=your-secret-key  # Linux/Mac
python run.py

# 生产模式 (waitress，无 WebSocket 传输)
pip install waitress
set FLASK_SECRET_KEY=your-secret-key
waitress-serve --host 0.0.0.0 --port 5000 app:app
```

访问 http://localhost:5000

## 使用流程

### 1. 用户管理 (`/users`)

- 管理员可创建/编辑/删除管理员、教师、学生账号
- 教师可创建/管理学生账号
- 支持 CSV 模板下载和批量导入用户

### 2. 课程与组管理 (`/classes`)

- 教师创建课程和组，将学生分配到组
- 支持 CSV 模板下载和批量导入课程、组、成员

### 3. 创建集群 (`/k8s`)

#### 单组创建
- 选择 PVE 服务器、PVE 节点、模板 VMID
- 设置主节点/子节点数量、CPU、内存
- 选择目标组
- 点击"创建集群"，观察进度条
- 创建完成后可选择 **部署 K8s**

#### 批量创建
- 选择多个组，为每个组创建一个独立集群
- 参数统一设置（节点数、规格、模板等）

### 4. 集群管理

- **查看详情** — 查看集群各 VM 状态、SSH 密钥
- **启动/停止** — 一键启停集群所有虚拟机
- **部署 K8s** — 自动通过 kubeasz 安装 Kubernetes
- **删除集群** — 释放所有虚拟机并清理 OpenWrt 配置

### 5. 角色说明

| 角色 | 权限 |
|---|---|---|
| admin | 所有功能，包括 PVE/OpenWrt/数据库配置、用户管理、WebSSH 管理面板 |
| teacher | 创建课程、组、学生账号，管理自己创建的集群，接管/查看学生会话 |
| student | 查看自己被分配的集群，启动/停止虚拟机，WebSSH 连接 client VM |

## 集群命名与网络规划

| 项 | 规则 |
|---|---|
| 集群名 | `k8s_1`, `k8s_2` ... 数据库自增 |
| VLAN ID | 100 + N |
| VLAN 设备 | `eth1.<VLAN_ID>` |
| 接口名 | `k8s_N` |
| 子网 | `10.100.N.0/24` |
| 网关 | `10.100.N.1` |
| dnsmasq | `k8s<N>` |
| 虚拟机 | `client-k8s<N>`, `master{i}-k8s<N>`, `node{i}-k8s<N>` |
| VMID | PVE API 自动分配 |
| MAC | `52:54:00` 前缀 + 随机后缀 |
| SSH 端口转发 | WAN:`50000+N` → client:22 |

## 文件结构

```
├── app.py                      # Flask 主程序与路由 (~2774 行)
├── run.py                      # 生产模式入口（自动生成 .secret_key）
├── .db_config.json             # 数据库切换配置（SQLite / PostgreSQL，含密码，gitignore）
├── .secret_key                 # 生产模式自动生成的密钥（gitignore）
├── modules/
│   ├── __init__.py
│   ├── db.py                   # SQLAlchemy 模型与数据库操作
│   ├── pve_client.py           # PVE API 封装 (proxmoxer)
│   ├── openwrt_client.py       # OpenWrt SSH/UCI 封装 (paramiko)
│   ├── k8s_manager.py          # 集群编排 + K8s 部署
│   ├── task_queue.py           # 异步任务队列（create/delete/deploy）
│   ├── ssh_terminal.py         # WebSSH 会话池管理 (paramiko)
│   ├── status_cache.py         # VM 状态后台缓存
│   └── pg_client.py            # PostgreSQL 连接测试 (pg8000)
├── templates/
│   ├── base.html               # 布局模板
│   ├── index.html              # 首页（admin/teacher）
│   ├── student.html            # 学生首页
│   ├── k8s.html                # K8s 集群管理
│   ├── k8s_logs.html           # 异步任务日志
│   ├── pve.html                # PVE 多服务器配置
│   ├── openwrt.html            # OpenWrt 配置
│   ├── db_config.html          # 数据库切换管理
│   ├── db_setup.html           # 首次启动数据库配置向导
│   ├── setup.html              # 初始管理员设置
│   ├── users.html              # 用户管理
│   ├── classes.html            # 课程与组管理
│   ├── login.html              # 登录
│   ├── 403.html                # 权限不足
│   ├── admin_webssh.html       # WebSSH 管理面板
│   └── _standalone_base.html   # 独立页面布局（登录/设置）
├── k8s_lab.db                  # SQLite 数据文件（运行后生成，.gitignore）
└── requirements.txt            # Python 依赖
```

## 数据存储

| 数据 | 存储位置 |
|---|---|
| 集群数据 | `clusters` + `vms` 表 |
| 用户数据 | `users` 表 |
| 课程/组数据 | `classes` + `groups` + `group_members` 表 |
| PVE 配置 | `pve_servers` 表（多服务器）或 `config` 表（旧版单服务器） |
| OpenWrt 配置 | `config` 表 |
| 数据库模式 | `.db_config.json` 指定 `sqlite` 或 `postgresql` |

集群编号由数据库自增 ID 自动分配。旧版 `.k8s_clusters.json`、`.pve_config.json`、`.openwrt_config.json` 文件会在启动时自动迁移到数据库。

## 依赖

- Python 3.8+
- Flask, Flask-SocketIO, Flask-Login, Flask-WTF
- proxmoxer, paramiko, sqlalchemy, cryptography, pg8000

详见 `requirements.txt`

## 注意事项

- PVE 模板必须已有 `qemu-guest-agent` 并启用
- `k8s` 用户需要在模板中配置 passwordless sudo
- client VM 第一次启动后需重启才能刷新 DHCP 主机名
- K8s 部署从 `http://10.11.43.82/download/` 下载离线安装包，需内部网络可达
- 生产环境必须设置环境变量 `FLASK_SECRET_KEY`；使用 `python run.py` 会自动生成 `.secret_key` 文件
- Flask-SocketIO 使用 threading 模式（非 eventlet/gevent），如需 WebSocket 传输需额外安装 `simple-websocket`
- OpenWrt VLAN 设备名含小数点，需通过 UCI 索引方式操作
- WebSSH 连接数可在管理面板 `/admin/webssh` 配置（全局/学生/教师上限、空闲超时）
