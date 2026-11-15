"""测试数据生成脚本（2026-11-15 增，手动执行——任何模式启动一律零灌入后，
需要测试数据的唯一入口）。

用法（conda activate clara 后，在 backend/ 下执行）：
    python gen_test_data.py                  # 默认追加 ~120 条任务（未来 14 天内）
    python gen_test_data.py --count 300      # 指定任务数
    python gen_test_data.py --days 30        # 指定排期跨度（天）
    python gen_test_data.py --wipe           # 先清空业务数据再生成（重开一局）
    python gen_test_data.py --wipe --count 200 --days 21

生成内容（全部随机）：
- 项目 3~5 个（十色板随机取色）；
- 任务属性按真实使用分布：未排期待办 ~25%、重复系列 ~15%（每日/每周随机
  星期组合）、已完成 ~20%、软删 ~5%、带备注 ~40%、优先级 高/中/低 ≈ 2:5:3；
- 已排期任务 duration 与 start/end 区间保持一致（业务不变式）。

注意：直接写 dev 库（backend/data/clara.db，路径经 config 解析）；重复执行
为叠加。--wipe 清业务九表（subtasks/reminders → tasks → projects/proposals/
messages/behavior_events/episodic_facts/stats_reports，子表先清）——配置表
（模型接入/数据源/设置等）与同步配置不动。
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


def _wipe(db) -> None:
    """清空业务九表（子表先清防外键；FTS 虚表随触发器自动）。"""
    from models import (BehaviorEvent, EpisodicFact, Message, Project, Proposal,
                        Reminder, StatsReport, Subtask, Task)
    for cls in (Subtask, Reminder, Task, StatsReport, Proposal, Message,
                BehaviorEvent, EpisodicFact, Project):
        n = db.query(cls).delete()
        if n:
            print(f"  清空 {cls.__tablename__}: {n} 行")
    db.commit()


def gen(args) -> None:
    from database import SessionLocal, init_db
    from models import Project, Task

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

        # —— 任务 ——
        n_sched_total = 0
        n_todo = n_rec = n_done = n_del = 0
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
                    task.is_deleted = True
                    task.deleted_at = datetime.now()
                    n_del += 1
            db.add(task)
        db.commit()

        print(f"任务 {args.count} 条：已排期 {n_sched_total}（含重复系列 {n_rec}、"
              f"已完成 {n_done}、软删 {n_del}）+ 未排期待办 {n_todo}")
        print(f"完成 → dev 库（data/clara.db）。刷新前端页面即可看到。")


def main() -> None:
    parser = argparse.ArgumentParser(description="随机生成 Clara 测试日程数据（手动执行）")
    parser.add_argument("--count", type=int, default=120, help="任务总数（默认 120）")
    parser.add_argument("--days", type=int, default=14, help="排期跨度天数（默认 14）")
    parser.add_argument("--wipe", action="store_true", help="先清空业务数据再生成")
    args = parser.parse_args()
    if args.count < 1 or args.days < 1:
        sys.exit("--count/--days 须为正整数")
    gen(args)


if __name__ == "__main__":
    main()
