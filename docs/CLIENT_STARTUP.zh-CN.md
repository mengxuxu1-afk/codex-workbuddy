# 首次准备共享客户端启动入口

已经有正常统一启动入口的机器无需重复安装。新使用者可以仅复制参考源码中的`workbuddy_client.py`到自己选择的中性工具目录，例如`D:/Tools/WorkBuddyClient`，不要把项目会话或状态放进该目录。

1. 保留自己正常安装并登录的官方WorkBuddy桌面版。确认当前无任务，必要时从托盘正常退出，不强杀。
2. 将`examples/client.template.json`复制为工具目录中的`client.json`。填写真实exe绝对路径、该exe实际SHA256与本机回环端口。配置仅这三个字段，不含项目路径、聊天或密钥。
3. 用PowerShell检查安装签名和Hash，替换以下示例路径：

```powershell
Get-AuthenticodeSignature -FilePath 'D:/Apps/WorkBuddy/WorkBuddy.exe'
Get-FileHash -LiteralPath 'D:/Apps/WorkBuddy/WorkBuddy.exe' -Algorithm SHA256
```

签名须Valid；客户端启动代码还会自行核验。然后使用自己已安装的Python执行：

```powershell
py -3.12 'D:/Tools/WorkBuddyClient/workbuddy_client.py' --config 'D:/Tools/WorkBuddyClient/client.json'
```

该入口只启动或复用一个客户端，不选择聊天、不发模型任务。已有进程未开启所需本机端点时会拒绝，不自动强杀。启动器退出后客户端独立存活，无需常驻终端；可按自身环境制作调用此命令的正常快捷方式。

每个目标项目的desktop.json端口和安装身份与这个入口一致，但会话、编辑锁和状态分别属于各项目。客户端或安装文件升级后重新验证Hash和兼容性；不要禁用签名/Hash检查。

本机调试端口只允许数字IPv4回环地址。不要映射公网、关闭系统安全设置或改全局执行策略。
