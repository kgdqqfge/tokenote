# Tokenote v1.1

本地只读的 Windows Agent 用量桌面客户端。Python 3.14 标准库 + Tkinter + SQLite（较早版本读取 Harness 压缩日志需 backports.zstd）；Trae 解密需要 cryptography。

## 启动

```powershell
cd <项目目录>
python -m pip install -r requirements.txt
python app.py
```

或双击 run.bat。只同步一次：`python app.py --sync-only`。exe 首次运行会显示一次使用提示，确认后不再显示。

环境要求：Windows 10/11 x64；推荐 Python 3.14，3.10–3.13 需要 requirements 中的 `backports.zstd`。Tkinter 随 Windows 官方 Python 安装包提供。

Windows 下同一统计库只允许运行一个实例。再次双击桌面快捷方式或启动脚本会唤起已有窗口；最小化窗口会恢复，最大化状态会保留。已有程序占用同一统计库时，`--sync-only` 会直接跳过。正常退出或进程异常终止后，系统自动释放运行锁，可以重新打开。

数据位于 `%LOCALAPPDATA%\Tokenote\tokenote.db`；可用 TOKENOTE_DATA_DIR 指定位置。

## 打包 exe

```powershell
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --clean Tokenote.spec
```

产物为 `dist\Tokenote.exe`（单文件，约 20 MB）。构建需在 Windows x64 上进行，不能跨平台交叉构建。

## 数据源

已接入 Codex、Trae CN、WorkBuddy、OpenCode、Kimi Code、DeepSeek Harness、OpenClaw（含 AutoClaw 的 OpenClaw 会话）、Qoder、Grok Build。

DeepSeek Harness 自动读取 `~/.dsh/sessions/**/session.jsonl.zstd` 或普通 `session.jsonl`，兼容 `DSH_HOME` 和 `TOKENOTE_DEEPSEEK_HARNESS_DIR` 指定路径；支持逐请求、模型切换和缓存占比，重复同步不重复累计。

OpenClaw 自动读取 `~/.openclaw/agents/*/sessions/*.jsonl` 及 `.openclaw-*` profile（例如 `.openclaw-autoclaw`）；重置、删除后保留的历史日志也会计入，复制或归档的同一条用量不会重复累计。可通过 `OPENCLAW_STATE_DIR` 或 `TOKENOTE_OPENCLAW_DIR` 指定状态目录。

Qoder 自动读取 `~/.qoder/projects/**/*.jsonl` 与 `~/.qoder-cn/projects/**/*.jsonl`，默认展示独立的 Credits、请求报告和计费状态，可切换 Token。当前第一方日志未提供有效 Token 计数，界面显示“未提供”；如果后续 BYOK 日志提供真实 Token，会按实际字段统计。Credits 不折算成 Token，也不混入全局 Token 总量。

Grok Build 自动读取 `~/.grok/sessions/<cwd>/<session-id>/updates.jsonl` 中 `turn_completed.usage` 的真实 Token，按模型拆分，保留缓存读/写和 reasoning；父会话已包含 subagent 用量，子会话不重复导入。兼容 `GROK_HOME` 和 `TOKENOTE_GROK_DIR`。不读取 `auth.json`、不调用 xAI API，也不保存聊天正文。

Cursor 的本地适配器已保留在代码中，但当前 Cursor 3.24.18 的内置模型会话把 `tokenCount` 落盘为 0，真实用量只在 Cursor 控制台；因此暂未在侧栏、同步服务和统计界面启用。待后续版本能提供真实计数后再开启。

已按公开格式增加 ZCode、Claude Code、Pi agents、Hermes、MiniMax Code 的适配器。没有相应数据文件时显示“未发现数据源”，不产生模拟数据；安装后需验证真实版本。Hermes 目前为会话/模型累计视图，不进入每日趋势。

字段、路径、时间口径和限制以各适配器实现为准。

## 界面

采用 macOS 风格的浅色界面：可滚动的灰白侧栏、圆角面板、分段切换控件和统一的数字排版；保留 Windows 原生窗口操作。

侧栏使用各 Agent 的原始图标，图片随程序本地打包；来源见 [图标来源说明](tokenote/ui/assets/agents/SOURCES.md)。

- 总量、含缓存输入、输出、缓存读取和缓存命中率；保留完整数字，并在卡片右上角显示两位小数的 M/B 用量摘要。
- 总览可按应用或按日期切换；单个应用时可按模型或按日期切换，模型视图对比最近 24 小时（Top 8）的用量。日期趋势显示最近14天并补齐零用量日期，悬停柱形显示日期、Token 数量和记录数。
- 热门模型栏贯穿右侧，按窗口高度自动调整每页数量；三色柱形统一表示新增输入、输出、缓存读写的占比，悬停查看具体数量。
- 最近记录与趋势图对齐，文本列与表头左对齐，移除来源列；“新增输入”与缓存分列，ISO 时间按本地时区显示。
- CSV 导出、自动同步、来源同步详情。

## Trae

Trae 用量由内置提取器读取，不再调用外部 Python。首次读取需要 Trae CN 正在运行：程序会扫描一次 Trae 进程内存获取数据库密钥，校验后缓存到 `%LOCALAPPDATA%\Tokenote\trae.key`，后续直接使用缓存；缓存失效时重新获取。

缓存密钥候选为 `%TEMP%\trae_key.bin`、`%LOCALAPPDATA%\Tokenote\trae.key` 和 TOKENOTE_TRAE_KEY；数据库路径可用 TOKENOTE_TRAE_DB 指定。

## 扩展

在 tokenote/adapters 中实现 Adapter，输出 UsageEvent，并在 tokenote/sync.py 注册；界面显示名在 tokenote/catalog.py。来源原始计数和归一化口径会一起保留。
