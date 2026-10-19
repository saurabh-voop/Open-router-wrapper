import { expect, test } from "@playwright/test";
import { SSEParser } from "../src/lib/sse";

test("SSE parser reassembles frames split across reads", () => {
  const wire =
    'data: {"type":"token","text":"Hel"}\n\n' +
    ": keep-alive\n\n" +
    'data: {"type":"token","text":"lo\\n\\nworld"}\r\n\r\n' +
    'data: {"type":"done","message_id":"a1"}\n\n';
  // Split at every possible position, including inside "\n\n" and inside JSON.
  for (let cut = 1; cut < wire.length; cut++) {
    const parser = new SSEParser();
    const events = [...parser.push(wire.slice(0, cut)), ...parser.push(wire.slice(cut)), ...parser.end()];
    expect(events).toEqual([
      { type: "token", text: "Hel" },
      { type: "token", text: "lo\n\nworld" },
      { type: "done", message_id: "a1" },
    ]);
  }
});

test("SSE parser flushes a final frame without a trailing blank line", () => {
  const parser = new SSEParser();
  expect(parser.push('data: {"type":"done","message_id":"x"}')).toEqual([]);
  expect(parser.end()).toEqual([{ type: "done", message_id: "x" }]);
});
