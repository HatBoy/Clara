"""测试数据生成脚本（2026-11-15 增；2026-11-17 真机测试批适配：补历史完成/
行为遥测/建议卡/清单样本，扩清 workflow_runs 等三表）。手动执行——任何模式
启动一律零灌入后，需要测试数据的唯一入口。

用法（conda activate clara 后，在 backend/ 下执行）：
    python gen_test_data.py                  # 默认追加 ~120 条任务（未来 14 天内）
    python gen_test_data.py --count 300      # 指定任务数
    python gen_test_data.py --days 30        # 指定排期跨度（天）
    python gen_test_data.py --history 0      # 关闭历史数据（默认 21 天）
    python gen_test_data.py --wipe           # 先清空业务数据再生成（重开一局）
    python gen_test_data.py --wipe --count 200 --days 21

生成内容（全部随机）：
- 项目 3~5 个（十色板随机取色）；
- 未来任务按真实使用分布：未排期待办 ~25%、重复系列 ~15%（每日/每周随机
  星期组合）、已完成 ~20%、软删 ~5%、带备注 ~40%、优先级 高/中/低 ≈ 2:5:3；
- 历史完成数据（2026-11-17 补——统计报告/精力画像/夜间反思都吃过去 2~4 周，
  此前全部数据落在今天及未来）：过去 --history 天每天 1~2 条已完成任务
  （done_at 带实际-计划 ±0~40min 偏差）+ 同步产 done 行为遥测事件（真实写点
  形态：prev_start=计划开始、delta=偏差）+ 少量 reschedule/unschedule/abandon
  事件铺精力低谷对照桶（完成倾向加权 9-11/15-16 点，拖延铺 13-14/20-21 点）；
- 建议卡各处理结果样本（accepted/modified/rejected/expired/pending ×
  schedule/inbox/question/preflight）——采纳率/信任渐进的口径数据；
- 子任务清单样例（checklist 有料）；2~3 条情节事实（FTS 检索有料）。
- 已排期任务 duration 与 start/end 区间保持一致（业务不变式）。

注意：直接写 dev 库（backend/data/clara.db，路径经 config 解析）；重复执行
为叠加。--wipe 清业务十二表（subtasks/reminders → tasks → projects/proposals/
messages/stats_messages/behavior_events/episodic_facts/stats_reports →
workflow_runs/ingestion_items，子表先清）——配置表（模型接入/数据源/设置等）、
同步配置与记忆画像（memory_rules）不动；workflow_runs 连带清是让定时流当天
可重触发（(name,run_date) 唯一防重入），ingestion_items 清是采集测试不被旧
指纹跨日去重。
"""

import argparse
import random
import sys
from datetime import datetime, timedelta

# 词库：动宾组合出足够多样的中文标题（≤30 字符约束）
_VERBS = ["评审", "编写", "整理", "准备", "复盘", "跟进", "调研", "优化", "联调", "梳理",
          "修订", "对接", "演练", "核对", "研究", "起草", "走查", "拆解", "排期", "敲定"]
_OBJECTS = ["季度OKR", "发布计划", "客户端方案", "接口文档", "周报", "数据看板", "客户反馈",
            "竞品分析", "部署脚本", "新员工手册", "预算表", "合作协议", "测试用例", "架构图",
            "安全审计", "复盘纪要", "招聘JD", "培训材料", "需求清单", "上线检查单"]
_NOTES = ["提前准备材料", "需要拉上相关同学", "输出结论同步群里", "注意截止时间",
          "拆两步做", "先出初稿再对齐", "完成后归档", "高风险项，留缓冲"]

# 与前端十色板同源（hex 形态；models.Project.color 接受 CSS 变量串或 hex）
_PALETTE = ["#e8724c", "#e6a23c", "#d4b106", "#7cb305", "#3fa45b",
            "#36ad9a", "#0098a8", "#3f88c5", "#7a63c2", "#b8479e"]
_WEEKDAYS = {1: "MO", 2: "TU", 3: "WE", 4: "TH", 5: "FR", 6: "SA", 7: "SU"}

# 时段桶（与 services/tasks._PERIODS 同界）：行为事件环境快照的 period 值
_PERIODS = ((300, "清晨"), (720, "上午"), (1080, "下午"), (1440, "晚上"))

# 历史完成/拖延的小时分布（精力曲线要出可读峰谷——均匀铺会两平）
_DONE_HOURS = [8, 9, 9, 10, 10, 11, 11, 15, 15, 16, 16, 17, 20]
_DEFER_HOURS = [13, 13, 14, 14, 20, 20, 21, 21]


def _period_of(dt: datetime) -> str:
    minute = dt.hour * 60 + dt.minute
    for cap, name in _PERIODS:
        if minute < cap:
            return name
    return "晚上"


def _ctx(rng: random.Random, anchor: datetime) -> dict:
    """环境快照（同 services/tasks._event_context 形态；前后会议真机按当日
    busy 最近邻算，合成数据随机近似——聚类只看同构性）。"""
    return {
        "weekday": anchor.weekday(),
        "period": _period_of(anchor),
        "before_meeting": rng.random() < 0.2,
        "after_meeting": rng.random() < 0.2,
    }


def _wipe(db) -> None:
    """清空业务十二表（子表先清防外键；FTS 虚表随触发器自动）。Message 须在
    Proposal 前——messages.proposal_id 外键引用（原序潜伏缺陷：此前 dev 库
    messages 恰无挂接 proposal 才未炸，2026-11-17 真机首跑暴露）。"""
    from models import (BehaviorEvent, EpisodicFact, IngestionItem, Message,
                        Project, Proposal, Reminder, StatsMessage, StatsReport,
                        Subtask, Task, WorkflowRun)
    for cls in (Subtask, Reminder, Task, Message, Proposal, StatsReport,
                StatsMessage, BehaviorEvent, EpisodicFact, Project,
                WorkflowRun, IngestionItem):
        n = db.query(cls).delete()
        if n:
            print(f"  清空 {cls.__tablename__}: {n} 行")
    db.commit()


def gen(args) -> None:
    from database import SessionLocal, init_db
    from models import BehaviorEvent, EpisodicFact, Proposal, Project, Subtask, Task

    rng = random.Random()  # 系统熵：每次执行结果不同
    today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    with SessionLocal() as db:
        init_db()  # 幂等建表（空库首跑脚本 / wipe 前表须已存在）
        if args.wipe:
            print("清空业务数据…")
            _wipe(db)

        # —— 项目 3~5 个 ——
        n_proj = rng.randint(3, 5)
        proj_names = rng.sample([f"{w}专项" for w in
                                 ["孵化", "增长", "基建", "客服", "数据", "移动端", "安全", "外包"]], n_proj)
        projects = [Project(name=name, color=rng.choice(_PALETTE), sort_order=i,
                            starred=rng.random() < 0.25)
                    for i, name in enumerate(proj_names)]
        db.add_all(projects)
        db.flush()
        print(f"项目 {n_proj} 个")

        # —— 未来任务（今天起 --days 天内）——
        n_sched_total = 0
        n_todo = n_rec = n_done = n_del = 0
        sched_objs = []  # 已排期未软删未完成（子任务挂载池；id 须 flush 后才有）
        for i in range(args.count):
            title = f"{rng.choice(_VERBS)}{rng.choice(_OBJECTS)}"
            priority = rng.choices([1, 2, 3], weights=[2, 5, 3])[0]
            task = Task(title=title, priority=priority, sort_order=i,
                        notes=rng.choice(_NOTES) if rng.random() < 0.4 else None,
                        project_id=rng.choice(projects).id if rng.random() < 0.7 else None)
            roll = rng.random()
            if roll < 0.25:
                n_todo += 1  # 未排期待办（收件箱）
            else:
                # 已排期：未来 --days 天内随机一天 + 工作时段（8-21 点）随机起整点/半点
                day0 = today0 + timedelta(days=rng.randint(0, args.days - 1),
                                          hours=rng.randint(8, 20),
                                          minutes=rng.choice([0, 30]))
                duration = rng.choice([30, 45, 60, 90, 120, 180])
                task.start_time = day0
                task.duration_mins = duration
                task.ever_scheduled = True
                n_sched_total += 1
                if rng.random() < 0.2:  # 重复系列：主行 start + duration 口径（end 恒空）
                    n_rec += 1
                    task.recurrence_rule = (
                        "FREQ=DAILY" if rng.random() < 0.4 else
                        "FREQ=WEEKLY;BYDAY=" + ",".join(
                            _WEEKDAYS[d] for d in sorted(
                                rng.sample([1, 2, 3, 4, 5], rng.randint(1, 3)))))
                else:
                    task.end_time = day0 + timedelta(minutes=duration)
                    if rng.random() < 0.2:  # 单次已排期中 ~20% 已完成
                        task.done_at = day0 + timedelta(minutes=duration)
                        n_done += 1
                if rng.random() < 0.07:  # 软删样例（草稿流产口径）
                    n_del += 1
                    task.is_deleted = True
                    task.deleted_at = datetime.now()
                elif not task.done_at:
                    sched_objs.append(task)
            db.add(task)
        db.commit()
        sched_ids = [t.id for t in sched_objs]  # commit 后 id 已赋（过期属性访问即刷新）

        print(f"未来任务 {args.count} 条：已排期 {n_sched_total}（含重复系列 {n_rec}、"
              f"已完成 {n_done}、软删 {n_del}）+ 未排期待办 {n_todo}")

        # —— 历史完成数据（过去 --history 天；统计/精力/反思的数据地基）——
        n_hist = n_defer = 0
        if args.history > 0:
            n_hist_target = max(12, args.count // 3)
            hist_ids = []
            for i in range(n_hist_target):
                back = rng.randint(1, args.history)  # 恒严格过去（back≥1）
                start = (today0 - timedelta(days=back)
                         + timedelta(hours=rng.choice(_DONE_HOURS),
                                     minutes=rng.choice([0, 30])))
                duration = rng.choice([30, 45, 60, 90, 120, 180])
                end = start + timedelta(minutes=duration)
                # 实际-计划偏差：±0~40 分钟（小偏差偏多的乐观分布）
                dev = (rng.choice([0, 0, 5, 10, 10, 15, 20, 25, 30, 40])
                       * rng.choice([-1, 1]))
                done_at = end + timedelta(minutes=dev)
                task = Task(
                    title=f"{rng.choice(_VERBS)}{rng.choice(_OBJECTS)}",
                    priority=rng.choices([1, 2, 3], weights=[2, 5, 3])[0],
                    sort_order=args.count + i,
                    project_id=rng.choice(projects).id if rng.random() < 0.7 else None,
                    start_time=start, end_time=end,
                    duration_mins=duration, ever_scheduled=True, done_at=done_at,
                    created_at=start - timedelta(hours=rng.randint(1, 48)),
                    updated_at=done_at)
                db.add(task)
                db.flush()  # id 为 flush 时赋（Python 侧 default），须先 flush 再取
                hist_ids.append(task.id)
                # done 遥测（真实写点形态：prev_start=计划开始 / delta=偏差分钟）
                db.add(BehaviorEvent(
                    task_id=task.id, kind="done",
                    prev_start=start, planned_min=duration, delta_min=dev,
                    context=_ctx(rng, start),
                    created_at=done_at, updated_at=done_at))
                n_hist += 1
            # defer 事件（精力低谷对照桶）：改期/取消排期/放弃，铺午后与深夜
            for _ in range(max(6, n_hist_target // 3)):
                back = rng.randint(1, args.history)
                prev = (today0 - timedelta(days=back)
                        + timedelta(hours=rng.choice(_DEFER_HOURS),
                                    minutes=rng.choice([0, 30])))
                at = prev + timedelta(minutes=30)
                kind = rng.choice(["reschedule", "reschedule",
                                   "unschedule", "abandon"])
                ev = dict(task_id=rng.choice(hist_ids), kind=kind, prev_start=prev,
                          context=_ctx(rng, prev), created_at=at, updated_at=at)
                if kind == "reschedule":
                    new = prev + timedelta(hours=rng.choice([1, 2, 3, 24]),
                                           minutes=rng.choice([0, 30]))
                    ev.update(new_start=new,
                              delta_min=round((new - prev).total_seconds() / 60))
                else:
                    ev.update(planned_min=rng.choice([30, 45, 60, 90]))
                db.add(BehaviorEvent(**ev))
                n_defer += 1
            db.commit()
            print(f"历史完成 {n_hist} 条（过去 {args.history} 天，含 done 遥测）"
                  f"+ defer 遥测 {n_defer} 条")

        # —— 建议卡样本（采纳率/信任渐进口径）——
        n_prop = 0
        for _ in range(12):
            kind = rng.choice(["schedule", "inbox", "question", "preflight"])
            created = datetime.now() - timedelta(days=rng.randint(0, 6),
                                                 hours=rng.randint(0, 12))
            outcome = rng.choice(["accepted", "accepted", "modified",
                                  "rejected", "expired", None])  # None=pending
            resolved = (created + timedelta(minutes=rng.randint(1, 240))
                        if outcome else None)
            payload = {
                "schedule": {"query": "安排深度工作",
                             "draft_plan": [{"title": "深度工作",
                                             "start_time": (created + timedelta(
                                                 days=1, hours=2)).isoformat(),
                                             "duration_mins": 90, "priority": 2}]},
                "inbox": {"items": [{"title": "整理会议纪要",
                                     "note": "周一例会产物"}]},
                "question": {"question": "周四下午已有安排，改到周五上午？",
                             "options": ["保持周四", "改周五上午"]},
                "preflight": {"checklist": ["确认会议室预订", "提前一天发材料"]},
            }[kind]
            db.add(Proposal(kind=kind, payload=payload, outcome=outcome,
                            created_at=created, resolved_at=resolved,
                            updated_at=resolved or created))
            n_prop += 1

        # —— 子任务清单（checklist 有料）——
        n_sub = 0
        if sched_ids:
            for tid in rng.sample(sched_ids, min(6, len(sched_ids))):
                for j in range(rng.randint(2, 3)):
                    db.add(Subtask(task_id=tid,
                                   title=rng.choice(["确认参会人", "准备材料",
                                                     "提前发议程", "订会议室",
                                                     "会后发纪要"]),
                                   done_at=datetime.now() if (j == 0 and rng.random() < 0.5)
                                   else None,
                                   sort_order=j))
                    n_sub += 1

        # —— 情节事实（FTS 检索有料）——
        n_fact = 0
        for topic, fact, days in (
            ("健康", "本周感冒初愈，避免安排高强度晚间任务", 5),
            ("出差", "下周三到周五出差上海，仅上午可排协作事务", 8),
            ("项目", "发射客户项目进入验收阶段，里程碑前优先保深度块", None),
        ):
            db.add(EpisodicFact(topic=topic, fact=fact, source="chat",
                                confidence=0.7,
                                expires_at=(datetime.now() + timedelta(days=days)
                                            if days else None)))
            n_fact += 1

        db.commit()
        print(f"建议卡 {n_prop} 张（各处理结果）、子任务 {n_sub} 行、情节事实 {n_fact} 条")
        print(f"完成 → dev 库（data/clara.db）。刷新前端页面即可看到。")


def main() -> None:
    parser = argparse.ArgumentParser(description="随机生成 Clara 测试日程数据（手动执行）")
    parser.add_argument("--count", type=int, default=120, help="未来任务总数（默认 120）")
    parser.add_argument("--days", type=int, default=14, help="排期跨度天数（默认 14）")
    parser.add_argument("--history", type=int, default=21,
                        help="历史完成数据天数（默认 21；0 = 关闭）")
    parser.add_argument("--wipe", action="store_true", help="先清空业务数据再生成")
    args = parser.parse_args()
    if args.count < 1 or args.days < 1 or args.history < 0:
        sys.exit("--count/--days 须为正整数，--history 须为非负整数")
    gen(args)


if __name__ == "__main__":
    main()
