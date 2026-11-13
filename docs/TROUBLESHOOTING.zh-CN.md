# 故障排查：先分清四种状态

> 处理原则：**先看真实回执，再决定动作**。`ACK` 只是已读，`recipient_received` 只是通知送达，`queued` / `sent_awaiting_ack` 只是传输回执，都不等于任务完成或验收通过。任何情况下不要抢锁、不要冒用他人会话 ID、不要把 `unknown` 改写成成功。

## 一、`error` / `offline` / `busy` / `unknown` 的区别

| 状态 | 含义 | 典型证据 | 允许的动作 | 禁止的动作 |
|---|---|---|---|---|
| **error** | 原生请求**已到达明确终态且为失败**（模型限流、宿主返回错误、权限拒绝等）；是否产生关联结果需另核，error不等于毫无副作用 | 原生 request `state=error`；无 `receive` / `result.*`；通知可能已可见 | 记录终态与请求 ID；核宿主当前状态；由持锁方按证据做**定向**异常收尾；如需继续，按当前源码 Hash 重新规划新任务 | 不重发同一事件；不伪造 `receive` / 结果文件；不把 error 当成功；不因超时抢锁 |
| **offline** | **连接前提不成立**：原会话未登记、心跳过期、绑定记录缺地址/PID、客户端未以支持的入口启动 | desktop_cdp应核回环端点、安装Hash、监听PID与SDK状态；session_not_registered/session_stale等是兼容native路线的登记错误，不与桌面路线混用 | 正常启动客户端并让宿主登记；核绑定项目/会话/PID/回环地址；重新做只读预检 | 不扫描全局会话或进程口令；不猜端口；不读凭据；不宣称「检查通过＝空闲会话可自动唤醒」 |
| **busy** | **资源被占用但身份正常**：另一会话持有编辑锁、原聊天有 active job、存在 pending 权限或任务仍在运行 | `begin` 返回 `blocked` + `lease.owner`；`idle=false`；`ready=false` 但 `gateway_connected=true` | 只读观察、等待；由实际 owner `finish` 收尾；权限卡交给用户处理；运行中的任务不覆盖协作程序 | 不按超时抢占锁；不冒用 owner；不取消可能属于用户的新任务；不当作断线重发 |
| **unknown** | **远端提交状态无法确定**：可能已送达也可能没有，回执为 `unknown_no_resubmit` / `sending` / `missing_no_resubmit` | 派发回执 `unknown_no_resubmit`；刷新导致 jobs 丢失；客户端超时但远端未确认停止 | **永久保留**该记录与累计次数；只有接收端用本轮**实际收到的通知正文**做 `receive` 才能消解；或由持锁方按原生 terminal 明确证据登记观察 | **禁止重发**；禁止改写成成功；禁止清预算；本地进程结束不等于远端已取消；未知 Token 保守预留 |

一句话记忆：`error` 是**明确失败**，`offline` 是**没连上**，`busy` 是**正被占用**，`unknown` 是**不知道** —— 只有前三种在证据齐全后可定向处理，`unknown` 只保留、不重放。

## 二、其他常见症状

| 症状 | 先核 | 允许修复 | 不允许 |
|---|---|---|---|
| `binding_changed` | Python / JS 的 `globalThis` key 与当前 adapter root、sid 是否成对一致 | 任务空闲时修正项目唯一 key 并加载新版本 | 删除其他项目 adapter；强覆盖活跃 job |
| 两个原生 request 匹配同一事件 | 时间、正文事件 marker、唯一 request_id | 置 `unknown` | 任选一个当本任务 |
| `ready=false` 但 `gateway_connected=true` | 窗口、选中聊天、pending、默认权限、是否运行中 | 报明确状态；用户授权后选择原聊天 | 当作断线重发 |
| 路由未验收 | `task.*` 前缀、`allowed_paths`、`workbuddy_route_verified` | 走 `bridge-probe` 或隔离目录的 `desktop-acceptance` | 手填 `verified=true`；借来源项目认证 |
| terminal 已到但无结果 | 秒/毫秒单位、唯一 `reply_to`、result 是否写入、是否仍持锁 | 只修证据明确的局部条件或执行顺序 | 新增全套状态机；删除收集硬条件 |
| `Write` 后又重领锁 | `finish` / `result` 两份记录是否都在 `finish` **之前**写完 | 修回报顺序 | 删除锁表；冒用 owner |
| `server` 有 `execution` 但任务失败 | 原生 terminal、结果正文、实际差异、独立测试 | 如实报失败并定向修复 | 用 `execution` 存在认证业务成功 |
| 重复 Hook / 被旧程序拦截 | 解码具体 command、旧脚本引用、任务是否活跃 | 备份后精确移除退役 handler，宿主重载 | 清空全部 Hooks；全局信任绕过 |
| 修改业务源码但 `tree_hash` 不变 | `TOP_FILES` / `TREES` / `EXTENSIONS` / `EXCLUDED` 是否覆盖目标真实源码范围 | 按目标业务补白名单并复测增删文件 | 扩大扫描到凭据目录；用 Hash 不变冒充「已核」 |
| 状态库结构不兼容 | `meta.root`、表/列、实际方法调用、事件与预算 ID | 在隔离副本上验证增量迁移后再执行已审核事务 | 删 `state.sqlite` 重建；用旧备份覆盖新事件 |

## 三、通用处理顺序

1. **只读**：`sync` 读完整报告与真实文件差异，逐页 `ack` 到 `more=false`；用 `status` 看锁、事件、投递回执。不要用通知摘要当已读。
2. **归类**：按上表判定 `error` / `offline` / `busy` / `unknown`，记录实际 ID、时间、命令与退出码。
3. **不扩权**：`blocked` 时只读；权限拒绝即停止并回报，不自拟命令、不换工具或路径绕过。
4. **最小修复**：只修证据明确的那一个条件，同一错误两次失败要换复现角度，不全链重跑。
5. **如实回报**：`finish` 写明修改、实际测试与剩余问题；失败也按实际证据回报，不包装成上线通过。
6. **回滚**：程序/配置回滚与数据库回滚分开；已有真实投递后不得用旧库覆盖新事件、`unknown` 与累计次数。

更完整的失败矩阵与交付报告模板见 `MIGRATION.zh-CN.md` 第 10、11 节。
