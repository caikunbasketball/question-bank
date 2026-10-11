// 供应商协议：
// - openai    : 所有 OpenAI 兼容服务（DeepSeek / 硅基流动 / 通义 / Kimi …）
// - gemini    : Google Gemini 原生协议
// - paddleocr : 文档解析专用通道（百度飞桨 AI Studio 的 PaddleOCR-VL 异步 job 接口），
//               只能挂到「文档 OCR 主解析」槽位，不能作文本/视觉对话模型
export type AIInterfaceType = 'openai' | 'gemini' | 'paddleocr'

export interface AIModel {
  id: number
  provider_id: number
  name: string
  is_vision_capable: boolean
  is_embedding_model: boolean
}

export interface AIProvider {
  id: number
  name: string
  interface_type: AIInterfaceType
  base_url?: string
  api_key: string
  is_active: boolean
  models: AIModel[]
}

export interface AIProviderCreate {
  name: string
  interface_type: AIInterfaceType
  base_url?: string
  api_key: string
  is_active?: boolean
  models?: { name: string; is_vision_capable: boolean; is_embedding_model: boolean }[]
}

export interface AIProviderUpdate {
  name?: string
  interface_type?: AIInterfaceType
  base_url?: string
  api_key?: string
  is_active?: boolean
}

export interface ActiveAIConfig {
  text_model_id: number | null
  vision_model_id: number | null
  /** 第一层：文档 OCR 主解析（PaddleOCR-VL） */
  ocr_model_id: number | null
  embedding_model_id: number | null
}
