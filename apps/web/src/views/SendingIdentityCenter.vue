<script setup lang="ts">
/* global document */
import { computed, inject, onMounted, reactive, ref } from "vue";
import { apiClient, createApiClient } from "../api/client";
import type { components } from "../api/api";
import { useQuoteRequestScope } from "./costing-quotes/quote-request-scope";
type IdentityView = components["schemas"]["IdentityView"];
type Registration = components["schemas"]["IdentityRegistrationBody"];
type InboundStatus = components["schemas"]["InboundStatus"];
type Review = components["schemas"]["InboundReviewView"];
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const identities = ref<IdentityView[]>([]), listLoading = ref(true), listError = ref<string | null>(null);
const checkFeedback = ref<string | null>(null), checkBusyId = ref<string | null>(null);
const binding = ref<InboundStatus | null>(null), bindingError = ref<string | null>(null);
const pendingRegistration = ref<Registration | null>(null), registrationUnknown = ref(false);
const warmupUnknown = ref<string | null>(null), busy = ref(false);
const selectedIdentity = ref<IdentityView | null>(null);
const reviews = ref<Review[] | null>(null), reviewError = ref<string | null>(null);
const form = reactive({ address: "", domain: "", role: "cold_outreach" as Registration["role"], display_name: "", connector_ref: "" });
const target = ref<number | null>(null);
const dialog = ref<{ kind: "register" } | { kind: "warmup"; identity: IdentityView; target: number } | { kind: "bind"; identity: IdentityView } | null>(null);
const requestKeys = new Map<string, string>();
function reset(): void {
  identities.value = []; selectedIdentity.value = null; binding.value = null; reviews.value = null;
  listError.value = null; checkFeedback.value = null; bindingError.value = null; reviewError.value = null;
  pendingRegistration.value = null; registrationUnknown.value = false; warmupUnknown.value = null;
  busy.value = false; checkBusyId.value = null; dialog.value = null; requestKeys.clear();
  Object.assign(form, { address: "", domain: "", role: "cold_outreach", display_name: "", connector_ref: "" }); target.value = null;
}
const gate = useQuoteRequestScope(client, () => [], () => { reset(); globalThis.queueMicrotask(() => void loadIdentities()); });
const formLocked = computed(() => busy.value || pendingRegistration.value !== null);
const stateLabels: Record<string,string> = {created:"已创建",auth_pending:"认证待修复",warming:"预热中",active:"活跃",throttled:"已限流",suspended:"已暂停",retired:"已退役"};
function protectedFailure(status: number): void {
  if ([401,403,404].includes(status)) { identities.value = []; selectedIdentity.value = null; binding.value = null; reviews.value = null; dialog.value = null; }
}
function putIdentity(value: IdentityView): void {
  selectedIdentity.value = value;
  identities.value = [...identities.value.filter(item => item.identity_id !== value.identity_id), value];
}
async function loadIdentities(): Promise<void> {
  const op = gate.begin("list"); if (!op?.valid()) return;
  listLoading.value = true; listError.value = null;
  try {
    const {data,response} = await client.GET("/crm/sending-identities/management", {params:{query:{limit:200}},signal:op.signal});
    if (!op.valid()) return;
    if (response.status !== 200 || !data) {
      identities.value = []; selectedIdentity.value = null; protectedFailure(response.status);
      listError.value = response.status === 403 ? "当前账号无法查看发件身份" : "发件身份加载失败，请刷新后重试";
    } else { identities.value = data; selectedIdentity.value = data.find(item => item.identity_id === selectedIdentity.value?.identity_id) ?? null; }
  } catch { if(op.valid()){identities.value=[];selectedIdentity.value=null;listError.value="发件身份加载失败，请刷新后重试";} }
  finally { if(op.valid()) listLoading.value=false; }
  if(op.valid()) await loadBinding();
}
async function loadBinding(): Promise<void> {
 const op=gate.begin("binding-read");if(!op?.valid())return;
 binding.value=null;bindingError.value=null;
 try {const result=await client.GET("/email-inbound/status",{signal:op.signal});if(!op.valid())return;
  if(result.response.status===200 && result.data)binding.value=result.data;
  else {bindingError.value="入站绑定状态暂不可读取";protectedFailure(result.response.status);}
 }catch {if(op.valid())bindingError.value="入站绑定状态暂不可读取";}
}
function prepareRegistration(): void {
 if(formLocked.value || !form.address.trim() || !form.domain.trim())return;
 pendingRegistration.value=Object.freeze({address:form.address.trim(),domain:form.domain.trim(),role:form.role,display_name:form.display_name.trim()||null,connector_ref:form.connector_ref.trim()||null,confirmed:true});
 dialog.value={kind:"register"};
}
function cancelDialog(): void {if(busy.value)return;if(dialog.value?.kind==="register")pendingRegistration.value=null;dialog.value=null;}
async function register(): Promise<void> {
 const payload=pendingRegistration.value;if(!payload || busy.value)return;
 const wasUnknown=registrationUnknown.value;
 const op=gate.begin("command");if(!op?.valid())return;busy.value=true;dialog.value=null;checkFeedback.value=null;
 try {const result=await client.POST("/crm/sending-identities",{body:payload,signal:op.signal});if(!op.valid())return;
  if(result.response.status===200&&result.data){putIdentity(result.data);pendingRegistration.value=null;registrationUnknown.value=false;checkFeedback.value="已核对登记身份，请继续认证检查";}
  else if(result.response.status>=500){registrationUnknown.value=true;checkFeedback.value="登记结果待核对；保留原登记内容，不要创建新的身份";}
  else {protectedFailure(result.response.status);if(wasUnknown){checkFeedback.value="原登记结果仍待核对，当前核对请求被拒绝";}else{pendingRegistration.value=null;registrationUnknown.value=false;checkFeedback.value="登记被拒绝，请核对权限与登记字段";}}
 }catch {if(op.valid()){registrationUnknown.value=true;checkFeedback.value="登记结果待核对；按原登记内容核对后再继续";}}
 finally {if(op.valid())busy.value=false;}
}
async function requestCheck(identityId:string): Promise<void> {
 if(checkBusyId.value || busy.value)return;
 const op=gate.begin("check");if(!op?.valid())return;checkBusyId.value=identityId;checkFeedback.value=null;
 const key=requestKeys.get(identityId)??globalThis.crypto.randomUUID();requestKeys.set(identityId,key);
 try {const result=await client.POST("/crm/sending-identities/{identity_id}/authentication-checks",{params:{path:{identity_id:identityId}},body:{request_key:key},signal:op.signal});if(!op.valid())return;
  if(result.response.status===200){checkFeedback.value="认证检查已提交；请刷新读取实际认证结果";if(result.data?.status==="succeeded"||result.data?.status==="failed"){requestKeys.delete(identityId);checkFeedback.value="此认证请求已完成，请刷新查看结果；再次检查会发起新请求";}}
  else {protectedFailure(result.response.status);checkFeedback.value=result.response.status===403?"当前账号无法查看发件身份":"认证检查结果待核对；重试保持原请求键";}
 }catch {if(op.valid())checkFeedback.value="认证检查结果待核对；重试保持原请求键";}
 finally {if(op.valid())checkBusyId.value=null;}
}
async function readExact(identityId:string): Promise<void> {
 const op=gate.begin("exact");if(!op?.valid())return;
 try {const result=await client.GET("/crm/sending-identities/{identity_id}",{params:{path:{identity_id:identityId}},signal:op.signal});if(!op.valid())return;
  if(result.response.status===200&&result.data?.identity_id===identityId){putIdentity(result.data);checkFeedback.value="已读取精确身份当前状态；原预热请求结果仍待核对，不自动重启";}
  else {protectedFailure(result.response.status);checkFeedback.value="精确身份状态暂不可读取；原请求仍待核对";}
 }catch {if(op.valid())checkFeedback.value="精确身份状态暂不可读取；原请求仍待核对";}
}
async function confirmCommand(): Promise<void> {
 const intent=dialog.value;if(!intent||busy.value)return;if(intent.kind==="register"){await register();return;}
 const op=gate.begin("command");if(!op?.valid())return;busy.value=true;dialog.value=null;checkFeedback.value=null;
 try {
  if(intent.kind==="warmup") {
   const result=await client.POST("/crm/sending-identities/{identity_id}/warmup",{params:{path:{identity_id:intent.identity.identity_id}},body:{target_daily_volume:intent.target,confirmed:true},signal:op.signal});
   if(!op.valid())return;
   if(result.response.status===200&&result.data){putIdentity(result.data);checkFeedback.value="预热已启动，日期与每日限额由原域固定曲线决定";}
   else if(result.response.status>=500){warmupUnknown.value=intent.identity.identity_id;checkFeedback.value="预热结果待核对，只读取精确身份当前状态";}
   else {protectedFailure(result.response.status);checkFeedback.value="预热被拒绝，请刷新核对权限、认证与当前状态";}
  } else {
   const result=await client.POST("/email-inbound/binding",{body:{identity_id:intent.identity.identity_id},signal:op.signal});if(!op.valid())return;
   if(result.response.status===200&&result.data){binding.value=result.data;checkFeedback.value="入站绑定已记录；处理状态待核对";}
   else {protectedFailure(result.response.status);checkFeedback.value="绑定未确认，请刷新核对当前绑定；不自动更换身份或重置同步位置";}
  }
 }catch {if(op.valid()){if(intent.kind==="warmup")warmupUnknown.value=intent.identity.identity_id;checkFeedback.value="操作结果待核对，请刷新精确记录，不自动重试";}}
 finally {if(op.valid())busy.value=false;}
}
async function loadReviews():Promise<void>{
 const op=gate.begin("reviews");if(!op?.valid())return;reviews.value=null;reviewError.value=null;
 try {const r=await client.GET("/email-inbound/reviews",{params:{query:{limit:50}},signal:op.signal});if(!op.valid())return;
  if(r.response.status===200&&r.data)reviews.value=r.data;else{reviewError.value="未关联邮件复核暂不可用";protectedFailure(r.response.status);}
 }catch {if(op.valid())reviewError.value="未关联邮件复核暂不可用";}
}
async function downloadReview(reviewId:string):Promise<void>{
 const op=gate.begin("review-download");if(!op?.valid())return;
 try{const r=await client.GET("/email-inbound/reviews/{review_id}/raw",{params:{path:{review_id:reviewId}},parseAs:"blob",cache:"no-store",signal:op.signal});if(!op.valid())return;
  if(r.response.status===200&&r.data){const url=globalThis.URL.createObjectURL(r.data);try{const a=document.createElement("a");a.href=url;a.download="inbound-review.eml";a.click();}finally{globalThis.URL.revokeObjectURL(url);}}
  else{protectedFailure(r.response.status);reviewError.value="复核原件暂不可用";}
 }catch{if(op.valid())reviewError.value="复核原件暂不可用";}
}
function warmupLabel(identity:IdentityView):string {return identity.warmup_day===null||identity.warmup_day===undefined?"尚未启动":`第 ${identity.warmup_day} / 28 天 · ${identity.warmup_complete?"已完成":"未完成"}`;}
function reputationLabel(identity:IdentityView):string {const r=identity.reputation;return r?`硬退信率 ${r.hard_bounce_rate} · 投诉率 ${r.complaint_rate}（代码确定性计算）`:"暂无样本";}
onMounted(()=>void loadIdentities());
</script>
<template>
  <div class="shell identity-shell">
    <div class="page-head">
      <h1>发件身份中心</h1><button
        :disabled="listLoading"
        @click="loadIdentities"
      >
        刷新状态
      </button>
    </div>
    <p class="meta">
      人工登记 → 认证检查 → 刷新核对认证 → 明确启动预热。仅显示后端事实，修改配置须逐次确认。
    </p>
    <form
      class="card register-form"
      @submit.prevent="prepareRegistration"
    >
      <h2>登记发件身份</h2>
      <label>发件地址<input
        v-model="form.address"
        aria-label="发件地址"
        type="email"
        required
        :disabled="formLocked"
      ></label>
      <label>发件域名<input
        v-model="form.domain"
        aria-label="发件域名"
        required
        :disabled="formLocked"
      ></label>
      <label>域角色<select
        v-model="form.role"
        aria-label="域角色"
        :disabled="formLocked"
      ><option value="cold_outreach">冷开发专用域</option><option value="primary_business">主业务域</option><option value="transactional">系统事务通知域</option></select></label>
      <label>显示名称（可选）<input
        v-model="form.display_name"
        aria-label="显示名称"
        :disabled="formLocked"
      ></label>
      <label>连接别名（可选，非凭证）<input
        v-model="form.connector_ref"
        aria-label="连接别名"
        :disabled="formLocked"
      ></label>
      <button
        type="submit"
        :disabled="formLocked"
      >
        登记发件身份
      </button>
      <button
        v-if="registrationUnknown"
        type="button"
        :disabled="busy"
        @click="register"
      >
        按原登记内容核对
      </button>
    </form>
    <div
      v-if="checkFeedback"
      class="feedback"
      role="status"
    >
      {{ checkFeedback }}
    </div>
    <p v-if="selectedIdentity">
      当前精确身份：{{ selectedIdentity.identity_id }} · {{ stateLabels[selectedIdentity.state] }} · 目标日量 {{ selectedIdentity.target_daily_volume??"未设置" }}
    </p>
    <button
      v-if="warmupUnknown"
      :disabled="busy"
      @click="readExact(warmupUnknown)"
    >
      核对原预热身份当前状态
    </button>
    <label>预热目标日量<input
      v-model.number="target"
      type="number"
      min="5"
      max="100"
      step="1"
      aria-label="预热目标日量"
      placeholder="请输入 5–100 的整数"
    ></label>
    <div
      v-if="listLoading"
      class="state"
      role="status"
    >
      正在加载发件身份…
    </div>
    <div
      v-else-if="listError"
      class="state"
      role="alert"
    >
      {{ listError }}
    </div>
    <div
      v-else-if="!identities.length"
      class="state"
    >
      暂无已登记发件身份
    </div>
    <section
      v-else
      class="grid"
      aria-label="发件身份卡片"
    >
      <article
        v-for="identity in identities"
        :key="identity.identity_id"
        class="card"
      >
        <div class="name">
          {{ identity.domain }}
        </div><div class="addr">
          {{ identity.address }}
        </div><code>{{ identity.identity_id }}</code>
        <div
          v-if="identity.auth"
          class="auth-grid"
        >
          <span
            v-for="kind in (['spf','dkim','dmarc'] as const)"
            :key="kind"
            class="auth-item"
            :class="identity.auth[`${kind}_passed`]?'pass':'fail'"
          >{{ kind.toUpperCase() }} {{ identity.auth[`${kind}_passed`]?"通过":"未通过" }}</span>
        </div>
        <p v-else>
          认证尚未完成
        </p>
        <dl class="kv">
          <dt>预热</dt><dd>{{ warmupLabel(identity) }}</dd><dt>今日剩余额度</dt><dd>{{ identity.remaining_today }} · 目标日量 {{ identity.target_daily_volume??"未设置" }}</dd><dt>信誉</dt><dd>{{ reputationLabel(identity) }}</dd><dt>状态</dt><dd>{{ stateLabels[identity.state] }}</dd>
        </dl>
        <button
          :disabled="checkBusyId!==null||busy"
          :aria-label="`对 ${identity.domain} 重新检查认证`"
          @click="requestCheck(identity.identity_id)"
        >
          重新检查认证
        </button>
        <button
          v-if="identity.state==='auth_pending'"
          :disabled="busy||warmupUnknown===identity.identity_id||!Number.isInteger(target)||Number(target)<5||Number(target)>100"
          @click="dialog={kind:'warmup',identity,target:Number(target)}"
        >
          启动预热
        </button>
        <button
          :disabled="busy||binding?.identity_id===identity.identity_id"
          @click="dialog={kind:'bind',identity}"
        >
          绑定本机入站邮箱
        </button>
      </article>
    </section>
    <section
      class="card"
      aria-label="入站绑定"
    >
      <h2>入站绑定</h2><p
        v-if="bindingError"
        role="status"
      >
        {{ bindingError }}
      </p><template v-else-if="binding">
        <p>{{ binding.identity_id?`当前绑定：${binding.identity_id}`:"未绑定" }}</p><p>{{ binding.state==='active'?"已绑定，处理状态待核对":binding.state==='waiting'?"暂态等待":binding.state==='blocked'?"处理已阻断":"尚未启用" }}</p><p v-if="binding.reason">
          固定原因：{{ binding.reason }}
        </p><p>最近完成：{{ binding.last_succeeded_at??"暂不可用" }}</p>
      </template>
    </section>
    <section
      id="inbound-reviews"
      class="card"
    >
      <h2>未关联邮件人工复核</h2><p>这里只读保存的复核记录；不代表已经进入会话，不提供重新绑定命令。</p><button @click="loadReviews">
        读取未关联邮件复核
      </button><p
        v-if="reviewError"
        role="status"
      >
        {{ reviewError }}
      </p><p v-if="reviews?.length===0">
        本次读取没有复核记录
      </p><article
        v-for="review in reviews??[]"
        :key="review.review_id"
      >
        <p>{{ review.review_id }} · {{ review.reason }} · {{ review.created_at }}</p><button
          v-if="review.archived"
          @click="downloadReview(review.review_id)"
        >
          下载复核邮件原件
        </button>
      </article>
    </section>
    <div
      v-if="dialog"
      class="dialog-backdrop"
    >
      <section
        class="card confirm-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="确认发件身份操作"
      >
        <h2>确认发件身份操作</h2>
        <template v-if="dialog.kind==='register'">
          <p>登记 {{ pendingRegistration?.address }} · {{ pendingRegistration?.domain }} · {{ pendingRegistration?.role }}</p><p>显示名称：{{ pendingRegistration?.display_name??"未设置" }} · 连接别名：{{ pendingRegistration?.connector_ref??"未设置" }}</p>
        </template>
        <template v-else>
          <p>{{ dialog.identity.address }} · {{ dialog.identity.identity_id }}</p><p v-if="dialog.kind==='warmup'">
            启动真实日期的 28 天固定预热曲线，目标日量 {{ dialog.target }}。认证通过不代表已启动。
          </p><p v-else>
            将本机受控单邮箱绑定到该身份。不更换已有绑定，不重置同步位置。
          </p>
        </template>
        <button
          :disabled="busy"
          @click="confirmCommand"
        >
          {{ dialog.kind==='register'?"确认登记":dialog.kind==='warmup'?"确认启动预热":"确认绑定" }}
        </button><button
          :disabled="busy"
          @click="cancelDialog"
        >
          取消
        </button>
      </section>
    </div>
  </div>
</template>
<style scoped>
.identity-shell { overflow-y:auto; }
.identity-shell > * { flex-shrink:0; }
.register-form { display:grid !important; grid-template-columns:repeat(3,minmax(0,1fr)); }
.register-form h2 { grid-column:1/-1; }
label { display:grid; gap:6px; min-width:0; }
input,select { min-width:0; width:100%; padding:8px; border:1px solid var(--border); border-radius:6px; background:var(--surface); }
.card,code,p,dd { min-width:0; overflow-wrap:anywhere; }
.grid { flex:none !important; overflow:visible !important; }
.dialog-backdrop { position:fixed;inset:0;background:#0006;z-index:1000;display:grid;place-items:center;padding:16px; }
.confirm-dialog { width:min(560px,100%);max-height:85vh;overflow:auto; }
@media(max-width:700px) { .grid,.register-form {grid-template-columns:1fr !important;} .kv {grid-template-columns:90px 1fr !important;} }

.state {
  color: var(--text-secondary);
  padding: var(--space4) 0;
}
.grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--space4);
  overflow-y: auto;
  flex: 1;
  min-height: 0;
  align-content: start;
  padding-bottom: var(--space4);
}
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: var(--space4);
  display: flex;
  flex-direction: column;
  gap: var(--space3);
}
.card .name {
  font-weight: 600;
}
.card .addr {
  color: var(--text-secondary);
  font-size: 12px;
  word-break: break-all;
}
.auth-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--space2);
}
.auth-item {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 6px 8px;
  font-size: 12px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.auth-item.pass {
  color: var(--fact);
  border-color: var(--fact);
  background: var(--fact-soft);
}
.auth-item.fail {
  color: var(--danger);
  border-color: var(--danger);
  background: var(--danger-soft);
}
.kv {
  display: grid;
  grid-template-columns: 110px 1fr;
  gap: var(--space2) var(--space4);
  font-size: 13px;
}
.kv dt {
  color: var(--text-secondary);
}
.kv dd {
  margin: 0;
}
.feedback {
  border: 1px solid var(--fact);
  background: var(--fact-soft);
  color: var(--fact);
  border-radius: var(--radius);
  padding: var(--space3) var(--space4);
  display: flex;
  gap: var(--space3);
  align-items: flex-start;
  font-size: 13px;
}
@media (max-width: 1240px) {
  .grid {
    grid-template-columns: repeat(2, 1fr);
  }
}
</style>
