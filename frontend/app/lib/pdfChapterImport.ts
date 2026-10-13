// PDF 章节导入：类型与纯函数（URL、区间校验、勾选统计）。
// 不依赖 Nuxt 运行时，便于聚焦单测。

// 相对路径即可：$api 插件已经设了 baseURL = '/api/v1'。
export const PDF_CHAPTER_API = '/pdf-chapter-imports'

/** 目录树节点。后端返回的就是这个形状，前端原地编辑后原样回传。 */
export interface TocChapter {
  title: string
  start_page: number
  end_page: number
  children: TocChapter[]
}

export interface ParseTocResponse {
  file_path: string
  filename: string
  book_title: string
  page_count: number
  toc_source: string
  page_offset: number
  message: string | null
  chapters: TocChapter[]
}

export type ChapterImportStatus =
  | 'pending'
  | 'processing'
  | 'completed'
  | 'failed'
  | 'skipped'
  | 'cancelled'

export type ChapterTaskStatus =
  | 'pending'
  | 'processing'
  | 'completed'
  | 'failed'
  | 'partial'
  | 'cancelled'

export interface ChapterImportItem {
  id: number
  title: string
  chapter_id: number | null
  section_id: number | null
  start_page: number
  end_page: number
  order_index: number
  status: ChapterImportStatus
  attempt_count: number
  error_message: string | null
  questions_count: number
  import_task_id: number | null
}

export interface ChapterImportTask {
  id: number
  book_id: number
  book_title: string | null
  subject_id: number | null
  source_filename: string | null
  status: ChapterTaskStatus
  error_message: string | null
  total_items: number
  completed_items: number
  failed_items: number
  skipped_items: number
  imported_questions: number
  items?: ChapterImportItem[]
}

export interface CreateTaskPayload {
  source_path: string
  book_title: string
  book_id?: number | null
  subject_id?: number | null
  mode?: string
  chapters: TocChapter[]
}

/**
 * 页面里的可编辑节点：后端形状 + 前端勾选态。
 * selected 只存在于编辑期，提交前会被剥掉。
 */
export interface EditableChapter extends Omit<TocChapter, 'children'> {
  children: EditableChapter[]
  selected: boolean
}

export function toEditable(chapters: TocChapter[]): EditableChapter[] {
  return (chapters ?? []).map((chapter) => ({
    title: chapter.title,
    start_page: Number(chapter.start_page),
    end_page: Number(chapter.end_page),
    children: toEditable(chapter.children ?? []),
    selected: true, // 默认全选：用户更可能是"去掉几章"，而不是"挑几章"
  }))
}

/** 剥掉前端勾选态，只回传勾选上的分支。 */
export function toPayload(chapters: EditableChapter[]): TocChapter[] {
  const out: TocChapter[] = []
  for (const chapter of chapters) {
    if (!chapter.selected) continue
    const children = toPayload(chapter.children ?? [])
    out.push({
      title: chapter.title,
      start_page: Number(chapter.start_page),
      end_page: Number(chapter.end_page),
      // 章被勾选但小节全不选时，按整章导入——这是用户更可能想要的结果。
      children: children.length ? children : [],
    })
  }
  return out
}

// --------------------------------------------------------------------------- #
// URL
// --------------------------------------------------------------------------- #

export const pdfChapterPaths = {
  parseToc: () => `${PDF_CHAPTER_API}/parse-toc`,
  tasks: () => `${PDF_CHAPTER_API}/tasks`,
  task: (id: number) => `${PDF_CHAPTER_API}/tasks/${id}`,
  runTask: (id: number) => `${PDF_CHAPTER_API}/tasks/${id}/run`,
  cancelTask: (id: number) => `${PDF_CHAPTER_API}/tasks/${id}/cancel`,
  retryItem: (id: number) => `${PDF_CHAPTER_API}/items/${id}/retry`,
  books: () => `${PDF_CHAPTER_API}/books`,
  bookChapters: (bookId: number) => `${PDF_CHAPTER_API}/books/${bookId}/chapters`,
}

// --------------------------------------------------------------------------- #
// 区间校验
// --------------------------------------------------------------------------- #

export interface RangeIssue {
  path: string
  message: string
}

/**
 * 校验用户手改后的页码区间。
 *
 * 只报"一定会出问题"的情况：越界、倒置、跨章重叠。不阻止重叠的**小节**
 * ——同一章里两个小节页码挨着甚至略有交叠很常见，后端 normalize 会各自收敛。
 */
export function validateRanges(chapters: TocChapter[], pageCount: number): RangeIssue[] {
  const issues: RangeIssue[] = []

  const walk = (list: TocChapter[], prefix: string) => {
    list.forEach((chapter, index) => {
      const path = `${prefix}[${index}] ${chapter.title || '(未命名)'}`
      const start = Number(chapter.start_page)
      const end = Number(chapter.end_page)

      if (!Number.isInteger(start) || !Number.isInteger(end)) {
        issues.push({ path, message: '页码必须是整数' })
        return
      }
      if (start < 1 || (pageCount > 0 && start > pageCount)) {
        issues.push({ path, message: `起始页 ${start} 超出范围(1-${pageCount})` })
      }
      if (end < 1 || (pageCount > 0 && end > pageCount)) {
        issues.push({ path, message: `结束页 ${end} 超出范围(1-${pageCount})` })
      }
      if (start > end) {
        issues.push({ path, message: `起始页 ${start} 大于结束页 ${end}` })
      }

      // 相邻章之间不应重叠：后一章的起点必须晚于前一章的终点。
      if (index > 0) {
        const prev = list[index - 1]
        if (Number(chapter.start_page) <= Number(prev.end_page)) {
          issues.push({
            path,
            message: `与上一项「${prev.title}」的页码重叠(上一项止于第 ${prev.end_page} 页)`,
          })
        }
      }

      if (chapter.children?.length) {
        walk(chapter.children, `${path} > `)
      }
    })
  }

  walk(chapters, '')
  return issues
}

// --------------------------------------------------------------------------- #
// 勾选与统计
// --------------------------------------------------------------------------- #

/** 展开成实际会生成的导入子项：章有小节则导出小节，否则导出整章。 */
export function flattenImportTargets(chapters: TocChapter[]): TocChapter[] {
  const out: TocChapter[] = []
  for (const chapter of chapters) {
    if (chapter.children?.length) {
      out.push(...chapter.children)
    } else {
      out.push(chapter)
    }
  }
  return out
}

export function countImportTargets(chapters: TocChapter[]): number {
  return flattenImportTargets(chapters).length
}

export function totalPagesOf(chapters: TocChapter[]): number {
  return flattenImportTargets(chapters).reduce(
    (sum, item) => sum + Math.max(0, Number(item.end_page) - Number(item.start_page) + 1),
    0,
  )
}

/**
 * 目录来源的中文说明。后端给的 toc_source 是枚举值，直接显示没人看得懂。
 */
export function tocSourceLabel(source: string): string {
  return (
    {
      bookmark: 'PDF 书签',
      text: '目录页文字层',
      ocr: '目录页 OCR',
      none: '未识别',
    } as Record<string, string>
  )[source] ?? source
}

export function itemStatusLabel(status: ChapterImportStatus): string {
  return (
    {
      pending: '等待中',
      processing: '进行中',
      completed: '已完成',
      failed: '失败',
      skipped: '已跳过',
      cancelled: '已取消',
    } as Record<string, string>
  )[status] ?? status
}

export function taskStatusLabel(status: ChapterTaskStatus): string {
  return (
    {
      pending: '待执行',
      processing: '执行中',
      completed: '已完成',
      failed: '失败',
      partial: '部分完成',
      cancelled: '已取消',
    } as Record<string, string>
  )[status] ?? status
}

/** 任务是否还在跑（前端据此决定是否继续轮询）。 */
export function isTaskRunning(status: ChapterTaskStatus): boolean {
  return status === 'processing' || status === 'pending'
}

/** 进度百分比；分母为 0 时返回 0，避免除零得到 NaN。 */
export function taskProgress(task: Pick<
  ChapterImportTask,
  'total_items' | 'completed_items' | 'skipped_items'
>): number {
  const total = task.total_items || 0
  if (!total) return 0
  const done = (task.completed_items || 0) + (task.skipped_items || 0)
  return Math.round((done / total) * 100)
}
