# FacilityMind

> 基于 LangGraph 的多 Agent 设施管理智能运营系统 —— 会推理、有记忆、越用越聪明

[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![LangGraph](https://img.shields.io/badge/orchestration-LangGraph-purple.svg)](https://github.com/langchain-ai/langgraph)

***

## 它解决什么问题

物业设备报修是一条重复链路：**接报修 → 判原因 → 派工人 → 验质量 → 出报告**。
FacilityMind 用**多个有明确职责的 Agent + 状态机编排**把这条链路自动化，每一步决策都结合知识库、带记忆、能自我纠错。

***

## 核心亮点

| 亮点                            | 它强在哪                                                     |
| ----------------------------- | -------------------------------------------------------- |
| 🧠 **ReAct 推理 + Reflect 自纠错** | 每个诊断/派单节点内部是 Thought→Action→Observe→Reflect 循环，评分不达标自动重试 |
| 💾 **四维记忆反哺**                 | Qdrant 案例库 + Redis 供应商口碑分数 + 预防规则库 + 人工查证，越用越上手 |
| 🎯 **意图识别路由**                 | 接入层自动判别工单类型，决定走哪条处理链路                    |
| 🔌 **MCP Server 可扩展**         | 预留 MCP 协议接口，可接入外部 IoT / 工单 / 设备系统                        |

***

## 架构

### 1. 九节点闭环 + 四维记忆反哺

![核心闭环 + 记忆反哺](docs/architecture-pipeline.svg)

| 阶段                              | 做什么              | 读/写哪些记忆                                         |
| ------------------------------- | ---------------- | ----------------------------------------------- |
| **Intake 受理**                   | 意图识别     | 知识库缺失 → 转人工查证                                   |
| **Diagnose 诊断**                 | 根因分析        | 读 Redis 口碑分排序供应商；读 Qdrant 召回历史好案例               |
| **Dispatch 派单**                 | 匹配最优维保商          | 综合口碑 × 技能匹配 × 价格选出性价比最高的                   |
| **Approval 审批**                 | 人机协作确认 | 高成本派单触发审批，人做最终决定                                |
| **Technician 回传 / QA / Report** | 师傅反馈、质检校验、结案归档   | 写预防规则(preventions)、存案例向量(Qdrant)、更新供应商口碑(Redis) |

***

### 2. ReAct Agent 循环 + Reflect 自纠错

诊断节点内部是一个完整的 **ReAct 推理循环**：思考→行动→观察→反思。

![ReAct Agent + Reflect 自纠错循环](docs/architecture-react.svg)

- **Thought** — LLM 推理：这是什么类型的故障？该查什么？
- **Action** — 调工具：检索知识库、查 IoT 数据、算供应商排名
- **Observe** — 读取返回结果，校验完整性
- **Reflect** — 质量评分：结果靠谱吗？相关度够吗？

评分不达标 → **自动重试**（改写查询换一种方式查）；达标 → 输出。

***

### 3. 编排模式：中心架构+多Agent串行+单Agent内并行处理任务

![三种编排模式](docs/architecture-orchestration.svg)

| 模式       | 用途                                                | 状态                     |
| -------- | ------------------------------------------------- | ---------------------- |
| **场景路由** | 接入层按场景分流（电梯/暖通/清洁/能耗/安防），中心只路由不干活 | ✅ 意图识别已落地，多场景扩展随业务增长启用 |
| **多Agent串行** | 主流程生命周期（Intake→...→Report），强依赖顺序执行                | ✅ 已实现（9 节点） |
| **并行取证** | Diagnose 节点内并发查知识库 + 维保商库，再汇合              | 🔜 待 IoT 接入            |

***

## 为什么越用越方便

### 记忆系统

| 记忆层        | 存储                         | 作用                  | 反哺方式                       |
| ---------- | -------------------------- | ------------------- | -------------------------- |
| **案例向量库**  | Qdrant                     | 历史好/坏工单  | 诊断时语义召回相似案例，派单时参考历史解法      |
| **供应商口碑分** | Redis（实时）                  | 每个维保商的综合 | 派单时自动按性价比排序，结案后按质检结果更新分数   |
| **预防规则库**  | preventions | 从历史故障提炼的预警规则        | 诊断命中规则时主动提示"这类问题上次是因为 XXX" |
| **人工查证通道** | 人工审核                       | 处理知识库覆盖不到的新问题       | 人工答案写回 KB，下次同类问题不再转人工      |

### 供应商性价比优选

派单综合以下维度实时打分，输出**排序列表**供审批人选择，而非给一个黑盒结果：

`历史口碑 × 技能匹配度 × 价格竞争力 × 近期可用性`

***

## 技术栈

| 层        | 选型                                                        |
| -------- | --------------------------------------------------------- |
| Agent 引擎 | LangGraph                              |
| 向量模型     | 智谱 Embedding-3                                            |
| 向量数据库    | Qdrant（案例库）                                               |
| 实时存储     | Redis                                       |
| 扩展协议     | MCP Server                                    |
| 离线友好     | 无 LLM Key 时整套降级为规则库                                  |

***

## 项目结构

```
facilitymind/
├── graph.py                 # LangGraph（9 节点）
├── state.py                 # 状态
├── agents/                  # 各阶段 Agent
│   ├── intake.py            # 受理 + 意图识别入口
│   ├── diagnose.py          # 诊断
│   ├── dispatch.py          # 派单
│   ├── approval.py          # 审核
│   ├── technician_report.py # 师傅回传
│   ├── qa.py                # 质检
│   ├── reflect.py           # Reflect 反思 / 自纠错重试
│   └── report.py            # 结案报告
├── memory/                  # 记忆系统
│   ├── qdrant_cases.py      # 案例向量库
│   └── redis_vendor.py      # 供应商口碑分
├── mcp/                     # MCP Server
├── ingest/                  # 语音/文本接入
│   ├── gate.py              # 意图识别
│   └── voice.py             # 语音转写
├── knowledge.py             # 故障知识库
├── llm.py                   # 多模型路由
├── tools/                   # 工具集
├── eval.py                  # 量化评估
└── README.md
```

***

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 配置环境变量（LLM API Key、Qdrant/Redis 连接）
cp .env.example .env
# 编辑 .env 填入你的配置

#跑预置的所有工单并生成报告
python -m facilitymind.eval --all --out eval_report.md --json eval_report.json

#跑工单T-001
python -m facilitymind.eval --id T-001
```
***

## 路线图

- [x] 串行骨干闭环（9 节点 StateGraph）
- [x] ReAct Agent + Reflect 自纠错
- [x] 意图识别路由（route\_intent）
- [x] Qdrant 案例库 + Redis 口碑分
- [x] 供应商性价比排序
- [x] 预防规则引擎
- [x] MCP Server 接口
- [ ] 并行取证（IoT 传感器接入）
- [ ] 多场景 Supervisor 路由扩展
- [ ] 多租户支持

***

## License

MIT
