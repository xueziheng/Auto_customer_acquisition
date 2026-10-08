import { createApp, nextTick, type App } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "../src/api/api";
import { createApiClient, type WebIdentityProvider, type WebRequestIdentity } from "../src/api/client";
import KnowledgeCenter from "../src/views/knowledge/KnowledgeCenter.vue";

type Document = components["schemas"]["KnowledgeDocumentView"];
type Detail = components["schemas"]["KnowledgeDocumentDetail"];
const tenant = "tn_01K39P9M5D6K4A91YEQ80EJZ0X";
const employee = "emp_01K39P9M5D6K4A91YEQ80EJZ0X";
const documentFixture: Document = {
  document_id: "kdoc_fixture", tenant_id: tenant,
  source: { filename: "valve.txt", mime_type: "text/plain", size_bytes: 14, sha256: "a".repeat(64), artifact_id: "art_fixture" },
  uploader_id: employee, status: "awaiting_confirmation", version: 3, job_id: "run_fixture",
  current_revision_id: "revision_fixture", export_state: "synced", failure_reason: null,
  can_retry: false, can_confirm: false, can_sync: false,
  created_at: "2026-10-08T04:00:00Z", updated_at: "2026-10-08T04:00:10Z",
};
function details(doc: Document = documentFixture): Detail {
  return {
    document: doc,
    revision: {
      revision_id: "revision_fixture", document_id: doc.document_id, job_id: doc.job_id,
      source_kind: "document_text", image_count: 0, image_source_pages: [], parse_warnings: [],
      source_text: "Material steel", analysis: {
        title: "Valve specifications",
        facts: [{ label: "Material", value: "steel", source_quote: "Material steel" }],
        inferences: [{ text: "May suit industrial applications", evidence_quotes: ["Material steel"], image_pages: [] }],
      },
      model: "synthetic-model", extracted_by: "synthetic-codex", extracted_at: "2026-10-08T04:00:10Z",
      fact_provenance: [], confirmed_by: null, confirmed_at: null,
    },
  };
}
function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}
function provider() {
  let revision = 1;
  let identity: WebRequestIdentity | null = { tenantId: tenant, employeeId: employee, mode: "fixed-dev" };
  const listeners = new Set<() => void>();
  const source: WebIdentityProvider = {
    current: () => identity, generation: () => revision,
    subscribe: (listener) => { listeners.add(listener); return () => { listeners.delete(listener); }; },
  };
  return { source, change(next: WebRequestIdentity | null) { identity = next; revision += 1; for (const listener of listeners) listener(); } };
}
const apps: App[] = [];
afterEach(() => { for (const app of apps.splice(0)) app.unmount(); document.body.replaceChildren(); vi.useRealTimers(); });
async function eventually(check: () => void): Promise<void> {
  let failure: unknown;
  for (let i = 0; i < 80; i += 1) {
    await nextTick(); await new Promise(resolve => globalThis.setTimeout(resolve, 0));
    try { check(); return; } catch (error) { failure = error; }
  }
  throw failure;
}
function mount(fetch: typeof globalThis.fetch, identity = provider()) {
  const root = document.createElement("div"); document.body.append(root);
  const client = createApiClient({ baseUrl: "https://tradeos.test", fetch }, identity.source);
  const app = createApp(KnowledgeCenter); app.provide("tradeos-api-client", client); app.mount(root); apps.push(app);
  return { root, identity };
}
function request(input: URL | RequestInfo): Request {
  if (!(input instanceof Request)) throw new Error("expected request");
  return input;
}
function select(root: HTMLElement, file: File): void {
  const input = root.querySelector<HTMLInputElement>('input[type="file"]')!;
  Object.defineProperty(input, "files", { value: [file], configurable: true }); input.dispatchEvent(new Event("change"));
}
function button(root: HTMLElement, label: string): HTMLButtonElement {
  const found = [...root.querySelectorAll("button")].find(item => item.textContent?.includes(label));
  if (!found) throw new Error(`button missing: ${label}`);
  return found;
}
function transport(doc: Document, posts: Request[] = []) {
  return vi.fn<typeof globalThis.fetch>(async input => {
    const req = request(input); const path = new URL(req.url).pathname;
    if (req.method === "POST") { posts.push(req); return json(path.endsWith("/confirm") ? details(doc) : doc, 202); }
    return json(path === "/knowledge/documents" ? { items: [doc], total: 1, limit: 20, offset: 0 } : details(doc));
  });
}

describe("企业共享资料库", () => {
  it("显示共享资料、原文证据和明确AI推断；员工不能看到确认操作", async () => {
    const { root } = mount(transport(documentFixture));
    await eventually(() => expect(root.textContent).toContain("valve.txt"));
    button(root, "valve.txt").click();
    await eventually(() => expect(root.textContent).toContain("Material steel"));
    expect(root.textContent).toContain("AI 推断 · 尚未验证");
    expect(root.textContent).toContain("等待管理员核对");
    expect(root.textContent).not.toContain("已核对原件，确认此版本");
  });

  it("管理员确认只发送后端提供的版本及修订ID，不发送角色或租户", async () => {
    const posts: Request[] = [];
    const doc = { ...documentFixture, can_confirm: true };
    const { root } = mount(transport(doc, posts));
    await eventually(() => expect(root.textContent).toContain("valve.txt")); button(root, "valve.txt").click();
    await eventually(() => expect(root.textContent).toContain("已核对原件，确认此版本"));
    button(root, "已核对原件，确认此版本").click();
    await eventually(() => expect(posts).toHaveLength(1));
    expect(await posts[0]!.json()).toEqual({ expected_version: 3, revision_id: "revision_fixture" });
  });

  it("上传原文保留文件名、MIME和原幂等键，未知响应重试不换键", async () => {
    const uploads: Request[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async input => {
      const req = request(input);
      if (req.method === "POST") { uploads.push(req); throw new Error("synthetic transport uncertainty"); }
      return json({ items: [], total: 0, limit: 20, offset: 0 });
    });
    const { root } = mount(fetch);
    select(root, new File(["Material steel"], "new-spec.txt", { type: "text/plain" }));
    await nextTick(); button(root, "上传并整理").click();
    await eventually(() => expect(root.textContent).toContain("上传结果待核对"));
    button(root, "上传并整理").click();
    await eventually(() => expect(uploads).toHaveLength(2));
    expect(uploads[0]!.headers.get("Idempotency-Key")).toBe(uploads[1]!.headers.get("Idempotency-Key"));
    expect(new URL(uploads[0]!.url).searchParams.toString()).toBe("filename=new-spec.txt");
    expect(uploads[0]!.headers.get("Content-Type")).toBe("text/plain");
    expect(await uploads[0]!.text()).toBe("Material steel");
  });

  it("拒绝不支持格式和大文件，不发起上传", async () => {
    const posts: Request[] = []; const { root } = mount(transport(documentFixture, posts));
    select(root, new File(["image"], "scan.heic", { type: "image/heic" })); await nextTick();
    expect(root.textContent).toContain("请选择 TXT");
    expect(button(root, "上传并整理").disabled).toBe(true);
    select(root, new File([new Uint8Array(10 * 1024 * 1024 + 1)], "large.txt", { type: "text/plain" })); await nextTick();
    expect(root.textContent).toContain("不能超过 10 MiB");
    expect(posts).toHaveLength(0);
  });

  it("图片判断单列为推断，标明AI转录和原PDF页码", async () => {
    const info = details();
    info.revision = { ...info.revision!, source_kind: "vision_transcription", image_count: 1, image_source_pages: [7], parse_warnings: ["vision_transcription_required"],
      analysis: { title: "Product photo", facts: [], inferences: [{ text: "Appears to be a valve", evidence_quotes: [], image_pages: [1] }] } };
    const fetch = vi.fn<typeof globalThis.fetch>(async input =>
      json(new URL(request(input).url).pathname === "/knowledge/documents"
        ? { items: [documentFixture], total: 1, limit: 20, offset: 0 } : info));
    const { root } = mount(fetch);
    await eventually(() => expect(root.textContent).toContain("valve.txt")); button(root, "valve.txt").click();
    await eventually(() => expect(root.textContent).toContain("AI 看图转录，需对照原件"));
    expect(root.textContent).toContain("没有可核验的文字事实");
    expect(root.textContent).toContain("图像 1 · 原文件第 7 页");
    expect(root.querySelector(".parse-warnings")?.textContent).toContain("包含需要 AI 看图转录的内容");
    expect(root.querySelector(".inference-section")?.textContent).toContain("Appears to be a valve");
    expect(root.querySelector(".fact-section")?.textContent).not.toContain("Appears to be a valve");
  });

  it.each([["photo.png", "image/png"], ["photo.jpg", "image/jpeg"], ["photo.webp", "image/webp"]])("上传图片 %s 保留真实MIME", async (filename, contentType) => {
    const posts: Request[] = [];
    const { root } = mount(transport(documentFixture, posts));
    select(root, new File(["synthetic-image-bytes"], filename, { type: contentType }));
    await nextTick(); button(root, "上传并整理").click();
    await eventually(() => expect(posts).toHaveLength(1));
    expect(posts[0]!.headers.get("Content-Type")).toBe(contentType);
    expect(new URL(posts[0]!.url).searchParams.get("filename")).toBe(filename);
  });

  it.each([
    ["spec.csv", "application/vnd.ms-excel", "text/csv"],
    ["spec.txt", "application/octet-stream", "text/plain"],
  ])("兼容浏览器MIME别名 %s，但发送扩展名对应的规范MIME", async (filename, browserMime, canonicalMime) => {
    const posts: Request[] = [];
    const { root } = mount(transport(documentFixture, posts));
    select(root, new File(["Synthetic material,steel"], filename, { type: browserMime }));
    await nextTick(); button(root, "上传并整理").click();
    await eventually(() => expect(posts).toHaveLength(1));
    expect(posts[0]!.headers.get("Content-Type")).toBe(canonicalMime);
    expect(new URL(posts[0]!.url).searchParams.get("filename")).toBe(filename);
  });

  it("拒绝真实文件类型与扩展名冲突，通用MIME也不能使未知扩展名通过", async () => {
    const posts: Request[] = [];
    const { root } = mount(transport(documentFixture, posts));
    for (const file of [
      new File(["synthetic image"], "spec.csv", { type: "image/png" }),
      new File(["synthetic document"], "spec.png", { type: "application/pdf" }),
      new File(["synthetic unknown"], "spec.exe", { type: "application/octet-stream" }),
    ]) {
      select(root, file); await nextTick();
      expect(root.textContent).toContain("请选择 TXT");
      expect(button(root, "上传并整理").disabled).toBe(true);
    }
    expect(posts).toHaveLength(0);
  });

  it("内容或输出超限给出拆分文件提示，不显示为未知模型结果", async () => {
    const doc: Document = { ...documentFixture, status: "failed", can_retry: true, failure_reason: "source_limit_exceeded" };
    const { root } = mount(transport(doc));
    await eventually(() => expect(root.textContent).toContain("valve.txt")); button(root, "valve.txt").click();
    await eventually(() => expect(root.textContent).toContain("请按产品或页码拆成更小的文件后上传"));
    expect(root.textContent).not.toContain("模型调用结果未知");
  });

  it("未知结果重试必须明确接受可能再次产生模型费用", async () => {
    const posts: Request[] = [];
    const doc: Document = { ...documentFixture, status: "unknown", can_retry: true, failure_reason: "model_result_unknown" };
    const { root } = mount(transport(doc, posts));
    await eventually(() => expect(root.textContent).toContain("valve.txt")); button(root, "valve.txt").click();
    await eventually(() => expect(root.textContent).toContain("我已核对记录"));
    expect(button(root, "重新整理").disabled).toBe(true);
    const checkbox = root.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
    checkbox.checked = true; checkbox.dispatchEvent(new Event("change")); await nextTick();
    button(root, "重新整理").click();
    await eventually(() => expect(posts).toHaveLength(1));
    expect(await posts[0]!.json()).toEqual({ expected_version: 3, acknowledge_unknown: true });
  });

  it("退出立即清空详情、文件和搜索，中止请求并忽略迟到的旧企业响应", async () => {
    let finish: (value: Response) => void = () => {};
    let pending: Request | undefined;
    const fetch = vi.fn<typeof globalThis.fetch>(async input => {
      const req = request(input);
      if (new URL(req.url).pathname !== "/knowledge/documents") {
        pending = req; return await new Promise<Response>(resolve => { finish = resolve; });
      }
      return json({ items: [documentFixture], total: 1, limit: 20, offset: 0 });
    });
    const { root, identity } = mount(fetch);
    await eventually(() => expect(root.textContent).toContain("valve.txt")); button(root, "valve.txt").click();
    await eventually(() => expect(pending).toBeDefined());
    select(root, new File(["new"], "new.txt", { type: "text/plain" }));
    identity.change(null); await nextTick();
    expect(pending!.signal.aborted).toBe(true);
    expect(root.textContent).not.toContain("valve.txt");
    expect(button(root, "上传并整理").disabled).toBe(true);
    finish(json(details()));
    await nextTick(); await new Promise(resolve => globalThis.setTimeout(resolve, 0)); await nextTick();
    expect(root.textContent).not.toContain("Valve specifications");
  });

  it("403清除已加载的详情，不把禁止访问渲染为空成功", async () => {
    let denied = false;
    const base = transport(documentFixture);
    const fetch = vi.fn<typeof globalThis.fetch>(async input => denied ? json({}, 403) : base(input));
    const { root } = mount(fetch);
    await eventually(() => expect(root.textContent).toContain("valve.txt")); button(root, "valve.txt").click();
    await eventually(() => expect(root.textContent).toContain("Valve specifications"));
    denied = true; button(root, "刷新").click();
    await eventually(() => expect(root.textContent).toContain("当前账号无权"));
    expect(root.textContent).not.toContain("Valve specifications");
    expect(root.textContent).not.toContain("valve.txt");
  });

  it("有界自动刷新到上限后暂停，后台任务不会被界面宣称取消", async () => {
    vi.useFakeTimers();
    const doc: Document = { ...documentFixture, status: "processing", current_revision_id: null };
    const fetch = transport(doc); const { root } = mount(fetch);
    await vi.advanceTimersByTimeAsync(0);
    for (let i = 0; i < 81; i += 1) await vi.advanceTimersByTimeAsync(3000);
    await nextTick();
    expect(root.textContent).toContain("自动刷新已暂停；后台任务继续处理");
    const count = fetch.mock.calls.length;
    await vi.advanceTimersByTimeAsync(30000);
    expect(fetch.mock.calls).toHaveLength(count);
  });
});
