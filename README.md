# FacilityMind

> 会「可追溯、可干预」的设施报修大脑 · 基于 LangGraph 的多智能体运营中枢
>
> A multi-agent operations center for facility & property management.

[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![LangGraph](https://img.shields.io/badge/orchestration-LangGraph-purple.svg)](https://github.com/langchain-ai/langgraph)

---

## 它解决什么问题

物业设施管理里大量工作是「接报修 → 判原因 → 派工人 → 验质量 → 出报告」的重复链路。
单 Agent 聊天机器人解决不了跨系统、跨专业的闭环；FacilityMind 用**多个有明确职责的 Agent + 状态机编排**，
把这条链路自动化、可追溯、可干预——而且诊断不是拍脑袋，是**结合知识库与大模型推理**做的。

---

## 30 秒看懂（直接看效果）

```bash
pip install -r requirements.txt
python -m facilitymind.web          # 启动后打开 http://127.0.0.1:8000
```

打开网页 → 选一张示例工单 → 点「运行」，你会看到：

1. **6 个 Agent 节点依次点亮**：受理 → 诊断 → 派单 → 审批 → 质检 → 报告
2. **诊断阶段结合故障知识库与 LLM 推理**给出可解释的根因与处置建议
3. **高成本派单弹出审批卡**：点「批准 / 驳回」，流程继续或终止
4. **结案面板汇总**诊断 / 派单 / 质检结论与可执行优化建议

> 一句话：这是一个**会自己查证、可追溯、可干预**的设施报修系统。
> 当前内置 20 张示例工单，开箱即跑；无 API Key 也能跑（自动走规则回退）。

<!-- 演示视频 / GIF 占位：把录好的短片放在 docs/demo.gif 或贴 YouTube 链接，访客 3 秒抓住重点 -->

---

## 核心亮点

| 亮点 | 它强在哪 |
| ---- | -------- |
| 🛡️ **可控 + 离线友好** | 高价值派单设一道人工闸（Human-in-the-Loop），成本超阈值才等人批；无 LLM Key 时整套降级为规则库，照样闭环 |
| 🤝 **多模型协作** | Model Registry 按 Agent 路由不同模型，无 Key 自动降级规则库；按模型拆分 Token / 成本 |
| 🔍 **全流程可观测** | 标准 logging 双路输出（CLI 控制台 + 文件落盘），节点耗时 / LLM tokens / 诊断结论清晰可读 |

---

## 架构

```mermaid
graph LR
    A[Intake 受理] --> B[Diagnose 诊断]
    B --> C[Dispatch 派单]
    C --> D{Approval<br/>人工闸}
    D -->|批准| E[QA 质检]
    D -->|驳回| X[(终止)]
    E --> F[Report 报告]
```

| Agent | 职责 |
| ----- | ---- |
| **Intake** | 把报修文本结构化为工单（类型 / 紧急度 / 位置） |
| **Diagnose** | 结合故障知识库 + LLM 推理，输出根因、处置、成本、SLA |
| **Dispatch** | 按技能匹配资源池，输出最优派单方案 |
| **Approval** | Human-in-the-Loop：成本超阈值时 `interrupt()` 暂停，等人工批准 / 驳回 |
| **QA** | 模拟现场执行，对照检查清单逐项核验，输出通过与综合评分 |
| **Report** | 汇总全线结论，生成结案摘要与可执行优化建议 |

---

## 快速开始

```bash
cd facilitymind
pip install -r requirements.txt
python -m facilitymind.web          # 默认 http://127.0.0.1:8000
```

- **不填任何 Key**：引擎自动走规则模式，开箱即跑。
- **想用大模型**：复制 `.env.example` 为 `.env`，填入兼容 OpenAI 的接口（已内置 DeepSeek / 通义千问 Qwen / 智谱 GLM / 本地 Ollama），填了即自动启用，任意 API 错误都安全回退规则库。

---

## 能力一览

| 能力 | 说明 |
| ---- | ---- |
| 🤝 多模型协作 | Model Registry 按 Agent 路由不同模型；无 Key 自动降级规则库；按模型拆分 Token / 成本 |
| 📚 内置知识库 | 8 类常见设施故障的处理经验 + 每类 QA 检查清单，离线可用 |
| ✅ 评估 Harness | 一键批量跑工单，量化完成率 / QA 通过率 / SLA / 成本 / Token，输出 Markdown + JSON |
| 🖥️ Web Dashboard | 浏览器内看 6-Agent 实时点亮、网页内人工确认、看评估图表 |

<details>
<summary><b>进阶：命令行 / 评估（点开）</b></summary>

**命令行跑单条工单**

```bash
python -m facilitymind.cli --id T-001                 # 成本超阈值会触发终端内人工确认
python -m facilitymind.cli --id T-001 --auto          # 跳过人工确认
python -m facilitymind.cli --id T-001 --compare      # 对比规则库 vs LLM 结论
```

**批量评估**

```bash
python -m facilitymind.eval --all --out eval_report.md --json eval_report.json
```

</details>

---

## 技术栈

- **编排**：LangGraph 状态机（节点可审计、可重放、interrupt 暂停/恢复）
- **Web**：FastAPI + SSE 实时流 + 原生 JS（零前端构建）
- **LLM 层**：可插拔 Model Registry（DeepSeek / Qwen / 智谱 / Ollama），无 Key 自动降级规则库
- **可观测**：标准 logging 模块（CLI 控制台 + 文件落盘，双路）

---

## Roadmap

- [x] 6-Agent 闭环（受理 → 诊断 → 派单 → 审批 → 质检 → 报告）
- [x] Human-in-the-Loop 审批、QA、报告
- [x] 评估 Harness、Web Dashboard
- [x] 多模型协作（按 Agent 路由）、标准 logging 可观测
- [ ] 经验记忆层（基于 Redis / Qdrant 重建）
- [ ] ReAct 自主诊断循环
- [ ] 多场景扩展（能耗优化 / 预防性保养）、对接 CMMS / ERP / IM、对话式 Intake 与长上下文压缩

## License

[MIT](LICENSE)
