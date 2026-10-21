<script setup lang="ts">
import { computed, inject, onMounted, ref, watch } from "vue";
import { apiClient, createApiClient } from "../../api/client";
import type { components } from "../../api/api";
import { useQuoteRequestScope } from "../costing-quotes/quote-request-scope";

const props = defineProps<{ messageId: string }>();
const emit = defineEmits<{ updated: []; denied: [] }>();
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
type View = components["schemas"]["ReplyRetryView"];
const view = ref<View | null>(null);
const pending = ref(false);
const loading = ref(false);
const message = ref("");
const key = ref("");
const resumeKey = ref("");
const unknown = ref(false);
const acknowledgeCost = ref(false);
const failedSubmission = ref<{key:string;expected_sequence:number;acknowledge_cost:true} | null>(null);
function clear() { view.value=null; pending.value=false; loading.value=false; message.value=""; key.value=""; resumeKey.value=""; unknown.value=false; acknowledgeCost.value=false; failedSubmission.value=null; }
const gate = useQuoteRequestScope(client,()=>[props.messageId],clear);
watch(()=>props.messageId,()=>{clear(); void read(true);});
const label = computed(()=>{
  const labels: Record<View["state"],string>={unavailable:"暂不能重新识别",eligible:"上次识别在模型派发前失败，可以重新识别一次。",queued:"已提交，等待重新识别",running:"正在重新识别",classified:"重新识别已保存，后续任务处理中",completed:"人工恢复已完成",failed:"本次恢复失败，请联系管理员核对",expired:"本次恢复已过期，请联系管理员核对"};
  if(view.value?.requires_cost_confirmation && view.value.can_retry)return "上次识别已结束，确认后可重新识别。";
  if(view.value?.reason==="quota_exhausted")return "当前模型额度不足";
  if(view.value?.reason==="model_unavailable")return "模型连接尚未就绪";
  return view.value ? labels[view.value.state] : "";
});
async function read(initial=false) {
  if(pending.value || loading.value || !gate.hasIdentity.value)return;
  const op=gate.begin("read");if(!op?.valid())return;
  loading.value=true;
  try {
    const result=await client.GET("/inbox/messages/{message_id}/retry-classification",{params:{path:{message_id:props.messageId}},signal:op.signal});
    if(!op.valid())return;
    if(result.response.status===401){clear();emit("denied");return;}
    // 恢复权限比邮件读取权限更窄；403只清除此恢复面板。
    if(result.response.status===403){clear();return;}
    if(result.response.ok && result.data){
      const previousSequence = failedSubmission.value?.expected_sequence ?? view.value?.expected_sequence;
      if(previousSequence != null && result.data.expected_sequence !== previousSequence){
        // 已观察到后一次终态；新尝试必须重新确认费用，不能沿用旧页的同意。
        failedSubmission.value=null;acknowledgeCost.value=false;unknown.value=false;message.value="";
      }
      view.value=result.data;
      if(!result.data.can_retry && !result.data.can_resume && result.data.state!=="unavailable") { unknown.value=false; message.value=""; }
      if(!initial && ["classified","completed"].includes(result.data.state))emit("updated");
    } else {view.value=null;acknowledgeCost.value=false;message.value="无法读取识别状态，请刷新核对";}
  }catch{if(op.valid()){view.value=null;acknowledgeCost.value=false;message.value="无法读取识别状态，请刷新核对";}}
  finally{if(op.valid())loading.value=false;}
}
async function submit(resume=false) {
  if(pending.value || loading.value || !(resume ? view.value?.can_resume : view.value?.can_retry) || !gate.hasIdentity.value)return;
  const op=gate.begin("submit");if(!op?.valid())return;
  const failed = !resume && view.value?.requires_cost_confirmation;
  if(failed && (!acknowledgeCost.value || view.value?.expected_sequence == null))return;
  if(failed && !failedSubmission.value){
    failedSubmission.value={key:crypto.randomUUID(),expected_sequence:view.value!.expected_sequence!,acknowledge_cost:true};
  }
  const requestKey=resume ? resumeKey : key;
  if(!requestKey.value)requestKey.value=crypto.randomUUID();
  pending.value=true; message.value="正在提交重新识别";
  try {
    const result = failed && failedSubmission.value
      ? await client.POST("/inbox/messages/{message_id}/retry-classification/failed",{params:{path:{message_id:props.messageId},header:{"Idempotency-Key":failedSubmission.value.key}},body:{expected_sequence:failedSubmission.value.expected_sequence,acknowledge_cost:true},signal:op.signal})
      : await client.POST(resume ? "/inbox/messages/{message_id}/retry-classification/resume" : "/inbox/messages/{message_id}/retry-classification",{params:{path:{message_id:props.messageId},header:{"Idempotency-Key":requestKey.value}},body:{},signal:op.signal});
    if(!op.valid())return;
    if(result.response.status===401 || result.response.status===403){clear();emit("denied");return;}
    if(result.response.ok && result.data){view.value=result.data;unknown.value=false;failedSubmission.value=null;acknowledgeCost.value=false;message.value="已提交，请刷新识别状态查看结果";}
    else {unknown.value=true;message.value="提交结果待核对；请先刷新识别状态，不会自动重试";view.value=null;}
  }catch{if(op.valid()){unknown.value=true;message.value="提交结果待核对；请先刷新识别状态，不会自动重试";view.value=null;}}
  finally{if(op.valid())pending.value=false;}
}
onMounted(()=>void read(true));
</script>

<template>
  <section v-if="view?.state !== 'unavailable' && (view || message)" class="reply-retry" aria-label="回复识别恢复">
    <p v-if="label">{{ label }}</p>
    <p v-if="message" role="status">{{ message }}</p>
    <template v-if="view">
      <small>原失败记录保留；重新识别会使用一次模型额度，不会重新发送邮件。</small>
      <label v-if="view.requires_cost_confirmation"><input v-model="acknowledgeCost" type="checkbox" :disabled="pending || loading">我确认上次识别已结束，接受重新识别可能再次产生模型费用。</label>
      <button v-if="view.can_retry" type="button" :disabled="pending || loading || (view.requires_cost_confirmation && !acknowledgeCost)" @click="submit(false)">{{ unknown ? '使用原请求继续' : '重新识别' }}</button>
      <button v-if="view.can_resume" type="button" :disabled="pending || loading" @click="submit(true)">继续未开始的识别</button>
      <small v-if="view.resumed">原测试请求的过期记录已保留，本次继续处理尚未开始的识别。</small>
    </template>
    <button type="button" :disabled="pending || loading" @click="read(false)">刷新识别状态</button>
  </section>
</template>

<style scoped>
.reply-retry { padding: 12px; margin: 10px 0; border: 1px solid var(--border); border-radius: var(--radius-sm); }
p { margin: 0 0 8px; } small { display: block; color: var(--text-secondary); margin-bottom: 8px; }
button { margin-right: 8px; }
</style>
