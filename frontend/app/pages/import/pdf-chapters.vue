<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import {
  Upload,
  Loader2,
  FileText,
  CheckCircle2,
  AlertCircle,
  Plus,
  Trash2,
  RotateCcw,
  Scissors,
} from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Separator } from '@/components/ui/separator'
import {
  Stepper,
  StepperItem,
  StepperIndicator,
  StepperTitle,
  StepperSeparator,
} from '@/components/ui/stepper'
import PageHeader from '@/components/PageHeader.vue'
import { toast } from 'vue-sonner'
import { useSubjectContext } from '@/composables/useSubjectContext'
import {
  type ChapterImportTask,
  type EditableChapter,
  type ParseTocResponse,
  type TocChapter,
  countImportTargets,
  isTaskRunning,
  itemStatusLabel,
  pdfChapterPaths,
  taskProgress,
  taskStatusLabel,
  tocSourceLabel,
  toEditable,
  toPayload,
  totalPagesOf,
  validateRanges,
} from '@/lib/pdfChapterImport'

definePageMeta({ layout: 'default' })

const { $api } = useNuxtApp()
const { currentSubjectId, currentSubject } = useSubjectContext()

// --- 步骤 ---
type Step = 'upload' | 'outline' | 'progress'
const step = ref<Step>('upload')
const stepIndex = computed(() => ({ upload: 1, outline: 2, progress: 3 })[step.value])

// --- Step 1: 上传 ---
const selectedFile = ref<File | null>(null)
const uploading = ref(false)
const toc = ref<ParseTocResponse | null>(null)
const chapters = ref<EditableChapter[]>([])
const parseError = ref<string | null>(null)
const pageOffset = ref<number | null>(null)

const onPickFile = (event: Event) => {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) selectedFile.value = file
}

async function parseToc() {
  if (!selectedFile.value) {
    toast.error('请先选择 PDF 文件')
    return
  }
  uploading.value = true
  parseError.value = null
  try {
    const form = new FormData()
    form.append('file', selectedFile.value)
    const qs = new URLSearchParams()
    if (pageOffset.value !== null) qs.set('page_offset', String(pageOffset.value))
    const url = `${pdfChapterPaths.parseToc()}?${qs.toString()}`

    const data = await $api<ParseTocResponse>(url, { method: 'POST', body: form })
    toc.value = data
    chapters.value = toEditable(data.chapters)
    pageOffset.value = data.page_offset ?? 0
    step.value = 'outline'

    if (!data.chapters?.length) {
      toast.warning(data.message || '未能自动识别目录，请手工添加章节')
    } else {
      toast.success(`识别到 ${data.chapters.length} 个章节`)
    }
  } catch (error: any) {
    parseError.value = error?.data?.detail ?? error?.message ?? '目录识别失败'
    toast.error(parseError.value)
  } finally {
    uploading.value = false
  }
}

/** 目录页码对不上时，用户改偏移重新解析一次。 */
async function reparse() {
  if (!selectedFile.value) return
  toc.value = null
  await parseToc()
}

// --- Step 2: 目录校正 ---
const pageCount = computed(() => toc.value?.page_count ?? 0)
const rangeIssues = computed(() => validateRanges(chapters.value, pageCount.value))

const selectedChapters = computed(() => toPayload(chapters.value))
const targetCount = computed(() => countImportTargets(selectedChapters.value))
const targetPages = computed(() => totalPagesOf(selectedChapters.value))

function addChapter() {
  const last = chapters.value[chapters.value.length - 1]
  const start = last ? Number(last.end_page) + 1 : 1
  chapters.value.push({
    title: `新章节 ${chapters.value.length + 1}`,
    start_page: Math.min(start, pageCount.value || start),
    end_page: Math.min(start, pageCount.value || start),
    children: [],
    selected: true,
  })
}

function addSection(chapter: EditableChapter) {
  const last = chapter.children[chapter.children.length - 1]
  const start = last ? Number(last.end_page) + 1 : Number(chapter.start_page)
  chapter.children.push({
    title: `新小节 ${chapter.children.length + 1}`,
    start_page: start,
    end_page: Number(chapter.end_page),
    children: [],
    selected: true,
  })
}

function removeChapter(index: number) {
  chapters.value.splice(index, 1)
}

function removeSection(chapter: EditableChapter, index: number) {
  chapter.children.splice(index, 1)
}

const allSelected = computed({
  get: () => chapters.value.length > 0 && chapters.value.every((c) => c.selected),
  set: (value: boolean) => chapters.value.forEach((c) => { c.selected = value }),
})

// --- Step 3: 提交与进度 ---
const bookTitle = ref('')
const submitting = ref(false)
const task = ref<ChapterImportTask | null>(null)
const running = ref(false)
let pollTimer: ReturnType<typeof setInterval> | null = null

onUnmounted(stopPolling)

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function submitTask() {
  if (!toc.value) return
  const payloadChapters = selectedChapters.value
  if (!payloadChapters.length) {
    toast.error('请至少勾选一个章节')
    return
  }
  if (rangeIssues.value.length) {
    toast.error('请先修正章节页码的问题')
    return
  }

  submitting.value = true
  try {
    const created = await $api<ChapterImportTask>(pdfChapterPaths.tasks(), {
      method: 'POST',
      body: {
        source_path: toc.value.file_path,
        book_title: bookTitle.value.trim() || toc.value.book_title,
        subject_id: currentSubjectId.value,
        mode: 'extract',
        chapters: payloadChapters,
      },
    })
    task.value = created
    step.value = 'progress'
    toast.success(`已创建任务，共 ${created.total_items} 个章节待导入`)
  } catch (error: any) {
    toast.error(error?.data?.detail ?? error?.message ?? '创建任务失败')
  } finally {
    submitting.value = false
  }
}

async function runTask() {
  if (!task.value) return
  running.value = true
  try {
    task.value = await $api<ChapterImportTask>(pdfChapterPaths.runTask(task.value.id), {
      method: 'POST',
    })
    startPolling()
  } catch (error: any) {
    toast.error(error?.data?.detail ?? error?.message ?? '执行失败')
    running.value = false
  }
}

async function refreshTask() {
  if (!task.value) return
  try {
    task.value = await $api<ChapterImportTask>(pdfChapterPaths.task(task.value.id))
    if (!isTaskRunning(task.value.status)) {
      stopPolling()
      running.value = false
    }
  } catch {
    stopPolling()
    running.value = false
  }
}

function startPolling() {
  stopPolling()
  pollTimer = setInterval(refreshTask, 2000)
}

async function retryItem(itemId: number) {
  try {
    const data = await $api<{ item: unknown; task: ChapterImportTask }>(
      pdfChapterPaths.retryItem(itemId),
      { method: 'POST' },
    )
    task.value = data.task
    await refreshTask()
    toast.success('已重新提交该章节')
  } catch (error: any) {
    toast.error(error?.data?.detail ?? error?.message ?? '重试失败')
  }
}

const progress = computed(() => (task.value ? taskProgress(task.value) : 0))
const finished = computed(() => !!task.value && !isTaskRunning(task.value.status))

function reset() {
  stopPolling()
  step.value = 'upload'
  selectedFile.value = null
  toc.value = null
  chapters.value = []
  task.value = null
  bookTitle.value = ''
  running.value = false
}

const subjectName = computed(() => currentSubject.value?.name ?? null)
</script>

<template>
  <div class="container mx-auto max-w-5xl py-6 space-y-6">
    <PageHeader title="PDF 章节导入" />
    <p class="-mt-2 text-sm text-muted-foreground">
      按目录拆分整本教材，逐章解析入库并自动关联书籍、章节与小节
    </p>

    <Stepper :model-value="stepIndex" class="mx-auto max-w-3xl">
      <StepperItem :step="1">
        <StepperIndicator><Upload class="size-4" /></StepperIndicator>
        <StepperTitle>上传并识别目录</StepperTitle>
      </StepperItem>
      <StepperSeparator />
      <StepperItem :step="2">
        <StepperIndicator><Scissors class="size-4" /></StepperIndicator>
        <StepperTitle>校正章节范围</StepperTitle>
      </StepperItem>
      <StepperSeparator />
      <StepperItem :step="3">
        <StepperIndicator><CheckCircle2 class="size-4" /></StepperIndicator>
        <StepperTitle>批量导入</StepperTitle>
      </StepperItem>
    </Stepper>

    <!-- ---------- Step 1: 上传 ---------- -->
    <section v-if="step === 'upload'" class="space-y-4">
      <div class="rounded-lg border border-dashed p-8 text-center space-y-4">
        <FileText class="mx-auto size-10 text-muted-foreground" />
        <div class="space-y-1">
          <p class="font-medium">选择一本教材 PDF</p>
          <p class="text-sm text-muted-foreground">
            优先读取 PDF 书签；没有书签时识别目录页，扫描件会走 OCR
          </p>
        </div>
        <Input type="file" accept=".pdf" class="mx-auto max-w-sm" @change="onPickFile" />
        <p v-if="selectedFile" class="text-sm">
          已选择：<span class="font-medium">{{ selectedFile.name }}</span>
        </p>
      </div>

      <div v-if="toc || parseError" class="flex items-center gap-3">
        <Label class="whitespace-nowrap">页码偏移</Label>
        <Input
          v-model.number="pageOffset"
          type="number"
          class="w-28"
          placeholder="自动"
        />
        <Button v-if="toc" variant="outline" size="sm" @click="reparse">
          按此偏移重新识别
        </Button>
        <span class="text-xs text-muted-foreground">
          目录印的页码与 PDF 页码不一致时填写
        </span>
      </div>

      <Alert v-if="parseError" variant="destructive">
        <AlertCircle class="size-4" />
        <AlertTitle>识别失败</AlertTitle>
        <AlertDescription>{{ parseError }}</AlertDescription>
      </Alert>

      <div class="flex justify-end">
        <Button :disabled="!selectedFile || uploading" @click="parseToc">
          <Loader2 v-if="uploading" class="mr-2 size-4 animate-spin" />
          识别目录
        </Button>
      </div>
    </section>

    <!-- ---------- Step 2: 目录校正 ---------- -->
    <section v-else-if="step === 'outline'" class="space-y-4">
      <div class="flex flex-wrap items-center gap-2 text-sm">
        <Badge variant="secondary">共 {{ pageCount }} 页</Badge>
        <Badge variant="secondary">来源：{{ tocSourceLabel(toc?.toc_source ?? '') }}</Badge>
        <Badge variant="secondary">偏移 {{ toc?.page_offset ?? 0 }}</Badge>
        <Badge variant="outline">已选 {{ targetCount }} 项 / {{ targetPages }} 页</Badge>
      </div>

      <Alert v-if="toc?.message">
        <AlertCircle class="size-4" />
        <AlertDescription>{{ toc.message }}</AlertDescription>
      </Alert>

      <Alert v-if="rangeIssues.length" variant="destructive">
        <AlertCircle class="size-4" />
        <AlertTitle>页码需要修正</AlertTitle>
        <AlertDescription>
          <ul class="list-disc pl-5 space-y-1">
            <li v-for="issue in rangeIssues" :key="issue.path + issue.message">
              {{ issue.path }}：{{ issue.message }}
            </li>
          </ul>
        </AlertDescription>
      </Alert>

      <div class="rounded-lg border">
        <div class="flex items-center gap-3 border-b px-4 py-2 text-sm">
          <Checkbox v-model="allSelected" />
          <span class="font-medium">全选</span>
          <Separator orientation="vertical" class="h-4" />
          <span class="text-muted-foreground">可直接修改章节名称与起止页码</span>
        </div>

        <div class="divide-y">
          <div v-for="(chapter, index) in chapters" :key="index" class="p-3 space-y-2">
            <div class="flex flex-wrap items-center gap-2">
              <Checkbox v-model="chapter.selected" />
              <Input v-model="chapter.title" class="h-8 w-56" placeholder="章节名称" />
              <div class="flex items-center gap-1 text-sm text-muted-foreground">
                <span>第</span>
                <Input v-model.number="chapter.start_page" type="number" class="h-8 w-20" />
                <span>– 第</span>
                <Input v-model.number="chapter.end_page" type="number" class="h-8 w-20" />
                <span>页</span>
              </div>
              <Button variant="ghost" size="sm" @click="addSection(chapter)">
                <Plus class="mr-1 size-3" />小节
              </Button>
              <Button variant="ghost" size="sm" @click="removeChapter(index)">
                <Trash2 class="size-3" />
              </Button>
            </div>

            <div
              v-for="(section, sIndex) in chapter.children"
              :key="sIndex"
              class="flex flex-wrap items-center gap-2 pl-8"
            >
              <Checkbox v-model="section.selected" />
              <Input v-model="section.title" class="h-8 w-52" placeholder="小节名称" />
              <div class="flex items-center gap-1 text-sm text-muted-foreground">
                <span>第</span>
                <Input v-model.number="section.start_page" type="number" class="h-8 w-20" />
                <span>– 第</span>
                <Input v-model.number="section.end_page" type="number" class="h-8 w-20" />
                <span>页</span>
              </div>
              <Button variant="ghost" size="sm" @click="removeSection(chapter, sIndex)">
                <Trash2 class="size-3" />
              </Button>
            </div>
          </div>

          <div v-if="!chapters.length" class="p-6 text-center text-sm text-muted-foreground">
            没有识别到章节。可以点下方按钮手工添加，或调整页码偏移后重新识别。
          </div>
        </div>

        <div class="border-t p-3">
          <Button variant="outline" size="sm" @click="addChapter">
            <Plus class="mr-1 size-4" />添加章节
          </Button>
        </div>
      </div>

      <div class="grid gap-2 sm:grid-cols-[1fr_auto] sm:items-end">
        <div class="space-y-1">
          <Label>书籍名称</Label>
          <Input v-model="bookTitle" :placeholder="toc?.book_title ?? '教材名称'" />
          <p class="text-xs text-muted-foreground">
            同名书籍会复用已有条目，题目挂到同一本书下
          </p>
        </div>
        <div class="text-sm text-muted-foreground sm:pb-2">
          <template v-if="subjectName">学科：{{ subjectName }}</template>
        </div>
      </div>

      <div class="flex justify-between">
        <Button variant="outline" @click="reset">重新选择文件</Button>
        <Button
          :disabled="!targetCount || submitting || rangeIssues.length > 0"
          @click="submitTask"
        >
          <Loader2 v-if="submitting" class="mr-2 size-4 animate-spin" />
          创建导入任务（{{ targetCount }} 项）
        </Button>
      </div>
    </section>

    <!-- ---------- Step 3: 进度 ---------- -->
    <section v-else class="space-y-4">
      <div class="rounded-lg border p-4 space-y-3">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div class="space-y-0.5">
            <p class="font-medium">{{ task?.book_title }}</p>
            <p class="text-sm text-muted-foreground">
              状态：{{ taskStatusLabel(task?.status ?? 'pending') }}
            </p>
          </div>
          <div class="flex gap-2">
            <Button v-if="!running && !finished" @click="runTask">开始导入</Button>
            <Button v-if="running" disabled>
              <Loader2 class="mr-2 size-4 animate-spin" />正在导入
            </Button>
            <Button v-if="finished" variant="outline" @click="reset">导入其他书</Button>
          </div>
        </div>

        <div class="h-2 w-full overflow-hidden rounded bg-muted">
          <div
            class="h-full bg-primary transition-all"
            :style="{ width: `${progress}%` }"
          />
        </div>

        <div class="flex flex-wrap gap-2 text-sm">
          <Badge variant="secondary">总章节 {{ task?.total_items ?? 0 }}</Badge>
          <Badge variant="secondary">完成 {{ task?.completed_items ?? 0 }}</Badge>
          <Badge v-if="task?.skipped_items" variant="outline">
            跳过 {{ task.skipped_items }}
          </Badge>
          <Badge v-if="task?.failed_items" variant="destructive">
            失败 {{ task.failed_items }}
          </Badge>
          <Badge variant="outline">入库题目 {{ task?.imported_questions ?? 0 }}</Badge>
        </div>
      </div>

      <div class="rounded-lg border divide-y">
        <div
          v-for="item in task?.items ?? []"
          :key="item.id"
          class="flex flex-wrap items-center justify-between gap-2 p-3 text-sm"
        >
          <div class="min-w-0 space-y-0.5">
            <p class="font-medium truncate">{{ item.title }}</p>
            <p class="text-xs text-muted-foreground">
              第 {{ item.start_page }}–{{ item.end_page }} 页 ·
              {{ itemStatusLabel(item.status as any) }} ·
              入库 {{ item.questions_count }} 题
              <template v-if="item.attempt_count > 1">· 已重试 {{ item.attempt_count - 1 }} 次</template>
            </p>
            <p v-if="item.error_message" class="text-xs text-destructive">
              {{ item.error_message }}
            </p>
          </div>
          <Button
            v-if="item.status === 'failed' || item.status === 'cancelled'"
            variant="outline"
            size="sm"
            :disabled="running"
            @click="retryItem(item.id)"
          >
            <RotateCcw class="mr-1 size-3" />重试
          </Button>
        </div>
      </div>

      <Alert v-if="finished && task?.failed_items">
        <AlertCircle class="size-4" />
        <AlertTitle>有章节未成功</AlertTitle>
        <AlertDescription>
          失败的章节可以单独重试，已成功的章节不会被重复导入。
        </AlertDescription>
      </Alert>
    </section>
  </div>
</template>
