# Agent 自动化（主动运转）：定时/事件驱动的无人值守会话

> 状态：草案（2026-10-29）
> 目标读者：实现者与评审者
> 关联文档：`agent-runtime-resilience.md`（运行中断语义）、`domain-events.md`（事件与 origin 因果链）、
> `agent-cli-integration.md`（mclaw 工具面与确认契约）、`cli.md`（x-cli-dangerous）、
> `cloud-push.md`（推送通道）、`tips.md`（使用提示）

---

## 0. 一句话

把 Agent 从「有人发消息才醒」升级为「定时/事件到点自己跑一轮」：

```
到期 → 过闸门 → 以 automation 身份跑一轮会话（仅 mclaw 工具）→ 结果按配置投递 → 留痕/退避/可一键停
```

用户侧的形态：说一句「每天早上八点给我一份昨日入库汇总」或「片库出现待识别就帮我处理，拿不准的列给我」，
Agent 调 `agent automations create` 建一条自动化；此后无需任何人工触发。

## 1. 动机

- 订阅管线已经全自动（搜种→匹配→下载→入库→对账），但**值守类**工作（待识别认领、洗版机会、
  链路体检、周报）仍靠人开口；Agent 能力足够，缺的只是「自己醒来」的机制。
- 现有 22 个定时任务全是确定性代码，不经过 LLM；本设计补上「定时任务 → Agent 会话」这一跳，
  让两类自动化合流（详见 §9 长期方向）。

## 2. 边界（v1 明确不做）

1. **不做成员开放**：v1 仅超管可建/可跑。成员级需先落地令牌降权（`member-permissions-v2.md` 前置条件），不在本设计内。
2. **不做任意工具面**：自动化会话固定 `mclaw` 档位（无 bash/read/write/edit），杜绝「自动化改文件系统」。
3. **不做自我增殖**：automation 身份的会话**禁止**调用 `agent automations *` 管理端点（防「自动化建自动化」的环）。
4. **不做事件触发**（v1 只做 cron/interval）：事件触发依赖 domain-events 消费者模型在会话侧的映射，放 v2。

## 3. 总体结构：六个循环（Loop）

一次自动化运行 = L1→L2→L3→L4→L5 同步链；L6 为异步旁路。

```
L1 Beat（节拍）      scheduler 每 60s tick，挑出到期的自动化
      ↓
L2 Gate（闸门）      kill switch → 静默时段 → 预算 → 单飞（single-flight）
      ↓
L3 Run（会话）       以 origin=automation 跑一轮 Agent（仅 mclaw 工具 + token 上限）
      ↓
L4 Deliver（投递）   结论写回 owner 会话 + 按 delivery 配置推送；空结论不打扰
      ↓
L5 Observe（观测）   run 落库；失败计数/退避/熔断；连续失败进待处理事项

L6 Maintain（维护，异步）   run 历史清理；用量汇总（P2）
```

### L1 · Beat（节拍与到期挑选）

- **触发**：新增内置定时任务 `run_agent_automations`（interval 60s，`@register_task` 注册，
  注册方式与 `torrent_sync.py` / `smart_scheduler.py` 一致——任务函数定义在服务层，模块导入即注册）。
- **做什么**：读 `agent_automation` 表中 `enabled=true 且 next_run_at <= now()` 的行，逐条调起；
  错过的多次到期**合并为一次**（`last_run_at` 与 `now` 之间只补跑 1 轮，不追跑历史）。
- **代码落点**：`src/movieclaw_api/services/agent_automations.py`（新文件，任务函数 + 到期计算）。
- **验收**：
  1. 建 interval=60s 的自动化，最多 2 个 tick 内出现首次 run 记录；
  2. `docker restart` 后，停机期间错过的到期合并为 1 次 run（不重复补跑）；
  3. `task_run` 表可见 `run_agent_automations` 的执行历史。

### L2 · Gate（闸门，顺序不可换）

- **顺序**：全局开关（kill switch）→ 静默时段 → 日预算 → 单飞。
  顺序错了会出现「预算耗尽仍在跑」「关不掉的自动化」。
- **全局开关**：设置页一项 `agent_automations_enabled`（默认 false，发布时默认关）。
- **静默时段**：per-automation 可选 `quiet_hours`（如 `23:00-07:00`），命中则跳过并顺延。
- **日预算**：per-automation `max_runs_per_day`（默认 12）+ 全局日 token 预算（设置项，默认 200k/天）。
- **单飞**：同一 automation 上一轮未结束时，本轮记 `skip(reason=busy)`，不排队。
- **验收**：
  1. 关闭全局开关后，≤1 个 tick 内不再有新 run 启动（在途轮次收到取消）；
  2. 静默时段内到期的自动化不运行，`next_run_at` 顺延到时段结束；
  3. 单飞验证：手动 run-now 一轮长任务期间，到期触发记 skip。

### L3 · Run（会话执行）

- **执行体**：复用通道侧的会话执行路径（`services/channel_agent.py` 的 `run_agent` 一族），
  差异点：`owner=自动化归属的超管`、`tool_profile="mclaw"`、`origin` 新增 `kind="automation"`。
- **会话归属**：每条自动化长期复用**同一个会话**（`automation_id ↔ session_id` 稳定映射），
  上下文可接续；用户在网页 `/sessions/{id}` 能看到全部历史轮次。
- **提示词**：run 时注入两段——自动化本身的 `prompt` + 一段固定的系统规约
  （「你是被定时调起的值守会话；只处理本轮事实；无事项时输出 NO-OP；不要反问」）。
- **熔断**：单轮 `max_tokens`（默认 30k）与墙钟上限（默认 10 分钟），超限优雅终止，
  已产生的内容照常落库，run 标 `status=budget_exceeded`。
- **代码落点**：`src/movieclaw_api/services/agent_automations.py`（执行）；
  `src/movieclaw_api/services/agent_sessions.py` / `channel_agent.py`（origin 与档位接线）；
  `src/movieclaw_api/services/auth.py`（签发带 `automation_id` 的 agent 令牌，仍 `is_admin=True`，
  但带标记供路由拒绝 `automations.*`）。
- **验收**：
  1. run 产生的会话在 `/sessions` 列表可见，条目带 automation 徽标，点开是完整时间线；
  2. 会话工具面只有 `mclaw`（无 bash/文件工具）——用会话内 `bash` 调用被拒验证；
  3. automation 身份调 `agent automations create` 得 403（防自我增殖，单测覆盖）；
  4. token 上限触发时 run 优雅终止且会话内容完整保留。

### L4 · Deliver（投递）

- **落会话**：结论写入归属会话（复用现有 agent 会话消息流，网页端天然可见）。
- **推送**：`delivery.channels ⊆ {push, im}`；走现有通知引擎（`services/push/engine.py`），
  模板注册进 `locales/*/agent.json`。**空结论（模型输出 NO-OP 或「无变化」）不推送**——
  「没事别说话」是主动消息的第一美德。
- **幂等**：推送以 `run_id` 为幂等键，重试不重复。
- **验收**：
  1. 有结论：App 收到一条推送，点开落到该轮会话；
  2. 连续 3 天 NO-OP：零推送，会话里各留一条短记录；
  3. 同一 run 重试投递，用户只收到一次。

### L5 · Observe（观测与熔断）

- **run 表**：`agent_automation_run(id, automation_id, started_at, finished_at, status,
  session_id, summary, tokens_used, skip_reason)`，status ∈ running/completed/no_op/failed/budget_exceeded/skipped。
- **失败处理**：连续失败 ≥3 次 → 自动 `enabled=false` + 写一条系统待处理事项（管理员视角，
  含最后错误全文）+ 推送告知 owner。恢复人工重新启用。
- **退避**：连续失败期间 `next_run_at` 指数退避（1h → 6h → 24h）。
- **验收**：
  1. 注入失败（如 prompt 引用不存在的命令）3 次后自动化被自动停用，待处理事项出现；
  2. 退避间隔按 1h/6h/24h 递增（表驱动单测）；
  3. run 历史 API 分页可查。

### L6 · Maintain（维护，P2）

- run 历史保留 90 天，清理走现有 `cleanup_task_runs` 模式新增一条任务；
- 周报 digest（本周运行次数/花费 token/NO-OP 率）——P2，可后置。

## 4. 数据模型

```
agent_automation(
  id INTEGER PK,
  owner_member_id INTEGER NOT NULL,          -- v1 恒为超管
  name TEXT NOT NULL,
  prompt TEXT NOT NULL,                      -- 值守指令全文
  trigger_type TEXT NOT NULL,                -- 'interval' | 'cron'
  trigger_expr TEXT NOT NULL,                -- 秒数 或 cron 五段式
  delivery_channels TEXT NOT NULL DEFAULT 'push',
  quiet_hours TEXT NULL,                     -- '23:00-07:00'
  max_runs_per_day INTEGER NOT NULL DEFAULT 12,
  max_tokens_per_run INTEGER NOT NULL DEFAULT 30000,
  enabled BOOLEAN NOT NULL DEFAULT true,
  session_id TEXT NULL,                      -- 稳定会话映射
  last_run_at TIMESTAMP NULL,
  next_run_at TIMESTAMP NOT NULL,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NULL,
  created_at / updated_at
)
agent_automation_run( ... 见 L5 ... )
```

- 迁移：`alembic/versions/xxxx_add_agent_automations.py`，遵循仓库迁移规范（含 downgrade）。

## 5. API 面（成员区路由 + CLI 自动生成）

路由挂 `routes/agent.py`（agent 域内），统一 `require_admin`：

| 端点 | 说明 | 危险标注 |
|---|---|---|
| `GET  /agent/automations` | 列表（含最近一次 run 摘要） | — |
| `POST /agent/automations` | 创建（校验 prompt 非空、trigger 合法） | — |
| `PUT  /agent/automations/{id}` | 改名/改触发/改投递/启停 | — |
| `DELETE /agent/automations/{id}` | 删除（run 历史保留） | `x-cli-dangerous: confirm` |
| `POST /agent/automations/{id}/run-now` | 立即跑一轮（同样过 L2 闸门） | — |

- OpenAPI 有 `operationId` 即自动长出 CLI 命令（`mclaw agent automations ...`）与 MCP 工具——
  **Agent 自己就能替用户建自动化**，这是本设计的关键闭环。
- i18n：错误与通知文案进 `locales/*/agent.json`（zh-Hans / en 同步）。

## 6. 提示词规约（Agent 建自动化时的行为）

系统提示词在现有 `<mclaw_service>` 契约上追加一段（`services/mclaw_tool.py`）：

> 用户表达「定时/每到/每天…就…」类诉求时，用 `agent automations create` 落成自动化并向用户复述
> 触发计划；修改前先 `list` 确认；删除需按 ⚠ 契约征得同意。自动化指令写成自包含的值守任务书
> （背景一句 + 要做什么 + 输出格式 + 拿不准时的处理），不要写成一问一答。

## 7. 实施分期与验收总表

| 期 | 内容 | 验收（=完成的定义） |
|---|---|---|
| P0 地基 | 表 + 迁移 + 服务骨架 + CRUD 路由 + i18n | 五端点通；`mclaw agent automations list` 出现；CRUD 表驱动测试绿 |
| P1 节拍 | `run_agent_automations` 任务 + 到期计算 + 补跑合并 | L1 三条验收 + task_run 可见 |
| P2 闸门 | 开关/静默/预算/单飞 | L2 三条验收（含 kill switch ≤1 tick） |
| P3 会话 | origin=automation + mclaw 档位 + 防自我增殖 + 熔断 | L3 四条验收 |
| P4 投递 | 会话落库 + 推送 + NO-OP 抑制 + 幂等 | L4 三条验收 |
| P5 观测 | run 表 + 失败熔断 + 退避 + 待处理事项 | L5 三条验收 |
| P6 打磨 | CLI 体验、设置页开关、清理任务、守护测试 | 守护测试钉死「automation 令牌不可调 automations.*」 |

每期一个 PR，合入前跑 `tests/api/` 全量；P3 起补 `tests/agent/` 用例。

## 8. 风险与对策

| 风险 | 对策 |
|---|---|
| LLM 成本失控（自动轮次 × token） | 三层预算（单轮/每日/全局）+ 默认关闭 + 用量进 run 表可审计 |
| 自动化互相触发成环 | automation 令牌禁调 `automations.*`；origin 因果链沿用 `via_agent` 标记，超限告警 |
| 「值守会话」误删数据 | mclaw 档位继承 ⚠ 契约（删除需本轮明确同意）；v1 工具面不含 bash |
| 服务器重启丢状态 | 全部状态在 SQLite（automation/run 两表），tick 幂等 |
| 打扰用户 | NO-OP 不推送；静默时段；全局开关默认关 |

## 9. 长期方向（不在本设计内）

- **事件触发**：订阅 `library.files.added` / `subscription.fulfilled` 等事件 → 触发自动化
  （复用 domain-events 可靠消费，注意防环：事件由自动化引起时应被 origin 过滤）。
- **与定时任务合流**：22 个确定性任务逐步允许「换成一段值守 prompt」。
- **成员级自动化**：待令牌降权落地后开放。

## 10. 参考实现锚点（当前 main @ 260f5bca）

| 用途 | 位置 |
|---|---|
| 会话执行复用 | `src/movieclaw_api/services/channel_agent.py:65`（run_agent）、`routes/agent.py:324`（session.start） |
| 工具档位 | `src/movieclaw_api/services/agent_toolkit.py:81`（profile_tools） |
| mclaw 工具硬闸 | `src/movieclaw_agent/tools/mclaw.py:67` |
| 定时任务注册 | `src/movieclaw_scheduler/registry.py`（@register_task），样例 `torrent_sync.py:487` |
| agent 令牌 | `src/movieclaw_api/services/auth.py:902` |
| 通知引擎 | `src/movieclaw_api/services/push/engine.py:62` |
| 管理员依赖 | `src/movieclaw_api/api/deps.py:199` |
