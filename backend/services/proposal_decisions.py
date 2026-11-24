"""建议结算：原始条目身份、任务写入及持久回执共享一个 SQLite 提交点。"""
from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
import json
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.orm import Session

from models import Message, Proposal, Task
from schemas import TaskCreate, TaskUpdate
from services import tasks, undo

KEYS = {'schedule': 'draft_plan', 'inbox': 'items', 'minutes': 'items', 'hardtask': 'steps', 'delete': 'tasks'}
STATE = '_decisions'


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def ids(pid, entries):
    return [str(uuid5(NAMESPACE_URL, f'clara:{pid}:{i}:{encoded(e)}')) for i, e in enumerate(entries)]


def visible_ids(row):
    payload = row.payload or {}
    ledger = payload.get(STATE) or {}
    if ledger:
        return ledger['ids'] if row.outcome else [i for i in ledger['ids'] if i not in ledger['receipts']]
    return ids(row.id, payload.get(KEYS.get(row.kind), []))


@contextmanager
def atomic(db):
    # 子会话只结束自己的逻辑事务；任务服务内的 commit 不发布外部连接。
    conn = db.connection()
    conn.exec_driver_sql('BEGIN IMMEDIATE')
    try:
        with undo.defer_publish(), Session(bind=conn, join_transaction_mode='rollback_only',
                                           expire_on_commit=False, autoflush=False) as writer:
            yield writer
            writer.flush()
            db.commit()
        db.expire_all()
    except BaseException:
        db.rollback()
        raise


def _ledger(row):
    if STATE in row.payload:
        return {**row.payload[STATE], 'receipts': dict(row.payload[STATE]['receipts'])}
    entries = row.payload.get(KEYS[row.kind], [])
    stable = ids(row.id, entries)
    receipts = {stable[n]: {'action': 'accept', 'task_id': None, 'scheduled': row.kind == 'schedule',
                            'sig': signature({'action': 'accept'})}
                for n in row.payload.get('accepted_indexes', []) if type(n) is int and 0 <= n < len(stable)}
    return {'entries': entries, 'ids': stable, 'evidence': row.payload.get('evidence', []), 'receipts': receipts}


def signature(cmd):
    return sha256(encoded([cmd['action'], cmd.get('entry') or {}, cmd.get('reason') or '']).encode()).hexdigest()


def project(kind, payload, ledger):
    """从原清单和回执投影待处理条目；终态保留原始快照供复盘。"""
    stable, entries, receipts = ledger['ids'], ledger['entries'], ledger['receipts']
    remaining = [i for i, identity in enumerate(stable) if identity not in receipts]
    show = remaining if remaining else list(range(len(stable)))
    result = {**payload, STATE: ledger, KEYS[kind]: [entries[i] for i in show]}
    result.pop('accepted_indexes', None)
    if kind == 'schedule' and ledger['evidence']:
        positions = {old: new for new, old in enumerate(show)}
        result['evidence'] = [{**e, 'idx': positions[e['idx']]} for e in ledger['evidence'] if e.get('idx') in positions]
    changes = [r['diff'] for r in receipts.values() if r.get('diff')]
    if changes:
        result.pop('modified_diff' if remaining else 'item_diffs', None)
        result['item_diffs' if remaining else 'modified_diff'] = changes
    reasons = [r['reason'] for r in receipts.values() if r.get('reason')]
    if reasons:
        result['reject_reason'] = '；'.join(reasons)[:200]
    actions = {r['action'] for r in receipts.values()}
    outcome = None if remaining else ('modified' if 'modify' in actions else
              'accepted' if actions - {'reject', 'skip'} else 'rejected' if 'reject' in actions else 'ignored')
    return result, outcome


def _apply(db, row, ledger, cmd, n):
    from services import ai_data, data_ingest
    action = cmd['action']
    identity = ledger['ids'][n]
    original = ledger['entries'][n]
    old = {'title': original} if isinstance(original, str) else original
    overlay = cmd.get('entry') or {}
    data = {**old, **{k: v for k, v in overlay.items() if k in ('title','duration','priority','start','notes','recurrence_rule')}}
    receipt = {'action': action, 'task_id': None, 'scheduled': False, 'sig': signature(cmd)}
    if cmd.get('reason'):
        receipt['reason'] = cmd['reason']
    if action in ('accept', 'reject', 'skip') and overlay:
        raise ValueError('本操作不接受编辑字段')
    if action == 'skip':
        return receipt
    if action == 'reject':
        mini = {KEYS[row.kind]: [original], 'evidence': [e for e in ledger['evidence'] if e.get('idx') == n]}
        data_ingest.on_proposal_settled(db, row.kind, mini, 'rejected')
        return receipt
    if row.kind == 'delete':
        if action != 'accept' or not old.get('task_id') or not tasks.delete_task(db, old['task_id']):
            raise ValueError('无法删除：原任务不存在或操作已改变')
        return {**receipt, 'task_id': old['task_id']}
    evidence = next((e for e in ledger['evidence'] if e.get('idx') == n), {})
    ref = {**evidence, **(old.get('source_ref') or {})}
    notes = data.get('notes')
    if row.kind == 'minutes':
        mid = row.payload.get('meeting_task_id')
        origin = f"来自《{row.payload['meeting_title']}》纪要" if row.payload.get('meeting_title') else '来自会议纪要'
        notes = (f'{notes}（{origin}）' if notes else origin)[:500]
        if mid:
            ref = {'source_key': f'minutes:{mid}'}
    fields = dict(title=data.get('title'), notes=notes, priority=data.get('priority'),
                  recurrence_rule=data.get('recurrence_rule'), source_key=ref.get('source_key'),
                  source_fingerprint=ref.get('fingerprint') or None, source_evidence=data.get('evidence') or evidence.get('evidence'),
                  duration_mins=data.get('duration'))
    if action != 'inbox' and (data.get('start') or action == 'schedule' or row.kind == 'schedule'):
        if not isinstance(data.get('start'), str):
            raise ValueError('排期缺少有效的开始时间')
        start = datetime.fromisoformat(data['start'])
        minutes = data.get('duration') or 60
        if type(minutes) is not int or not 1 <= minutes <= 1440:
            raise ValueError('条目时长须在 1–1440 分钟内')
        fields.update(start_time=start, end_time=start + timedelta(minutes=minutes))
    if action == 'modify':
        diff = ai_data._modified_diff([data], [old])
        if diff:
            receipt['diff'] = {**diff[0], 'idx': n}
    body = TaskCreate(**fields)
    tid = old.get('task_id')
    if tid:
        patch = {'start_time': body.start_time, 'end_time': body.end_time}
        if action == 'modify':
            patch.update({k: getattr(body, k) for k in ('title','priority','notes','recurrence_rule') if k in overlay})
        task = tasks.update_task(db, tid, TaskUpdate(**patch))
        if task is None:
            raise ValueError('原任务已删除，不会重新创建')
    else:
        # 同一条目在两台尚未同步的设备上采纳也只生成同一任务主键。
        tid = str(uuid5(NAMESPACE_URL, f'clara:proposal-task:{identity}'))
        if db.get(Task, tid) is not None:
            raise ValueError('发现已有任务但缺少回执，请先同步')
        task = tasks.create_task(db, body, task_id=tid)
    if action == 'smart' and task.start_time is None:
        from agents.engine import auto_schedule_run, NoSlotError
        try:
            decision = auto_schedule_run(db, task.id)
        except NoSlotError:
            pass  # 保留同一个待办和明确的未排期回执。
        else:
            tasks.apply_slot_decisions(db, [decision])
            db.refresh(task)
    return {**receipt, 'task_id': task.id, 'scheduled': task.start_time is not None}


def decide(db, pid, commands):
    """同批原子；同决定重放不写入；不同内容／目标的旧窗口一律冲突。"""
    results = []
    with atomic(db) as writer:
        row = writer.get(Proposal, pid)
        if row is None:
            raise KeyError(pid)
        if row.kind not in KEYS:
            raise ValueError('本建议不含可结算条目')
        ledger = _ledger(row)
        changed = False
        for cmd in commands:
            identity = cmd['item_id']
            prior = ledger['receipts'].get(identity)
            if prior is not None:
                if prior['sig'] != signature(cmd):
                    raise ValueError('该条目已按不同决定处理，请刷新')
                receipt = prior
            else:
                if row.outcome or identity not in ledger['ids']:
                    raise ValueError('建议已变更，请刷新')
                receipt = _apply(writer, row, ledger, cmd, ledger['ids'].index(identity))
                ledger['receipts'][identity] = receipt
                changed = True
            results.append({k: v for k, v in dict(item_id=identity, **receipt).items() if k in ('item_id','action','task_id','scheduled')})
        if changed:
            row.payload, row.outcome = project(row.kind, row.payload, ledger)
            if row.outcome:
                row.resolved_at = datetime.now()
    return {'proposal': db.get(Proposal, pid), 'receipts': results}


def resolve(db, pid, outcome, reason=None):
    from services import ai_data, data_ingest
    with atomic(db) as writer:
        row = writer.get(Proposal, pid)
        if row is None:
            raise KeyError(pid)
        if row.outcome is not None:
            if row.outcome != outcome:
                raise ValueError('该建议已经结算，请刷新')
        elif row.kind in KEYS and visible_ids(row):
            raise ValueError('此建议必须按条目标识结算，请刷新')
        elif row.kind == 'minutes_invite' and outcome == 'accepted':
            raise ValueError('请先提交纪要原文')
        else:
            ai_data.record_proposal_outcome(writer, pid, outcome, reason)
            data_ingest.on_proposal_settled(writer, row.kind, row.payload, outcome)
    return db.get(Proposal, pid)


def invite_result(db, body):
    if not body.invite_id:
        return None
    row = db.get(Proposal, body.invite_id)
    if row is None or row.kind != 'minutes_invite':
        raise KeyError(body.invite_id)
    if (row.payload or {}).get('meeting_task_id') != body.task_id:
        raise ValueError('邀约会议不匹配')
    if row.outcome:
        child = db.get(Proposal, row.payload.get('minutes_id')) if row.payload.get('minutes_id') else None
        if child and row.outcome == 'accepted' and child.payload.get('source_text') == body.text:
            return child
        raise ValueError('该邀约已按其他决定结算')
    return None


def save_minutes(db, body, payload, reply):
    with atomic(db) as writer:
        row = invite_result(writer, body)
        if row is not None:
            pid, created = row.id, False
        else:
            pid = str(uuid5(NAMESPACE_URL, 'clara:minutes:' + body.invite_id)) if body.invite_id else None
            row = Proposal(id=pid, kind='minutes', payload=payload)
            writer.add(row)
            writer.flush()
            writer.add(Message(role='assistant', content=reply, proposal_id=row.id))
            if body.invite_id:
                invite = writer.get(Proposal, body.invite_id)
                invite.payload = {**invite.payload, 'minutes_id': row.id}
                invite.outcome, invite.resolved_at = 'accepted', datetime.now()
            pid, created = row.id, True
    return db.get(Proposal, pid), created


def merge_synced(left, right, winner):
    """不同条目的离线回执可并集；相反决定不能让整行 LWW 静默胜出。"""
    a = (left.get('payload') or {}).get(STATE)
    b = (right.get('payload') or {}).get(STATE)
    if not a and not b:
        return winner
    if not a or not b:
        bare, recorded = (left, right) if b else (right, left)
        if bare.get('outcome') and bare.get('outcome') != recorded.get('outcome'):
            raise ValueError('旧设备与本机的建议决定冲突')
        return recorded
    if a['ids'] != b['ids']:
        raise ValueError('建议原始条目不一致')
    receipts = dict(a['receipts'])
    if a == b and left.get('outcome') == right.get('outcome'):
        return winner
    for k, v in b['receipts'].items():
        if k in receipts and (v['sig'] != receipts[k]['sig'] or v['task_id'] != receipts[k]['task_id'] or v['scheduled'] != receipts[k]['scheduled']):
            raise ValueError('两台设备对同一建议条目作出了不同决定')
        receipts[k] = v
    ledger = {**a, 'receipts': {k:receipts[k] for k in a['ids'] if k in receipts}}
    payload, outcome = project(winner['kind'], winner['payload'], ledger)
    return {**winner, 'payload':payload, 'outcome':outcome,
            'resolved_at':(max((v for v in (left.get('resolved_at'),right.get('resolved_at'),winner.get('updated_at')) if v), default=None) if outcome else None)}
