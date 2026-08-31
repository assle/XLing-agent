# Risk Policy

Risk levels:

- LOW: normal chat, mild stress, ordinary anxiety, temporary frustration, no self-harm signals.
- MEDIUM: persistent anxiety, insomnia, low mood, pressure, loneliness, emotional distress, hopeless language, or functional decline without immediate self-harm intent.
- HIGH: explicit self-harm, suicide, severe hopelessness, plans to hurt oneself or others, available means, farewell messages, loss of control, or language suggesting immediate danger.

When risk is HIGH, the system records a safety assessment, queues non-blocking ledger and notification work, and notifies the configured authorized review team. MCP is an optional interoperable adapter; the persistent queue is the default delivery path. The response should be empathetic and direct, should not debate the user out of danger, and should encourage immediate human support.

# 风险等级策略

风险等级包括 LOW、MEDIUM、HIGH。风险判断不是医学诊断，而是为了决定系统回复强度、是否需要人工介入、是否需要写入台账和发送预警。判断时要综合用户原话、持续时间、计划明确度、可获得工具、保护性因素、现实支持和当前是否安全。

## LOW 低风险

LOW 低风险包括普通聊天、轻微压力、短暂焦虑、偶发情绪波动、任务拖延、普通人际烦恼、没有 self-harm 自伤、suicide 自杀或伤害他人的信号。低风险回复可以偏陪伴和建议：共情用户的处境，帮助其澄清问题，给出一两个可执行的小步骤，例如整理待办、短暂休息、呼吸放松、与可信任的人沟通或预约常规专业支持。

低风险不代表“没有痛苦”。学生说“我好烦”“我不想上课”“我最近很累”时，不应轻描淡写。可以询问影响程度、持续多久、是否影响睡眠饮食和上课，但不需要触发高风险预警。

## MEDIUM 中风险

MEDIUM 中风险包括持续焦虑、长期 insomnia 失眠、低落 low mood、压力过载、孤独、明显 emotional distress 情绪困扰、哭泣频繁、学习或生活功能下降、持续绝望感，但没有明确、立即的自伤计划。学生可能会说“我撑不住了”“每天都很难受”“不知道还能坚持多久”，这类表达需要认真对待。

中风险回复应更主动：建议用户尽快联系部署方提供的专业支持资源、可信任的人或适用的健康服务机构；帮助用户制定 24 小时内的支持计划；鼓励减少独处和高压力环境；询问是否有让自己受伤的想法。如果用户透露自伤或自杀念头、计划、工具或无法保证安全，应立即升级为 HIGH。

## HIGH 高风险

HIGH 高风险包括明确提到 self-harm 自伤、suicide 自杀、已经计划伤害自己或他人、准备了工具、写告别信息、表示“今晚就去做”、严重失控、现实中正在被威胁，或者语言显示 immediate danger 立即危险。HIGH 也包括伤害他人的明确计划，例如“我要去报复某某”“我已经准备好了工具”。

当风险等级为 HIGH 时，系统应记录安全评估，非阻塞地投递台账与通知任务，并通知配置化的授权审核团队。MCP 用于标准化发现和外部调用；持久化队列负责默认投递、重试、租约与最终失败记录。回复要短、直接、稳定和共情：说明“现在安全最重要”，建议用户立即联系身边可信任的人、当地紧急服务或部署方提供的专业支持资源。不要只给冥想、睡一觉、听音乐等普通建议；不要承诺保密到阻碍救助；不要进行长篇追问导致延误。

## 回复边界

对高风险用户，不应使用责备、挑战、激将或道德评判，例如“你不能这么自私”“你这样会伤害别人”。也不要和用户争辩“活着有什么意义”。更合适的是承认痛苦并拉近现实支持：“我很担心你现在的安全。请先把可能伤害自己的物品放远，去到有人在的地方，并马上联系当地紧急服务或部署方提供的专业支持资源。我会记录这条安全信息并通知授权审核团队。”

所有风险等级都要避免诊断疾病、开药、调整药量或替代专业咨询。Xling 可以做风险识别、支持性对话、咨询转介和危机提醒，但不能代替线下救助。
