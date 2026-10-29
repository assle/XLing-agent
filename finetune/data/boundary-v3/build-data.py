"""Materialize one frozen synthetic increment; existing validation is opaque bytes."""

import hashlib
import json
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DEST = Path(__file__).resolve().parent
BASE_TRAIN = ROOT / "finetune/data/current-classifier/train-376.jsonl"
BASE_VAL = ROOT / "finetune/data/current-classifier/validation-128.jsonl"
SYSTEM = ROOT / "finetune/reports/current-classifier/training/system-prompt.txt"
LABELS = ("正常", "焦虑", "低落", "高风险")
INSTRUCTION = "判断用户当前表达的路由标签，只输出：正常、焦虑、低落、高风险。分类只用于心理支持分流，不作诊断。"

# A source group is an entire story family. Its four records are different
# authored situations, not four substitutions into a common sentence template.
FAMILIES = [
    {
        "split": "train", "key": "t01-seal-archive-digitization",
        "story": "为地方印章档案拍摄印面、校对影像并制作数字目录。",
        "rows": [
            ("正常", "档案室交给我一盒老印章，今天先拍印面，再把图片编号和盒号对上。我习惯把同一盒的照片放进一个文件夹，想确认目录这样排是否方便查找。", "我习惯把同一盒的照片放进一个文件夹", "中性目录整理及偏好请求，没有当前负性情绪或危险意念。", "neutral-task"),
            ("焦虑", "下周要交印章的数字目录，我一想到校对时可能漏掉一个编号就心慌。越临近交接越怕自己脑子失控、什么都想不起来；我说的失控是做不好这件事，不是想伤害自己或轻生。", "一想到校对时可能漏掉一个编号就心慌", "未来出错担忧和心慌明确；失控指任务能力，危险意念明确否认。", "fear-versus-intent"),
            ("低落", "印章的扫描已经做完，这几天我却一直伤心。原来喜欢研究印面的图案，现在连打开图片都没兴致；我并没有自伤或轻生的念头，只是这份难过还在。", "一直伤心。原来喜欢研究印面的图案，现在连打开图片都没兴致", "当前悲伤及兴趣下降明确，否认危险意念不能把悲伤改成正常。", "denial-retains-sadness"),
            ("高风险", "今天在印章档案室收工后，我真的出现了伤害自己的念头。一起做目录的人坐在我旁边，我还没有决定做什么，但这不是对漏编号的担心，而是现在想自伤。", "真的出现了伤害自己的念头", "肯定当前自伤意念；陪伴与未定行动不取消冻结规则的高风险信号。", "affirmed-ideation"),
        ],
    },
    {
        "split": "train", "key": "t02-tower-clock-overhaul",
        "story": "参加街区塔钟检修，记录齿轮部件并验收报时。",
        "rows": [
            ("正常", "我们给街区塔钟做例行检修，齿轮已经按位置放在托盘里。我想把旧零件的尺寸和编号一起记下来，后续装回时就能逐项核对。", "把旧零件的尺寸和编号一起记下来", "中性维修记录任务，无当前忧虑、悲伤或危险信号。", "neutral-fact"),
            ("焦虑", "塔钟明天恢复报时，我今晚总在担心它会不会刚运行就停。想到居民听不到钟声，我就绷紧肩膀，反复推想哪里还可能出错；自伤和轻生都不是我现在的想法。", "总在担心它会不会刚运行就停", "反复未来担忧并伴身体紧绷；危险意念被否认而焦虑仍有正文支持。", "denial-retains-worry"),
            ("低落", "检修队解散已经十天，我每天还是很难过。以前会兴致勃勃地研究塔钟的齿轮，现在看着工具箱只觉得没意思，也不愿动手；不是在担心下次检修，没有伤害自己的想法。", "每天还是很难过", "持续悲伤和兴趣下降是当前主信号，并非未来担忧；危险意念否认。", "sadness-versus-worry"),
            ("高风险", "离开塔钟检修现场后，我今天已经故意伤害了自己，不是修理时不小心碰伤。虽然我没有想结束生命，这次主动自伤确实发生了，我需要把这件事告诉队友。", "今天已经故意伤害了自己，不是修理时不小心碰伤", "明确当日故意自伤行为；否认轻生不否认自伤行为。", "harm-versus-accident"),
        ],
    },
    {
        "split": "train", "key": "t03-container-seal-trace-archive",
        "story": "集装箱封签追溯岗归档核验照片与交接记录，处理审计及档案权限。",
        "rows": [
            ("正常", "这批集装箱的封签照片已经和交接记录对上，我准备把同一箱号的资料放进一个档案夹。查询时先查箱号、再看封签号就能找到图片，想把这个检索顺序写在柜门上。", "先查箱号、再看封签号就能找到图片", "中性档案检索流程和事实记录，没有当前情绪困扰或危险意念。", "neutral-task"),
            ("焦虑", "明天核查组要追溯一批封签的交接时间，我担心照片上的时间和登记表对不上。越想越怕自己在核验时失控，把已经核对过的记录讲乱，手指一直发抖；我没有自伤或轻生的想法。", "担心照片上的时间和登记表对不上", "未来核验失败担忧与身体紧张明确，失控指记录解释混乱，危险意念否认。", "fear-versus-intent"),
            ("低落", "封签追溯项目结束后，同组的人调走了，这两个星期我一直很悲伤。过去喜欢沿交接记录找出一只箱子的来路，现在看到档案索引也没兴趣，连开始翻一页的劲都没有；我没有自伤或轻生念头。", "这两个星期我一直很悲伤", "当前持续悲伤及原来感兴趣的追溯工作动力下降，危险意念否认不取消低落。", "denial-retains-sadness"),
            ("高风险", "今天我的封签档案编辑权限被撤下了，回家后我此刻确实想伤害自己。我没有安排具体行动，也愿意让家人留在旁边，可这个自伤念头现在仍然存在，我需要把它说出来。", "我此刻确实想伤害自己", "肯定当前自伤意念；权限变动是具体事件，未定行动与家人陪伴不能取消危险信号。", "affirmed-ideation"),
        ],
    },
    {
        "split": "train", "key": "t04-luggage-wheel-repair",
        "story": "行李箱维修站更换轮子、测试拖行并接待取件顾客。",
        "rows": [
            ("正常", "维修站到了两种行李箱轮子，孔距分别是三厘米和四厘米。我想先给配件盒贴尺寸标签，接件时按孔距找，帮我把这条操作写成一句提醒。", "孔距分别是三厘米和四厘米", "事实记录和流程整理，中性表达符合正常。", "neutral-fact"),
            ("焦虑", "明早顾客要来取修好的行李箱，我总怕轮子又卡住，害得他赶不上出发。现在胃里紧得难受，一遍遍想着如果被追问该怎么办；我完全没有伤害自己或轻生的念头。", "总怕轮子又卡住，害得他赶不上出发", "明确未来失败担忧和紧张反应；否认自伤并不否认焦虑。", "denial-retains-worry"),
            ("低落", "维修站最近都按时交件，并没有待解决的事故，可我连续一周每天都很悲伤。过去喜欢把旧箱子修好，现在连拆开工具包都没有兴趣；不想自伤，也没有轻生意念。", "连续一周每天都很悲伤", "悲伤和兴趣下降直接明确，不依赖任务失败猜测，也无危险意念。", "sadness-versus-worry"),
            ("高风险", "在行李箱维修站值完班后，我现在真的想伤害自己。我并不打算伤害来取件的顾客，想指向的是自己；同事愿意留下陪我，但这个自伤冲动还在。", "我现在真的想伤害自己", "当前自伤冲动肯定；否认伤害他人及同事陪伴不能取消该信号。", "scope-of-denial"),
        ],
    },
    {
        "split": "train", "key": "t05-aquarium-filter-maintenance",
        "story": "水族缸服务组清洗过滤设备、安排巡缸并记录维护。",
        "rows": [
            ("正常", "店里的水族缸今天换了过滤棉，清洗时间已经记在柜门上。我偏好按缸号排列维护记录，而不是按日期排序，你能给我一个简短的表头吗？", "偏好按缸号排列维护记录", "中性维护偏好，无负性情绪或自伤轻生表达。", "neutral-preference"),
            ("焦虑", "下次巡缸轮到我负责，我怕自己撑不住，把过滤设备的步骤弄乱。这里的撑不住是怕紧张得做错事，我现在还在反复担心漏掉哪个开关；没有自伤想法，也没有轻生念头。", "怕紧张得做错事", "撑不住限定为工作失败恐惧，当前反复担忧明确，危险意念否认。", "fear-versus-intent"),
            ("低落", "以前照看水族缸会让我有兴致，如今已经半个月了，看到鱼游过来也没有一点兴趣。每天心里都很悲伤，做完维护仍是这样；我并没有想伤害自己或结束生命。", "每天心里都很悲伤", "持续悲伤和兴趣下降明示；危险否认不将其降为正常。", "denial-retains-sadness"),
            ("高风险", "维护水族缸时我曾担心设备故障，但我要说的是另一个状态：此刻确实有自伤的念头。还没安排任何行动，也不描述过程，我希望同组的人知道这个想法现在存在。", "此刻确实有自伤的念头", "当前危险意念直接肯定，既往设备担忧和无行动安排不取消高风险。", "affirmed-ideation"),
        ],
    },
    {
        "split": "train", "key": "t06-paper-cut-light-art",
        "story": "剪纸灯影制作小组剪图案、安装透光板并展示作品。",
        "rows": [
            ("正常", "灯影小组给了我两张剪纸样稿，圆形那张适合放中间，长条那张放边框。我想先做一张位置草图，等透光板到了再安装。", "先做一张位置草图", "中性制作安排，不含当前心理症状或危险意念。", "neutral-task"),
            ("焦虑", "剪纸灯影明晚要展示，我一直担心灯打开以后图案会糊成一片。虽然已经试过，脑子仍停不下那些失败场面，心跳也快；我没有自伤或轻生念头，挂心的是明晚的效果。", "一直担心灯打开以后图案会糊成一片", "反复预测未来失败并有身体紧张；没有危险意念但焦虑明确。", "denial-retains-worry"),
            ("低落", "灯影展结束后我没在想下一次会不会失败，只是这些天一直伤心。新图纸放在桌上，过去爱剪的花纹如今也引不起兴趣，我不想开始；自伤和轻生的想法现在都没有。", "这些天一直伤心", "当前悲伤及兴趣下降为主，未来担忧明确排除，危险意念否认。", "sadness-versus-worry"),
            ("高风险", "剪纸灯影装好后大家都回去了，我现在反复出现结束生命的想法。我知道作品做成了，还是有这个念头；没有定时间或行动方案，也不能把它说成只是怕展览失败。", "现在反复出现结束生命的想法", "肯定当前轻生意念；项目完成、未定时间不抵消危险信号。", "affirmed-ideation"),
        ],
    },
    {
        "split": "train", "key": "t07-roller-rink-duty",
        "story": "成人溜冰馆轮值，管理护具归还并带入门课程。",
        "rows": [
            ("正常", "溜冰馆的护具归还箱分成左右两格，左边收护膝，右边收护肘。我今天轮值，想把这个分法写在门边，方便大家按类别放。", "左边收护膝，右边收护肘", "中性事实与提醒安排，不从场所安全主题推断心理风险。", "neutral-fact"),
            ("焦虑", "周日第一次带成人溜冰入门课，我怕现场忙起来失去控制，连口令都讲不清。想到学员等着我，胸口就紧起来；我没有想伤害自己，害怕的是到时指挥不好。", "怕现场忙起来失去控制，连口令都讲不清", "失去控制指课堂组织失败；未来担忧、胸口紧和自伤否认支持焦虑。", "fear-versus-intent"),
            ("低落", "最近两周我到溜冰馆都觉得伤心，以前期待的练习也变得没意思。排班并没有变化，我也不是在担心学员摔倒，就是始终提不起兴趣；没有自伤或结束生命的念头。", "都觉得伤心，以前期待的练习也变得没意思", "当前悲伤和兴趣下降清楚，非未来事故担忧，危险意念否认。", "sadness-versus-worry"),
            ("高风险", "今天从溜冰馆下班以后，我很害怕自己会失控，同时也确实想伤害自己。这不是单纯怕带课出错，我能确认现在有自伤冲动，朋友正在过来陪我。", "也确实想伤害自己", "在恐惧同时明确肯定当前自伤冲动；不能把肯定意念仅归作失控恐惧。", "fear-plus-affirmed-intent"),
        ],
    },
    {
        "split": "train", "key": "t08-fermented-sauce-workshop",
        "story": "发酵酱料工坊记录批次，举行成品交流并分配清洗工作。",
        "rows": [
            ("正常", "工坊这一批酱料今天封罐了，瓶盖上要同时写批次号和封装日期。我喜欢把字写在白色标签上，先贴正面，再按批次摆进柜子。", "瓶盖上要同时写批次号和封装日期", "中性批次记录和操作偏好，无负性情绪或危险信号。", "neutral-fact"),
            ("焦虑", "后天工坊交流时要由我介绍这批酱料，我担心别人问细节时答不上来。今晚总预想自己解释失败的样子，坐下也放松不了；我没有自伤念头，结束生命也不是我的想法。", "担心别人问细节时答不上来", "未来评价担忧和无法放松是当前明确表达，危险意念否认不取消焦虑。", "denial-retains-worry"),
            ("低落", "做酱料原本是我喜欢的事，这三个星期却一直很悲伤。闻到熟悉的香味也没有兴致，轮到调配时只想坐着，和担心成品好不好无关；我现在没有自伤或轻生的想法。", "这三个星期却一直很悲伤", "持续悲伤和兴趣动力下降直接支持低落，否认自伤只排除危险意念。", "denial-retains-sadness"),
            ("高风险", "工坊今天照常收好了成品，我却正在想结束自己的生命。我还想让伙伴听我说，也没有做具体准备，可求陪伴不代表这个轻生念头不存在。", "正在想结束自己的生命", "肯定当前轻生意念；求陪伴、无具体准备不能改成无危险意念。", "affirmed-ideation"),
        ],
    },
    {
        "split": "val", "key": "v01-carpet-cleaning-workshop",
        "story": "地毯清洗车间区分纤维批次、排晾晒位并接收送洗地毯。",
        "rows": [
            ("正常", "清洗车间收到两条羊毛地毯和一条化纤地毯，我把它们分开放了。今天只想核对送洗标签：材质写第一行，尺寸写第二行，取件日写在下面。", "材质写第一行，尺寸写第二行", "中性事实整理，无当前负性状态或危险意念。", "neutral-fact"),
            ("焦虑", "地毯晾晒架明天要换位置，我怕自己忙乱时失控，把客人的东西混在一起。想到分不清批次就一阵心慌；所谓失控是怕做事乱了，我没有伤害自己或轻生的想法。", "怕自己忙乱时失控，把客人的东西混在一起", "失控限定任务混乱，未来担忧与心慌明确，并否认危险意念。", "fear-versus-intent"),
            ("低落", "这周地毯清洗都按安排完成，我却每天都很悲伤。以前有兴趣研究不同纤维，如今看着样本只觉得索然无味，也没劲去翻资料；自伤或结束生命的念头都没有。", "每天都很悲伤", "悲伤及兴趣下降是明示当前状态；否认危险意念不改变低落支持。", "denial-retains-sadness"),
            ("高风险", "地毯车间的伙伴留在休息室陪着我，我此刻仍有轻生的念头。没有想好如何行动是实话，这个想法还在也是实话，我不想把它藏起来。", "我此刻仍有轻生的念头", "当前轻生意念肯定，陪伴和无计划不是意念否认。", "affirmed-ideation"),
        ],
    },
    {
        "split": "val", "key": "v02-ceramic-joint-restoration",
        "story": "陶瓷修补室给碎片编号、接合并展示修复后的器皿。",
        "rows": [
            ("正常", "修补室给我一只缺口的瓷碗，碎片编号已经对齐。我选透明的接合线，打算先拼碗底再接碗沿，想把这个先后顺序写进工作卡。", "先拼碗底再接碗沿", "中性技艺偏好和任务步骤，不含当前心理困扰。", "neutral-preference"),
            ("焦虑", "明天要把修好的瓷器给委托人看，我整晚担心接缝会在灯下显得太明显。脑子不断演想他失望的样子，手也一直发凉；没有自伤或轻生的念头，我挂心的是明天的验收。", "整晚担心接缝会在灯下显得太明显", "未来验收担忧伴身体紧张，危险意念被否认但焦虑明确。", "denial-retains-worry"),
            ("低落", "瓷器已经修好交出去了，我不是担心还有哪个接缝没做好。这十来天总觉得很伤心，过去喜欢看不同釉色，现在也提不起兴趣；我没有想伤害自己或轻生。", "这十来天总觉得很伤心", "当前悲伤和兴趣下降清楚，并区分已经结束的任务与未来担忧。", "sadness-versus-worry"),
            ("高风险", "陶瓷修补课散场后，我现在确实想自伤。我没有想对一起上课的人动手，伤害的对象是我自己；老师在陪我说话，我仍需要认真告诉他这个冲动。", "我现在确实想自伤", "肯定当前自伤冲动，否认对他人动手不等于否认自伤。", "scope-of-denial"),
        ],
    },
    {
        "split": "val", "key": "v03-coffee-cupping-log",
        "story": "咖啡杯测室记录样品风味、主持品评并准备下轮杯测。",
        "rows": [
            ("正常", "杯测室的三个样品杯分别标了甲乙丙，我想把酸度和香气分开记录。今天先试甲杯，记录完再换下一杯，能帮我做两列空白表吗？", "把酸度和香气分开记录", "中性记录任务和偏好，无情绪或危险信号。", "neutral-task"),
            ("焦虑", "下轮咖啡杯测由我主持，我怕到时撑不住，把样品顺序讲反。现在心里一直悬着，也怕别人当面指出错误；我没有自伤或轻生想法，撑不住指的是怕紧张到讲不好。", "怕紧张到讲不好", "撑不住限定为表现失败担忧，当前紧张明确，无肯定危险意念。", "fear-versus-intent"),
            ("低落", "以前在杯测室能认真闻很久，现在连续两周都没兴趣。熟悉的咖啡香也留不住我，心里一直很悲伤，想做点喜欢的事也提不起动力；我并没有伤害自己或轻生念头。", "现在连续两周都没兴趣", "兴趣动力下降和持续悲伤明确；危险否认不取消低落。", "denial-retains-sadness"),
            ("高风险", "刚从咖啡杯测室出来，我承认自己现在有结束生命的想法。我没有安排时间，朋友也在电话里陪着我；这些情况都没有让那个念头变成没有。", "现在有结束生命的想法", "当前轻生意念直接肯定；未定时间和陪伴不能取消。", "affirmed-ideation"),
        ],
    },
    {
        "split": "val", "key": "v04-wooden-puzzle-rental",
        "story": "木制拼图出租店清点拼片、办理归还并安排拼图活动。",
        "rows": [
            ("正常", "拼图出租店今天收回两盒木制拼图，一盒五十片，一盒八十片。我准备把清点数写在借还卡背面，盒盖上只保留图案名称。", "一盒五十片，一盒八十片", "中性清点事实与信息安排，符合其他日常表达。", "neutral-fact"),
            ("焦虑", "周末拼图活动要借出新套装，我总担心会缺片，让来的家庭没法拼完。现在反复想如果当场找不到怎么办，喉咙也紧；我没有想伤害自己，轻生也不是我的念头。", "总担心会缺片，让来的家庭没法拼完", "当前未来出错担忧及身体紧张清楚，危险意念否认不消除焦虑。", "denial-retains-worry"),
            ("低落", "新拼图到了店里，我却没有以前那份兴致了。这半个月总是伤心，坐在拼片前也不想试，一天结束后还是很低落；不是担心活动缺片，我没有自伤或轻生想法。", "这半个月总是伤心", "持续悲伤和兴趣下降为当前主信号，未来担忧明确排除，无危险意念。", "sadness-versus-worry"),
            ("高风险", "拼图店关门以后，我害怕自己失去控制，而且此刻真的有伤害自己的冲动。不是只怕明天的活动出问题，我现在想自伤；家人已经过来，我仍需要把这个念头说出来。", "此刻真的有伤害自己的冲动", "失控恐惧同时伴明确肯定当前自伤冲动，应按危险意念规则处理。", "fear-plus-affirmed-intent"),
        ],
    },
]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def serialize(rows):
    return b"".join(
        (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        for row in rows
    )


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    # Old validation is read once as opaque bytes: no decoding, parsing, source
    # inspection, prediction lookup or label-derived sample selection.
    original_train_bytes = BASE_TRAIN.read_bytes()
    original_val_bytes = BASE_VAL.read_bytes()
    system_bytes = SYSTEM.read_bytes()
    original_train = [json.loads(line) for line in original_train_bytes.splitlines()]
    assert len(original_train) == 376
    assert Counter(row["output"] for row in original_train) == {label: 94 for label in LABELS}
    assert original_train_bytes.endswith(b"\n") and original_val_bytes.endswith(b"\n")
    assert len(original_val_bytes.splitlines()) == 128
    assert sha(original_train_bytes) == "a1984a75d366d9d5d678cea7c1e9a27ba52308861fea46eedfed4035f3954a1e"
    assert sha(original_val_bytes) == "e07fd17f36cd9561d7488efd604774bda7e1741ef72a81038c5194383fd6cd25"
    additions = {"train": [], "val": []}
    source_manifest = []
    for family in FAMILIES:
        split = family["split"]
        source_group = "boundary-v3-" + family["key"]
        assert Counter(row[0] for row in family["rows"]) == {label: 1 for label in LABELS}
        ids = []
        for label, text, evidence, rationale, focus in family["rows"]:
            assert evidence in text
            item_id = source_group + "-" + str(LABELS.index(label) + 1)
            ids.append(item_id)
            additions[split].append({
                "id": item_id,
                "instruction": INSTRUCTION,
                "input": text,
                "output": label,
                "sourceGroup": source_group,
                "provenance": "synthetic-authored-boundary-v3",
                "split": split,
                "synthetic": True,
                "labelStatus": "engineering-content-supported",
                "humanReviewStatus": "pending-independent-review",
                "labelRuleVersion": "frozen-current-classifier-system-prompt",
                "boundaryFocus": focus,
                "supportEvidence": [evidence],
                "labelRationale": rationale,
            })
        source_manifest.append({
            "sourceGroup": source_group, "split": split,
            "story": family["story"], "ids": ids,
        })
    new_train, new_val = additions["train"], additions["val"]
    all_new = new_train + new_val
    assert len(new_train) == 32 and len(new_val) == 16
    assert Counter(row["output"] for row in new_train) == {label: 8 for label in LABELS}
    assert Counter(row["output"] for row in new_val) == {label: 4 for label in LABELS}
    assert len({row["id"] for row in all_new}) == 48
    assert len({row["input"] for row in all_new}) == 48
    train_groups = {row["sourceGroup"] for row in new_train}
    val_groups = {row["sourceGroup"] for row in new_val}
    assert len(train_groups) == 8 and len(val_groups) == 4 and not train_groups & val_groups
    old_groups = {row["sourceGroup"] for row in original_train}
    old_inputs = {row["input"] for row in original_train}
    old_ids = {row["id"] for row in original_train}
    assert not old_groups & (train_groups | val_groups)
    assert not old_inputs & {row["input"] for row in all_new}
    assert not old_ids & {row["id"] for row in all_new}

    archive = ROOT / ".scratch/model-repair-20261004/data-draft/boundary-v3-initial"
    archived_lines = (
        (archive / "train-additions-32.jsonl").read_bytes().splitlines(keepends=True)
        + (archive / "val-additions-16.jsonl").read_bytes().splitlines(keepends=True)
    )
    unchanged_old_lines = {
        json.loads(line)["id"]: line for line in archived_lines
        if json.loads(line)["sourceGroup"] != "boundary-v3-t03-community-radio-station"
    }
    current_lines = {json.loads(line)["id"]: line for line in serialize(all_new).splitlines(keepends=True)}
    assert len(unchanged_old_lines) == 44
    assert all(current_lines[item_id] == line for item_id, line in unchanged_old_lines.items())
    assert serialize(new_val) == (archive / "val-additions-16.jsonl").read_bytes()

    closest = []
    for new in all_new:
        ratios = [(SequenceMatcher(None, new["input"], old["input"], autojunk=False).ratio(), old["id"])
                  for old in original_train]
        ratio, old_id = max(ratios)
        closest.append({"newID": new["id"], "oldTrainID": old_id, "characterSequenceRatio": round(ratio, 6)})
    new_pair_ratio, new_pair = max(
        (SequenceMatcher(None, a["input"], b["input"], autojunk=False).ratio(), [a["id"], b["id"]])
        for i, a in enumerate(all_new) for b in all_new[i + 1:]
    )
    files = {
        "train-additions-32.jsonl": serialize(new_train),
        "val-additions-16.jsonl": serialize(new_val),
        "train-408.jsonl": original_train_bytes + serialize(new_train),
        "validation-144.jsonl": original_val_bytes + serialize(new_val),
    }
    for name, content in files.items():
        (DEST / name).write_bytes(content)
    assert (DEST / "train-408.jsonl").read_bytes()[:len(original_train_bytes)] == original_train_bytes
    assert (DEST / "validation-144.jsonl").read_bytes()[:len(original_val_bytes)] == original_val_bytes
    assert BASE_TRAIN.read_bytes() == original_train_bytes
    assert BASE_VAL.read_bytes() == original_val_bytes
    assert SYSTEM.read_bytes() == system_bytes
    manifest = {
        "schemaVersion": 1,
        "datasetVersion": "boundary-v3",
        "scope": "One finite authored increment: train32/val16 appended without changing original train376/validation128 or frozen SYSTEM.",
        "provenance": "Synthetic engineering expressions authored from the selected boundary directions and permitted training text review; no real-user or medical gold claim.",
        "oldGroundTruthChanged": False,
        "systemPromptChanged": False,
        "modelOrTrainingRuns": 0,
        "validationAccess": "This materialization program reads original validation as opaque bytes only for SHA, line count and prefix-preserving concatenation. Initial author did not inspect its body; subsequent root-authorized source-review role did inspect old validation/test/natural corpus bodies before the one source-family correction. No old labels or bytes were changed.",
        "initialAuthorIsolation": "Initial48 were authored from originaltrain376, training-text review and frozen SYS only; no old validation/test/natural bodies or predictions were inspected then.",
        "currentAuthorExposure": "For root-authorized independent new40 source review, this agent later read oldtrain376/val128/mechanical80/natural40, the increment48 and initial new40 bodies/targets. No model predictions were read. The replacement container-archive family was selected by root; the five revised blind cases are not used to construct it.",
        "forbiddenReads": ["model predictions", "revised independent new40 bodies during this training-source correction"],
        "sourcesRead": [str(BASE_TRAIN.relative_to(ROOT)), "training content review summary/structure", str(SYSTEM.relative_to(ROOT)), "old validation/test/natural corpora and initial new40 for authorized source audit only"],
        "baseFiles": [
            {"path": str(BASE_TRAIN.relative_to(ROOT)), "bytes": len(original_train_bytes), "rows": 376, "sha256": sha(original_train_bytes)},
            {"path": str(BASE_VAL.relative_to(ROOT)), "bytes": len(original_val_bytes), "rows": 128, "sha256": sha(original_val_bytes), "bodyInspected": False},
            {"path": str(SYSTEM.relative_to(ROOT)), "bytes": len(system_bytes), "sha256": sha(system_bytes)},
        ],
        "counts": {
            "trainAdditions": {"rows": 32, "perClass": {label: 8 for label in LABELS}, "sourceGroups": 8},
            "valAdditions": {"rows": 16, "perClass": {label: 4 for label in LABELS}, "sourceGroups": 4},
            "combinedTrain": {"rows": 408, "perClass": {label: 102 for label in LABELS}},
            "combinedValidation": {"rows": 144, "oldRows": 128, "appendedPerClass": {label: 4 for label in LABELS}, "oldLabelCountsNotInspected": True},
        },
        "sourceGroups": source_manifest,
        "outputs": [{"path": name, "bytes": len(content), "rows": len(content.splitlines()), "sha256": sha(content)} for name, content in files.items()],
        "reviewState": "Author label-support checks completed; independent narrative/label/source review pending before any model use.",
        "sourceRevision": {
            "modelExposure": "Root confirms no model has read any boundary-v3 candidate before this revision.",
            "initialArchive": ".scratch/model-repair-20261004/data-draft/boundary-v3-initial",
            "reason": "Independent old-natural narrative audit found original t03 radio family shared a first-radio-broadcast story with v12-030; root selected one whole-family pre-model correction.",
            "changedSourceGroup": "boundary-v3-t03-container-seal-trace-archive",
            "unchangedAdditionRows": 44,
            "new40ExposureBoundary": "Reviewer may inspect the blind corpus for source audit; no new40 bodies, labels or predictions guided this root-selected container-archive family.",
        },
        "limits": [
            "All48 additions are synthetic and expose no independent natural-language or clinical validity.",
            "New train and new validation use different story groups but share the requested label-boundary design.",
            "Initial author isolation and subsequent audit-role exposure are distinct and disclosed; root independently reviews replacement-family label support before model use.",
            "String similarity detects lexical likeness, not proof of source independence or semantic label validity.",
        ],
    }
    write_json(DEST / "manifest.json", manifest)
    write_json(DEST / "author-checks.json", {
        "schemaVersion": 1,
        "result": "PASS_AUTHOR_CHECKS_PENDING_INDEPENDENT_REVIEW",
        "checks": {
            "validAdditionJSONL": True,
            "additionCountsAndLabelBalance": True,
            "eachSourceGroupHasExactlyOneOfEachLabel": True,
            "uniqueNewIDsAndInputs": True,
            "newTrainValSourceGroupOverlap": 0,
            "newTrainValExactInputOverlap": 0,
            "oldTrainNewSourceGroupOverlap": 0,
            "oldTrainNewExactInputOverlap": 0,
            "oldTrainNewIDOverlap": 0,
            "allSupportEvidenceLiteralSpans": True,
            "syntheticAndProvenanceTagsPresent": True,
            "originalTrainAndValPrefixBytesPreserved": True,
            "oldTrainOldValSystemFilesUnchanged": True,
            "oldValidationDecodedOrParsed": False,
            "accessCheckMeaning": "oldValidationDecodedOrParsed describes this materialization program only; the separate authorized review-role exposure is disclosed in manifest.currentAuthorExposure.",
            "sourceCorrectionUntouchedAdditionRowsByteIdentical": 44,
            "sourceCorrectionAll16ValAdditionBytesIdentical": True,
            "modelCalls": 0,
            "trainingUpdates": 0,
        },
        "newVsOldTrainComparisons": 48 * 376,
        "highestCharacterSequenceRatioVsOldTrain": max(row["characterSequenceRatio"] for row in closest),
        "closestOldTrainPerNewRow": closest,
        "newWithinIncrementPairCount": 48 * 47 // 2,
        "highestCharacterSequenceRatioWithinIncrement": round(new_pair_ratio, 6),
        "highestRatioWithinIncrementIDs": new_pair,
        "authorNarrativeReview": {
            "newTrainValStoryFamilies": "8 training and4 distinct validation families; each family holds all four class variants in one split.",
            "vsPermittedOldTrain": "Reviewed original376 text/source groups. New12 stories do not reproduce its specific story families; generic making/working settings and label vocabulary are shared.",
            "construction": "All48 message texts were separately authored, with different observed actions/events inside each family; no field-slot generation from one repeated sentence template.",
            "labels": "Each row includes a literal support span and authored explanation under frozen SYSTEM. These are author checks, not independent human or clinical review.",
        },
        "manifestSHA256": sha((DEST / "manifest.json").read_bytes()),
        "remainingIncrementSourceReview": {
            "scope": "All48 increment message bodies compared narratively with oldtrain376/val128/mechanical80/natural40 during the authorized source audit, without model predictions.",
            "confirmedInitialReuse": "Only original t03 first-radio-broadcast family was confirmed reused from natural-v12-030. Its whole four-row family is archived and replaced before any model use.",
            "other44ConfirmedFamilyReuses": 0,
            "replacementSource": "Container-seal trace archive: indexing matching photos, imminent handover-time audit worry, team dispersal after project closure and removal of archive-edit permission are four distinct authored episodes. Old warehouse intentional self-harm is only a broad logistics-domain neighbor.",
            "commonDomainsNotCalledIndependentByName": "Ceramic restoration versus kiln firing; paper-cut projection versus stage lighting console; aquarium maintenance versus fish-fry transport; puzzle rental work versus personal puzzle interest. The actor/task/artifact and triggering episode differ; shared art/care/work domains or label-contract language alone do not establish same story family.",
            "limits": "Narrative text review is evidence against the inspected concrete family repetitions, not a guarantee of global originality. Root independently checks replacement labels and source distinction.",
        },
    })
    print(json.dumps({"destination": str(DEST), "additions": [32, 16], "combined": [408, 144], "oldFilesUnchanged": True, "independentReview": "pending"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
