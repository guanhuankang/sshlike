# hpcsh (shared-disk offline HPC terminal)

通过共享磁盘目录实现类 SSH 交互终端：

- 登录节点运行客户端，发送键盘输入到会话目录
- 计算节点运行服务端，使用 PTY 执行 shell 并回传输出
- 全程无网络互通，仅依赖共享存储（NFS/Lustre 等）

## 目录协议

共享根目录示例：`/share/hpc_remote`

节点目录：`/share/hpc_remote/node01/`

会话目录：`/share/hpc_remote/node01/session_<sid>/`

会话目录内文件：

- `meta.json`：会话元信息
- `state.json`：状态与心跳
- `in.log`：客户端输入事件（stdin/resize/signal/heartbeat）
- `out.log`：服务端输出事件（stdout/exit）
- `event.log`：诊断事件
- `lock`：服务端会话锁
- `pid`：远端 shell pid

## 使用方式

> 需要 Python 3.9+，服务端应运行在 Linux 计算节点（依赖 `pty`）。

### 0) 入口命令

在项目目录内可直接使用统一入口：

- `./hpcsh`

可选：加入 PATH，直接像 `ssh` 一样调用：

```bash
chmod +x hpcsh hpcsh_client.py hpcsh_server.py
sudo ln -sf "$(pwd)/hpcsh" /usr/local/bin/hpcsh
```

### 1) 在计算节点启动服务端

```bash
./hpcsh server node01 --root /share/hpc_remote
```

服务端会持续监听：`/share/hpc_remote/node01/session_*`

### 2) 在登录节点启动客户端

```bash
./hpcsh node01 --root /share/hpc_remote
```

进入交互后可直接运行命令，例如：

- `hostname`
- `nvidia-smi`
- `top`

输入 `exit` 退出远端 shell。

## 可选参数

客户端：

- `--session <sid>`：复用既有会话
- `--no-cleanup`：退出时不删除会话目录（便于排障/重连）

服务端：

- `hpcsh server <node> --root <dir>`

## 设计要点

- 通信协议采用 append-only NDJSON，轮询读取增量偏移
- 使用 PTY 保证交互程序兼容（`top/vim` 等）
- 多会话通过 `session_<sid>` 隔离，服务端通过 `lock` 防重入
