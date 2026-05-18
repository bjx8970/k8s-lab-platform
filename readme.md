# K8s Lab Platform

基于 Flask 的 Web 管理平台，自动化完成 PVE 虚拟机克隆 → OpenWrt 网络配置 → K8s 集群部署的全流程。

## 架构

```
用户浏览器 → Flask Web (app.py)
                ├── PVE API (proxmoxer)      → Proxmox VE 虚拟化 (支持多服务器)
                ├── OpenWrt SSH (paramiko)   → OpenWrt 路由器 (UCI)
                ├── SQLite / PostgreSQL      → 集群数据持久化 (sqlalchemy)
                └── pg_client (pg8000)       → PostgreSQL 连接测试
```

## 功能

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
- **异步任务** — 创建/删除/部署均异步执行，前端实时显示进度条 + 详细日志页面
- **集群删除** — 释放所有虚拟机并清理 OpenWrt 配置
- **数据库切换** — 支持 SQLite ↔ PostgreSQL 在线切换和数据迁移

## 前置条件

1. **Proxmox VE** 7.x+
   - 已配置 API Token（无需 root 密码）
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

启动后通过 Web 页面配置：

1. **PVE 配置** (`/pve`) — 添加一个或多个 PVE 服务器
   - 名称、Host、User、Token Name、Token Value、Verify SSL、Port
2. **OpenWrt 配置** (`/openwrt`) — 填写路由器 SSH 信息
   - Host、Username、Password、Port
3. **数据库配置** (`/db`) — 切换 SQLite / PostgreSQL 模式

PVE 和 OpenWrt 配置保存在数据库的 `config` 表（单值）或 `pve_servers` 表（多服务器）。

## 启动

```bash
# 开发模式
python app.py

# 生产模式
pip install waitress
set FLASK_SECRET_KEY=your-secret-key   # Windows
export FLASK_SECRET_KEY=your-secret-key  # Linux/Mac
waitress-serve --host 0.0.0.0 --port 5000 app:app
```

访问 http://localhost:5000

## 创建集群

1. 进入 **K8s 集群管理** (`/k8s`)
2. 填写参数：
   - PVE 服务器、PVE 节点、模板 VMID
   - 主节点/子节点数量、CPU、内存
   - client 密码（默认 `k8s.1234`）
3. 点击"创建集群"，观察进度条
4. 点击"查看详细日志"在新标签页打开实时日志
5. 创建完成后可选择 **部署 K8s**，自动通过 kubeasz 安装 Kubernetes

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
| 虚拟机 | `client-k8s<N>`, `master1-k8s<N>`, `node1-k8s<N>` |
| VMID | PVE API 自动分配 |
| MAC | `52:54:00` 前缀 + 随机后缀 |
| SSH 端口转发 | WAN:`50000+N` → client:22 |

## 文件结构

```
├── app.py                      # Flask 主程序与路由
├── .db_config.json             # 数据库切换配置（SQLite / PostgreSQL）
├── modules/
│   ├── __init__.py
│   ├── db.py                   # SQLAlchemy 模型与数据库操作
│   ├── pve_client.py           # PVE API 封装 (proxmoxer)
│   ├── openwrt_client.py       # OpenWrt SSH/UCI 封装 (paramiko)
│   ├── k8s_manager.py          # 集群编排 + 异步任务 + K8s 部署
│   └── pg_client.py            # PostgreSQL 连接测试 (pg8000)
├── templates/
│   ├── base.html               # 布局模板
│   ├── index.html              # 首页
│   ├── k8s.html                # K8s 管理页面
│   ├── k8s_logs.html           # 执行日志查看页面
│   ├── pve.html                # PVE 多服务器配置页面
│   ├── openwrt.html            # OpenWrt 配置页面
│   ├── db_config.html          # 数据库切换页面
│   ├── users.html              # 用户页面
│   └── classes.html            # 班级页面
├── k8s_lab.db                  # SQLite 数据文件（运行后生成，.gitignore）
└── requirements.txt            # Python 依赖
```

## 数据存储

| 数据 | 存储位置 |
|---|---|
| 集群数据 | `clusters` + `vms` 表 |
| PVE 配置 | `pve_servers` 表（多服务器）或 `config` 表（旧版单服务器） |
| OpenWrt 配置 | `config` 表 |
| 数据库模式 | `.db_config.json` 指定 `sqlite` 或 `postgresql` |

集群编号由数据库自增 ID 自动分配。旧版 `.k8s_clusters.json`、`.pve_config.json`、`.openwrt_config.json` 文件会在启动时自动迁移到数据库。

## 依赖

- Python 3.8+
- flask, proxmoxer, paramiko, sqlalchemy, cryptography, pg8000

详见 `requirements.txt`

## 注意事项

- PVE 模板必须已有 `qemu-guest-agent` 并启用
- `k8s` 用户需要在模板中配置 passwordless sudo
- client VM 第一次启动后需重启才能刷新 DHCP 主机名
- K8s 部署从 `http://10.11.43.82/download/` 下载离线安装包，需内部网络可达
- 生产环境必须设置环境变量 `FLASK_SECRET_KEY`，否则使用不安全的默认值
- OpenWrt VLAN 设备名含小数点，需通过 UCI 索引方式操作
