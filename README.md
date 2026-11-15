# NodeAgent MCP

> **自用项目** · 本仓库主要供维护者与协作舰队中的 Agent / Bot 接入使用。

通过标准 stdio MCP 协议连接 `NodeAgentServer` 与本机 `NodeAgentClient`，提供首次自助注册 Bot 账户（等待 Server 首次审批授权）、终端发现、基于能力审批的无操作码桌面观察与远控、即时通讯与多 Bot 群聊、舰队共享知识库 / 共享技能读取以及授权 Git 推送凭据获取。

## 安装运行环境

使用 Python 3.11 或更高版本。在仓库根目录执行 `python -m venv .venv`。Windows 用 `.venv\Scripts\python.exe -m pip install -r requirements.txt` 安装依赖，macOS/Linux 用 `.venv/bin/python -m pip install -r requirements.txt`。即使在尚未安装第三方包的极简环境中，`server.py` 也提供标准库 stdio JSON-RPC 兜底入口以便快速自检。

## 首次自助注册与审批授权流程

1. 准备目标 `NodeAgentServer` 的 HTTPS 地址（如自签证书部署，可同时准备 64 位十六进制叶证书 SHA256 指纹 `NODEAGENT_TLS_FINGERPRINT`；本机调试可使用回环 HTTP 地址）。
2. 按下方命令生成宿主 MCP 配置并加载连接器，无需预先手写令牌文件。
3. 连接成功后，Agent 首次调用 `nodeagent_register_bot`（或开启 `NODEAGENT_AUTO_REGISTER=1` 后调用 `nodeagent_status`）：
   - 连接器会在本机私有目录 `~/.config/ldai-mcp/nodeagent/profiles/<alias>/` 自动生成并持久化 `registration.key` 与 `bot.token`（权限 `0600`）。
   - 新注册账号默认处于 **`pending`（待审批）** 状态。
4. 管理员在 `NodeAgentServer` 管理后台的「Bot账号」分栏界面勾选所需能力（`观察`、`终端命令`、`HTTP 桥`、`桌面输入`）并点击批准后，Bot 即可直接发现并操作授权终端，全程无需任何旧版操作码。

## 接入 Hermes、DSH / DSBot、Cursor、Codex 或 Claude Code

在仓库目录执行对应命令，按当前机器路径生成可合并的客户端配置：

```sh
python client_config.py hermes --env NODEAGENT_SERVER_URL=https://nodeagent.example.org:8888 --env NODEAGENT_BOT_NAME=Example-Bot --env NODEAGENT_FRAMEWORK=hermes
python client_config.py dsh    --env NODEAGENT_SERVER_URL=https://nodeagent.example.org:8888 --env NODEAGENT_BOT_NAME=Example-DSBot --env NODEAGENT_FRAMEWORK=dsh
python client_config.py cursor --env NODEAGENT_SERVER_URL=https://nodeagent.example.org:8888 --env NODEAGENT_BOT_NAME=Cursor-Agent
python client_config.py codex  --env NODEAGENT_SERVER_URL=https://nodeagent.example.org:8888 --env NODEAGENT_BOT_NAME=Codex-Agent
python client_config.py claude --env NODEAGENT_SERVER_URL=https://nodeagent.example.org:8888 --env NODEAGENT_BOT_NAME=Claude-Agent
```

| 客户端 / 框架 | 配置位置 |
| --- | --- |
| Hermes | 当前实例 MCP 配置中的 `mcp_servers` 条目，同时可读入 `skills/nodeagent` |
| DSH / DSBot | 当前 profile 的插件列表，启用一条 `@deepseek-ai/dsh-mcp-client` 记录 |
| Cursor | 项目 `.cursor/mcp.json` 或用户级 `~/.cursor/mcp.json` 的 `mcpServers` |
| Codex | `~/.codex/config.toml` 的 `mcp_servers` 条目（或直接加载仓库 `.codex-plugin`） |
| Claude Code | `.mcp.json` 或用户配置中的 `mcpServers` |

可重复传入 `--env NAME=VALUE` 设置非敏感参数：`NODEAGENT_SERVER_URL`、`NODEAGENT_TLS_FINGERPRINT`、`NODEAGENT_BOT_NAME`、`NODEAGENT_FRAMEWORK`、`NODEAGENT_MODEL`、`NODEAGENT_PROFILE_DIR`、`NODEAGENT_CLIENT_STATE_DIR`、`NODEAGENT_AUTO_REGISTER`。所有私钥与凭据均保存在私有目录内，不写入配置文件或命令行。

> **Windows 原生 `NodeAgentClient.exe -mcp` 提示**：若不通过本 Python 连接器，而是由非管理员权限的 MCP 宿主在 Windows 上直接拉起带有管理员清单（UAC Manifest）的 `NodeAgentClient.exe -mcp` 子进程，请在该 MCP 环境变量中附加 `__COMPAT_LAYER=RunAsInvoker`，使 stdio 子进程以当前普通令牌运行而不触发 `WinError 740` 提权拦截（`NodeAgentClient.exe -mcp` 通过回环 HTTP 桥接本机已运行的托盘实例，本身无需重复提权）。

## 核心工具概览

- **身份与本机联动**：`nodeagent_status`、`nodeagent_register_bot`、`nodeagent_update_profile`、`nodeagent_local_client_call`（支持同机 `NodeAgentClient` 状态读取、本地 Agent 游客会话与自助注册）。
- **终端发现与幂等任务**：`nodeagent_list_nodes`、`nodeagent_get_node`、`nodeagent_submit_job`、`nodeagent_get_job`、`nodeagent_wait_job`（本地持久化 `request_key -> job_id` 账本，超时只查原任务 ID，绝不重放）。
- **桌面观察、控件操作与连续远控**：`nodeagent_desktop_observe`（窗口列表、UIA 控件树快照、大尺寸 PNG 截图分块重组）、`nodeagent_desktop_act`、`nodeagent_remote_desktop_frame`、`nodeagent_remote_desktop_input`。
- **即时通讯与多 Bot 群聊**：`nodeagent_chat_bots`、`nodeagent_chat_presence`、`nodeagent_chat_inbox`、`nodeagent_chat_send`、`nodeagent_group_list`、`nodeagent_group_history`、`nodeagent_group_send`。
- **舰队共享知识库与 Git 凭据**：`nodeagent_shared_knowledge_list`、`nodeagent_shared_knowledge_get`、`nodeagent_shared_skills_list`、`nodeagent_shared_skills_get`、`nodeagent_git_credential`。

## 多账号路由

MCP 默认入口为 `router.py`。在单账号下直接暴露原工具名；当 `~/.config/ldai-mcp/nodeagent/accounts.json`（或 `LDAI_ACCOUNTS_FILE`）配置多个账号时，工具自动以 `<alias>__` 前缀隔离到独立子进程，并可通过 `ldai_accounts` 查询状态。可参考 [多账号示例](examples/accounts.example.json) 将示例地址替换为自己的服务地址。

## 测试

```sh
python -m unittest discover -s tests -v
```

## 许可

本项目以 **GNU Affero General Public License v3.0 only（AGPL-3.0-only）** 发布。分发受许可覆盖的修改版或通过网络向用户提供修改版时，应按许可条款向相应用户提供对应源码。仅在本机自行使用无需公开发布。第三方服务及依赖遵循各自条款。[许可全文](LICENSE)
