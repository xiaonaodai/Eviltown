# AGENTS.md — EvilTown 项目指令

## 项目简介
元胞自动机 + 多智能体（ABCA）生活观测游戏：格子上住着一群有 OCEAN 人格、记忆与愿望的小人，按规则生活、工作、社交。Python 驱动仿真，`web/` 前端实时展示。

## 技术栈
- Python 3（`from __future__ import annotations`、`@dataclass`、类型注解兼作文档）
- SQLite（`eviltown/storage/db.py`，幂等建表 + 索引）
- PyYAML（`config.yaml`）、pytest（`tests/`）
- 后端 `server.py` 提供 HTTP 接口与热配置 [待确认：Web 框架]

## 目录与核心模块
| 路径 | 职责 |
|---|---|
| `eviltown/agents/sim.py` | 仿真主循环（~1500 行）：tick、需求衰减、事件日志、日终结算 |
| `eviltown/agents/brain.py` | **快脑决策**：纯效用函数 `decide()` + 噪声 → `Action`；**不调 LLM** |
| `eviltown/agents/resident.py` | 居民数据：`Ocean`(6 维) / 需求 / 钱包 / `journal_bias` / `wish` |
| `eviltown/agents/narrative.py` | 日终日记 + 复盘（reflection），LLM 产出文本与软偏好 |
| `eviltown/agents/wish.py` | 出生时生成长期愿望（LLM，归一化到 5 个 kind） |
| `eviltown/agents/memory_rag.py` | 长期记忆检索：`TownHistory.add/search/recall` |
| `eviltown/agents/interactions.py` / `relations.py` | 相遇结算 / 人物关系 |
| `eviltown/agents/llm_client.py` | LLM 客户端（`chat()`、失败降级） |
| `eviltown/world/` | `grid` / `fields` / `buildings` / `pathfind` / `clock` |
| `eviltown/storage/` | `db.py`（表与迁移）/ `repo.py` / `vector_store.py`（sqlite-vec → 余弦 → 关键词 三级降级） |
| `eviltown/config.py` | 配置 dataclass + `load_config()`，镜像 `config.yaml` |
| `server.py` / `web/` | HTTP 服务 / 前端（地图、详情面板、事件流） |
| `tests/` | pytest：`test_eviltown.py` / `test_memory_rag.py` / `test_reflect.py` |

## 构建与测试
```bash
pip install -r requirements.txt
python server.py                      # 启动服务
python -X utf8 -m pytest -q           # 全量测试（Windows 必须带 -X utf8，否则中文断言乱码）
```

## 核心红线（改代码前必读）
1. **LLM 不进主循环**：`decide()` 只做效用打分；LLM 只允许产出文本或**软权重**（`wish` kind、`recall` bonus、`journal_bias`）。
2. **只加不删**：LLM 产物只能加减分，不得删候选、不得覆盖阈值与硬规则（快饿晕的人照样先吃饭）。
3. **新功能默认关闭**：配置层加 `enabled: false`，关闭时零行为变化、零额外调用。
4. **严格校验 LLM 输出**：白名单 + clamp + 解析失败整批丢弃，异常必须吞掉（不能影响日记与主循环）。
5. **注释解释「为什么」**：涉及产品口径的标注日期（如「用户 2026-11-10 指出」）。

## 代码风格
- 中文注释/文档字符串；英文只用于标识符与 README 英文段
- 数据用 `@dataclass`；有默认值的字段集中在字段区后段，避免与必填字段顺序冲突
- 宽异常捕获写明理由并带 `# noqa: BLE001`
- 改配置结构时同步 `config.py` 的 dataclass 与 `config.yaml`
- README 中英两段（`# EvilTown` / `# EvilTown（中文）`）需同步增补

## 常用工作流
- 功能开发走分支 `feat/<name>`，PR 关联 issue（`Closes #N`），合并前跑全量 pytest
- 大改动用委派 worktree（`.link-agent/worktrees/…`）隔离，验证后 `worktree merge`
- 本机**没有 ripgrep**：`search(action=content)` 是暴力遍历（慢、可能扫不完），已知文件名优先 `read_file`
- 推送通道：`fetch` 用 https，`push` 用 `ssh://git@ssh.github.com:443/xiaonaodai/Eviltown.git`

## 待确认
- `server.py` 使用的 Web 框架（未读该文件）
- `web/` 的构建方式（未见 package.json，按静态资源处理）
