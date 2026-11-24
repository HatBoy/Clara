import { useEffect, useState } from "react";
import { getJson, request } from "../lib/api";

type Calibration = { title: string; previous: number; minutes: number; days: number };

export default function WeeklyCalibration({ refreshKey }: { refreshKey?: number }) {
  const [cards, setCards] = useState<Calibration[]>([]);
  const [notice, setNotice] = useState("");
  useEffect(() => {
    let active = true;
    void getJson<Calibration[]>("/api/strategy/calibrations").then(v => { if (active) setCards(v); }).catch(() => {});
    return () => { active = false; };
  }, [refreshKey]);
  if (!cards.length && !notice) return null;
  return <section className="report-card"><strong>本周估时校准</strong>
    {cards.map(card => <p key={card.title}>近七天有 {card.days} 个工作日，「{card.title}」的计划时长从 {card.previous} 分钟调整到 {card.minutes} 分钟。下次是否按此预留？
      <button type="button" onClick={async () => {
        try {
          await request("POST", `/api/strategy/calibrations?${new URLSearchParams({title:card.title, minutes:String(card.minutes)})}`);
          setCards(prev => prev.filter(v => v.title !== card.title));
          setNotice(`已将「${card.title}」的建议估时设为 ${card.minutes} 分钟。明确指定时长仍优先。`);
        } catch { setNotice("未能采纳，请刷新本周报告后重试。"); }
      }}>按 {card.minutes} 分钟预留</button></p>)}
    {notice && <p role="status">{notice}</p>}
  </section>;
}
