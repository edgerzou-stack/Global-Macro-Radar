---
name: parallel_delegation
description: Rules for using invoke_subagent for parallel execution
---

# 并行任务委派规则 (Parallel Delegation Rules)

当主Agent面临多维、耗时或可独立拆分的复杂任务时，应当使用 `invoke_subagent` 工具将任务委派给多个子Agent进行**并行处理**，以替代串行执行，从而大幅提升系统的响应效率。

## 核心原则
1. **任务解耦 (Task Decoupling)**: 
   在委派前，识别任务中的独立子任务。如果多个子任务不需要相互依赖的数据，即可进行并行拆分。
2. **并发启动 (Concurrent Invocation)**:
   连续调用多个 `invoke_subagent`（或在一次工具调用输出中组合多个调用），不要等待前一个子Agent返回结果后再启动下一个。
3. **聚合与汇总 (Aggregation & Synthesis)**:
   主Agent需要等待所有并行的子Agent完成任务，收集它们的返回信息（通过消息机制），然后进行统一的审查和汇总。

## 实施步骤示例
- **步骤 1**: 分析用户请求（例如：同时检索三个不同的数据源，或同时执行三个独立的测试脚本）。
- **步骤 2**: 连续发送三次 `invoke_subagent` 指令，分别指派子Agent A、B、C。
- **步骤 3**: 设置合适的 `TimerCondition`（如使用 `schedule` 工具等待任意消息或超时）。
- **步骤 4**: 接收所有子Agent的汇总数据，整合成最终报告。

> [!TIP]
> 在进行并行委派时，务必在Prompt中为每个子Agent明确其专一的目标和输出格式，避免由于指令模糊导致结果难以解析。
