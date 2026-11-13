# 安全说明

## 一、这个仓库不包含什么

本仓库是**文档 + 冻结脱敏参考源码 + 占位模板**的集合。它不应包含、也不接受提交以下内容：

- 真实项目路径、真实会话 UUID、真实 Codex / WorkBuddy 聊天 ID
- `.collab/`（协作状态库、通知、`desktop.json`、冻结 protocol、交接记录）、`.work/`（运行产物、日志、缓存）
- `state.sqlite` / 任何数据库、`.env`、密钥、访问令牌、用户级 settings、进程环境提取结果
- 官方安装 exe 的实际 SHA256、绑定 PID、回环地址与端口的实际登记
- 用户素材、业务源码、知识库内容、聊天记录、未公开的验证证据

根目录 `.gitignore` 已排除 `.collab/`、`.work/`、虚拟环境、`__pycache__/`、`*.sqlite*`、`*.db`、`.env*`、`*.local.json`、`node_modules/`、`private/`、`secrets/`、`credentials/`。**`.gitignore` 只是最后一道防线**：提交前请自行检查，特别是示例配置、截图、日志片段和 Mermaid / SVG 文本内是否夹带真实值。

`SOURCE_MANIFEST.json` 明确注明：「Reference source and fake test UUID fixtures only. No live project configuration, chats, credentials or verification state.」测试中的 UUID 均为假夹具，不要替换成真实 ID。

## 二、使用时的安全边界

- **项目隔离**：一个 Codex 项目对应一个 WorkBuddy 项目、同一个物理目录。不跨项目读取、引用、复制或修改文件、凭据、素材、知识与聊天上下文。技术可访问 ≠ 已授权。
- **最小权限**：保留宿主原生权限卡，桥接不调用批准或权限切换接口；不使用 `allow_always` / `fullAccess` 代替用户确认；任务策略冻结 event、session、源码 Hash、允许路径 / 命令与期限，模型提出新请求不扩大权限。
- **只连本机回环**：连接前提要核绑定项目 / 会话 / PID / 安装路径；不扫描全局会话或进程环境、不猜测端口、不读取用户级口令、不关闭鉴权。
- **不伪造证据**：`receive` 必须使用本轮实际收到的通知正文；不能由发送方代填凭据、不能把共享库已读当实际收到、不能把 `ACK` / 队列成功 / 接口 200 当作任务完成或验收通过。
- **未知状态**：`unknown_no_resubmit` / `sending` 永久保留、禁止重发；本地进程结束不代表远端已取消；客户端超时不等于远端已停止。
- **锁**：`begin` 被 `blocked` 时只读，不抢锁、不冒用 owner、不换会话 ID 绕过。
- **Hooks**：项目 Hooks 必须由宿主实际加载并经用户真实信任；编码命令由目标项目本地 `install.py` 重新生成，不从其他项目复制，不绕过信任机制。
- **状态库**：不删库重建、不用旧备份覆盖新事件；回滚时程序/配置与数据库分开处理。SQLite 备份用一致性快照，不在活跃 WAL 时只复制单文件。

## 三、发布前的自查清单

1. 全仓搜索是否存在真实 UUID、真实绝对路径、安装 Hash、`verified=true`、协议正文 —— 有则移除或改为占位符。
2. 确认 `reference/` 只含脱敏对照源码，`examples/` 全为 `TARGET_*` / `<...>` 占位。
3. 确认没有 `.collab/`、`.work/`、数据库、日志、凭据、压缩包被打包。
4. 确认 `SOURCE_MANIFEST.json` 的 Hash 与 `reference/` 实际文件一致；不一致先做差异归类。
5. 确认 `THIRD_PARTY_NOTICES.md` 如实反映上游与许可状态；**在许可未确认前不添加 LICENSE、不声称 MIT**。
6. 确认 README 与文档没有宣称一键安装、全版本支持或多项目并发已验收。

## 四、报告安全问题

请勿在公开 issue 中粘贴真实配置、会话 ID、通知正文或凭据。请通过你所使用的代码托管平台提供的**私有漏洞报告渠道**联系维护者，包含：影响范围、复现前提（不含真实数据）、实际回执、建议的缓解方式。维护者会在核实后于本文件补充说明。

本仓库当前没有常驻服务、没有对外 API、不处理用户账号或支付，也不收集遥测；参考源码只在用户自己的项目内、经明确授权后运行。
