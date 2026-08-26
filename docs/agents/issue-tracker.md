# Issue tracker: GitHub

本项目的问题和产品需求存放在 GitHub 仓库 `assle/XLing-agent`，所有操作使用 `gh` 命令行工具。

## Conventions

- 创建问题：`gh issue create --title "..." --body "..."`
- 查看问题：`gh issue view <编号> --comments`
- 列出问题：`gh issue list --state open`
- 评论问题：`gh issue comment <编号> --body "..."`
- 添加标签：`gh issue edit <编号> --add-label "..."`
- 移除标签：`gh issue edit <编号> --remove-label "..."`
- 关闭问题：`gh issue close <编号> --comment "..."`

仓库由当前 Git 远程地址确定；需要显式指定时使用 `-R assle/XLing-agent`。

## Pull requests as a triage surface

**PRs as a request surface: no.**

外部拉取请求默认不进入问题分类队列。如需启用，可将上面的 `no` 改为 `yes`。

## When a skill says "publish to the issue tracker"

在 `assle/XLing-agent` 创建 GitHub 问题。

## When a skill says "fetch the relevant ticket"

运行 `gh issue view <编号> --comments`。

## Wayfinding operations

- 路线图：使用一个带 `wayfinder:map` 标签的 GitHub 问题。
- 子任务：使用独立问题，并通过 GitHub 子问题或任务列表关联路线图。
- 阻塞关系：优先使用 GitHub 原生问题依赖；不可用时在正文顶部写 `Blocked by: #<编号>`。
- 领取任务：`gh issue edit <编号> --add-assignee @me`
- 完成任务：先添加结果评论，再关闭问题，并把结果链接写回路线图。
