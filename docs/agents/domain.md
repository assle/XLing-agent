# Domain Docs

工程技能探索本项目时，应按以下规则读取领域文档。

## Before exploring, read these

- 根目录的 `CONTEXT.md`：项目术语表。
- `docs/adr/`：与当前修改区域相关的架构决策文档。
- 如果将来出现 `CONTEXT-MAP.md`，先通过它找到与当前任务相关的术语表。

文件不存在时静默继续，不提前创建空文件。只有真正确定新术语或重要决策时才创建相应内容。

## File structure

本项目采用单一上下文结构：

```text
/
├── CONTEXT.md
├── docs/
│   └── adr/
└── app/
```

## Use the glossary's vocabulary

问题标题、改造方案、测试名称和文档统一使用 `CONTEXT.md` 中的正式术语，不使用其中明确列为应避免的同义词。

需要的概念尚未出现在术语表中时，应先确认它是否属于本项目，再通过领域建模流程补充。

## Flag ADR conflicts

如果计划修改的内容与现有架构决策冲突，必须明确指出，不能静默覆盖。
