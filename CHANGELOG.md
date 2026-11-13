# 变更记录

本仓库采用「按日期的整理稿」方式记录，尚未发布正式版本。所有条目仅描述**仓库内文档与素材的变化**，不代表任何目标项目已部署、已接通或已验收。

## 2026-11-13 — 公开仓库整理稿（初版）

### 新增

- `README.md`：用途、先看限制、一个 Codex 项目对应一个 WorkBuddy 项目（含两张原创配图相对链接）、安装前提、仓库结构、快速上手摘要、计划—执行—验收分工、已知限制、文档索引。
- `docs/QUICKSTART.zh-CN.md`：分步上手、虚构占位符命令示例、明确「占位符原样复制不可执行」、旧项目先盘点/备份/隔离再部署、禁止直接覆盖 `state.sqlite`。
- `docs/ARCHITECTURE.zh-CN.md`：两张 Mermaid 图、协作状态机、`same_call` 同调用回报、结果收集硬条件、秒/毫秒时间契约、安全边界与未覆盖能力。
- `docs/TROUBLESHOOTING.zh-CN.md`：`error` / `offline` / `busy` / `unknown` 四种状态的区别、常见症状表、通用处理顺序。
- `CONTRIBUTING.md`：可改/不可改范围、冻结参考源码与 `SOURCE_MANIFEST.json` 的联动要求、禁止提交真实数据、验证与风格约定。
- `SECURITY.md`：不包含项、`.gitignore` 覆盖、使用时安全边界、发布前自查清单、私有漏洞报告渠道。
- `THIRD_PARTY_NOTICES.md`：如实记录上游 `workbuddy-acp-bridge`（<https://github.com/jie023/workbuddy-acp-bridge>）与作者署名、来源 ZIP SHA256、改编说明；**明确标注许可状态未确认、仓库无 LICENSE、不声明 MIT**；依赖与配图许可另列。
- `CHANGELOG.md`（本文件）。

### 修改

- `docs/MIGRATION.zh-CN.md`：**仅修订开头与第 0 节源码入口说明**，改为以仓库内 `reference/setup/collaboration/` 与根目录 `SOURCE_MANIFEST.json`（基线 `desktop-visible-20261112`）为唯一源码依据，不再依赖未随本仓库分发的旧 ZIP 与 V2 锚点 JSON。第 1 节之后的变量表、逐文件修改、运行契约、验收矩阵与失败处理**保持原文不变**。

### 由 Codex 预先备好、本版未改动

- `reference/setup/collaboration/`：23 个冻结脱敏参考源码/测试/来源文件，逐文件 SHA256 见 `SOURCE_MANIFEST.json`。
- `examples/`：4 个占位模板（MCP、desktop、项目变量表、旧通道清理盘点）。
- `assets/`：`project-pairing.mmd` / `.svg` / `.png` 与 `task-lifecycle.svg` 两张原创配图。
- `.gitignore`、`requirements-reference.txt`、`SOURCE_MANIFEST.json`。

### 明确未做 / 未验收

- 未修改任何参考运行源码或状态逻辑，未运行剪辑、测试、部署或发布；未访问目标发布目录，未 push、未联网。
- 未提供通用一键安装器；不宣称所有 WorkBuddy / SDK / Python 版本支持。
- **多项目同一桌面 composer 并发未实现、未验收**；客户端及原聊天就绪后可由Codex明确派单。任意快捷方式启动即连上、所有权限弹窗路径仍需单独验证。
- 按维护者明确要求公开发布，保留上游来源说明，不擅自指定第三方许可证。发布前由Codex完成脱敏、源码Hash、链接和测试核验。

### Codex独立验收补充

- 修正空闲派单与错误结果的表述，补充首次客户端启动说明及client模板。
- 独立审查发现并补齐QUICKSTART的新库初始化命令，明确先init/兼容检查、后安装Hooks与绑定。
- 公开仓库实际路径中的153项参考回归通过，无跳过；冻结源码与模板在Git索引中的27行Hash全部与清单一致。
- 配置Git属性保留冻结文件原始字节，避免克隆后的换行转换使Hash失效。提供Windows参考测试工作流；远端CI结果以实际Actions记录为准。
