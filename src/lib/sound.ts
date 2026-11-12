// 提示音播放（设置页预览与日程提醒触发共用）：模块级单例——切档/连发先停旧
// 再播新；加载/自动播放失败静默降级（音效缺席不阻塞提醒呈现）。off 直接不播。
import type { Sound } from "./settings";

let current: HTMLAudioElement | null = null;

export function playSound(name: Sound): void {
  if (name === "off") return;
  current?.pause();
  current = new Audio(`${import.meta.env.BASE_URL}${name}.mp3`);
  current.play().catch(() => {});
}
