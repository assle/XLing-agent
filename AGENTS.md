# AGENTS.md

## 回答风格

- 回答必须通俗易懂，面向非技术背景的读者。
- 不得无必要地使用缩写、专业术语或英文词汇。
- 如果确实需要使用缩写、专业术语或英文，必须在第一次出现时用括号给出简短易懂的解释。
  例如：使用 CBT（认知行为疗法，一种通过改变想法来改善情绪的心理方法）时须解释。

## Agent skills

### Issue tracker

问题和产品需求统一存放在 GitHub 仓库 `assle/XLing-agent`。参见 `docs/agents/issue-tracker.md`。

### Triage labels

使用五个默认分类标签：`needs-triage`、`needs-info`、`ready-for-agent`、`ready-for-human`、`wontfix`。参见 `docs/agents/triage-labels.md`。

### Domain docs

采用单一上下文结构：根目录 `CONTEXT.md` 与 `docs/adr/`。参见 `docs/agents/domain.md`。
