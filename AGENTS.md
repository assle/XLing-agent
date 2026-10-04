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

问题和产品需求统一存放在 GitHub 仓库 `assle/XLing-agent`。参见 `docs/agents/issue-tracker.md`。

### Triage labels

使用五个默认分类标签：`needs-triage`、`needs-info`、`ready-for-agent`、`ready-for-human`、`wontfix`。参见 `docs/agents/triage-labels.md`。

### Domain docs

采用单一上下文结构：根目录 `CONTEXT.md` 与 `docs/adr/`。参见 `docs/agents/domain.md`。
