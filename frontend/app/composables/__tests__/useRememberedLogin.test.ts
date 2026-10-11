import { describe, it, expect, beforeEach } from 'vitest'
import { useRememberedLogin, obfuscate, deobfuscate } from '../useRememberedLogin'

describe('obfuscate / deobfuscate', () => {
    it('round-trips an ASCII credential', () => {
        const plain = JSON.stringify({ username: 'admin', password: 'Qbank@2026' })
        expect(deobfuscate(obfuscate(plain))).toBe(plain)
    })

    it('round-trips a Chinese username and unicode password', () => {
        // 用户名可能是中文，密码可能含 emoji —— 必须按 UTF-8 字节处理，
        // 直接用 btoa 会抛 InvalidCharacterError。
        const plain = JSON.stringify({ username: '陈老师', password: '密钥🔐-p@ss' })
        expect(deobfuscate(obfuscate(plain))).toBe(plain)
    })

    it('handles empty strings', () => {
        expect(deobfuscate(obfuscate(''))).toBe('')
    })

    it('does not leave the plaintext password readable in the encoded form', () => {
        const encoded = obfuscate(JSON.stringify({ username: 'admin', password: 'Qbank@2026' }))
        expect(encoded).not.toContain('Qbank@2026')
        expect(encoded).not.toContain('admin')
    })
})

describe('useRememberedLogin', () => {
    beforeEach(() => {
        window.localStorage.clear()
    })

    it('returns null when nothing was saved', () => {
        expect(useRememberedLogin().load()).toBeNull()
    })

    it('saves and loads credentials', () => {
        const { save, load, hasRemembered } = useRememberedLogin()
        save({ username: 'admin', password: 'Qbank@2026' })

        expect(load()).toEqual({ username: 'admin', password: 'Qbank@2026' })
        expect(hasRemembered()).toBe(true)
    })

    it('clears credentials', () => {
        const { save, clear, load, hasRemembered } = useRememberedLogin()
        save({ username: 'admin', password: 'Qbank@2026' })
        clear()

        expect(load()).toBeNull()
        expect(hasRemembered()).toBe(false)
    })

    it('does not store the password in plaintext', () => {
        useRememberedLogin().save({ username: 'admin', password: 'Qbank@2026' })
        const raw = window.localStorage.getItem('qb-remembered-login') ?? ''
        expect(raw).not.toContain('Qbank@2026')
    })

    it('recovers from corrupted storage instead of throwing', () => {
        window.localStorage.setItem('qb-remembered-login', 'not-valid-base64!!!')
        const { load, hasRemembered } = useRememberedLogin()

        expect(() => load()).not.toThrow()
        expect(load()).toBeNull()
        // 损坏数据应被清掉，避免每次加载都失败
        expect(window.localStorage.getItem('qb-remembered-login')).toBeNull()
        expect(hasRemembered()).toBe(false)
    })

    it('rejects a well-formed payload with the wrong shape', () => {
        window.localStorage.setItem('qb-remembered-login', obfuscate(JSON.stringify({ foo: 1 })))
        expect(useRememberedLogin().load()).toBeNull()
    })

    it('preserves a password containing whitespace and special characters', () => {
        const { save, load } = useRememberedLogin()
        const password = ' a b\tc"d\\e\'f '
        save({ username: 'u ser', password })
        expect(load()).toEqual({ username: 'u ser', password })
    })
})
