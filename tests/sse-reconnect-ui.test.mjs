import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { SourceTextModule } from "node:vm";

const module = new SourceTextModule(await readFile(new URL("../app/static/sse.js", import.meta.url), "utf8"));
await module.link(() => { throw new Error("sse.js should not import other modules"); });
await module.evaluate();
const { createRealtimeConnection } = module.namespace;
const encoder = new TextEncoder();

function responseFor(events, hang = true) {
  const frames = events.map((event) => encoder.encode(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`));
  return {
    body: {
      getReader: () => ({
        async read() {
          if (frames.length) return { done: false, value: frames.shift() };
          if (hang) return new Promise(() => {});
          return { done: true };
        },
        async cancel() {},
        releaseLock() {}
      })
    }
  };
}

const ordered = [];
let releaseSnapshot;
const snapshotGate = new Promise((resolve) => { releaseSnapshot = resolve; });
const first = createRealtimeConnection({
  api: async () => responseFor([{ type: "support_message", messageId: 9, content: "缓存事件" }]),
  path: "/events",
  loadSnapshot: () => snapshotGate,
  onSnapshot: () => ordered.push("snapshot"),
  onEvent: (event) => ordered.push(event.content)
});
await new Promise((resolve) => setTimeout(resolve, 10));
assert.deepEqual(ordered, []);
releaseSnapshot([]);
await new Promise((resolve) => setTimeout(resolve, 10));
assert.deepEqual(ordered, ["snapshot", "缓存事件"]);
first.close();

let snapshotLoads = 0;
const second = createRealtimeConnection({
  api: async () => responseFor([{ type: "resync_required" }], false),
  path: "/events",
  loadSnapshot: async () => { snapshotLoads += 1; return []; },
  onSnapshot: () => {},
  onEvent: () => {}
});
await new Promise((resolve) => setTimeout(resolve, 1150));
assert.equal(snapshotLoads, 2);
second.close();
console.log("SSE snapshot buffering and reconnect regressions passed");
