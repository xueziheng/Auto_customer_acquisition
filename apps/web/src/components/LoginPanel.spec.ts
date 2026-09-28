import { afterEach, expect, it, vi } from "vitest";
import { createApp, nextTick } from "vue";
import LoginPanel from "./LoginPanel.vue";
const login = vi.fn();
vi.mock("../api/authentication", () => ({ login: (...args: unknown[]) => login(...args) }));
afterEach(() => { document.body.replaceChildren(); vi.clearAllMocks(); });
it("中文登录表单失败后清空密码，阻止重复提交并提供固定错误", async () => {
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp(LoginPanel); app.mount(host);
  const fields = host.querySelectorAll("input");
  expect(fields.length).toBe(2);
  fields[0]!.value = "synthetic"; fields[0]!.dispatchEvent(new Event("input"));
  fields[1]!.value = crypto.randomUUID(); fields[1]!.dispatchEvent(new Event("input"));
  login.mockRejectedValueOnce(new Error("登录失败，请检查账号、密码及本机服务"));
  host.querySelector("form")!.dispatchEvent(new Event("submit"));
  await nextTick(); await nextTick();
  expect(fields[1]!.value.length).toBe(0);
  expect(host.textContent).toContain("登录失败");
  app.unmount();
});

it("接受邮箱登录并且不猜测尚未读取的服务配置", async () => {
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp(LoginPanel); app.mount(host);
  const fields = host.querySelectorAll("input");
  const email = `${"owner".repeat(10)}@subdomain.example.test`;
  fields[0]!.value = ` ${email} `; fields[0]!.dispatchEvent(new Event("input"));
  fields[1]!.value = crypto.randomUUID(); fields[1]!.dispatchEvent(new Event("input"));
  login.mockResolvedValueOnce(undefined);
  host.querySelector("form")!.dispatchEvent(new Event("submit"));
  await nextTick(); await nextTick();
  expect(fields[0]!.maxLength).toBe(254);
  expect(login.mock.calls.at(-1)?.[0]).toBe(email);
  expect(host.textContent).toContain("邮箱或用户名");
  expect(host.textContent).not.toContain("邮件未配置");
  app.unmount();
});
