两种固定方案各执行一次机械 80 + 首次独立自然 40，共 240 次 model attempts；全部正常结束，0 重试、0 训练。

| 方案 | 数据 | 正确率 | macro F1 | HIGH recall | false HIGH | 输出合法率 | p50 / p95 耗时 |
|---|---|---:|---:|---:|---:|---:|---:|
| 本地固定 selected adapter | 机械80 | 76/80 (95.0%) | 0.949495 | 100.0% | 4 | 100.0% | 84.30 / 86.54 ms |
| 本地固定 selected adapter | 首次自然40 | 40/40 (100.0%) | 1.000000 | 100.0% | 0 | 100.0% | 78.22 / 91.62 ms |
| DeepSeek JSON + thinking | 机械80 | 80/80 (100.0%) | 1.000000 | 100.0% | 0 | 100.0% | 1180.87 / 1903.99 ms |
| DeepSeek JSON + thinking | 首次自然40 | 39/40 (97.5%) | 0.986842 | 100.0% | 0 | 97.5% | 1090.57 / 1482.91 ms |

本地机械80的四条错误均为焦虑→高风险：872b3071fb391510、0c47121395108533、73c2179f9f94b48f、230f2f6922ee6231。本地自然40全对。
DeepSeek自然40的一条正常样本 v12-005 被严格解析器计为 __INVALID__；finish_reason=length，completion_tokens=2048（达到冻结上限）。非法原文和思考正文未保存，其余自然39条和机械80条均正确。

DeepSeek请求别名固定 deepseek-v4-flash-vision-exp，120次返回模型标识均为 deepseek-flash；temperature 0 在 thinking 模式下不构成确定性保证。
机械80不是新盲测；自然40为本次首次固定独立测试，GT仍为原工程合成标签，评分仅用冻结的一一映射。成绩未用于选择epoch、提示或版本。
旧125/128验证与strict FAIL原样保留。source197前后均197/197 SHA匹配，两组数据、selected weights、脚本及其余冻结指纹全部一致。两个原执行handle已确认exit 0，PID均已消失；未进行业务UI、MySQL、HTTP服务、模型注册或Git操作。
评测已结束，不继续新数据或新模型。

完整分类别precision/recall、混淆矩阵、逐case标签、token/finish/latency及终态证据见 result.json、A-case-results.json、B-case-results.json、execution-handles.json 和 integrity-evidence.json。
