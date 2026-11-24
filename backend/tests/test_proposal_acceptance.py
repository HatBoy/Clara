"""建议条目回执：同卡重复请求、条目失败和旧卡片重试均不重复写任务。"""
import pytest
from sqlalchemy import select
from conftest import TestingSessionLocal
from models import Task
from services.ai_data import create_proposal


@pytest.mark.parametrize('kind,key,items', [
    ('inbox', 'items', [{'title':'甲'}, {'title':'乙'}]),
    ('schedule', 'draft_plan', [{'title':x, 'start':'2026-11-25T09:00', 'duration':30} for x in ('甲','乙')]),
    ('hardtask', 'steps', ['甲','乙']),
])
def test_retry_after_response_loss(client, kind, key, items):
    with TestingSessionLocal() as db:
        pid = create_proposal(db, kind=kind, payload={key:items}).id
    url = f'/api/ai/proposals/{pid}/accept-item'
    def accept(index, expected=items):
        return client.post(url, json={'index':index,'expected':expected})
    assert accept(0).json()['outcome'] is None
    assert accept(0).json()['settled_count'] == 1
    assert accept(1, list(reversed(items))).status_code == 409
    assert accept(1).json()['outcome'] == 'accepted'
    assert accept(1).json()['outcome'] == 'accepted'
    with TestingSessionLocal() as db:
        assert sorted(db.scalars(select(Task.title))) == ['乙','甲']


def test_invalid_item_rolls_back_receipt(client):
    items = [{'title':'甲'}, {'title':''}]
    with TestingSessionLocal() as db:
        pid = create_proposal(db, kind='inbox', payload={'items':items}).id
    url = f'/api/ai/proposals/{pid}/accept-item'
    assert client.post(url, json={'index':0,'expected':items}).is_success
    assert client.post(url, json={'index':1,'expected':items}).status_code == 409
    row = client.get('/api/ai/proposals').json()[0]
    assert row['outcome'] is None and row['settled_count'] == 1


def test_batch_rolls_back_tasks_ledger_and_undo(client):
    from services import tasks, undo
    from schemas import TaskCreate
    with TestingSessionLocal() as db:
        first = tasks.create_task(db, TaskCreate(title='原会议'))
        row = create_proposal(db, kind='delete', payload={'tasks':[
            {'task_id':first.id, 'title':'原会议'}, {'task_id':'missing', 'title':'已删'}]})
        pid, tid, stable = row.id, first.id, row.item_ids
    response = client.post(f'/api/ai/proposals/{pid}/decide', json={'items':[
        {'item_id':i, 'action':'accept'} for i in stable]})
    assert response.status_code == 409
    with TestingSessionLocal() as db:
        assert tasks.get_task(db,tid) is not None
    assert not undo.state()['available']
    assert client.get(f'/api/ai/proposals/{pid}').json()['settled_count'] == 0


def test_duplicate_titles_edited_skip_and_stale_window(client):
    with TestingSessionLocal() as db:
        row = create_proposal(db,kind='inbox',payload={'items':[{'title':'回访'}]*3})
        pid, stable = row.id, row.item_ids
    url = f'/api/ai/proposals/{pid}'
    assert len(set(stable)) == 3
    command = {'item_id':stable[1],'action':'modify','entry':{'title':'回访乙','duration':45}}
    decided = client.post(url+'/decide',json={'items':[command]}).json()
    assert decided['proposal']['item_ids'] == [stable[0],stable[2]]
    assert decided['receipts'] == client.post(url+'/decide',json={'items':[command]}).json()['receipts']
    assert client.post(url+'/decide',json={'items':[{**command,'entry':{'title':'回访丙'}}]}).status_code == 409
    assert client.post(url+'/outcome',json={'outcome':'rejected'}).status_code == 409
    resp = client.post(url+'/decide',json={'items':[{'item_id':stable[0],'action':'skip'},
                                                 {'item_id':stable[2],'action':'reject'}]}).json()
    assert resp['proposal']['outcome'] == 'modified'
    with TestingSessionLocal() as db:
        assert list(db.scalars(select(Task.title))) == ['回访乙']


def test_smart_no_slot_replays_original_task(client, monkeypatch):
    from agents import engine
    with TestingSessionLocal() as db:
        row = create_proposal(db, kind='inbox', payload={'items':[{'title':'待安排'}]})
        pid, stable = row.id,row.item_ids[0]
    calls = []
    def no_slot(db, tid):
        calls.append(tid)
        raise engine.NoSlotError('无可用时段')
    monkeypatch.setattr(engine, 'auto_schedule_run', no_slot)
    cmd = {'items':[{'item_id':stable,'action':'smart'}]}
    first = client.post(f'/api/ai/proposals/{pid}/decide',json=cmd).json()['receipts'][0]
    again = client.post(f'/api/ai/proposals/{pid}/decide',json=cmd).json()['receipts'][0]
    assert first == again and not first['scheduled'] and calls == [first['task_id']]
    with TestingSessionLocal() as db:
        assert db.get(Task,first['task_id']).start_time is None


def test_file_db_same_decision_concurrent_and_after_restart(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from database import Base
    from services.proposal_decisions import decide
    uri = f'sqlite:///{tmp_path / "双窗.db"}'
    engine = create_engine(uri)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        row = create_proposal(db,kind='inbox',payload={'items':[{'title':'只写一次'}]})
        pid, stable = row.id,row.item_ids[0]
    command = [{'item_id':stable,'action':'accept'}]
    barrier = Barrier(2)
    def worker():
        with Session(engine) as db:
            barrier.wait(timeout=5)
            return decide(db,pid,command)['receipts'][0]
    with ThreadPoolExecutor(2) as pool:
        a,b = pool.submit(worker),pool.submit(worker)
        assert a.result() == b.result()
    engine.dispose()
    engine = create_engine(uri)
    with Session(engine) as db:
        assert decide(db,pid,command)['receipts'][0] == a.result()
        assert list(db.scalars(select(Task.id))) == [a.result()['task_id']]
    engine.dispose()


def test_synced_receipts_union_and_conflict_are_direction_independent():
    from copy import deepcopy
    from services import sync_engine as sync
    from services.proposal_decisions import decide
    from services.row_codec import dump_table
    from models import Proposal
    with TestingSessionLocal() as db:
        row = create_proposal(db,kind='inbox',payload={'items':[{'title':'甲'},{'title':'乙'}]})
        pid, stable = row.id,row.item_ids
        base = deepcopy(row.payload)
        decide(db,pid,[{'item_id':stable[0],'action':'accept'}]); left = dump_table(db,'proposals')
        row = db.get(Proposal,pid); row.payload = base; db.commit()
        decide(db,pid,[{'item_id':stable[1],'action':'skip'}]); right = dump_table(db,'proposals')
        def merge(a,b): return sync._merge_table('proposals',a,None,b)['merged']
        joined = merge(left,right)
        assert joined == merge(right,left) and joined[0]['outcome'] == 'accepted'
        assert len(joined[0]['payload']['_decisions']['receipts']) == 2
        assert merge(joined,joined) == joined
        row = db.get(Proposal,pid); row.payload=base; db.commit()
        decide(db,pid,[{'item_id':stable[0],'action':'reject'}]); opposite = dump_table(db,'proposals')
        with pytest.raises(sync.SyncAbort,match='结算冲突'):
            merge(left,opposite)


def test_schedule_drag_and_minutes_provenance(client):
    with TestingSessionLocal() as db:
        row = create_proposal(db,kind='minutes',payload={'meeting_task_id':'m','meeting_title':'评审',
            'items':[{'title':'整理问题','notes':'核对需求','priority':1}]})
        cmd = {'items':[{'item_id':row.item_ids[0],'action':'schedule',
                         'entry':{'start':'2026-11-30T09:00','duration':45}}]}
        url = f'/api/ai/proposals/{row.id}/decide'
    receipt = client.post(url,json=cmd).json()['receipts'][0]
    with TestingSessionLocal() as db:
        row = db.get(Task,receipt['task_id'])
        assert receipt['scheduled'] and row.duration_mins == 45 and row.source_key == 'minutes:m'
        assert row.notes == '核对需求（来自《评审》纪要）' and row.priority == 1
