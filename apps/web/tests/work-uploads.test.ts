import { createApp, nextTick, type App as VueApp } from "vue";
import { describe, expect, it, vi } from "vitest";

import type { components } from "../src/api/api";
import { createApiClient } from "../src/api/client";
import App from "../src/App.vue";
import router from "../src/router";

type Extraction = components["schemas"]["WorkExtractionView"];
type Payload = components["schemas"]["ExtractionPayload"];
type Upload = components["schemas"]["WorkUploadView"];

const upload: Upload = {
  account_id: null,
  artifact_id: "art_01K39P9M5D6K4A91YEQ80EJZ0X",
  created_at: "2026-08-23T08:00:00Z",
  customer_timezone: "Asia/Shanghai",
  employee_id: "emp_01K39P9M5D6K4A91YEQ80EJZ0X",
  need_id: null,
  occurred_at: "2026-08-23T07:30:00Z",
  opportunity_id: null,
  source_kind: "pdf_text",
  status: "awaiting_confirmation",
  tenant_id: "tn_01K39P9M5D6K4A91YEQ80EJZ0X",
  upload_id: "upl_01K39P9M5D6K4A91YEQ80EJZ0X",
};

const payload: Payload = {
  commitments: [{
    action: "发送正式规格",
    commitment_type: "employee",
    due_at: "2026-08-25T09:00:00+08:00",
    due_at_uncertain: false,
    verbatim: "I will send the final specification on Monday.",
  }],
  facts: [{
    evidence_quote: "We need 500 units",
    fact_type: "customer_statement",
    value: "500 units",
  }],
  need_field_updates: [{
    evidence_quote: "Ship to Rotterdam",
    field_name: "destination",
    value: "Rotterdam",
  }],
  progress_note: {
    evidence_quotes: ["Customer asked for the final specification"],
    summary: "客户等待最终规格",
  },
};

const extraction: Extraction = {
  confirmation: null,
  created_at: "2026-08-23T08:01:00Z",
  extracted_by: "team-operations-v1",
  extraction_id: "wex_01K39P9M5D6K4A91YEQ80EJZ0X",
  payload,
  upload_id: upload.upload_id,
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function asRequest(input: URL | RequestInfo): Request {
  if (input instanceof Request) return input;
  throw new TypeError("fake transport requires a Request");
}

async function eventually(assertion: () => void): Promise<void> {
  let latestError: unknown;
  for (let attempt = 0; attempt < 80; attempt += 1) {
    await nextTick();
    await new Promise((resolve) => setTimeout(resolve, 0));
    try {
      assertion();
      return;
    } catch (error) {
      latestError = error;
    }
  }
  throw latestError;
}

async function mountPage(fetch: typeof globalThis.fetch): Promise<{
  app: VueApp;
  root: HTMLElement;
}> {
  const root = document.createElement("div");
  document.body.replaceChildren(root);
  const app = createApp(App);
  app.provide(
    "tradeos-api-client",
    createApiClient({ baseUrl: "https://tradeos.test", fetch }),
  );
  app.use(router);
  app.mount(root);
  await router.replace("/work-uploads");
  await new Promise((resolve) => setTimeout(resolve, 0));
  return { app, root };
}

describe("WorkUploads", () => {
  it("renders the original, immutable Agent extraction and editable employee revision", async () => {
    const confirmations: Payload[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = asRequest(input);
      const url = new URL(request.url);
      if (request.method === "GET" && url.pathname === "/work-uploads") {
        return jsonResponse([upload]);
      }
      if (request.method === "GET" && url.pathname.endsWith("/artifact")) {
        return new Response("pdf", {
          status: 200,
          headers: { "content-type": "application/octet-stream" },
        });
      }
      if (request.method === "GET" && url.pathname.endsWith("/extraction")) {
        return jsonResponse(extraction);
      }
      if (request.method === "POST" && url.pathname.endsWith("/confirm")) {
        const corrected = await request.json() as Payload;
        confirmations.push(corrected);
        return jsonResponse({
          ...extraction,
          confirmation: {
            confirmation_id: "wcf_01K39P9M5D6K4A91YEQ80EJZ0X",
            confirmed_at: "2026-08-23T08:05:00Z",
            confirmed_by: upload.employee_id,
            payload: corrected,
            revision: 1,
          },
        });
      }
      return jsonResponse({ code: "unexpected", message: "unexpected" }, 500);
    });
    const { root } = await mountPage(fetch);

    await eventually(() => {
      expect(root.textContent).toContain("等待员工确认");
    });
    const open = [...root.querySelectorAll("button")].find((candidate) =>
      candidate.textContent?.includes("打开对照"),
    );
    expect(open).toBeTruthy();
    (open as HTMLButtonElement).click();

    await eventually(() => {
      expect(root.textContent).toContain("原件证据");
      expect(root.textContent).toContain("智能助手原始提取");
      expect(root.textContent).toContain("员工修订版本");
      expect(root.textContent).toContain("We need 500 units");
    });
    const factInput = root.querySelector<HTMLInputElement>(
      '[data-revision-fact="0"] input',
    );
    expect(factInput).not.toBeNull();
    if (factInput) {
      factInput.value = "600 units";
      factInput.dispatchEvent(new Event("input"));
    }
    const confirm = [...root.querySelectorAll("button")].find((candidate) =>
      candidate.textContent?.includes("确认修订并生效"),
    );
    expect(confirm).toBeTruthy();
    (confirm as HTMLButtonElement).click();

    await eventually(() => {
      expect(confirmations).toHaveLength(1);
      expect(confirmations[0]?.facts[0]?.value).toBe("600 units");
      expect(root.textContent).toContain("已确认版本 1");
    });
    expect(extraction.payload.facts[0]?.value).toBe("500 units");
  });

  it("uploads a supported local file and refreshes the employee ledger", async () => {
    const uploads: Upload[] = [];
    let rawRequest: Request | null = null;
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = asRequest(input);
      const url = new URL(request.url);
      if (request.method === "GET" && url.pathname === "/work-uploads") {
        return jsonResponse(uploads);
      }
      if (request.method === "POST" && url.pathname === "/work-uploads") {
        rawRequest = request;
        uploads.unshift({ ...upload, status: "uploaded" });
        return jsonResponse(uploads[0], 201);
      }
      return jsonResponse({ code: "unexpected", message: "unexpected" }, 500);
    });
    const { root } = await mountPage(fetch);
    await eventually(() => {
      expect(root.textContent).toContain("还没有上传记录");
    });
    const fileInput = root.querySelector<HTMLInputElement>('input[type="file"]');
    expect(fileInput).not.toBeNull();
    const file = new File(["pdf"], "客户询价.pdf", { type: "application/pdf" });
    Object.defineProperty(fileInput, "files", { value: [file] });
    fileInput?.dispatchEvent(new Event("change"));
    await eventually(() => {
      expect(root.textContent).toContain("客户询价.pdf");
    });
    const submit = [...root.querySelectorAll("button")].find((candidate) =>
      candidate.textContent?.includes("上传并进入提取队列"),
    );
    expect(submit).toBeTruthy();
    (submit as HTMLButtonElement).click();

    await eventually(() => {
      expect(rawRequest).not.toBeNull();
      expect(root.textContent).toContain(upload.upload_id);
      expect(root.textContent).toContain("已保存，等待提取");
    });
    const request = rawRequest as unknown as Request;
    expect(request.headers.get("content-type")).toBe("application/pdf");
    expect(new URL(request.url).searchParams.get("source_kind")).toBe("pdf_text");
    expect(await request.text()).toBe("pdf");
  });
});
