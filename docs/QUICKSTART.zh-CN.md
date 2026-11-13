# 快速上手（分步）

> 本文是**分步说明**，不是可一键运行的脚本。所有命令都是示例，含 `D:/Projects/...`、`TARGET_*`、`<...>` 等**占位符**，原样复制不可执行；必须先在目标项目按 `docs/MIGRATION.zh-CN.md` 第 1 节填定真实变量表并逐一替换。
>
> 前置：你已取得**目标项目**的明确用户授权；两端指向同一个真实物理目录；两个会话 UUID 来自宿主实际回执；Windows + WorkBuddy 桌面版（基线 5.9.3）+ Python 3.12+ + Node 就绪。本仓库只提供对照源码与模板，不替你判断目标环境。

## 步骤 0：读契约、定变量

先完整读 `docs/MIGRATION.zh-CN.md`，尤其是第 0 节源码入口、第 1 节变量表、第 6 节**必须保留的运行契约**、第 9 节验收矩阵。把 `examples/项目变量表.template.json` 复制到**目标项目自己的部署记录**中填写，不要提交回公开仓库。

| 占位符 | 含义 | 示例（虚构，须替换） |
|---|---|---|
| `TARGET_PROJECT_ABSOLUTE_ROOT` | 两端同一个物理目录 | `D:/Projects/Project1` |
| `TARGET_PROJECT_ID` / `TARGET_SLUG` | 项目唯一逻辑标识 | `project1` |
| `ACTUAL_TARGET_CODEX_SESSION_UUID` | 目标 Codex 真实会话 | `<从宿主回执取得>` |
| `ACTUAL_TARGET_WORKBUDDY_SESSION_UUID` | 目标 WorkBuddy 原聊天真实会话 | `<从宿主回执取得>` |
| `TARGET_PYTHON_ABSOLUTE_PATH` | 目标项目自己的解释器 | `D:/Projects/Project1/.venv-bridge/Scripts/python.exe` |
| `SIGNED_WORKBUDDY_EXE_ABSOLUTE_PATH` | 官方安装 exe | `<在目标机器核签名后填写>` |
| `REVALIDATED_INSTALLATION_SHA256` | 该 exe 在目标机器的实际 Hash | `<在目标机器重新计算>` |

**不要**照抄任何文档里的会话 ID、不要随机生成 UUID、不要用来源项目的安装 Hash 冒充已核实。

## 步骤 1：旧项目先盘点、备份、隔离（不可跳过）

若目标项目已有旧的协作尝试，先按 `examples/旧通道清理盘点.template.csv` 逐条登记：原文件/配置条目、实际引用与调用方、进程端口归属证据、仍使用/废弃/待确认、原 SHA256、备份路径、隔离路径、回滚方法、验证结果。

1. 核目标目录、现有规则、Git 状态（无 Git 则逐文件 Hash）、两端会话与进程；遵守目标项目已有的 `sync/ack/begin/finish`，不借来源会话 ID 冒用。
2. 核旧任务是否仍在运行、是否有租约或 `unknown`。**运行中不覆盖协作程序**；锁由实际 owner 收尾，`unknown` 不重发，不按超时抢锁。
3. 备份旧 `setup/collaboration`、项目 MCP、两端 Hooks 与规则、`desktop.json`。**状态库用 `sqlite3.Connection.backup` 做一致性快照**，不能在活跃 WAL 时只复制 `state.sqlite` 单个文件。备份存目标项目隔离目录并记录 Hash，不进入公开仓库。
4. 对每个文件判定「仍使用 / 已废弃 / 待确认」。**隔离优先于删除**；名称含 ACP、旧日期、test 不代表无用。尤其不能批量删除 `.collab/`、`.work/`、`.codex/`、`.codebuddy/`、全局技能、用户聊天与未知投递记录。
5. 清理 Hooks 时解码 `EncodedCommand`，只移除指向**已确认退役**入口的具体 handler，保留同 row 其他 handler 与全部业务 Hook；新 `install.py` 只按精确 command 去重，不会替你识别语义相同但命令不同的旧 Hook。

> **绝不直接覆盖 `state.sqlite`**：新项目无库才 `init`；有库先走 `MIGRATION.zh-CN.md` 第 7 节兼容检查（比对 `meta` / `files` / `events` / `cursors` / `reports` / `lease` / 连接表），保留 `meta.project_id`、事件 ID、游标、历史授权与 `unknown` 记录。不能删库重建来「统一名字」，也不能在已有真实投递后用旧备份覆盖新事件。

## 步骤 2：复制参考源码并核 Hash

以本仓库 `reference/setup/collaboration/` 与根目录 `SOURCE_MANIFEST.json` 为**唯一源码入口**（不依赖任何未随仓库分发的旧 ZIP）。按原布局复制到目标项目：

```text
D:/Projects/Project1/setup/collaboration/
  acp_mcp.py  desktop_bridge.py  desktop_session.js  install.py  project_sync.py
  workbuddy_client.py  workbuddy_desktop.py  workbuddy_local.py
  test_*.py  test_desktop_session.js
  workbuddy_acp_bridge/  __init__.py server.py project.py gateway.py acp_client.py
                         requirements.txt UPSTREAM.md
```

复制后逐文件核对 `SOURCE_MANIFEST.json` 中的 SHA256；Hash 不一致先做差异归类，不把不同版本拼成「同一个已验收版本」。**不复制** `.collab/`、虚拟环境、运行日志、安装 Hash、真实会话 ID、`verified`、冻结 protocol 与任务产物。来源登记（`UPSTREAM.md`）与随附许可资料要保留，见 `THIRD_PARTY_NOTICES.md`。

## 步骤 3：按迁移契约做项目适配

按 `MIGRATION.zh-CN.md` 第 4 节逐文件修改（旧值 → 新值、锚点、所有关联调用点、验证）。要点：

- `project.py` 的 `PROJECT_ID` 与 `.collab/<项目>` 兼容目录；`server.py` 的服务器名 / 描述 / `WB_PROJECT_ID` 校验；`project_sync.py` 的兼容路径、MCP 豁免前缀 `mcp__<别名>__workbuddy_*`、以及 `TOP_FILES` / `TREES` / `EXTENSIONS` / `EXCLUDED` **必须按目标业务真实源码范围调整**，否则源码 Hash 会漏掉业务文件。
- `desktop_session.js` 的 `globalThis` key 与 `workbuddy_desktop.py::DesktopConnection.call` 的同名 key **成对同步修改**；JS `start` 与 Python `_submit` 的事件前缀同时改；测试中的引用同步。否则同一客户端里第二个项目会 `binding_changed`。
- `install.py` 与 `set_policy` 的解释器路径改为目标项目自己的 Python；`ROOT.parents` 层级保持。`project_sync.py::ROOT` 保持 `parents[2]`，`server.py` 保持 `parents[3]`，不要改成接受任意传入根。
- 旧 ACP / native 模块仍有真实 import，**不能凭文件名删除**；`workbuddy_local.py` 的兼容路径按契约同步。
- 保留 `dispatch` 累计次数、事件归属、源码 Hash、`sending`/`unknown`、`reply_to` 链、`same_call` 返回分支，以及第 6 节全部时间单位与结果收集硬条件。

## 步骤 4：生成配置（合并，不覆盖）

由 `examples/MCP.template.toml` 与 `examples/desktop.template.json` 生成目标项目自己的 `.codex/config.toml`（仅合并该项目专属表，保留其他 MCP / 业务配置）与 `.collab/desktop.json`（**新建**：填项目 root、`project_id`、两个真实会话、`native_workbuddy_session`、安装 Hash、回环端口、`desktop_cdp`；`verified` 初始 `false`）。不要复制来源项目的会话、Hash、事件或 `verified` 历史。

此步骤只填写并核对配置，尚不执行绑定。数据库初始化或兼容检查、客户端就绪后，才在目标 Codex 持有编辑租约时完成原聊天绑定（核 metadata 与默认权限，不创建聊天、不改权限）。填完并核实全部字段后才置 `enabled=true`。项目规则、AGENTS 与实际加载的规则保持一致，不导入来源项目业务或历史授权。

## 步骤 5：环境与依赖

复用目标项目已存在且兼容的 Python 环境，或新建一套；参考依赖见根目录 `requirements-reference.txt`（`mcp==2.3.0`、`httpx==0.28.1`、`websockets==16.1.1`），**不要为桥接覆盖目标业务依赖**。用 `python -X utf8` 运行协作脚本。

```text
# 示例（占位符，替换后才可执行）
D:/Projects/Project1/.venv-bridge/Scripts/python.exe -X utf8 -m compileall -q D:/Projects/Project1/setup/collaboration
```

## 步骤 5.5：启动客户端

新使用者先按[共享客户端启动说明](CLIENT_STARTUP.zh-CN.md)建立本机入口；已准备好的机器直接复用。原生客户端及回环端点就绪后，再做项目绑定与派单。不要假定读者机器已经装有作者的统一启动器。

## 步骤 5.6：先初始化新库，已有库只做兼容检查

先核目标项目是否已有 `.collab/state.sqlite`。**仅当该文件不存在**时，使用选定的目标项目解释器初始化：

```text
# 示例路径须替换；仅用于没有协作库的新项目
D:/Projects/Project1/.venv-bridge/Scripts/python.exe -X utf8 D:/Projects/Project1/setup/collaboration/project_sync.py init
```

核输出为 `initialized`，绑定根目录正确。已有协作库**不运行此命令**，按迁移指南第7节核表结构、原根目录、事件、未知投递与预算兼容，保留数据。不能删库重建。

完成本步骤后再安装Hooks。否则Hook首次执行sync/transaction就可能因尚未初始化而拦截后续工具。原聊天绑定也在初始化后按步骤7执行。

## 步骤 6：安装 Hooks 并经宿主真实信任

```text
# 示例（占位符，替换后才可执行）
D:/Projects/Project1/.venv-bridge/Scripts/python.exe -X utf8 D:/Projects/Project1/setup/collaboration/install.py
```

安装器只写项目配置，**不替用户信任 Hooks**；宿主须分别审核并真实加载。核编码命令内只含目标路径，保留原有业务 Hook。`configured` 不等于通道已接通。目标项目当前 Hook 的编码命令**不能从其他项目复制**，必须由目标本地重新生成。

## 步骤 7：握手 → 真实往返 → 独立验收

```text
# 示例：两端各用自己的 agent / 真实 session；占位符必须替换，前台执行
D:/Projects/Project1/.venv-bridge/Scripts/python.exe -X utf8 D:/Projects/Project1/setup/collaboration/project_sync.py sync --agent workbuddy --session <ACTUAL_TARGET_WORKBUDDY_SESSION_UUID>
D:/Projects/Project1/.venv-bridge/Scripts/python.exe -X utf8 D:/Projects/Project1/setup/collaboration/project_sync.py ack  --agent workbuddy --session <ACTUAL_TARGET_WORKBUDDY_SESSION_UUID> --token <本页 ack_token>
```

`sync` 返回 `more=true` 时**必须**先 `ack` 本页 token 再 `sync`，直到 `more=false`。随后 `receive`（用本轮实际通知正文）→ `begin` → 限定实施 → 先写好 `finish` / `result` 记录 → `finish` 释放锁 → `message` 提交唯一 `reply_to` 关联结果 → 停止。`same_call` 不额外 `dispatch`。

验收顺序见 `MIGRATION.zh-CN.md` 第 8–9 节：先只读握手（如 `task.bridge-probe.*`），再在隔离目录做一次小函数 + 测试的真实局部修改，由 Codex 独立重跑测试并核原生请求终态、会话 / 项目、唯一关联结果与锁释放。`verified=false` 阶段普通业务写入被门禁拒绝是预期，不能手填 `true` 绕开。

## 步骤 8：刷新 / 重启 / 隔离与失败处理

按项目逐个验证刷新、正常退出与重启后同一原聊天的续接、上下文复用与目录隔离；确认无其他项目正在运行任务并有授权后再重启客户端。失败按 `docs/TROUBLESHOOTING.zh-CN.md` 与 `MIGRATION.zh-CN.md` 第 10 节处理：回滚只针对已审核的局部范围，不覆盖新事件；`unknown` 永久保留、不重发；任何一步都不宣称「配置改完即可并行」——**多项目同 composer 并发未实现、未验收**。

最后一步应由用户在目标项目原聊天亲自完成一次真实任务，才算目标端验收；来源对话不代替。
