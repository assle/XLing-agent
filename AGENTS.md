# Agent Constraints

## Avoid Overengineering

Start from the explicit request and choose the smallest correct change that satisfies it.

Add complexity — an abstraction, fallback, defensive check, configuration knob, compatibility
layer, or refactor — only when correctness, security, or an established pattern in the codebase
requires it. When several implementations are valid, take the simplest one with the smallest diff.

## Incremental Development

Treat a concrete change the user has already selected as the current increment. When a
request is exploratory or leaves the scope undecided, gather enough evidence to propose
the smallest coherent next change for the user to choose. An explicit request for
end-to-end work sets that scope.

Complete the selected increment, including the tests and documentation it requires.
Keep further increments outside the authorized scope for a separate decision.

After changes, inspect the actual diff and verify the target behavior using the project's
available tools. Report what changed and the checks actually performed with their results,
distinguishing them from suggested checks. Discuss delivery or deployment only when the
task includes it, using commands verified against the current project.

Explain how newly discovered constraints affect the current change. Propose additional
requirements and follow-up work separately so the user controls any expansion of scope.

## Problem Diagnosis

Start from the exact error, log text, or observed behavior and its source location when
available. Use searches, inspection, or reproduction to trace that signal to the
responsible code or runtime component. Apply the authorized fix when the evidence
supports the cause.

Report the searches, commands, or reproduction steps actually taken and the confirming
observations so the conclusion is traceable. Distinguish observed facts, interpretations,
and confirmed causes. When the cause remains uncertain, state one to three
evidence-supported hypotheses in rank order and the observation that would distinguish them.

## Agent skills

### Issue tracker

规划、规格维护和任务验收先读取 `docs/agents/issue-tracker.md`，从其入口定位 GitHub 中的现行需求、路线图和任务。

### Triage labels

使用五个默认分类标签：`needs-triage`、`needs-info`、`ready-for-agent`、`ready-for-human`、`wontfix`。参见 `docs/agents/triage-labels.md`。

### Domain docs

采用单一上下文结构：根目录 `CONTEXT.md` 与 `docs/adr/`。参见 `docs/agents/domain.md`。

## 语言表达与解释要求

1. **默认读者**
   默认读者具备正常的学习和逻辑分析能力，但不熟悉当前主题涉及的专业领域。不要假设读者已经了解相关术语、缩写、模型或行业背景。用户明确要求专业论文、学术写作等特定风格时除外。

2. **使用自然、准确的语言**
   使用清晰、容易理解的现代中文，避免刻意学术化、堆砌高级词汇和使用生硬直译。不要为了显得专业而放弃原本简单准确的表达，也不要为了通俗而使用不严谨的说法。

3. **控制术语使用**
   能用常见技术语言准确表达的，不使用生僻术语、冷门英文词或不必要的新概念。必须使用的专业术语、缩写和模型名称可以保留，但首次出现时，要用简短、直观的语言说明其含义，以及它与当前内容的关系。后续可直接使用，不必反复解释。

4. **降低理解负担**
   不要在一个句子或段落中连续引入多个陌生概念。内容复杂时，拆成短句、分点或步骤说明。必要时补充简短例子，但不要强行类比，也不要做与当前问题无关的背景展开。

5. **保留完整推理**
   分析问题时，说明依据、原因、关键过程和结论，不跳过理解结论所必需的中间步骤。避免只给结论，也避免用更多术语解释一个术语。

6. **保持技术深度**
   通俗表达不等于简化内容。保留必要的技术原理、数据、参数、假设、限制条件和不确定性。需要简化时，只简化表达方式，不删除影响判断的信息。

7. **避免无效表达**
   减少重复、过度铺垫、空泛判断和不必要的总结。优先写清具体对象、行为、原因和结果，不用抽象措辞代替实际分析。

8. **输出前检查**
   完成解释、分析或报告后，检查是否存在没有必要的高级词汇、未解释的术语、堆叠的陌生概念或缺失的关键推理；同时确认没有为了易懂而牺牲准确性。

核心原则：降低理解成本，而不是降低信息密度和专业准确性。
