<template>
  <div class="chat-container">
    <div class="chat-header">
      <a-button type="link" @click="goHome">← 用表单填写</a-button>
      <span class="chat-title">💬 直接和助手说你想怎么玩</span>
      <a-button type="link" :disabled="!hasPlan" @click="openResult">
        在结果页打开(可编辑 / 导出) →
      </a-button>
    </div>

    <div ref="listRef" class="chat-list">
      <div v-if="!messages.length" class="empty">
        <div class="empty-title">说一句就行,信息不够我会问你 👇</div>
        <div class="starters">
          <a-tag v-for="chip in STARTERS" :key="chip" class="starter" @click="useStarter(chip)">
            {{ chip }}
          </a-tag>
        </div>
      </div>

      <div v-for="(msg, index) in messages" :key="index" :class="['row', msg.role]">
        <div v-if="msg.role === 'user'" class="bubble user-bubble">{{ msg.text }}</div>

        <div v-else class="bubble bot-bubble">
          <div v-if="msg.text" class="bot-text">{{ msg.text }}</div>
          <div v-if="msg.failed" class="bot-failed">这条回复没有走完,可以再试一次。</div>

          <div v-if="msg.progress && msg.progress.length" class="progress-box">
            <div v-for="row in msg.progress" :key="row.label" class="progress-row">
              <span class="progress-icon">{{ stageIcon(row.status) }}</span>
              <span :class="['progress-label', row.status]">{{ row.label }}</span>
              <span v-if="row.elapsed" class="progress-elapsed">{{ row.elapsed }}s</span>
            </div>
          </div>

          <div v-if="msg.plan" class="artifact-box">
            <TripPlanView :plan="msg.plan" />
            <div class="artifact-actions">
              <a-button type="primary" size="small" @click="openResult">
                查看完整行程 →
              </a-button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="chat-input">
      <a-textarea
        v-model:value="draft"
        :rows="2"
        :disabled="sending"
        placeholder="例如:带六岁小孩,北京玩三天,下周末出发"
        @pressEnter="onPressEnter"
      />
      <a-button type="primary" size="large" :loading="sending" @click="send">发送</a-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, ref } from 'vue'
import { useRouter } from 'vue-router'
import TripPlanView from '@/components/TripPlanView.vue'
import { chatStream } from '@/services/api'
import type { ChatTurn, TripPlan, TripProgressEvent } from '@/types'

type StageStatus = 'pending' | 'running' | 'done'

interface ProgressRow {
  label: string
  status: StageStatus
  elapsed?: number
}

interface ChatBubble {
  role: 'user' | 'assistant'
  text?: string
  progress?: ProgressRow[]
  plan?: TripPlan | null
  degraded?: boolean
  failed?: boolean
}

const router = useRouter()

// 这四步是后端进度事件里会出现的阶段(与 NODE_LABELS 对应)。
// 一开始就按"等待中"列出来,免得前 10 秒界面看着像死了。
const STAGE_LABELS = ['景点检索', '天气查询', '酒店检索', '生成行程']

const STARTERS = [
  '带六岁小孩,北京玩三天,下周末出发',
  '上海周末两日游,想吃本地小吃',
  '成都四天,节奏慢一点'
]

const messages = ref<ChatBubble[]>([])
const draft = ref('')
const sending = ref(false)
const listRef = ref<HTMLElement | null>(null)
const history = ref<ChatTurn[]>([])

const latestPlan = ref<TripPlan | null>(null)
const latestDegraded = ref(false)
const hasPlan = ref(false)

const stageIcon = (status: StageStatus): string => {
  if (status === 'done') return '✅'
  if (status === 'running') return '⏳'
  return '⚪'
}

const scrollToBottom = () => {
  nextTick(() => {
    const el = listRef.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

const applyProgress = (index: number, event: TripProgressEvent) => {
  // 注意:必须通过 messages.value[index] 拿(而不是缓存对象引用),
  // 否则改的是原始对象,Vue 不会重新渲染。
  const msg = messages.value[index]
  if (!msg.progress) msg.progress = []

  const row = msg.progress.find((item) => item.label === event.label)
  if (row) {
    row.status = event.status
    row.elapsed = event.elapsed
    return
  }

  // 三路检索是并行的,事件到达顺序不固定;按固定顺序插进去,
  // 免得清单每次刷新都换位置。
  const row2 = { label: event.label, status: event.status, elapsed: event.elapsed }
  const rank = (label: string) => {
    const i = STAGE_LABELS.indexOf(label)
    return i === -1 ? STAGE_LABELS.length : i
  }
  const at = msg.progress.findIndex((item) => rank(item.label) > rank(row2.label))
  if (at === -1) {
    msg.progress.push(row2)
  } else {
    msg.progress.splice(at, 0, row2)
  }
}

const send = async () => {
  const text = draft.value.trim()
  if (!text || sending.value) return

  draft.value = ''
  messages.value.push({ role: 'user', text })

  // 发给后端的上下文是"这轮之前"的历史
  const historyBefore = history.value.slice()
  history.value.push({ role: 'user', content: text })

  // 注意:这里**不能**预先把四个阶段摆成"等待中"。
  // 因为这一轮未必会规划 —— 用户说一句"你好",后端只回一句话、不会发任何进度事件,
  // 那几行就会永远停在 ⚪,看起来像"它要去生成计划然后卡死了"(踩过这个 bug)。
  // 现在改成:有进度事件才建清单。
  const botIndex = messages.value.push({ role: 'assistant', text: '' }) - 1

  sending.value = true
  scrollToBottom()

  try {
    await chatStream(
      { message: text, history: historyBefore },
      {
        onMessage: (chunk) => {
          // message 事件是**逐字流出来的增量**(后端边生成边推),
          // 所以这里是追加,不能拼换行 —— 否则每个字之间都会断行。
          const msg = messages.value[botIndex]
          msg.text = (msg.text ?? '') + chunk
          scrollToBottom()
        },
        onProgress: (event) => {
          applyProgress(botIndex, event)
          scrollToBottom()
        },
        onArtifact: (event) => {
          const msg = messages.value[botIndex]
          msg.plan = event.data
          msg.degraded = event.degraded
          latestPlan.value = event.data
          latestDegraded.value = event.degraded
          hasPlan.value = true
          scrollToBottom()
        }
      }
    )

    const msg = messages.value[botIndex]
    if (msg.text) {
      history.value.push({ role: 'assistant', content: msg.text })
    }
  } catch (error: any) {
    const msg = messages.value[botIndex]
    msg.failed = true
    msg.text = msg.text
      ? `${msg.text}\n⚠️ ${error?.message || '请求失败,请稍后重试'}`
      : `⚠️ ${error?.message || '请求失败,请稍后重试'}`
  } finally {
    sending.value = false
    scrollToBottom()
  }
}

const onPressEnter = (event: KeyboardEvent) => {
  if (event.shiftKey) return // Shift+Enter 换行
  event.preventDefault()
  send()
}

const useStarter = (chip: string) => {
  draft.value = chip
}

const goHome = () => {
  router.push('/')
}

const openResult = () => {
  const plan = latestPlan.value
  if (!plan) return
  // 结果页是从 sessionStorage 读的,这里按它的约定塞进去
  sessionStorage.setItem('tripPlan', JSON.stringify(plan))
  sessionStorage.setItem('tripPlanDegraded', latestDegraded.value ? '1' : '0')
  router.push('/result')
}
</script>

<style scoped>
.chat-container {
  display: flex;
  flex-direction: column;
  max-width: 880px;
  margin: 0 auto;
  height: calc(100vh - 220px);
  min-height: 460px;
  background: #fff;
  border-radius: 16px;
  box-shadow: 0 8px 30px rgba(102, 126, 234, 0.12);
  overflow: hidden;
}

.chat-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-bottom: 1px solid #f0f0f5;
}

.chat-title {
  flex: 1;
  text-align: center;
  color: #4664d6;
  font-weight: 600;
}

.chat-list {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  background: #fafbff;
}

.empty {
  text-align: center;
  padding-top: 40px;
}

.empty-title {
  color: #8c8c9a;
  margin-bottom: 16px;
}

.starters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
}

.starter {
  cursor: pointer;
  padding: 4px 12px;
  border-radius: 16px;
}

.row {
  display: flex;
  margin-bottom: 14px;
}

.row.user {
  justify-content: flex-end;
}

.bubble {
  max-width: 82%;
  padding: 10px 14px;
  border-radius: 12px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}

.user-bubble {
  background: linear-gradient(135deg, #667eea, #764ba2);
  color: #fff;
}

.bot-bubble {
  background: #fff;
  border: 1px solid #ececf4;
  color: #333;
}

.bot-failed {
  margin-top: 6px;
  color: #d4380d;
  font-size: 12px;
}

.progress-box {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed #ececf4;
}

.progress-row {
  display: flex;
  align-items: baseline;
  gap: 6px;
  font-size: 13px;
  padding: 1px 0;
}

.progress-icon {
  flex: none;
}

.progress-label {
  color: #9aa0b4;
}

.progress-label.running {
  color: #4664d6;
  font-weight: 600;
}

.progress-label.done {
  color: #52c41a;
}

.progress-elapsed {
  margin-left: auto;
  color: #b0b5c3;
  font-size: 12px;
}

.artifact-box {
  margin-top: 10px;
}

.artifact-actions {
  margin-top: 8px;
  text-align: right;
}

.chat-input {
  display: flex;
  gap: 10px;
  align-items: flex-end;
  padding: 12px 16px;
  border-top: 1px solid #f0f0f5;
  background: #fff;
}
</style>