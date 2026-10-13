import { describe, it, expect } from 'vitest'
import type { EditableChapter, TocChapter } from '@/lib/pdfChapterImport'
import {
  countImportTargets,
  flattenImportTargets,
  isTaskRunning,
  pdfChapterPaths,
  taskProgress,
  tocSourceLabel,
  toEditable,
  toPayload,
  totalPagesOf,
  validateRanges,
} from '@/lib/pdfChapterImport'

function chapter(
  title: string,
  start: number,
  end: number,
  children: TocChapter[] = [],
): TocChapter {
  return { title, start_page: start, end_page: end, children }
}

const SAMPLE: TocChapter[] = [
  chapter('第一章 集合', 1, 6, [chapter('1.1 概念', 2, 3), chapter('1.2 子集', 4, 6)]),
  chapter('第二章 函数', 7, 9),
]

describe('validateRanges', () => {
  it('合法区间不报错', () => {
    expect(validateRanges(SAMPLE, 12)).toEqual([])
  })

  it('起始页大于结束页要报错', () => {
    const issues = validateRanges([chapter('倒挂', 5, 2)], 12)
    expect(issues).toHaveLength(1)
    expect(issues[0].message).toContain('大于结束页')
  })

  it('页码超出 PDF 总页数要报错', () => {
    const issues = validateRanges([chapter('越界', 1, 99)], 12)
    expect(issues.some((i) => i.message.includes('超出范围'))).toBe(true)
  })

  it('相邻章页码重叠要报错', () => {
    const issues = validateRanges([chapter('A', 1, 5), chapter('B', 4, 8)], 12)
    expect(issues.some((i) => i.message.includes('重叠'))).toBe(true)
  })

  it('会递归校验小节', () => {
    const issues = validateRanges(
      [chapter('章', 1, 8, [chapter('小节', 3, 2)])],
      12,
    )
    expect(issues).toHaveLength(1)
    expect(issues[0].path).toContain('小节')
  })

  it('页码为非整数时报错', () => {
    const issues = validateRanges([chapter('x', 1.5, 3)], 12)
    expect(issues).toHaveLength(1)
    expect(issues[0].message).toContain('整数')
  })
})

describe('flattenImportTargets', () => {
  it('章有小节时展开为小节', () => {
    const flat = flattenImportTargets(SAMPLE)
    expect(flat.map((f) => f.title)).toEqual(['1.1 概念', '1.2 子集', '第二章 函数'])
  })

  it('统计与页数按展开后的结果算', () => {
    expect(countImportTargets(SAMPLE)).toBe(3)
    // 2 页 + 3 页 + 3 页
    expect(totalPagesOf(SAMPLE)).toBe(8)
  })
})

describe('toEditable / toPayload', () => {
  it('默认全选', () => {
    const editable = toEditable(SAMPLE)
    expect(editable.every((c) => c.selected)).toBe(true)
  })

  it('未勾选的分支不会进入提交数据', () => {
    const editable = toEditable(SAMPLE)
    editable[1].selected = false
    const payload = toPayload(editable)
    expect(payload).toHaveLength(1)
    expect(payload[0].title).toBe('第一章 集合')
  })

  it('提交数据不含前端勾选态字段', () => {
    const payload = toPayload(toEditable(SAMPLE))
    expect(Object.keys(payload[0])).toEqual(['title', 'start_page', 'end_page', 'children'])
  })

  it('章勾选但小节全不选时按整章提交', () => {
    const editable: EditableChapter[] = toEditable([chapter('整章', 1, 5, [chapter('小节', 1, 2)])])
    editable[0].children[0].selected = false
    const payload = toPayload(editable)
    expect(payload[0].children).toEqual([])
  })
})

describe('taskProgress', () => {
  it('完成与跳过都计入进度', () => {
    expect(taskProgress({ total_items: 4, completed_items: 2, skipped_items: 1 })).toBe(75)
  })

  it('总数为 0 时返回 0 而不是 NaN', () => {
    expect(taskProgress({ total_items: 0, completed_items: 0, skipped_items: 0 })).toBe(0)
  })
})

describe('isTaskRunning', () => {
  it('只把 pending / processing 视为在跑', () => {
    expect(isTaskRunning('processing')).toBe(true)
    expect(isTaskRunning('pending')).toBe(true)
    expect(isTaskRunning('completed')).toBe(false)
    expect(isTaskRunning('partial')).toBe(false)
  })
})

describe('枚举值转中文', () => {
  it('目录来源可读', () => {
    expect(tocSourceLabel('bookmark')).toBe('PDF 书签')
    expect(tocSourceLabel('ocr')).toBe('目录页 OCR')
  })
})

describe('pdfChapterPaths', () => {
  it('路径不带 /api/v1 前缀（$api 已设 baseURL）', () => {
    expect(pdfChapterPaths.parseToc()).toBe('/pdf-chapter-imports/parse-toc')
    expect(pdfChapterPaths.tasks()).toBe('/pdf-chapter-imports/tasks')
    expect(pdfChapterPaths.runTask(7)).toBe('/pdf-chapter-imports/tasks/7/run')
    expect(pdfChapterPaths.retryItem(3)).toBe('/pdf-chapter-imports/items/3/retry')
  })
})
