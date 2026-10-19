import { expect, test } from "@playwright/test";
import { conversation, json, me, message, mockApi, sse, stream } from "./mock-api";

const MODEL = "openai/gpt-5";
const routing = {
  category: "actions",
  reason: "asks to send an email",
  switched: false,
  forced: false,
  chosen_model: MODEL,
  answered_model: MODEL,
  overridden_to: null,
};
const toolInput = { to: "client@example.com", subject: "Invoice", body: "Attached." };

test("approval_required shows an approval card and Approve resumes the answer", async ({ page }) => {
  let phase: "empty" | "paused" | "done" = "empty";
  let approveCalls = 0;

  const user = message({ id: "m1", role: "user", text: "Email the invoice to the client" });
  const answer = (status: string, text: string, runStatus: string) =>
    message({
      id: "a1",
      role: "assistant",
      parent_id: "m1",
      text,
      status,
      model: MODEL,
      routing,
      tool_runs: [
        { id: "tr1", tool: "gmail_send", status: runStatus, input: toolInput, output_summary: null, duration_ms: null },
      ],
    });

  await mockApi(page, {
    "GET /me": (route) => json(route, me),
    "GET /conversations": (route) => json(route, [conversation("c2", "Invoice email")]),
    "GET /conversations/c2": (route) =>
      json(route, {
        ...conversation("c2", "Invoice email"),
        messages:
          phase === "empty"
            ? []
            : phase === "paused"
              ? [user, answer("awaiting_approval", "I'll send this email:", "pending_approval")]
              : [user, answer("done", "I'll send this email:\n\nSent the invoice.", "succeeded")],
      }),
    "POST /conversations/c2/messages": (route) => {
      phase = "paused";
      return stream(
        route,
        sse([
          {
            type: "route",
            message_id: "a1",
            model: MODEL,
            category: "actions",
            reason: "asks to send an email",
            switched: false,
            forced: false,
            confidential: false,
          },
          { type: "token", text: "I'll send this email:" },
          { type: "approval_required", tool_run_id: "tr1", tool: "gmail_send", input: toolInput },
          { type: "done", message_id: "a1", paused: true },
        ]),
      );
    },
    "POST /tool-runs/tr1/approve": (route) => {
      approveCalls += 1;
      phase = "done";
      return stream(
        route,
        sse([
          { type: "tool_start", tool_run_id: "tr1", tool: "gmail_send", input: toolInput },
          { type: "tool_end", tool_run_id: "tr1", tool: "gmail_send", ok: true, summary: "sent", duration_ms: 800 },
          { type: "token", text: "\n\nSent the invoice." },
          { type: "done", message_id: "a1" },
        ]),
      );
    },
  });

  await page.goto("/?c=c2");
  await expect(page.getByTestId("thread-title")).toHaveText("Invoice email");

  await page.getByTestId("composer-input").fill("Email the invoice to the client");
  await page.getByTestId("composer-input").press("Enter");

  const card = page.getByTestId("approval-card");
  await expect(card).toBeVisible();
  await expect(card).toContainText("gmail_send");
  await expect(card).toContainText('"to": "client@example.com"');

  const approveRequest = page.waitForRequest(
    (req) => req.method() === "POST" && new URL(req.url()).pathname === "/api/tool-runs/tr1/approve",
  );
  await card.getByRole("button", { name: "Approve" }).click();
  await approveRequest;
  expect(approveCalls).toBe(1);

  const answerEl = page.getByTestId("assistant-message");
  await expect(answerEl).toContainText("Sent the invoice.");
  await expect(answerEl.getByTestId("tool-run")).toContainText("Ran gmail_send");
  await expect(page.getByTestId("approval-card")).toHaveCount(0);
  // The answer stays a single message (the continuation streamed into it).
  await expect(answerEl).toHaveCount(1);
});
