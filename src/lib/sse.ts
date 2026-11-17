/**
 * POST SSE 流式消费单源（2026-11-17 架构批 4）。
 *
 * 此前分帧解析器在 lib/ai.ts streamChat 与 lib/api.ts askStats 逐字重复约
 * 25 行（连注释都同款）——加第三条 POST 流就会长出第三份。帧协议本身是
 * 后端 sse_kit 的镜像：data 帧恒单行 JSON，": connected"/": ping" 注释帧
 * 以空载荷形态被跳过；坏载荷静默丢弃（后端口径内不该发生，不炸整流）。
 *
 * 刻意不做重连：这两条流是请求作用域（一轮对话/一次问答），失败语义是
 * finally 重拉真值兜底（App 层既有设计），自动重发请求会重复落用户消息。
 * 长订阅（提醒/工作流事件）走 EventSource，重连由浏览器原生提供。
 */
export async function streamSsePost<T>(
  url: string,
  body: unknown,
  label: string,
  onEvent: (ev: T) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`${label}请求失败: ${res.status}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // SSE 帧以空行（\n\n）分隔；帧内取 "data: " 前缀行拼载荷（本协议恒单行）
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const data = frame
        .split("\n")
        .filter((l) => l.startsWith("data: "))
        .map((l) => l.slice(6))
        .join("\n");
      if (!data) continue; // ": connected" / ": ping" 注释帧
      try {
        onEvent(JSON.parse(data) as T);
      } catch {
        // 坏帧丢弃
      }
    }
  }
}
