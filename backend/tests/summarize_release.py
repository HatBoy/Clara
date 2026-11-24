"""只读取指定三轮结果，不把未采样的层次记为通过。"""
import json
import statistics
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# 三轮共同的聚焦验收清单；其他文件仍计入全库，不用文件名推算产品正确率。
GROUPS = {
    '1 排期常识': ('test_qa_d1_commonsense',),
    '2 管理哲学': ('test_qa_d2_philosophy', 'test_qa_d9_philosophy_matrix'),
    '3 幕僚长': ('test_qa_d3_scenarios', 'test_qa_d10_strategist_prep'),
    '4 记忆': ('test_qa_d4_memory',),
    '5 同步灾备': ('test_qa_d7_sync_local', 'test_qa_sync_wal', 'test_device_secrets', 'test_sidecar_exit'),
    '6 数据源': ('test_qa_d5_datasource', 'test_qa_d6_fallback'),
    '7 通讯': ('test_chat_channels',),
    '8 性能': ('test_qa_perf',),
}


def summarize(folder):
    cases = list(ET.parse(folder / 'results.xml').iter('testcase'))
    def passed(case):
        return all(case.find(k) is None for k in ('skipped', 'failure', 'error'))
    def counts(items):
        return {'通过': sum(passed(c) for c in items), '已收集': len(items)}
    times = json.loads((folder / 'startup.json').read_text(encoding='utf-8'))['samples']
    groups = {name: counts([c for c in cases if c.get('classname').split('.')[-1] in files])
              for name, files in GROUPS.items()}
    perf = [line for c in cases if c.get('classname','').endswith('test_qa_perf')
            for line in (c.findtext('system-out') or '').splitlines()
            if any(tag in line for tag in ('P50', 'P95', '容量：'))]
    return {'轮次': folder.name, '全库': counts(cases), '模块': groups,
            '源码启动中位毫秒': round(statistics.median(s['start_ms'] for s in times), 2),
            '源码启动原始样本': times, '性能': perf}


if __name__ == '__main__':
    print(json.dumps([summarize(Path(p)) for p in sys.argv[1:]], ensure_ascii=False, indent=2))
