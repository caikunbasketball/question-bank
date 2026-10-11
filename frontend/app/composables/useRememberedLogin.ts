/**
 * 「记住账号密码」本地存储。
 *
 * ⚠️ 安全说明（重要，请勿误读）
 * 这里对密码只做了**可逆混淆**（XOR + Base64），**不是加密**。
 * 只要拿到浏览器数据就能还原，因此它**不能**防御恶意脚本或有心之人。
 * 它唯一的作用是：避免密码在 localStorage / DevTools 里以明文直接可读
 * （防肩窥、防随手 grep）。
 *
 * 真正安全的「记住密码」应交给浏览器自带的密码管理器
 * —— 登录页已同时补上 `autocomplete="username" / "current-password"`，
 * 浏览器会主动询问是否保存，那条路径由操作系统钥匙串保护，优于本文件。
 *
 * 本机制定位为「个人本机（127.0.0.1）自用」的便利功能。
 * 公用电脑上不要勾选。
 */

const STORAGE_KEY = 'qb-remembered-login'
const OBFUSCATION_KEY = 'questionbank-remember-v1'

export interface RememberedLogin {
  username: string
  password: string
}

function xor (bytes: Uint8Array): Uint8Array {
  const key = new TextEncoder().encode(OBFUSCATION_KEY)
  const out = new Uint8Array(bytes.length)
  for (let i = 0; i < bytes.length; i++) {
    out[i] = bytes[i]! ^ key[i % key.length]!
  }
  return out
}

function bytesToBase64 (bytes: Uint8Array): string {
  let binary = ''
  for (const b of bytes) binary += String.fromCharCode(b)
  return btoa(binary)
}

function base64ToBytes (b64: string): Uint8Array {
  const binary = atob(b64)
  const out = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i)
  return out
}

/** 可逆混淆（非加密），见文件头说明。 */
export function obfuscate (text: string): string {
  return bytesToBase64(xor(new TextEncoder().encode(text)))
}

/** 还原 `obfuscate` 的结果；输入非法时抛错，由调用方兜底。 */
export function deobfuscate (encoded: string): string {
  return new TextDecoder().decode(xor(base64ToBytes(encoded)))
}

export const useRememberedLogin = () => {
  /**
   * 浏览器环境下可用的 localStorage。
   *
   * 用 `typeof window` 而非 `import.meta.client` 做判断：
   * 前者在 Nuxt 客户端、静态预渲染（window 不存在）与 vitest 三种环境下
   * 都成立，因此这段逻辑可以被单元测试覆盖。
   */
  const storage = (): Storage | null => {
    if (typeof window === 'undefined') return null
    try {
      // 隐私模式 / 禁用存储时会抛错
      return window.localStorage
    } catch {
      return null
    }
  }

  const load = (): RememberedLogin | null => {
    const store = storage()
    if (!store) return null
    try {
      const raw = store.getItem(STORAGE_KEY)
      if (!raw) return null
      const parsed = JSON.parse(deobfuscate(raw)) as Partial<RememberedLogin>
      if (typeof parsed?.username !== 'string' || typeof parsed?.password !== 'string') {
        return null
      }
      return { username: parsed.username, password: parsed.password }
    } catch {
      // 数据损坏或密钥变更 → 清掉，避免反复报错
      try {
        store.removeItem(STORAGE_KEY)
      } catch {
        /* ignore */
      }
      return null
    }
  }

  const save = (value: RememberedLogin): void => {
    const store = storage()
    if (!store) return
    try {
      store.setItem(
        STORAGE_KEY,
        obfuscate(JSON.stringify({ username: value.username, password: value.password })),
      )
    } catch {
      /* 存储配额满或被禁用：静默忽略，不阻断登录 */
    }
  }

  const clear = (): void => {
    const store = storage()
    if (!store) return
    try {
      store.removeItem(STORAGE_KEY)
    } catch {
      /* ignore */
    }
  }

  const hasRemembered = (): boolean => load() !== null

  return { load, save, clear, hasRemembered }
}
