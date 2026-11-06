// 共享 SSE 解析与实时连接：先缓存事件，再用数据库快照对齐后交付。

export function parseSse(buffer, onEvent) {
  const parts = buffer.split("\n\n");
  const rest = parts.pop();
  for (const part of parts) {
    const dataLine = part.split("\n").find((line) => line.startsWith("data: "));
    if (!dataLine) continue;
    onEvent(JSON.parse(dataLine.slice(6)));
  }
  return rest;
}

export async function readSse(response, onEvent, signal = null) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = parseSse(buffer, onEvent);
    }
  } finally {
    if (signal?.aborted) await reader.cancel().catch(() => {});
    reader.releaseLock?.();
  }
}

export function createRealtimeConnection({ api, path, loadSnapshot, onSnapshot, onEvent, onError }) {
  let closed = false;
  let controller = null;
  let retryTimer = null;
  let attempt = 0;

  async function run() {
    const buffered = [];
    let snapshotReady = false;
    controller = new AbortController();
    let reading = null;
    try {
      const response = await api(path, { signal: controller.signal });
      reading = readSse(response, (event) => {
        if (event.type === "resync_required") throw new Error("需要重新同步实时消息");
        if (snapshotReady) onEvent(event);
        else buffered.push(event);
      }, controller.signal);
      reading.catch(() => {});
      const snapshot = await loadSnapshot();
      if (closed) return;
      onSnapshot(snapshot);
      attempt = 0;
      snapshotReady = true;
      for (const event of buffered) onEvent(event);
      await reading;
      if (!closed) throw new Error("实时连接已中断");
    } catch (error) {
      if (controller) controller.abort();
      if (reading) await reading.catch(() => {});
      if (closed) return;
      onError?.(error);
      if (error?.status === 401 || error?.status === 403) return;
      const delay = Math.min(15000, 1000 * (2 ** attempt));
      attempt += 1;
      retryTimer = setTimeout(run, delay);
    }
  }

  run();
  return {
    close() {
      closed = true;
      clearTimeout(retryTimer);
      controller?.abort();
    }
  };
}
