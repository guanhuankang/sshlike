# hpcsh（共享盘离线 HPC 终端）

通过共享磁盘上的目录与文件，在**无网络互通**的登录节点与计算节点之间实现类 SSH 的交互终端：

- 登录节点跑客户端，把键盘输入写入会话目录
- 计算节点跑服务端，用 PTY 起 shell 并把输出写回
- 仅依赖 NFS / Lustre 等共享存储

## 命令一览

**共享根目录**：若设置了环境变量 **`HPCSH_ROOT`**，则所有命令使用该路径；**未设置时**默认为执行命令时的**当前工作目录**（`hpcsh login` 与 `hpcsh server` 必须在同一共享路径下工作，双方应用相同的默认或显式设置 `HPCSH_ROOT`）。

| 命令 | 说明 |
|------|------|
| `hpcsh ls` | 列出共享根下各**节点目录**名（一般与计算节点名一致） |
| `hpcsh sessions <node>` | 列出该节点下已有**会话**，表头为 SESSION_ID / STATE / USER / CREATED，便于复制 id 重连 |
| `hpcsh server --node <node>` | 在**计算节点**上常驻监听，处理 `session_*` 目录（需 Linux，`pty`） |
| `hpcsh login <node>` | 在**登录节点**上连接该节点，进入交互 shell |
| `hpcsh exec <script> --node <node>` | 在**登录节点**上连接该节点，执行本地脚本后自动退出 |

查看帮助：`hpcsh -h`，各子命令：`hpcsh login -h` 等。

## 环境与安装

- Python **3.9+**
- **`HPCSH_ROOT`**（可选）：显式指定共享根；不设则使用**启动命令时的当前目录**。跨机场景建议在双方 `export HPCSH_ROOT=/share/hpc_remote`，避免依赖各自 cwd。

**推荐：pip 安装（会安装 `hpcsh` 到当前环境的 scripts 目录）**

```bash
cd /path/to/sshlike
pip install .
# 本地开发可写：pip install -e .
hpcsh -h
```

若终端提示找不到 `hpcsh`，把 pip 的 bin 目录加入 `PATH`（例如 `python3 -m site --user-base` 下的 `bin`），或使用下面的直接调用方式。

**不安装包时**，在项目目录中可直接调用 CLI 模块：

```bash
cd /share/hpc_remote   # 或先 export HPCSH_ROOT=...
python3 hpcsh_cli.py -h
python3 hpcsh_cli.py ls
python3 hpcsh_cli.py login node01
```

（也可直接运行 `hpcsh_client.py` / `hpcsh_server.py` 并传 `--node`；未设置 `HPCSH_ROOT` 时与 CLI 相同，默认 cwd。）

## 典型使用流程

**1. 计算节点**启动服务端（每个需跑 shell 的节点各启一个，`<node>` 与目录名一致）。在共享根对应目录下执行，或设置 `HPCSH_ROOT`：

```bash
cd /share/hpc_remote && hpcsh server --node node01
# 或：export HPCSH_ROOT=/share/hpc_remote && hpcsh server --node node01
```

服务端监听：`<共享根>/node01/session_*`。

**2. 登录节点**连接并操作（与上一步使用**同一共享根**：同样 `cd` 到该目录或设置相同的 `HPCSH_ROOT`）：

```bash
cd /share/hpc_remote && hpcsh login node01
```

**3. 查看节点与会话（重连）**

```bash
cd /share/hpc_remote   # 与 login/server 一致
hpcsh ls
hpcsh sessions node01
hpcsh login node01 --session <SESSION_ID>
hpcsh exec ./job.sh --node node01
```

新建会话时，id 为 **8 位小写字母与数字**；旧目录若仍是长 id，也可照常用于 `--session`。

进入远端 shell 后可运行 `hostname`、`nvidia-smi`、`top` 等；输入 `exit` 退出远端 shell。

## `login` 可选参数

| 参数 | 含义 |
|------|------|
| `--session <id>` | 接入已有会话（id 见 `hpcsh sessions <node>`） |
| `--no-cleanup` | 客户端退出时不删除会话目录，便于排障或再次 `--session` 重连 |

## `exec` 用法

```bash
hpcsh exec ./script.sh --node node01
```

- `script` 是登录节点上的本地脚本文件路径（按 UTF-8 读取）。
- 会自动把脚本内容发送到远端 shell 执行，并追加 `exit` 在脚本结束后退出会话。
- 可选参数与 `login` 一致：`--session <id>`、`--no-cleanup`。

## 目录协议

共享根示例：`/share/hpc_remote`

- 节点目录：`/share/hpc_remote/node01/`
- 会话目录：`/share/hpc_remote/node01/session_<id>/`（`<id>` 为新会话时的 8 位短 id，或与历史数据一致的长 id）

会话目录内文件：

- `meta.json`：会话元信息（用户、创建时间等）
- `state.json`：状态与心跳
- `in.log`：客户端事件（stdin / resize / signal / heartbeat）
- `out.log`：服务端输出（stdout / stderr / exit）
- `event.log`：诊断事件
- `lock`：服务端会话锁
- `pid`：远端 shell 进程号

## 设计要点

- 通信为 append-only NDJSON，客户端与服务端轮询增量偏移
- PTY 保证 `top` / `vim` 等交互程序可用
- 多会话以 `session_<id>` 隔离；服务端用 `lock` 避免同一会话重复 attach
