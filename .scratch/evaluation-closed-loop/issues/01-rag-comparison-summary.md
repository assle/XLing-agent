Status: ready-for-agent

## Parent

`.scratch/evaluation-closed-loop/spec.md`

## What to build

在现有 RAG 评测 runner 基础上新增对比汇总能力。新增 `build_comparison()` 函数，读取 baseline / multi-query / hybrid-rrf / llm-rerank 四种策略的 summary JSON 文件，将 5 个指标（recallAtK / precisionAtK / mrr / ndcgAtK / hitRate）并排排列，计算最强策略相对 baseline 的 delta，输出两份产物：一份 JSON 汇总（程序可读）和一份 Markdown 对比表（人类可读，含指标列、策略行、delta 行）。

对比汇总函数优先读取已生成的 summary 文件；若某策略的 summary 不存在，在对比表中标注"未运行"而非报错。

新增两个配置项到 `app/core/config.py` 的 `Settings` 类：对比 JSON 输出路径和对比 Markdown 输出路径，通过 `.env` 配置，风格与现有 `rag_eval_*` 配置项一致。

runner 的 `__main__` 入口新增 `comparison` 子命令，运行对比汇总并输出两份产物。

## Acceptance criteria

- [ ] `build_comparison()` 函数能读取 4 份 summary JSON，输出含 5 个指标并排 + delta 的结构
- [ ] 缺失的 summary 文件在对比表中标注"未运行"，不报错
- [ ] JSON 汇总输出到配置路径，结构正确
- [ ] Markdown 对比表输出到配置路径，含表头、4 策略行、delta 行
- [ ] 新增配置项加入 `Settings` 类，有合理默认值，通过 `.env` 可覆盖
- [ ] `python -m app.rag_eval.runner comparison` 能运行对比汇总
- [ ] 测试用 4 份已知 summary JSON 验证对比和 delta 计算的正确性（纯函数，不需要 LLM）
- [ ] 测试验证缺失 summary 时的降级行为
- [ ] 现有 RAG eval 测试不受影响

## Blocked by

None - can start immediately
