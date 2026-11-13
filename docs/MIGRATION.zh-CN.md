# Codex—WorkBuddy桌面协作：逐文件迁移实施方案 V2

日期：2026-11-12 编写及源码核对：Codex；2026-11-13 由 WorkBuddy 整理为公开仓库版，**仅修订本页开头与第 0 节的源码入口说明**，第 1 节之后的变量表、逐文件修改、运行契约、验收矩阵与失败处理保持原文不变。

本版替代旧《协作通道跨项目迁移说明》的实施指导；不覆盖原包，不改写既有验收记录。目标是移植现有桌面链路，不重新设计一个桥接框架。本文不表示目标项目已经部署或验收。本文描述迁移方式，不代表读者的目标项目已经部署。

> **公开仓库版源码入口（2026-11-13 修订，请先读）**
>
> 本仓库随附 `reference/setup/collaboration/` 冻结脱敏参考源码，根目录 `SOURCE_MANIFEST.json` 记录基线标识 `desktop-visible-20261112` 及全部 **27 个条目的逐文件 SHA256**（23 个源码/测试/来源登记文件 + 4 个 `examples/` 模板）。**迁移以这两者为唯一源码入口**；不依赖任何未随本仓库分发的旧 ZIP 或配套锚点 JSON。
>
> 因此：下文凡出现「基线包」「参考包」「基线参考包」「`reference-source/setup/collaboration/`」字样，**一律指本仓库的 `reference/setup/collaboration/`**。`SOURCE_MANIFEST.json` 中的 `baseline_zip_sha256` 只是历史溯源字段，对应归档**未随仓库分发，不得作为部署输入或版本证明**；同理，`迁移源码锚点与Hash_V2_20261112.json` 及包内 SHA256SUMS / MANIFEST 也不在仓库内，锚点与行号由目标项目在自己的副本中按第 4 节逐项检索核实。
>
> 逐文件 Hash 以 `SOURCE_MANIFEST.json` 为准；本文引用 `SOURCE_MANIFEST.json` 之外的任何外部归档都不属于本仓库的交付物。

## 0. 先确定版本、责任和交付物

基线为本仓库 `reference/setup/collaboration/` 冻结脱敏参考源码，版本依据为根目录 `SOURCE_MANIFEST.json`：

| 字段 | 含义 |
|---|---|
| `baseline` | `desktop-visible-20261112`，基线标识 |
| `source_files` | 逐文件相对路径与 SHA256，复制前后均须逐项核对 |
| `note` | 仅含参考源码与假 UUID 夹具，不含真实项目配置、聊天、凭据或验证状态 |
| `baseline_zip_sha256` | 仅历史溯源；对应归档未分发，不是源码入口 |

该目录是**源码对照**，不是项目运行配置；`.collab/`、虚拟环境、运行日志、安装 Hash、真实会话 ID、`verified`、冻结 protocol 与任务产物均不在其中。复制前逐文件核对 `SOURCE_MANIFEST.json` 的 Hash；不能以 Git HEAD 作为全部版本证明，因为原工程存在未提交文件。行号只便于定位，修改以函数和锚点为准；源码 Hash 与清单不一致时先做差异归类，不把不同版本拼接成“同一个已验收版本”。

执行者在目标项目操作；Codex负责验收。必须先取得该目标项目的明确授权。只获来源包读取权，不等于能改来源项目。目标项目不从来源路径import，不共享SQLite、任务状态或聊天；客户端统一启动器可以共用。

完成时至少交付：目标变量表、修改文件及前后Hash、旧入口盘点和隔离清单、配置备份、测试日志、实际派单/原生请求/关联结果证据、剩余风险。不能只交“配置成功”。

## 1. 一次填定的变量表

下面是**示例**，不是对某个实际目录的部署授权。替换后把表保存在目标项目自己的部署记录里。

| 变量 | 示例值 | 取值规则 |
|---|---|---|
| TARGET_ROOT | `D:\Projects\example-project` | 两边实际同一个物理目录；拒绝符号链接/junction越界，不靠窗口名称判断 |
| PROJECT_ID | `example-project` | 项目唯一逻辑标识；不使用来源的video-edit |
| MCP_ALIAS | `workbuddy_example_project` | 项目唯一，字母/数字/下划线；不带连字符 |
| ADAPTER_KEY | `__exampleProjectVisibleBridgeV3` | JS合法点访问标识，项目唯一；JS、Python与测试一致 |
| REQUEST_PREFIX | `example-project-event-` | JS与Python同时修改，保留event整数校验 |
| PY_REL | `.venv-bridge/Scripts/python.exe` | 可选已存在且兼容的目标项目环境，不必再创建一套；不能指到来源虚拟环境 |
| PY_ABS | `D:/Projects/example-project/.venv-bridge/Scripts/python.exe` | 所有helper与配置使用同一个解释器 |
| CODEX_SESSION | `<真实目标Codex会话UUID>` | 从目标宿主实际提供的ID核实，不照抄本对话ID、不随机生成 |
| WB_SESSION | `<真实目标WorkBuddy聊天UUID>` | 对指定原聊天做精确metadata核对，不创建同UUID空聊天、不查全局聊天列表猜测 |
| WORKBUDDY_EXE | `<已核实官方安装绝对路径>` | 核安装身份；不是项目副本 |
| EXE_SHA256 | `<该安装exe实际SHA256>` | 在目标机器重新计算，不能复制来源Hash冒充已核实 |
| CDP_PORT | `9333`（同机现有统一启动器示例） | 与已授权公共启动器一致；多项目不是各开一个客户端/端口 |

注意有两种project_id：`project.py::PROJECT_ID`及配置是逻辑标识；`state.sqlite.meta.project_id`是初始化生成的库UUID。**不要把库UUID改成上表字符串，也不要重建库来统一名字。**

默认环境：Windows、WorkBuddy Desktop 5.9.3对应SDK；Python 3.12及以上（使用Path.is_junction）；Node用于JS行为测试。其他客户端版本需重新验证SDK，不直接继承结论。基线验证依赖为mcp 2.3.0、httpx 0.28.1、websockets 16.1.1；复用目标环境前检查兼容性，不能为了桥接覆盖业务依赖。

## 2. 清理旧尝试：先停写、备份、辨认引用，后替换

1. 核目标目录、AGENTS、当前Git状态（无Git则逐文件Hash）、原两端会话与进程。遵守目标项目现有sync/ack/begin/finish；不得借来源会话ID冒用目标Codex。
2. 核旧任务是否仍在运行、是否有租约或unknown。运行中不覆盖协作程序；锁由实际owner收尾，未知不重发，不按超时抢锁。
3. 备份旧 `setup/collaboration`、项目MCP、两端Hooks与规则、desktop.json；SQLite用sqlite3.Connection.backup做一致性快照，不能在活跃WAL时仅复制state.sqlite一个文件。备份存目标项目隔离目录，限制访问，不进入迁移包。记录每个备份Hash。
4. 对文件列出：当前入口、import/调用方、配置引用、进程是否仍使用、保留/替换/待确认、备份与恢复位置。名称含ACP、旧日期、test不代表无用。
5. 将被新入口完全替代且确无调用方的实验入口隔离；仍有import、业务引用或未知关联的文件保留。尤其不能批量删除`.collab`、`.work`、`.codex`、`.codebuddy`、全局技能、用户聊天和未知投递记录。
6. 基线仍import `.acp_client`、`.gateway`、`workbuddy_local`等依赖，先保留。旧目标特有的治理/恢复模块，只有在所有调用与测试已处理后才隔离。**不因迁移删除业务技能、KB、服务端或客户连接器。**
7. 清理Hook时解码其EncodedCommand，仅移除调用“已确认退役的本目标项目协作入口”的具体handler；保留同row其他handler、其他业务Hook和所有非hooks配置。新install脚本只按精确command去重，**不会替你识别旧语义相同但命令不同的重复Hook**。必须先处理这项，否则可能双重领锁。

隔离优先于删除。未发生新任务提交前可以按备份恢复程序/配置；已经有真实投递后，不得恢复旧SQLite覆盖新事件、unknown与累计次数。数据库回滚与文件回滚必须分开。

## 3. 复制什么，不复制什么

基线参考包中23个源码/测试文件整体保留固定布局，再按下节修改。运行目录仍是：

```text
<TARGET_ROOT>/setup/collaboration/
  acp_mcp.py                 project_sync.py
  install.py                desktop_bridge.py
  desktop_session.js        workbuddy_desktop.py
  workbuddy_client.py       workbuddy_local.py
  test_*.py                 test_desktop_session.js
  workbuddy_acp_bridge/
    __init__.py server.py project.py gateway.py acp_client.py
    requirements.txt UPSTREAM.md
```

来源登记不等于完整许可证，随附许可资料也要保留。`.collab`、虚拟环境、运行日志、安装Hash、真实会话ID、verified、冻结protocol与任务产物一律不从来源复制。目标已有的运行记录保留在原地。

## 4. 逐文件修改：旧内容→新内容

### 4.1 `workbuddy_acp_bridge/project.py`

只做本节列明的命名适配，保留PermissionPolicy、原子写入与兼容依赖：

```python
# 旧
PROJECT_ID = 'video-edit'
self.folder = safe(self.root, self.root / '.collab/video-edit')
# 新（示例）
PROJECT_ID = 'example-project'
self.folder = safe(self.root, self.root / '.collab/example-project')
```

线程名`video-edit-acp-io`可改为`example-project-acp-io`，它只是诊断名称。逻辑project_id比较继续引用PROJECT_ID。桌面路线不调用pair/reset；修改兼容目录是防止未来误串名，**不迁移或创建隐藏ACP会话**。

### 4.2 `project_sync.py`：项目标识和MCP豁免

精确替换本文件中的兼容路径及相等比较：

```python
# 旧 → 新
'.collab/video-edit/session.json'  # → '.collab/example-project/session.json'
'.collab/video-edit'               # → '.collab/example-project'
s.get('project_id') != 'video-edit' # → != 'example-project'
state.get('project_id') == 'video-edit' # → == 'example-project'
```

`hook()`里五个显式豁免工具的前缀从`mcp__workbuddy_video_edit__`改为`mcp__workbuddy_example_project__`，后缀status/run/cancel/pair/reset不变。基线recover不在这五个豁免内，但这**不保证Hook自动领锁**：TOOL_PATTERN可能因旧别名里的edit偶然匹配，新别名未必匹配。目标必须显式执行`sync→逐页ack→begin→workbuddy_recover→finish`，并用新MCP别名测试。运行时recover的真实Codex租约硬条件保持，不能靠工具名称判断是否持锁；加入或不加入豁免也不代替该硬条件。迁移阶段保留五项豁免，不顺手改工具策略。

不得改dispatch的累计次数、事件归属、源码Hash、投递前二次核验、原子claim、sending/unknown、reply_to链或same_call返回分支。默认初次加两次局修；历史额外授权按原根留存，不把迁移作为清预算方法。

### 4.3 `project_sync.py`：源码范围

保留`ROOT=Path(__file__).resolve().parents[2]`，保持本方案目录布局。**不要改成接受任意任务传入的WB_PROJECT_ROOT。** server同样严格核固定根。

`TOP_FILES`按目标真实顶层规则/构建配置列举，并保留实际JS与测试文件的登记，例如：

```python
TOP_FILES = {'AGENTS.md', 'CODEBUDDY.md', 'README.md',
             'pyproject.toml', 'requirements.txt', '.gitignore',
             'setup/collaboration/desktop_session.js',
             'setup/collaboration/test_desktop_session.js'}
# 仅是普通Python项目示例；缺少的目录不会凭空创建。
TREES = ('setup', 'src', 'tests', 'docs', '.codex', '.codebuddy', '.agents')
```

实际项目若有personal_system、scripts、references、02_Skills、03_知识库、04_模板或其他语言业务目录，要按其真实入口加入；不能直接用示例替掉现有完整范围。原示例video-remix技能路径不迁移。目标语言的扩展名如`.js/.ts/.tsx/.vue/.cs/.go`按实际加入EXTENSIONS，不默认全盘收集。

`source_paths()`末尾的`materials/audiovisual/library.json`是原示例工程专属登记：无对应资产时**只删除该manifest赋值、存在检查、append三行**；目标若有确实参与执行的结构化索引，换成已审定的本项目相对路径。不要保留无业务依据的视频资产路径。

保留safe、拒绝链接、单文件4MiB/总64MiB/6000文件上限及扫描中变更检测。EXCLUDED增补目标虚拟环境、归档、成稿、私有状态、密钥目录名；不要通过扩大扫描读取凭据。目录名排除不等于任意新名字的密钥都受保护，需先审查白名单范围。

验收：修改目标真实业务源码、规则、desktop_session.js都应改变tree_hash；仅写排除目录中的缓存不改变；重复snapshot不应无故变化。测试后恢复测试内容并记录恢复Hash。

### 4.4 `desktop_session.js`与`workbuddy_desktop.py`：成对修改

```javascript
// desktop_session.js 旧
const key = '__videoEditVisibleBridgeV3', version = __ADAPTER_HASH__;
// 新
const key = '__exampleProjectVisibleBridgeV3', version = __ADAPTER_HASH__;
// start中的旧字符串 'video-edit-event-' → 'example-project-event-'
```

```python
# workbuddy_desktop.py::DesktopConnection.call 旧 → 新
'globalThis.__videoEditVisibleBridgeV3.'  # → 'globalThis.__exampleProjectVisibleBridgeV3.'
# _submit 旧 → 新
f'video-edit-event-{event}'              # → f'example-project-event-{event}'
# set_policy中helper解释器旧 → 新
self.root/'.work/开发环境/Scripts/python.exe'
# → self.root/'.venv-bridge/Scripts/python.exe'（必须等于PY_REL）
```

`set_policy`的脚本仍为`self.root/'setup/collaboration/project_sync.py'`。Python/JS的`__PROJECT_ROOT__`、`__DESKTOP_SESSION__`、`__ADAPTER_HASH__`占位机制保持；不把真实路径和ID写进JS文件。不要更改中文事件marker，因为notification、read、terminal和真实请求正文共同使用它。改变marker属于协议改造，不属于命名迁移。

### 4.5 `workbuddy_acp_bridge/server.py`

两处服务器名称与描述按项目改：

```python
server = Server('workbuddy_example_project', version='0.2.0',
                on_list_tools=_list_tools, on_call_tool=_call_tool)
# InitializationOptions的server_name也从workbuddy_video_edit改为workbuddy_example_project
# status工具描述中的video-edit only改为example-project only
```

保留固定`ROOT=parents[3]`、WB_PROJECT_ROOT与固定ROOT的相等校验、WB_PROJECT_ID与PROJECT_ID校验、WB_CODEX_SESSION与desktop.json原Codex绑定校验。工具本名仍是`workbuddy_status/run/cancel/recover/pair/reset`；宿主前缀由MCP_ALIAS决定，不将工具本名也加项目名。

保留`execute()`顺序：binding→参数白名单→set_policy→真实dispatch→读取本次result文件→附execution→`independently_accepted=False`。不迁移时顺手新增跨层状态模型，也不把execution存在当成独立验收。

### 4.6 `install.py`

```python
# 旧 → 新，选定后与set_policy和MCP一致
python = safe(ROOT, ROOT / '.work/开发环境/Scripts/python.exe')
# → python = safe(ROOT, ROOT / '.venv-bridge/Scripts/python.exe')
# 备份路径改为目标本次专用目录，避免沿用来源日期
backup = safe(ROOT, ROOT / '.work/collaboration_migration_v2/baseline')
```

ROOT仍parents[2]，脚本路径不变。EncodedCommand由目标本地重生成，不复制成品字符串；保留UTF8、UTF16LE编码与两端handler格式。安装前按§2处理旧重复Hook，备份核对后再运行。基线安装器实际入口只有`python setup/collaboration/install.py`，**不要照其他旧版本文档添加本基线不支持的--prepare或--install-hooks**。

installer只写项目配置，不替用户信任Hook；宿主实际加载与信任必须单独验收。其输出configured不是通道已接通。

### 4.7 测试及保留文件

在`test_project_sync.py/test_hook_e2e.py/test_project_acp.py/test_desktop_cdp.py/test_desktop_session.js`中，逐文件定位PROJECT_ID、MCP前缀、JS key、request前缀、Python相对路径和源码范围fixture，按上文同步修改；其他命中也须核对。保留假UUID，不改成目标真实聊天ID；不要删除旧负例或把拒绝改为放行。

`test_desktop_session.js`中的`context.__videoEditVisibleBridgeV3`改为`context.__exampleProjectVisibleBridgeV3`；所有`video-edit-event-7`改为`example-project-event-7`。D:/project、original等测试fixture不是部署路径，不必替换。

**另一个必须改的兼容路径**：`workbuddy_local.py::_project_runtime_status`中的`root / '.collab/video-edit/session.json'`改为`root / '.collab/example-project/session.json'`，与4.1/4.2同步；相关test_project_acp及test_workbuddy_local的fixture路径同步。它是实际worker读取位置，不是可以留着的诊断文字；只改此路径，不启动隐藏会话。

`acp_mcp.py`、`desktop_bridge.py`、`gateway.py`、`acp_client.py`、`workbuddy_client.py`和`__init__.py`原则上保留原逻辑；`workbuddy_local.py`除上述兼容路径外保留原逻辑。兼容报错中的video-edit可以作为原来源诊断文字明确登记，或仅改文字；不为消灭字符串引入新传输分支。不执行隐藏pair/reset、不新增CLI恢复、不接模型Key、不自动回退native_reply。

## 5. 配置文件：改成什么

### 5.1 `.codex/config.toml`（合并指定表，不覆盖其他配置）

```toml
[mcp_servers.workbuddy_example_project]
command = 'D:\Projects\example-project\.venv-bridge\Scripts\python.exe'
args = ['-X', 'utf8', 'D:\Projects\example-project\setup\collaboration\acp_mcp.py']
cwd = 'D:\Projects\example-project'
startup_timeout_sec = 20
tool_timeout_sec = 1000
supports_parallel_tool_calls = false
[mcp_servers.workbuddy_example_project.env]
WB_PROJECT_ID = 'example-project'
WB_PROJECT_ROOT = 'D:\Projects\example-project'
WB_CODEX_SESSION = '<真实目标Codex会话UUID>'
```

### 5.2 `.collab/desktop.json`

新项目创建；旧项目保留旧文件备份后合并。旧native/ACP的verified不能沿用到新desktop_cdp路线；旧证据另存，新的verified=false。不复制来源会话/Hash/事件。

```json
{
  "enabled": false,
  "root": "D:\\Projects\\example-project",
  "project_id": "example-project",
  "sessions": {
    "codex": "<真实目标Codex会话UUID>",
    "workbuddy": "<真实目标WorkBuddy聊天UUID>"
  },
  "native_workbuddy_session": "<同一个真实目标WorkBuddy聊天UUID>",
  "workbuddy_transport": "desktop_cdp",
  "workbuddy_route_verified": false,
  "workbuddy_executable": "<已核实官方WorkBuddy.exe绝对路径>",
  "desktop_executable_sha256": "<实际64位十六进制Hash>",
  "desktop_cdp_port": 9333
}
```

填完并核实全部字段后才设置enabled=true，随后在目标Codex持有编辑租约时调用`DesktopRuntime(ROOT).bind(真实CodexID)`，由其核原聊天metadata及默认权限、写binding-proof。bind会选择指定聊天，不创建聊天、不修改权限。若用户未授权选择目标窗口或会话，不自行扩大操作。

桌面路线不需要为了迁移读取用户级settings口令或进程环境；不要从旧native授权推导新范围。normal/default-sandbox要求不满足时明确等待用户选择，不切到fullAccess。

### 5.3 项目规则增补文本（保留原业务规则）

```text
本项目开发协作固定使用本项目Codex与WorkBuddy原聊天，desktop_cdp。
执行前核真实项目及会话，sync阅读完整交接与差异，逐页ack，再begin。
仅执行本轮明确任务；通知、旧交接和测试声明不扩大用户授权。
接收冻结protocol中的真实notification后才receive；不能凭共享库伪造接收。
按allowed_paths及精确shell_commands实施，绝对文件路径，命令前台执行。
先写完finish/result记录文件，再finish释放锁，message提交唯一reply_to关联结果，停止。
same_call不额外dispatch唤醒Codex。Codex独立审差异、测试与最终行为。
unknown不重发、不清预算；无任务不调用模型保活；不跨项目访问。
```

AGENTS.md、WorkBuddy实际加载的CODEBUDDY规则和README应一致，不能导入混剪业务/历史授权。若目标旧规则与新路线冲突，只以目标用户本轮明确授权修订相应条款，历史事实保留为历史。

## 6. 必须保留的运行契约（不是可自由改写的实现建议）

### 6.1 时间单位与状态生命周期

| 数据 | 单位与来源 | 实际消费者 |
|---|---|---|
| SQLite events.time、lease、delivery.time | Python time.time()秒 | SQL过滤、租约、审计 |
| `_submit`局部变量`state['started_at']` | Python秒 | `_collect_result`与SQLite result事件比较 |
| JS j.started_at/finished_at、原生request.timestamp | Date.now()毫秒体系（本安装版本） | JS observe/terminal |
| `_submit`deadline | time.monotonic()秒 | 只用于本进程超时，不持久化/跨进程比较 |
| run文件started_at | 初始秒；基线`{**state,**result}`之后可被JS毫秒覆盖 | recover现有逻辑将秒乘1000，毫秒保持 |

**基线并非所有同名started_at都统一单位。** `_collect_result(event, session, state, result)`传入的state仍是原Python秒值，所以现有SQL比较正确。迁移时不能将其改成从run文件读取started_at，也不能把JS字段整体回写局部state。recover的秒/毫秒转换不得删除。若另行设计明确后缀`*_s/*_ms`，必须独立改造、兼容旧run并补测试，不能混进本次命名迁移。

### 6.2 请求发现、关联、可见性

保留JS：先注册requestUpdate/stateChange监听再调用原生prepareInput；start只提交一次；未关联时`requests(0,1)`仅查看**已绑定聊天的最近一条候选**，再经同一observe核时间、正文事件marker与唯一request_id；已关联后按该ID查询。不是扫描全局历史，也不是见到最近completed就算本任务成功。

两个不同原生request匹配同一事件时置unknown，不能任选一个。刷新导致jobs丢失返回missing_no_resubmit，不能自动再次start。版本更新时已有active job拒绝替换adapter。

`document.body.innerText.includes(marker)`只设置prompt_visible，是展示证据，不参与身份或完成判定。基线只保存布尔，不导出整页文本。迁移保留现有行为；如果目标隐私规则禁止此读取，须单列变更与等效验收，不能把prompt_visible=false当作任务未执行，也不能放宽关联判定来补救。

### 6.3 结果收集与返回

正常顺序：原生本次请求终态→绑定原聊天就绪/idle→`_collect_result`→写result-ID.json→清本次current→dispatch返回→server附execution→Codex独立验收。

`_collect_result`保留以下硬条件：无编辑租约；原task属于绑定Codex；来自绑定WorkBuddy、reply_to为该task且to=codex的message恰好一条；事件时间不早于局部Python开始时间；key以result.开头。不能只靠文件存在、ACK或idle代替。

server确实按result文件存在附execution，但该文件在正常链路上已有上述受控生成条件；execution仍`independently_accepted=false`。保留这个生产者—消费者关系，不把server一行代码单独解释成“无需身份校验”。同样，原终态可能是error/cancelled，不等于业务完成；验收必须读取terminal、result正文、真实差异和测试。

禁止新增“四里程碑/collection_state/execution_status”等体系作为迁移前提，也禁止以删除现有收集约束换取绿色结果。若目标已有这些字段，不直接大范围删除：先找消费者，兼容字段可保留，影响基线行为的分支须逐项隔离并测试。

### 6.4 权限、锁、回报顺序

set_policy冻结目标event、session、源码Hash、allowed_paths/commands、期限和通知。任务不因模型提出请求而扩大权限。绝对路径、固定cd前缀、原生权限卡与任务拒绝门同时保留。权限卡可以等待用户；桥接不自动批准。

WorkBuddy必须先写finish/result两份note，再finish、message；否则finish后再Write会被Hook重新领锁，收集失败。same_call误dispatch只返回无需另唤醒，不进行第二次发送。

`receive`证明真实通知已到，ACK仅已读；不自行填receive补证。通知不超过4000字符；长任务计划放目标项目Hash绑定文件，summary只给路径、目标和保护项，不把全文堆进通知。

### 6.5 超时、断线、恢复

断线只重新附着观察，基线最多两次且核PID/进程创建时间；绝不重发start。刷新、超时或结果缺失保留unknown及current，客户端等待超时不等于远端停止。

cancel没有可靠的本任务专属终止接口时返回未确认，不擅自取消可能属于用户的新任务。recover要求绑定Codex租约、精确event/session、原生终态与idle，确认后仅记录terminal观察并清活动范围；不把旧unknown改成成功、不重派旧事件，也不承诺恢复原调用已超时丢失的完整回报。

## 7. 旧目标数据库与状态兼容

新项目无库才init；有库先检查meta.root、表名/列与实际方法调用。保留meta.project_id、events ID、cursors、reports、lease、deliveries、额外授权及未知记录。不能删除state.sqlite后重新init。

至少比对meta/files/events/cursors/reports/lease/connections；当前基线transaction会按需创建notifications/notification_offers/notification_rounds，dispatch按需创建desktop_deliveries。其他版本新增字段/表不因此删除。旧目标特殊预算与授权字段可能由旧代码消费，须列兼容表，不能覆盖project_sync后假定这些限制仍生效。

有不兼容项时先在目标隔离副本上验证增量迁移和记录数量/ID/预算保留，再对原库执行已审核事务。无法证明兼容则阻塞，而不是整库替换。迁移所需SQL不在未知目标上预先猜写。

## 8. 分阶段执行：明确入口与命令

下面命令由**目标项目对话内部执行**，不是要求普通用户手填协议。

1. 完成§1变量表、§2备份清理、§4源码适配；Python/依赖及Node可用。
2. 新库：目标Python执行`setup/collaboration/project_sync.py init`。已有库：走§7兼容检查，不init。
3. 整理重复Hook后，目标Python执行`setup/collaboration/install.py`；核编码内只有目标路径，保留业务Hook。宿主分别信任并实际触发，不能伪造connections记录。
4. 合并目标MCP与desktop配置，核真实会话。Codex刷新加载项目MCP；只打开窗口不等于工具已加载。
5. Codex本端sync/逐页ack/begin；按§5完成绑定；finish释放锁。status应核metadata.project_root/session_id、ready、permission_mode与PID；route_verified仍false。
6. 建真实`task.bridge-probe.<唯一键>`事件，summary简短。finish后调用目标`workbuddy_run(event_id, allowed_paths=[".collab/notes"], allowed_commands=[], timeout_seconds=300)`。不能用模拟传输或手工写结果文件代替真实往返。
7. WorkBuddy按本次冻结protocol的实际命令执行sync→逐页ack→receive→begin→写记录→finish→message→STOP。参数取本次真实token/event，不从文档抄固定数值。
8. 再建独立`task.desktop-acceptance.<唯一键>`：只在`.work/desktop_acceptance`内创建一个小函数与测试文件，精确批准本项目Python运行该测试的命令。verified=false阶段普通task或业务写入会被门禁拒绝，这是预期；不能先改verified=true绕开。
9. Codex核实际文件、原生terminal、唯一reply_to结果、锁释放及独立重跑测试。最后让用户在目标Codex对话验证真实派单，这是目标端验收，不由来源对话冒充。
10. 顺序测试空闲、刷新、正常重启同一聊天及上下文续接；必要重启先确认无其他项目运行任务并有授权。确认后才记录目标路线限定verified。多项目同一composer并发仍未实现，不宣称可并发。

MCP未加载时可在目标对话用`acp_mcp.py --operation status`做无模型状态核对，**这不等于MCP工具往返已验收**。没有真实ID、宿主信任或默认权限，列明待人工步骤；不能代造UUID、提升权限或换隐藏会话解决。

## 9. 必须运行的验收矩阵

| 项 | 操作与判定 | 证据 |
|---|---|---|
| 静态适配 | 所有活动旧标识逐项处理；兼容诊断文字可解释留存；没有来源import/链接 | 修改表、锚点扫描、Hash |
| 语法/配置 | Python compileall、JSON/TOML解析、JS node --check | 实际命令/退出码 |
| 协作回归 | `python -X utf8 -m unittest discover -s setup/collaboration -p 'test_*.py' -v` | 全日志；不得为迁移跳过失败用例 |
| JS行为 | 事件迟到/缺失时最近候选兜底；错误marker/旧时间/双request拒绝；刷新不重发 | test_desktop_session.js及实际断言 |
| 时间契约 | Python秒与SQL秒；run被JS覆盖后recover仍识别毫秒 | 单测构造固定秒/毫秒，不用睡眠碰运气 |
| 收集条件 | 正确关联通过；错session、错reply_to、旧结果、两条结果、未释放锁拒绝 | 收集层测试 |
| 同调用 | WorkBuddy真实修改→finish→message→原MCP附execution；不第二次dispatch | request ID、event/result ID、日志 |
| 取消/超时 | 只记录真实证据；unknown保留，提交次数不增；不取消新用户任务 | 状态及次数对照 |
| 数据保护 | 业务源码/KB/技能/作品未改；原事件/未知/预算保留 | 前后清单与数据库计数/ID |
| 目标独立验收 | 用户从目标Codex原聊天完成一次实际任务 | 原聊天工具调用与独立测试 |

未有对应测试就补在目标协作测试内，不必给每个字段再造Agent。基线153项通过是来源副本证据，不自动证明每一新增矩阵项已被现有断言充分覆盖，更不证明目标实测。新测试要验证行为，不只检查字符串存在。

## 10. 失败怎么处理，防止再改一天

| 症状 | 先核 | 允许修复 | 不允许 |
|---|---|---|---|
| binding_changed | Python/JS key与当前adapter root/sid | 项目唯一key成对修正；任务空闲时加载新版本 | 删其他项目adapter/强覆盖活跃job |
| ready=false但gateway_connected=true | 窗口、选中聊天、pending、默认权限、是否运行中 | 报明确状态；用户授权后选择原聊天 | 当作断线重发 |
| 路由未验收 | task前缀、allowed_paths、verified | bridge-probe或隔离desktop-acceptance | 手填true/借来源认证 |
| terminal已到但无结果 | 秒/毫秒、唯一reply_to、result写入、是否仍持锁 | 只修证据明确的局部条件/执行顺序 | 增加全套状态机/删除硬条件 |
| 没收到requestUpdate | 未关联时最近候选是否仍经observe | 恢复基线受限兜底并测负例 | 扫全局聊天/选最近completed |
| Write后重领锁 | finish/result文件是否都在finish前写完 | 修回报顺序 | 删除锁表/冒用owner |
| server有execution但任务失败 | native terminal、正文、实际差异与独立测试 | 如实报失败、定向修复 | 用execution存在认证业务成功 |
| 重复Hook/被旧程序拦截 | 解码具体command、旧脚本引用、任务是否活跃 | 备份后精确移除退役handler，目标宿主重载 | 清空全部Hooks/全局信任 |

同一错误两次失败换复现角度，先验证单一失败条件，不全链重跑。任何新增技术改造独立列目标、范围、兼容与测试，不能再以“移植”名义不断扩写。

## 11. 交付报告模板

```text
目标项目/真实两端会话：
来源ZIP Hash/源码Hash：
保留、替换、隔离文件及依据：
每个实际修改文件：锚点、旧值、新值、关联调用方、验证：
数据库/未知事件/预算保护核对：
已执行命令、退出码、日志路径：
真实task/result/request ID与回报方式：
Codex独立验收结果：
仍待用户操作/未验证项：
回滚范围（不得覆盖新事件）：
```

若最后一步由用户回目标Codex测试，交付应写“部署与本地测试完成，目标原聊天往返待验收”，不能提前写“全自动闭环”。
