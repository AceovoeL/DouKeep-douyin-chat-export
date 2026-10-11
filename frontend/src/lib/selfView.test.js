import { describe, it, expect, beforeEach } from 'vitest'
import {
  LEGACY_GLOBAL_KEY, selfUidStorageKey, readStoredSelfUid, writeStoredSelfUid,
  clearLegacyGlobalSelfUid,
} from './selfView.js'

// 用内存里的假 storage，不碰真实 localStorage。
function fakeStorage() {
  const map = new Map()
  return {
    getItem: k => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: k => map.delete(k),
    _map: map,
  }
}

const CONV_A = '0:1:100:200'
const CONV_B = '0:1:100:300'

describe('selfView · 视角按会话各记一份', () => {
  let store
  beforeEach(() => { store = fakeStorage() })

  it('键名带会话 id', () => {
    expect(selfUidStorageKey(CONV_A)).toBe(`selfUid:${CONV_A}`)
    expect(selfUidStorageKey(CONV_B)).toBe(`selfUid:${CONV_B}`)
    expect(selfUidStorageKey('')).toBe('')
  })

  it('没记过时读到空串（= 回到自动）', () => {
    expect(readStoredSelfUid(CONV_A, store)).toBe('')
  })

  it('记下之后读得回来', () => {
    writeStoredSelfUid(CONV_A, 'uid-peer-1', store)
    expect(readStoredSelfUid(CONV_A, store)).toBe('uid-peer-1')
  })

  // 用户 2026-10-11 反馈的毛病：在 A 会话切了视角，点开 B 会话还是那个视角。
  it('A 会话选过的视角不会渗到 B 会话', () => {
    writeStoredSelfUid(CONV_A, 'uid-peer-1', store)
    expect(readStoredSelfUid(CONV_B, store)).toBe('')
  })

  it('两个会话各记各的', () => {
    writeStoredSelfUid(CONV_A, 'uid-peer-1', store)
    writeStoredSelfUid(CONV_B, 'uid-peer-2', store)
    expect(readStoredSelfUid(CONV_A, store)).toBe('uid-peer-1')
    expect(readStoredSelfUid(CONV_B, store)).toBe('uid-peer-2')
  })

  it('写空 = 清除这个会话的选择，回到自动', () => {
    writeStoredSelfUid(CONV_A, 'uid-peer-1', store)
    writeStoredSelfUid(CONV_A, '', store)
    expect(readStoredSelfUid(CONV_A, store)).toBe('')
  })

  it('没有会话 id 时不写也不读', () => {
    writeStoredSelfUid('', 'uid-peer-1', store)
    expect(readStoredSelfUid('', store)).toBe('')
    expect(store._map.size).toBe(0)
  })

  it('storage 读不了时不抛错', () => {
    const broken = { getItem() { throw new Error('nope') }, setItem() { throw new Error('nope') }, removeItem() { throw new Error('nope') } }
    expect(readStoredSelfUid(CONV_A, broken)).toBe('')
    expect(() => writeStoredSelfUid(CONV_A, 'x', broken)).not.toThrow()
  })
})

describe('selfView · 老版本的全局键', () => {
  it('升级后会被清掉，免得继续串会话', () => {
    const store = fakeStorage()
    store.setItem(LEGACY_GLOBAL_KEY, 'uid-old')
    clearLegacyGlobalSelfUid(store)
    expect(store.getItem(LEGACY_GLOBAL_KEY)).toBe(null)
  })

  it('读会话视角时不认老键', () => {
    const store = fakeStorage()
    store.setItem(LEGACY_GLOBAL_KEY, 'uid-old')
    expect(readStoredSelfUid(CONV_A, store)).toBe('')
  })
})
