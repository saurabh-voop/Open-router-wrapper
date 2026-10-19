import { expect, test } from "@playwright/test";
import { conversation, json, me, message, mockApi, sse, stream } from "./mock-api";

const MODEL = "anthropic/claude-sonnet-4.5";

test("sending a message streams tokens, shows the switch notice and the model badge", async ({ page }) => {
  let sendBody: unknown = null;
  let created = false;
  let releaseDetail!: () => void;
  const detailGate = new Promise<void>((resolve) => (releaseDetail = resolve));

  const routing = {
    category: "coding",
    reason: "asks to write a Python script",
    switched: true,
    forced: false,
    chosen_model: MODEL,
    answered_model: MODEL,
    overridden_to: null,
  };

  await mockApi(page, {
    "GET /me": (route) => json(route, me),
    "GET /conversations": (route) =>
      json(route, created ? [conversation("c1", "Write a python script", { current_model: MODEL })] : []),
    "POST /conversations": (route) => {
      created = true;
      return json(route, conversation("c1"), 201);
    },
    "POST /conversations/c1/messages": (route, request) => {
      sendBody = request.postDataJSON();
      return stream(
        route,
        sse([
          {
            type: "route",
            message_id: "a1",
            model: MODEL,
            category: "coding",
            reason: "asks to write a Python script",
            switched: true,
            forced: false,
            confidential: false,
          },
          { type: "token", text: "Here is " },
          { type: "token", text: "**the script**" },
          { type: "tool_start", tool_run_id: "t1", tool: "run_python", input: { code: "print(1)" } },
          {
            type: "tool_end",
            tool_run_id: "t1",
            tool: "run_python",
            ok: true,
            summary: "printed 1",
            duration_ms: 1234,
          },
          { type: "citation", index: 1, url: "https://docs.python.org/3/", title: "Python docs" },
          {
            type: "usage",
            cost_usd: "0.0100",
            prompt_tokens: 100,
            completion_tokens: 20,
            month_spend_usd: "3.22",
            monthly_cap_usd: "50.00",
            budget_warning: false,
          },
          { type: "done", message_id: "a1" },
        ]),
      );
    },
    // Hold the post-stream refresh so the live (streamed) state can be asserted first.
    "GET /conversations/c1": async (route) => {
      await detailGate;
      await json(route, {
        ...conversation("c1", "Write a python script", { current_model: MODEL, current_category: "coding" }),
        messages: [
          message({ id: "m1", role: "user", text: "Write a python script" }),
          message({
            id: "a1",
            role: "assistant",
            parent_id: "m1",
            text: "Here is **the script** (stored)",
            model: MODEL,
            routing,
            cost_usd: "0.0100",
            tool_runs: [
              {
                id: "t1",
                tool: "run_python",
                status: "succeeded",
                input: {},
                output_summary: "printed 1",
                duration_ms: 1234,
              },
            ],
            citations: [{ index: 1, url: "https://docs.python.org/3/", title: "Python docs" }],
          }),
        ],
      });
    },
  });

  await page.goto("/");
  await expect(page.getByTestId("spend")).toHaveText("$3.21 / $50.00");

  const input = page.getByTestId("composer-input");
  await input.fill("Write a python script");
  await input.press("Enter");

  // Live, streamed state.
  const answer = page.getByTestId("assistant-message");
  await expect(answer.getByTestId("answer-text")).toContainText("Here is the script");
  await expect(answer.locator("strong")).toHaveText("the script");
  await expect(page.getByTestId("route-notice")).toHaveText(/Switched to claude-sonnet-4\.5 for coding\./);
  const badge = answer.getByTestId("model-badge");
  await expect(badge).toContainText("claude-sonnet-4.5");
  await expect(badge.locator("[title]").first()).toHaveAttribute("title", MODEL);
  await expect(answer.getByTestId("tool-run")).toContainText("Ran run_python · 1.2s");
  await expect(answer.getByRole("link", { name: "Python docs" })).toHaveAttribute("href", "https://docs.python.org/3/");
  await expect(page.getByTestId("spend")).toHaveText("$3.22 / $50.00");
  await expect(page.getByTestId("user-message")).toHaveText("Write a python script");
  await expect(page).toHaveURL(/\?c=c1$/);

  expect(sendBody).toEqual({
    text: "Write a python script",
    attachment_ids: [],
    web_search: false,
    code_execution: false,
    connectors: false,
  });

  // After `done`, the stored conversation replaces the live view; the notice persists.
  releaseDetail();
  await expect(answer.getByTestId("answer-text")).toContainText("(stored)");
  await expect(page.getByTestId("route-notice")).toBeVisible();
  await expect(answer.getByTestId("model-badge")).toContainText("claude-sonnet-4.5");
  // The sidebar refreshes and shows the auto title.
  await expect(page.getByTestId("conversation-list")).toContainText("Write a python script");
  await expect(page.getByTestId("thread-title")).toHaveText("Write a python script");
});

test("a 409 from the send endpoint shows the detail message", async ({ page }) => {
  await mockApi(page, {
    "GET /me": (route) => json(route, me),
    "GET /conversations": (route) => json(route, [conversation("c9", "Busy chat")]),
    "GET /conversations/c9": (route) => json(route, { ...conversation("c9", "Busy chat"), messages: [] }),
    "POST /conversations/c9/messages": (route) =>
      json(route, { detail: "Wait for the current answer to finish." }, 409),
  });
  await page.goto("/?c=c9");
  await expect(page.getByTestId("thread-title")).toHaveText("Busy chat");
  await page.getByTestId("composer-input").fill("hello");
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Wait for the current answer to finish." })).toBeVisible();
  await expect(page.getByTestId("composer-input")).toHaveValue("hello");
});
