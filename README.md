# agymodel — Antigravity 端点与性能检测工具

零第三方依赖的 **Antigravity** 模型底层端点、生成速率（TPS / TTFT）与 Token 消耗检测工具。同时支持 **Antigravity App** 与 **Antigravity CLI** 会话数据库。

## 核心特性

- **真实底层端点透视**：直接解析会话 SQLite 数据库中的 `gen_metadata` Protobuf 载荷，对比客户端配置模型（`client_model`）与服务端真实调用的底层端点标识（`backend_model`）及内部枚举（`model_enum`）。
- **精准速率与延迟计算**：
  - 区分首包等待延迟（`TTFT`，含 Prompt 预填充与思考耗时）与流式接收耗时（`Stream Duration`）。
  - 同时计算**端到端生成速率（TPS）**与**流式解码速率（Streaming TPS）**。
- **完整 Token 构成拆解**：统计每轮的新增输入 Token、**上下文缓存命中（Cached Tokens）**、思考 Token（Thinking）与正文输出 Token（Content）。
- **轮次级问答与工具摘要关联**：自动关联 `steps` 表，提取首条提问作为会话可读标题；在表格中点击任意轮次即可展开查看触发该轮的用户提问、思考摘要、模型回复与工具调用列表。
- **极简清水界面 + 零外部依赖**：纯 Python 标准库后端 + 原生 CSS/JS 前端（无任何外部 CDN 依赖），支持独立无边框桌面窗口（App Mode）、浏览器标签页与 CLI 终端表格三种运行形态，内置浅色/深色主题切换与无闪烁增量轮询。

## 目录结构

```text
agymodel/
├── agymodel/
│   ├── __init__.py
│   ├── proto.py       # 零依赖 Protobuf Wire 协议解析器
│   ├── parser.py      # SQLite 安全读取、gen_metadata 与 steps 精准关联解析
│   ├── cli.py         # 终端命令行表格输出
│   └── server.py      # 本地多线程 HTTP 服务与独立窗口启动器
├── static/
│   ├── index.html     # 语义化前端视图结构
│   ├── style.css      # 清水极简风格原生 CSS (支持浅色/深色主题切换)
│   └── app.js         # 增量无闪烁轮询与点击展开交互逻辑
├── run.py             # 统一入口脚本 (支持桌面独立窗口 / 浏览器 / CLI)
└── start_dashboard.sh # 一键启动脚本
```

## 快速开始

环境要求：**Python 3.8+**（仅使用标准库，无需 `pip install` 任何依赖）。

### 1. 启动图形界面（默认独立无边框桌面窗口）

```bash
./start_dashboard.sh
# 或
python3 run.py
```

- 在系统默认浏览器标签页打开：
  ```bash
  python3 run.py --browser
  ```
- 仅后台启动 HTTP 服务（不自动弹窗）：
  ```bash
  python3 run.py --no-open -p 8765
  ```

### 2. 终端命令行模式 (CLI)

```bash
python3 run.py -l              # 列出最近会话列表 (含会话标题、真实端点与 TPS)
python3 run.py --cli           # 在终端查看最新会话最新一轮详情
python3 run.py -a              # 在终端查看最新会话的全部轮次表格
python3 run.py -c <ID前缀> -a  # 查看指定前缀会话的全部轮次
python3 run.py --json          # 输出 JSON 格式结构化数据
```
