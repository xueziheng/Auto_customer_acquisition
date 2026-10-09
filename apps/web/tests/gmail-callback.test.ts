import { expect, it } from "vitest";
import { captureGmailCallback } from "../src/gmail-callback";

it("clears callback query before retaining the code only in memory", () => {
  const changes: unknown[][] = [];
  const result = captureGmailCallback({
    pathname: "/inbox/mailbox/google-callback", search: "?code=synthetic-code&state=synthetic-state&scope=readonly",
  }, { replaceState: (...args: unknown[]) => { changes.push(args); } });
  expect(changes).toEqual([[null, "", "/inbox/mailbox"]]);
  expect(result).toEqual({ code: "synthetic-code", state: "synthetic-state", failed: false });
});

it("rejects missing or duplicate state and still clears the browser URL", () => {
  let cleaned = false;
  const result = captureGmailCallback({
    pathname: "/inbox/mailbox/google-callback", search: "?code=synthetic-code&state=a&state=b",
  }, { replaceState: () => { cleaned = true; } });
  expect(cleaned).toBe(true);
  expect(result?.failed).toBe(true);
});

it("leaves ordinary mailbox searches alone", () => {
  expect(captureGmailCallback({ pathname: "/inbox/mailbox", search: "?search=invoice" }, {
    replaceState: () => { throw new Error("ordinary navigation"); },
  })).toBeNull();
});
