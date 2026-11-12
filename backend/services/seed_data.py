"""seed 测试数据：前端 src/data/tasks.ts 的 TASKS/PROJECTS 机械平移（117 任务 + 3 项目）。

字段刻意沿用前端形状，便于与原 mock 逐条人工对照：
- dateOffset：相对运行当天的天偏移（今天 = 0），由 seed.py 换算为绝对 naive 时间入库
- start / end：小时小数（14.5 = 14:30）
- priority：high / medium / low 字符串（入库转 INT）
- project：项目 name（入库转 project_id）
- recurrence：判别 dict（入库转 RRULE 字符串）
- minutes：预计耗时（分钟）；duration 展示文本与 mock 数字 id 不入库（前者前端派生，后者换 UUID）
"""

SEED_PROJECTS: list[dict] = [
    {"name": "Alpha Launch", "color": "var(--accent)"},
    {"name": "日常运营", "color": "var(--green)"},
    {"name": "学习提升", "color": "var(--orange)"},
]

SEED_TASKS: list[dict] = [
    # —— 已排期任务（原日历 mock；本周区 -6..+6 保证任意一天运行都有完整一周，
    #    月视图窗口扩展到约 -45..+58 覆盖前后翻月页；时段单位为小时，小数表分钟；
    #    重复日程为单条规则（基准日 + recurrence），日历经真展开在匹配日自动显示，不再逐日复制）——
    # —— 本周前三日（按运行日动态对位，如周五运行时 -4=周一；周一组刻意交叠演示周视图泳道并排）——
    {"dateOffset": -6, "title": "周计划梳理", "start": 9, "end": 10, "priority": "low", "recurrence": {"freq": "weekly", "weekdays": [1]}},
    {"dateOffset": -4, "title": "OKR 对齐会", "start": 10, "end": 11.5, "project": "Alpha Launch", "priority": "high"},
    {"dateOffset": -4, "title": "迭代排期", "start": 10.5, "end": 11.5, "project": "日常运营", "priority": "medium"},
    {"dateOffset": -4, "title": "健身房", "start": 18.5, "end": 19.5, "priority": "low"},
    {"dateOffset": -3, "title": "客户支持复盘", "start": 13, "end": 14, "project": "日常运营", "priority": "medium"},
    {"dateOffset": -3, "title": "深度工作：架构设计", "start": 15, "end": 17, "project": "日常运营", "priority": "high"},
    {"dateOffset": -3, "title": "晚餐聚会", "start": 18.5, "priority": "low"},
    {"dateOffset": -2, "title": "用户访谈", "start": 11, "end": 12, "project": "日常运营", "priority": "high"},
    {"dateOffset": -2, "title": "写作时间", "start": 16, "end": 17, "priority": "low", "recurrence": {"freq": "weekly", "weekdays": [5]}},
    {"dateOffset": -2, "title": "夜跑", "start": 19.5, "priority": "low"},
    # —— 日视图定稿演示数据（-1 昨天 / 0 今天 / 1 明天，逐字冻结）——
    {"dateOffset": -1, "title": "版本发布检查", "start": 10, "end": 11, "project": "日常运营", "priority": "high"},
    {"dateOffset": -1, "title": "需求评审", "start": 14, "end": 15.5, "project": "日常运营", "priority": "high"},
    {"dateOffset": -1, "title": "代码走查", "start": 16, "end": 17, "priority": "medium"},
    {"dateOffset": 0, "title": "产品迭代里程碑评审", "start": 10, "end": 11.5, "project": "Alpha Launch", "priority": "high"},
    {"dateOffset": 0, "title": "团队站会", "start": 14, "end": 15, "project": "日常运营", "priority": "high", "recurrence": {"freq": "daily"}},
    {"dateOffset": 0, "title": "技术预研", "start": 14.5, "end": 15.5, "project": "日常运营", "priority": "medium"},
    {"dateOffset": 0, "title": "撰写周报", "start": 15.5, "end": 16.5, "priority": "medium"},
    {"dateOffset": 0, "title": "设计对齐", "start": 16, "end": 17, "priority": "high"},
    {"dateOffset": 0, "title": "回复合作方邮件", "start": 17, "project": "日常运营", "priority": "medium"},
    {"dateOffset": 0, "title": "夜跑", "start": 19.5, "priority": "low"},
    {"dateOffset": 1, "title": "客户访谈", "start": 9.5, "end": 10.5, "project": "日常运营", "priority": "high"},
    {"dateOffset": 1, "title": "晨间复盘", "start": 9.75, "end": 10.75, "priority": "low"},
    {"dateOffset": 1, "title": "1v1 沟通", "start": 11, "end": 12, "priority": "high"},
    {"dateOffset": 1, "title": "设计评审", "start": 14.5, "end": 15.5, "project": "日常运营", "priority": "high"},
    {"dateOffset": 1, "title": "阅读时光", "start": 20.5, "end": 21.5, "priority": "low"},
    # —— 本周日（周末轻量）——
    {"dateOffset": 2, "title": "整理下周计划", "start": 15, "end": 16, "priority": "low"},
    {"dateOffset": 2, "title": "电影之夜", "start": 20, "priority": "low"},
    # —— 换日运行兜底：上周末与下一周（保证前后周切换均有数据）——
    {"dateOffset": -6, "title": "英语课", "start": 20, "end": 21, "priority": "low", "recurrence": {"freq": "weekly", "weekdays": [1, 3, 5]}},
    {"dateOffset": -6, "title": "户外骑行", "start": 9, "end": 11, "priority": "low"},
    {"dateOffset": -5, "title": "周末整理", "start": 15, "end": 16, "priority": "low"},
    {"dateOffset": -5, "title": "团队周会", "start": 10, "end": 11, "project": "日常运营", "priority": "high", "recurrence": {"freq": "weekly", "weekdays": [2]}},
    {"dateOffset": 4, "title": "技术分享", "start": 15, "end": 16, "priority": "medium"},
    {"dateOffset": 5, "title": "1v1 沟通", "start": 11, "end": 12, "priority": "high"},
    {"dateOffset": 5, "title": "瑜伽课", "start": 18.5, "end": 19.5, "priority": "low"},
    {"dateOffset": 6, "title": "版本回归测试", "start": 10, "end": 11.5, "project": "日常运营", "priority": "high"},
    {"dateOffset": 6, "title": "团队复盘", "start": 14, "end": 15, "priority": "medium"},
    # —— 月视图网格补齐：上月末尾（月网格首行的上月格，演示非当月弱化与已过降淡叠加）——
    {"dateOffset": -11, "title": "月度复盘", "start": 10, "end": 11.5, "priority": "medium"},
    {"dateOffset": -11, "title": "团队午餐", "start": 12, "priority": "low"},
    {"dateOffset": -10, "title": "晨会同步", "start": 9.5, "end": 10.5, "project": "日常运营", "priority": "medium", "recurrence": {"freq": "daily"}},
    {"dateOffset": -10, "title": "原型走查", "start": 14, "end": 15, "project": "日常运营", "priority": "high"},
    {"dateOffset": -9, "title": "季度规划对齐", "start": 10, "end": 12, "project": "Alpha Launch", "priority": "high"},
    {"dateOffset": -9, "title": "夜跑", "start": 19.5, "priority": "low"},
    {"dateOffset": -8, "title": "客户支持复盘", "start": 13, "end": 14, "project": "日常运营", "priority": "medium"},
    # —— 月视图主演示：本月下半（近今天密、远期疏；+10 密集格演示格内截断与长标题省略号）——
    {"dateOffset": 7, "title": "游泳训练", "start": 18.5, "end": 19.5, "priority": "low"},
    {"dateOffset": 8, "title": "需求评审", "start": 14, "end": 15.5, "project": "日常运营", "priority": "high"},
    {"dateOffset": 9, "title": "1v1 沟通", "start": 11, "end": 12, "priority": "high"},
    {"dateOffset": 9, "title": "技术分享", "start": 15, "end": 16, "priority": "medium"},
    {"dateOffset": 10, "title": "产品迭代里程碑评审与跨团队对齐", "start": 9.5, "end": 11, "project": "Alpha Launch", "priority": "high"},
    {"dateOffset": 10, "title": "用户访谈", "start": 11, "end": 12, "project": "日常运营", "priority": "high"},
    {"dateOffset": 10, "title": "撰写周报", "start": 14, "end": 15, "priority": "medium"},
    {"dateOffset": 10, "title": "设计对齐", "start": 15, "end": 16, "priority": "high"},
    {"dateOffset": 10, "title": "健身房", "start": 18.5, "end": 19.5, "priority": "low"},
    {"dateOffset": 11, "title": "版本发布检查", "start": 10, "end": 11, "project": "日常运营", "priority": "high"},
    {"dateOffset": 11, "title": "夜跑", "start": 19.5, "priority": "low"},
    {"dateOffset": 12, "title": "周末整理", "start": 15, "end": 16, "priority": "low"},
    {"dateOffset": 13, "title": "户外骑行", "start": 9, "end": 11, "priority": "low"},
    {"dateOffset": 13, "title": "电影之夜", "start": 20, "priority": "low"},
    {"dateOffset": 14, "title": "OKR 对齐会", "start": 10, "end": 11.5, "project": "Alpha Launch", "priority": "high"},
    {"dateOffset": 15, "title": "技术预研", "start": 14.5, "end": 15.5, "project": "日常运营", "priority": "medium"},
    {"dateOffset": 16, "title": "客户访谈", "start": 9.5, "end": 10.5, "project": "日常运营", "priority": "high"},
    {"dateOffset": 16, "title": "瑜伽课", "start": 18.5, "end": 19.5, "priority": "low"},
    {"dateOffset": 17, "title": "代码走查", "start": 16, "end": 17, "priority": "medium"},
    {"dateOffset": 19, "title": "深度工作：架构设计", "start": 13, "end": 15, "project": "日常运营", "priority": "high"},
    {"dateOffset": 20, "title": "版本回归测试", "start": 10, "end": 11.5, "project": "日常运营", "priority": "high"},
    {"dateOffset": 21, "title": "读书会", "start": 19, "end": 20.5, "priority": "low"},
    {"dateOffset": 23, "title": "合作方洽谈", "start": 11, "end": 12, "project": "日常运营", "priority": "high"},
    {"dateOffset": 24, "title": "家庭日", "start": 10, "priority": "low"},
    {"dateOffset": 25, "title": "团队复盘", "start": 14, "end": 15, "priority": "medium"},
    {"dateOffset": 27, "title": "设计评审", "start": 14.5, "end": 15.5, "project": "日常运营", "priority": "high"},
    {"dateOffset": 29, "title": "客户支持复盘", "start": 13, "end": 14, "project": "日常运营", "priority": "medium"},
    {"dateOffset": 29, "title": "健身房", "start": 18.5, "end": 19.5, "priority": "low"},
    {"dateOffset": 30, "title": "月度总结", "start": 15, "end": 16.5, "priority": "medium"},
    # —— 月视图翻页采样：前月/次月网格（每 2–3 天 1 条，保证翻页不空版）——
    {"dateOffset": -45, "title": "季度 OKR 启动会", "start": 10, "end": 11.5, "project": "Alpha Launch", "priority": "high"},
    {"dateOffset": -41, "title": "用户访谈", "start": 11, "end": 12, "project": "日常运营", "priority": "high"},
    {"dateOffset": -39, "title": "技术分享", "start": 15, "end": 16, "priority": "medium"},
    {"dateOffset": -37, "title": "游泳训练", "start": 18.5, "end": 19.5, "priority": "low"},
    {"dateOffset": -35, "title": "产品晨会", "start": 9, "end": 10, "project": "日常运营", "priority": "medium", "recurrence": {"freq": "daily"}},
    {"dateOffset": -33, "title": "设计评审", "start": 14.5, "end": 15.5, "project": "日常运营", "priority": "high"},
    {"dateOffset": -31, "title": "夜跑", "start": 19.5, "priority": "low"},
    {"dateOffset": -27, "title": "客户支持复盘", "start": 13, "end": 14, "project": "日常运营", "priority": "medium"},
    {"dateOffset": -25, "title": "版本发布检查", "start": 10, "end": 11, "project": "日常运营", "priority": "high"},
    {"dateOffset": -23, "title": "瑜伽课", "start": 18.5, "end": 19.5, "priority": "low"},
    {"dateOffset": -21, "title": "团队复盘", "start": 14, "end": 15, "priority": "medium"},
    {"dateOffset": -19, "title": "OKR 对齐会", "start": 10, "end": 11.5, "project": "Alpha Launch", "priority": "high"},
    {"dateOffset": -15, "title": "客户访谈", "start": 9.5, "end": 10.5, "project": "日常运营", "priority": "high"},
    {"dateOffset": -12, "title": "代码走查", "start": 16, "end": 17, "priority": "medium"},
    {"dateOffset": 35, "title": "需求评审", "start": 14, "end": 15.5, "project": "日常运营", "priority": "high"},
    {"dateOffset": 37, "title": "健身房", "start": 18.5, "end": 19.5, "priority": "low"},
    {"dateOffset": 39, "title": "技术预研", "start": 14.5, "end": 15.5, "project": "日常运营", "priority": "medium"},
    {"dateOffset": 41, "title": "客户访谈", "start": 9.5, "end": 10.5, "project": "日常运营", "priority": "high"},
    {"dateOffset": 45, "title": "版本回归测试", "start": 10, "end": 11.5, "project": "日常运营", "priority": "high"},
    {"dateOffset": 47, "title": "夜跑", "start": 19.5, "priority": "low"},
    {"dateOffset": 49, "title": "季度规划对齐", "start": 10, "end": 12, "project": "Alpha Launch", "priority": "high"},
    {"dateOffset": 51, "title": "设计对齐", "start": 15, "end": 16, "priority": "high"},
    {"dateOffset": 53, "title": "团队复盘", "start": 14, "end": 15, "priority": "medium"},
    {"dateOffset": 55, "title": "瑜伽课", "start": 18.5, "end": 19.5, "priority": "low"},
    {"dateOffset": 57, "title": "月度复盘", "start": 10, "end": 11.5, "priority": "medium"},
    # —— 未排期待办（原收件箱 mock；部分已归属项目演示项目页与"所有待办"混排）——
    # 日常运营（3 条）
    {"title": "回复李总关于合规审查的邮件，需要抄送法务部", "minutes": 30, "project": "日常运营", "priority": "high"},
    {"title": "整理产品评审会议纪要并发到群里", "project": "日常运营", "priority": "medium"},
    {"title": "给新同事开通 Jira 与 Confluence 权限", "minutes": 10, "project": "日常运营", "priority": "low"},
    # Alpha Launch（2 条）
    {"title": "给设计团队反馈新版原型标注", "minutes": 25, "project": "Alpha Launch", "priority": "high"},
    {"title": "准备周五 OKR 季度汇报的 PPT 大纲", "minutes": 45, "project": "Alpha Launch", "priority": "high"},
    # 学习提升（4 条）
    {"title": "学习 Rust 所有权与生命周期章节", "minutes": 90, "project": "学习提升", "priority": "medium"},
    {"title": "读完《置身事内》第三章", "minutes": 40, "project": "学习提升", "priority": "low"},
    {"title": "给开源项目提一个文档 typo 的 PR", "minutes": 15, "project": "学习提升", "priority": "low"},
    {"title": "写一篇关于时间块工作法的博客草稿", "minutes": 60, "project": "学习提升", "priority": "medium"},
    # 未归属项目（收件箱默认可见；覆盖工作/差旅/健康/生活/财务等类型，带/不带耗时混合）
    {"title": "预定下周去上海出差的机酒", "minutes": 20, "note": "护照在书房保险柜，出票前核对姓名拼音", "priority": "medium"},
    {"title": "预约牙医复诊", "priority": "low"},
    {"title": "预约年度体检套餐", "priority": "low"},
    {"title": "去药店取上周的化验报告", "priority": "low"},
    {"title": "给爸妈回个视频电话", "minutes": 30, "priority": "low"},
    {"title": "研究并预订下月去成都的火车票", "minutes": 20, "priority": "medium"},
    {"title": "整理 9 月信用卡账单并核对报销单", "minutes": 15, "priority": "medium"},
    {"title": "把冬装送去干洗", "priority": "low"},
    {"title": "修理书房吱呀响的椅子", "priority": "low"},
    {"title": "续费域名和云服务器", "minutes": 10, "priority": "medium"},
    {"title": "采购下周早餐食材和咖啡豆", "minutes": 40, "priority": "low"},
]

# —— 数据源页测试建议卡（2026-10-26 增 + 同日扩充批，非前端 mock 平移，为数据源页量身造）——
# 9 卡 24 条，覆盖面板全部展示形态：
#   ① 五种源分组（滴答清单 / 钉钉待办 CLI + 飞书日历 CLI + Notion Composio + 本地文件夹）
#   ② 同源跨多卡——分组按条目自身的源走（滴答清单与本地文件夹各两张卡聚同组）
#   ③ inbox / schedule 两 kind 混排（schedule 溯源在 payload.evidence，idx 对齐条目序）
#   ④ 条目形态矩阵：优先级三档 / 有无耗时 / 有无备注 / 有无素材摘录 / 有无原始时间提示
# 无来源条目（无 source_ref / 无 evidence 覆盖）不造卡：该形态已被整条丢弃——
# 数据源页每条建议必有明确来源（2026-10-26 用户产品规则，Reduce 白名单后滤升级）。
# schedule 条目沿用相对偏移（dayOffset / hour 小时小数），由 seed.py 换算绝对
# naive ISO；指纹用 seed- 前缀假值，不与真实采集指纹冲突。
SEED_PROPOSALS: list[dict] = [
    # 滴答清单（todoist CLI）· inbox 卡 2 条：条目自带完整 source_ref
    {
        "kind": "inbox",
        "items": [
            {
                "title": "给妈妈预订生日餐厅",
                "duration": 30,
                "notes": "她想去江边那家杭帮菜，记得要靠窗位",
                "priority": 1,
                "evidence": "滴答清单任务原文：给妈妈订生日餐厅！提前一周订不然没位子",
                "source_ref": {"source": "滴答清单", "source_key": "cli:todoist", "item_id": "seed-td-001",
                               "title": "给妈妈订生日餐厅", "time": None, "fingerprint": "seed-td-001"},
            },
            {
                "title": "预约年度体检套餐",
                "duration": 15,
                "priority": 3,
                "evidence": "滴答清单任务原文：预约年度体检（去年拖到 12 月才约）",
                "source_ref": {"source": "滴答清单", "source_key": "cli:todoist", "item_id": "seed-td-002",
                               "title": "预约年度体检", "time": None, "fingerprint": "seed-td-002"},
            },
        ],
    },
    # 飞书日历（lark CLI）· schedule 卡 2 条：溯源在 payload.evidence
    {
        "kind": "schedule",
        "entries": [
            {"dayOffset": 1, "hour": 15, "title": "产品需求评审会", "duration": 90, "priority": 1},
            {"dayOffset": 3, "hour": 10.5, "title": "新员工入职沟通", "duration": 60},
        ],
        "evidence": [
            {"idx": 0, "source": "飞书日历", "source_key": "cli:lark", "item_id": "seed-lark-001",
             "title": "产品需求评审会（周五 15:00）", "time": None, "fingerprint": "seed-lark-001"},
            {"idx": 1, "source": "飞书日历", "source_key": "cli:lark", "item_id": "seed-lark-002",
             "title": "新员工入职沟通", "time": None, "fingerprint": "seed-lark-002"},
        ],
    },
    # 本地文件夹 · inbox 卡 3 条（笔记提炼类：素材摘录进 evidence）
    {
        "kind": "inbox",
        "items": [
            {
                "title": "读《深度工作》第三章",
                "duration": 40,
                "priority": 3,
                "evidence": "笔记《读书清单.md》：今年书单第 12 本《深度工作》，读到第三章并做摘录",
                "source_ref": {"source": "本地文件夹", "source_key": "folder:notes", "item_id": "seed-note-001",
                               "title": "读书清单.md", "time": None, "fingerprint": "seed-note-001"},
            },
            {
                "title": "整理本月读书摘录",
                "duration": 25,
                "priority": 3,
                "evidence": "笔记《读书清单.md》：每月底把当月摘录整理进知识库",
                "source_ref": {"source": "本地文件夹", "source_key": "folder:notes", "item_id": "seed-note-002",
                               "title": "读书清单.md", "time": None, "fingerprint": "seed-note-002"},
            },
            {
                "title": "写读书笔记：时间块工作法的实践反思",
                "duration": 45,
                "priority": 2,
                "evidence": "笔记《时间块实践.md》：试了两周时间块，把踩的坑记下来发博客",
                "source_ref": {"source": "本地文件夹", "source_key": "folder:notes", "item_id": "seed-note-003",
                               "title": "时间块实践.md", "time": None, "fingerprint": "seed-note-003"},
            },
        ],
    },
    # 本地文件夹 · schedule 卡 1 条（与上张 inbox 卡同源 → 验证分组按条目聚合）
    {
        "kind": "schedule",
        "entries": [{"dayOffset": 5, "hour": 9, "title": "周末徒步：银湖山郊野径", "duration": 240}],
        "evidence": [
            {"idx": 0, "source": "本地文件夹", "source_key": "folder:notes", "item_id": "seed-note-004",
             "title": "周末计划.md", "time": None, "fingerprint": "seed-note-004"},
        ],
    },
    # —— 2026-10-26 扩充批：再灌 5 卡 16 条，覆盖更多源组与条目形态 ——
    # 钉钉待办（dws CLI）· inbox 卡 4 条：工作类，优先级三档 / 备注·摘录有无混合
    {
        "kind": "inbox",
        "items": [
            {
                "title": "整理季度 OKR 进展并在周会前发出",
                "duration": 45,
                "notes": "Q4 进展过半，重点写北极星指标卡住的原因",
                "priority": 1,
                "evidence": "钉钉待办原文：整理季度 OKR 进展并在周会前发出，周四前完成",
                "source_ref": {"source": "钉钉待办", "source_key": "cli:dws", "item_id": "seed-dw-001",
                               "title": "整理季度 OKR 进展", "time": "2026-10-27 18:00", "fingerprint": "seed-dw-001"},
            },
            {
                "title": "提交上季度差旅报销单",
                "duration": 20,
                "priority": 2,
                "source_ref": {"source": "钉钉待办", "source_key": "cli:dws", "item_id": "seed-dw-002",
                               "title": "提交差旅报销单", "time": None, "fingerprint": "seed-dw-002"},
            },
            {
                "title": "给实习生准备入职引导文档",
                "duration": 60,
                "notes": "按去年模板改，权限申请流程有更新",
                "priority": 3,
                "source_ref": {"source": "钉钉待办", "source_key": "cli:dws", "item_id": "seed-dw-003",
                               "title": "实习生入职引导文档", "time": None, "fingerprint": "seed-dw-003"},
            },
            {
                "title": "更新产品官网价格页文案",
                "duration": 30,
                "priority": 2,
                "evidence": "钉钉待办原文：价格页文案和新定价方案对不上，本周内更新",
                "source_ref": {"source": "钉钉待办", "source_key": "cli:dws", "item_id": "seed-dw-004",
                               "title": "更新价格页文案", "time": None, "fingerprint": "seed-dw-004"},
            },
        ],
    },
    # Notion（Composio 应用）· inbox 卡 3 条：文档类，含未估时（duration 缺省）形态
    {
        "kind": "inbox",
        "items": [
            {
                "title": "把读书笔记迁移到 Notion 知识库",
                "priority": 3,
                "source_ref": {"source": "Notion", "source_key": "composio:notion", "item_id": "seed-nt-001",
                               "title": "读书笔记迁移计划", "time": None, "fingerprint": "seed-nt-001"},
            },
            {
                "title": "整理产品需求池：补齐验收标准",
                "duration": 90,
                "priority": 1,
                "evidence": "Notion 页面《需求池》：3 条需求缺验收标准，评审前补齐",
                "source_ref": {"source": "Notion", "source_key": "composio:notion", "item_id": "seed-nt-002",
                               "title": "产品需求池", "time": None, "fingerprint": "seed-nt-002"},
            },
            {
                "title": "归档已完成的项目复盘文档",
                "duration": 15,
                "priority": 3,
                "source_ref": {"source": "Notion", "source_key": "composio:notion", "item_id": "seed-nt-003",
                               "title": "项目复盘归档", "time": None, "fingerprint": "seed-nt-003"},
            },
        ],
    },
    # 飞书日历（lark CLI）· schedule 卡 3 条：早会 / 晚间活动 / 远期，时长三档
    {
        "kind": "schedule",
        "entries": [
            {"dayOffset": 2, "hour": 9.5, "title": "部门季度复盘会", "duration": 120, "priority": 1},
            {"dayOffset": 6, "hour": 19.5, "title": "产品分享直播", "duration": 90},
            {"dayOffset": 8, "hour": 14, "title": "供应商季度沟通", "duration": 60},
        ],
        "evidence": [
            {"idx": 0, "source": "飞书日历", "source_key": "cli:lark", "item_id": "seed-lark-101",
             "title": "部门季度复盘会（周四 09:30）", "time": None, "fingerprint": "seed-lark-101"},
            {"idx": 1, "source": "飞书日历", "source_key": "cli:lark", "item_id": "seed-lark-102",
             "title": "产品分享直播", "time": None, "fingerprint": "seed-lark-102"},
            {"idx": 2, "source": "飞书日历", "source_key": "cli:lark", "item_id": "seed-lark-103",
             "title": "供应商季度沟通", "time": None, "fingerprint": "seed-lark-103"},
        ],
    },
    # 滴答清单（todoist CLI）· 第二张 inbox 卡 4 条：生活类，含未估时与带原始时间形态
    {
        "kind": "inbox",
        "items": [
            {
                "title": "预约车辆年检",
                "duration": 20,
                "priority": 2,
                "source_ref": {"source": "滴答清单", "source_key": "cli:todoist", "item_id": "seed-td-101",
                               "title": "预约车辆年检", "time": "2026-10-28 09:00", "fingerprint": "seed-td-101"},
            },
            {
                "title": "给爸爸买生日礼物：一双跑步鞋",
                "duration": 30,
                "notes": "鞋码 42，他喜欢灰白色系",
                "priority": 1,
                "source_ref": {"source": "滴答清单", "source_key": "cli:todoist", "item_id": "seed-td-102",
                               "title": "爸爸生日礼物", "time": None, "fingerprint": "seed-td-102"},
            },
            {
                "title": "预约宠物医院：猫咪年度疫苗",
                "priority": 2,
                "source_ref": {"source": "滴答清单", "source_key": "cli:todoist", "item_id": "seed-td-103",
                               "title": "猫咪年度疫苗", "time": None, "fingerprint": "seed-td-103"},
            },
            {
                "title": "修复阳台漏水的水龙头",
                "priority": 3,
                "source_ref": {"source": "滴答清单", "source_key": "cli:todoist", "item_id": "seed-td-104",
                               "title": "修阳台水龙头", "time": None, "fingerprint": "seed-td-104"},
            },
        ],
    },
    # 本地文件夹 · 第二张 schedule 卡 2 条：晚间运动与家务（evidence 指向不同笔记文件）
    {
        "kind": "schedule",
        "entries": [
            {"dayOffset": 2, "hour": 19.5, "title": "夜跑 5 公里", "duration": 40},
            {"dayOffset": 4, "hour": 10, "title": "周日大扫除", "duration": 120, "priority": 3},
        ],
        "evidence": [
            {"idx": 0, "source": "本地文件夹", "source_key": "folder:notes", "item_id": "seed-note-101",
             "title": "健康打卡.md", "time": None, "fingerprint": "seed-note-101"},
            {"idx": 1, "source": "本地文件夹", "source_key": "folder:notes", "item_id": "seed-note-102",
             "title": "家务安排.md", "time": None, "fingerprint": "seed-note-102"},
        ],
    },
]
