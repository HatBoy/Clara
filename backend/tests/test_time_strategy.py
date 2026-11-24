"""用户批准的四项时间策略：预览不写日程、到期恢复、周校准确认后才生效。"""
from datetime import date, datetime, time, timedelta

from conftest import TestingSessionLocal
from models import BehaviorEvent, Task
from services import ai_data, preference_slots, time_strategy
from services.settings import get_settings
from services.tasks import similar_done_durations


def test_focus_preview_and_nightly_note_do_not_move_meeting(client):
    tomorrow = date.today() + timedelta(days=1)
    zero = datetime.combine(tomorrow, time.min)
    with TestingSessionLocal() as db:
        settings = get_settings(db)
        settings.calendar_start_min, settings.calendar_end_min = 540, 660
        settings.lunch_enabled = settings.meeting_buffer_enabled = False
        meeting = Task(title='客户会议', start_time=zero-timedelta(days=1)+timedelta(hours=9), duration_mins=30)
        db.add(meeting)
        db.commit()
        key = meeting.id
    uri = '/api/strategy/focus'
    result = client.get(uri, params={'task_id':key,'original':date.today(),'day':tomorrow,'start':585,'end':615}).json()
    assert result == {'before':120,'after':45,'last_focus':True,'alternative':'09:30'}
    with TestingSessionLocal() as db:
        assert db.get(Task,key).start_time.date() == date.today()
        assert time_strategy.nightly_focus_note(db,[{'task_id':key,'title':'客户会议','start':(zero+timedelta(minutes=585)).isoformat(),'duration':30}],tomorrow)
    result = client.get(uri, params={'task_id':key,'original':date.today(),'day':tomorrow,'start':540,'end':570}).json()
    assert not result['last_focus'] and result['after'] == 90


def test_rule_pause_targets_schedule_day_and_keeps_original(client):
    rule = client.post('/api/ai/memory-rules',json={'category':'会议','content':'每天上午不排会'}).json()
    key = rule['id']
    paused = client.post(f'/api/strategy/rules/{key}/pause?pause=true').json()
    until = date.fromisoformat(paused['pause_until'])
    assert until.weekday() == 6 and paused['content'] == rule['content'] and paused['confidence'] == 1
    morning = datetime.combine(until,time(10))
    with TestingSessionLocal() as db:
        windows = preference_slots.parse_avoid_windows(ai_data.injected_rules(db).all)
        assert not preference_slots.hits_window(morning,30,windows)
        assert preference_slots.hits_window(morning+timedelta(days=1),30,windows)
    assert client.post(f'/api/strategy/rules/{key}/pause?pause=false').json()['pause_until'] is None


def test_calibration_counts_independent_weekdays_and_requires_confirmation(client):
    now = datetime.now()
    workdays = [now-timedelta(days=i) for i in range(7) if (now-timedelta(days=i)).weekday()<5]
    with TestingSessionLocal() as db:
        task = Task(title='项目评审')
        db.add(task)
        db.flush()
        key = task.id
        def sample(day):
            db.add(BehaviorEvent(task_id=key,kind='resize',planned_min=30,delta_min=15,created_at=day))
        for _ in range(4): sample(workdays[0])
        sample(workdays[1]); db.commit()
        assert not time_strategy.weekly_calibrations(db)
        sample(workdays[2]); db.commit()
        assert similar_done_durations(db,'项目评审') is None
    cards = client.get('/api/strategy/calibrations').json()
    assert cards == [{'title':'项目评审','previous':30,'minutes':45,'days':3}]
    assert client.post('/api/strategy/calibrations',params={'title':'项目评审','minutes':45}).status_code==200
    with TestingSessionLocal() as db:
        assert similar_done_durations(db,'项目评审！') == 45
        assert not time_strategy.weekly_calibrations(db)


def test_resize_with_unchanged_start_preserves_evidence_and_omits_exception_days(client):
    from schemas import TaskUpdate
    from services.tasks import update_task
    now = datetime.now().replace(second=0, microsecond=0)
    rule = client.post('/api/ai/memory-rules', json={'category':'作息','content':'上午留空'}).json()
    client.post(f"/api/strategy/rules/{rule['id']}/pause?pause=true")
    with TestingSessionLocal() as db:
        t = Task(title='评审', start_time=now, end_time=now+timedelta(minutes=30), duration_mins=30)
        db.add(t); db.commit()
        for minutes in [45,30,45]:
            update_task(db,t.id,TaskUpdate(start_time=now,end_time=now+timedelta(minutes=minutes)))
        events = db.query(BehaviorEvent).filter_by(task_id=t.id,kind='resize').all()
        assert sorted(ev.planned_min for ev in events) == [30,30,45]
        assert all(ev.context['temporary_exception'] for ev in events)
        assert ai_data.aggregate_deviations(db) == []
