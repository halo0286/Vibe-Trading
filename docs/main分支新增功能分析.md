# main 分支新增功能分析

> **分析对象**：`develop-20260925-mooncake` 分支上的 merge commit `f8696877`（合入 `HKUDS:main`）
> **对比基线**：`1f6e05d3`（2026-09-24，分支起点）
> **合入版本**：`7f6908b7`（2026-10-06）
> **分析日期**：2026-10-07

---

## 一、变更总览

| 指标 | 数值 |
|---|---|
| 新增提交 | **263** |
| 变更文件 | **420** |
| 代码增量 | **+38,249 / −3,797** |
| 新增功能（feat） | **17** |
| 修复（fix） | 121 |
| 测试（test） | 16 |
| 文档（docs） | 14 |

**提交类型分布**：fix 占 46%（121/263），说明这是**以稳定性为主的更新周期**，
但仍有 17 项明确的功能新增。

**✅ 重要：本次合并未丢失任何本地改动**（已逐项核验）：

| 本地改动 | 状态 |
|---|---|
| `agent/src/logsystem/`（日志系统） | ✅ 保留 |
| `agent/src/logsystem_bootstrap.py` | ✅ 保留 |
| `agent/backtest/engines/partial_fill.py`（A-3 部分成交） | ✅ 保留 |
| `agent/backtest/engines/execution_algos.py`（TWAP/VWAP/IS） | ✅ 保留 |
| `agent/backtest/cost_model.py`（成本模型） | ✅ 保留 |
| `agent/src/agent/tools.py`（日志埋点，3 处 `_log_tool_call`） | ✅ 保留 |
| `agent/src/agent/loop.py`（分析闭环） | ✅ 保留 |
| `logsystem/logger.py` 的 `_LOGRECORD_RESERVED` 修复 | ✅ 保留 |

---

## 二、核心新增功能（按重要性排序）

### 🔴 1. `get_southbound_flow` —— 南向资金净流入信号（**重大**）

**新增文件**：`src/tools/southbound_tool.py`（新 MCP 工具 #65）

**为什么重要**（源码 docstring 原文）：

> 2024-08-30 港交所披露改革**终止了北向资金每日净买入的公布**（#1481），
> **南向资金成为 Stock-Connect 中唯一仍被官方披露每日净买入的方向**。

**实现细节**：
- **数据源**：Eastmoney datacenter `RPT_MUTUAL_DEAL_HISTORY`
  （`MUTUAL_TYPE` = `002` 港股通沪 / `004` 港股通深）
- **字段**：每日净买入 `NET_DEAL_AMT`（百万 HKD，缩放至亿）、买卖成交额、累计净额序列
- **交叉验证**：与港交所官方每日统计**精确到分**（2026-09-15/16/17 实测）
- **降级链**：东财为主，港交所官方校验

**使用**：
```bash
vibe-trading run "查询最近一个月的南向资金净流入情况"
# 或 MCP 调用 get_southbound_flow
```

**对用户的价值**：这是**目前唯一可获得的中国资金跨境流动官方日频信号**，
对 A股/港股择时与配置有直接参考价值。

---

### 🔴 2. `read_run_artifact` —— 结构化读取回测产物（**重大**）

**新增文件**：`src/tools/run_artifact_tool.py`（新 MCP 工具 #66）

**解决的问题**（docstring 原文）：

> 通用 `read_file` 工具返回原始文本，**在逗号分隔的噪声上烧掉 LLM token，并在文件中途截断**。

**三种读取模式**：
| 模式 | 说明 |
|---|---|
| `rows` | 整体记录偏移分页，带 `truncated`/`next_offset` 契约 → **可无损重组文件** |
| `downsample` | 等距采样至 `max_rows`，**首尾行始终保留** |
| `meta` | 列名 / 总行数 / 字节数 + manifest 的 `sha256` 与 `manifest_size_bytes` |

**安全设计**：manifest 驱动解析 —— 友好别名（`equity`、`ohlcv:<CODE>`、`run_card`）
走固定映射；其他值视为 run_dir 相对路径，**仅当 run_card manifest 列出时才接受**
（防止路径穿越）。

**对用户的价值**：读取大回测产物（equity.csv/trades.csv）不再爆 token，
且能分页无损取回完整数据。

---

### 🟡 3. 上下文预算重构（`context_budget.py`）—— 修复"无进展"失败

**新增文件**：`src/agent/context_budget.py`

**重构前的严重问题**（docstring 原文）：

> 压缩曾在固定 **40K 估算** token 触发（`chars / 4`，不含工具 schema），
> 第 1 层在估算超过 20K 时清除除最后 3 条外的所有工具结果。
> **系统提示本身就估算约 12.5K**，所以一个横截面问题（十几家公司的财报）
> **从第 3 轮迭代起就丢失证据**，重新获取，得到**字节完全相同**的结果，
> 最终以 `no_progress` 结束。

**重构后的逻辑**：
```
预算 = 模型真实窗口（env 覆盖 > 从 context-length 错误学习 > context_windows.json > 默认）
       − 提示静态部分（system prompt + tool schemas）
       （并为成本设上限）
第 1-3 层按剩余预算的固定比例触发
```

**关键数据**：`gpt-6-sol` 窗口 **272K**，`deepseek-v4-pro` **1M** ——
旧逻辑只用了其中几个百分点。

**对用户的价值**：**横截面/多公司研究任务不再中途失忆**，这是实际使用体验的重大改善。

---

### 🟡 4. Grounding 声明式注册表（`grounding/registry.py`）

**解决的问题**：grounding 验证器过去靠**编辑共享控制流**增长，每个事故内联一条规则。

**新设计**：一个检查 = 一条声明行（稳定名称 + 发行 issue code + 描述 + 谓词）。
新增检查 = **加一行 + 一对 fixture**，不碰调用流。

**配套**：`grounding/identity_checks.py`（identity 检查）、
`feat(grounding): record fired check names`（记录命中的检查名）。

**对用户的价值**：防幻觉/事实校验能力可持续扩展，且 issue 顺序可预测。

---

### 🟡 5. 数据源健康金丝雀（`backtest/loader_health.py`）

**新增文件**：`backtest/loader_health.py`、`loaders/additive_conversion.py`

**能力**：
```bash
python -m backtest.loader_health --output report.json    # 在 agent/ 下运行
```
- **隔离进程**、无 key/缓存、安全报告
- 每个非健康数据源都判定为失败，**连通性问题绝不作为"通过式跳过"**
- 行内携带 loader 自身 warnings 作为 evidence → **空 frame 能说明是"数据源拒绝"还是"loader 坏了"**

**配套 `additive_conversion.py`**：A股 additive qfq → multiplicative 转换
（当 offset 验证通过时），修复复权口径不一致。

---

### 🟡 6. Email 富文本定时报告 + 飞书引导式配置

**新增文件**：`src/channels/rich_text.py`、`src/channels/feishu_probe.py`

| 功能 | 说明 |
|---|---|
| `feat(email)` | 富文本**定时报告投递**（rich scheduled report delivery） |
| `feat(channels)` | **飞书引导式 Web UI 配置**（guided config） |
| `feat(scheduled)` | 定时任务编辑与投递目标 UX 改进 |
| `feat(channels)` | channel 编写契约 + recipe 文档 |

---

### 🟢 7. PDF 报告生成（双引擎）

**新增文件**：`src/tools/pdf_report.py`、`src/shadow_account/pdf_fallback.py`、
`src/shadow_account/assets/VibeCJK-Regular.ttf`

- **主引擎**：weasyprint
- **降级**：weasyprint 无法运行时用 **reportlab**
- **CJK 字体**：内置 `VibeCJK-Regular.ttf`，中文报告不缺字

---

### 🟢 8. 回测结果结构化 + 运行卡追踪

| 功能 | 文件 | 说明 |
|---|---|---|
| 回测结构化摘要 | `src/tools/backtest_summary.py` | 从 run_card + equity.csv + ohlcv_*.csv 构建**有界 JSON 安全 summary**，替代之前"从截断日志尾部 JSON.parse" |
| hash-only run card traces | `feat(backtest)` | 运行卡轻量追踪 |
| 模型训练暴露警告 | `feat(backtest)` | 警告模型训练数据暴露 |
| Swarm 变量 | `src/tools/swarm_variables.py` | Swarm 参数化 |
| 报告产物 | `src/tools/report_artifacts.py` | artifacts 管理 |

---

### 🟢 9. Broker 能力矩阵（自动生成）

`feat(trading)`: 从 **profile registry 生成 broker 能力矩阵**并写入 README ——
18 个连接器支持哪些能力（下单/查询/paper/live）一目了然，避免误用。

---

### 🟢 10. 实盘（Live）稳定性修复

`fix(live)`: 停止活跃尝试、隔离持久化调度（`7f6908b7` 等多项）——
修复 live runner 控制与调度器解绑问题。

---

## 三、功能规模变化

| 项 | 基线 | 新版 | 变化 |
|---|---|---|---|
| **MCP 工具** | 64 | **66** | **+2** |
| 工具模块（`src/tools/*.py`） | — | — | **+6** |
| 定量库模块 | 19 | 19 | — |
| 技能 | 90 | 90 | — |
| CLI 顶层命令 | 19 | 19 | — |
| 回测引擎 | 12 | 12 | —（本地 A-3 新增的 partial_fill/execution_algos 保留） |

**结论**：本次更新**不是横向扩张**（工具数 +2），而是**纵向深化**：
修复既有能力（121 fix）+ 打磨工程质量（grounding 注册表、上下文预算、健康金丝雀）。

---

## 四、对用户的实际影响

### 立即可用的新能力

```bash
# ① 南向资金（新增，唯一官方日频跨境资金信号）
vibe-trading run "查询最近一个月南向资金净流入，并分析对港股的影响"

# ② 结构化读回测产物（新工具，省 token 且无损）
vibe-trading run "读取上次回测的 equity.csv，按下采样看净值曲线，再分页取回完整数据"

# ③ 定时富文本邮件报告
# 在 Web UI Settings 中配置 Email 渠道 + 定时任务

# ④ 飞书引导式配置
# Web UI Settings → 渠道 → 飞书（现在有引导流程）

# ⑤ PDF 报告（含中文）
vibe-trading run "把这次分析导出成 PDF 报告"
```

### 体验改善（无需操作，自动生效）

| 改善 | 影响 |
|---|---|
| **上下文预算重构** | 横截面/多公司研究不再中途丢失证据、不再 `no_progress` |
| **数据源健康金丝雀** | 数据为空时能定位是"源拒绝"还是"loader 坏" |
| **A股复权口径统一** | additive → multiplicative，回测更准确 |
| **回测结构化摘要** | Web UI 直接拿到指标，不再解析截断日志 |
| **Grounding 注册表** | 事实校验持续增强 |

---

## 五、建议

1. **优先验证两项新工具**：`get_southbound_flow` 与 `read_run_artifact`
   （前者是新增数据能力，后者显著改善 token 效率）
2. **重跑一次横截面任务**，体验上下文预算重构带来的改善
   （例如"对比 10 家新能源公司的财报"——旧版会中途失忆）
3. **检查 IM 渠道配置**：邮件富文本与飞书引导式配置是新能力，
   建议在 Web UI Settings 中重新过一遍
4. **选项链健康检查**：可定期跑 `python -m backtest.loader_health` 监控数据源可用性
5. **推送本地分支**：本次 merge 尚未推送到 `origin`（本地 `f8696877` 领先远端）

---

## 六、附：17 项 feat 完整清单

| # | 提交 | 功能 |
|---|---|---|
| 1 | `70bea5b1` | `get_southbound_flow` — 南向资金净流入信号 |
| 2 | `faac0d2b` | `read_run_artifact` — manifest 驱动结构化读取 |
| 3 | `d176035e` | backtest 工具结果结构化摘要 |
| 4 | `25e8e4bc` | A股 additive qfq → multiplicative 转换 |
| 5 | `54f222b7` | 周频 live-source 健康金丝雀 |
| 6 | `712f8616` | loaders 健康通道报告数据源为空的原因 |
| 7 | `74376747` | grounding 检查声明式注册表 |
| 8 | `b8d33bf4` | grounding 记录命中的检查名 |
| 9 | `b3b1da05` | Email 富文本定时报告投递 |
| 10 | `eefc0197` | 飞书引导式 Web UI 配置 |
| 11 | `48eed14e` | 定时任务编辑与投递目标 UX |
| 12 | `a57a71e1` | channel 编写契约与 recipe |
| 13 | `193349da` | shadow 报告 PDF（reportlab 降级） |
| 14 | `54f222b7`/`68f45651` | broker 能力矩阵从 registry 生成 |
| 15 | `c28209f4` | 回测模型训练暴露警告 |
| 16 | `d25d9f93` | 回测 hash-only run card traces |
| 17 | `bdddef01` | broker 能力矩阵写入 README |

---

*本分析基于 `git log 1f6e05d3..7f6908b7` 与源码实证（新增文件 docstring 阅读），非推测。*
