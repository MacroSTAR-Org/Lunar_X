# Changelog

本项目遵循语义化版本；版本号唯一来源是 `core/version.py`，打包与 WebUI 均从这里读取。

## [0.1.0.128] - 2026-11-05

### Added

- 事件分发按会话任务化：同一群/私聊严格串行，不同会话并发，慢插件不再拖住整个 Bot。
- 每个会话的事件积压上限 `conversation_queue_size`，超出后丢弃新事件并计数告警。
- 插件 handler 超时保护 `plugin_timeout`，卡死的插件会被取消，后续插件继续处理。
- 有界事件队列 `event_queue_size`，队列满时丢弃最旧事件而不是无限堆积。
- 运行统计日志：队列深度、丢弃数、会话积压、派发中任务数、峰值与平均派发延迟。
- AI 管线 LLM 调用统计：每 25 次调用输出调用数、失败数与失败率。
- AI 单会话待回复上限 `agent.max_pending_replies`，慢模型期间不再无限堆积后台任务。
- WebUI `GET /healthz`（无需登录），返回状态与版本号，供容器/反代探针使用。
- Dockerfile、docker-compose、容器入口脚本，WebUI 与 Bot 共享状态卷。

### Changed

- 版本号统一为 `core/version.py` 单一来源；`pyproject.toml` 动态读取，WebUI 不再解析注释。
- WebUI 配置写入（主配置、协议端配置、管理员、安全设置、改密、插件配置）统一加跨进程文件锁。
- 插件首次初始化与插件安装改用跨进程锁，避免多 gunicorn worker 竞态。

### Fixed

- 修复 websockets 17 下连接正常关闭却不触发重连、导致 Bot 静默失联的问题。
- 修复同一秒内修改插件且文件大小不变时，Python 复用旧 `.pyc`、热重载不生效的问题。
- 修复 Docker 镜像内虚拟环境不在 `PATH`、`gunicorn` 无法启动的问题。

### Known limitations

- WebUI 仍是单文件 Flask 应用，尚未拆分为蓝图/服务层。
- 插件市场 SHA256 校验目前是可选强化，尚未强制。
- `computer_use` 依赖提示词与黑名单约束，不是操作系统级沙箱；生产环境应使用低权限账号或容器隔离。
