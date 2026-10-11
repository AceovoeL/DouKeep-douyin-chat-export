// 「我是谁」（视角）在浏览器里的存法。
//
// 以前只有一个全局键 selfUid：在某个会话里把视角切成对方之后，点开另一个会话
// 还沿用上一次的选择（用户 2026-10-11 反馈）。现在**按会话各存一份**，切会话时
// 读各自那份，没记过的会话就回到「自动认出来的那个」。
//
// 单独成模块是为了让 MessageList 和 SearchBar 用同一套键名，且能单测。
const PREFIX = 'selfUid'

//: 老版本用过的全局键。现在不再读它（读了又会串会话），只负责清掉。
export const LEGACY_GLOBAL_KEY = PREFIX

//: 某个会话存视角用的键；没有会话 id 时返回空串。
export function selfUidStorageKey(convId) {
  return convId ? `${PREFIX}:${convId}` : ''
}

//: 读某个会话记下的视角 uid；没记过时返回空串（= 用自动认出来的那个）。
export function readStoredSelfUid(convId, storage = globalThis.localStorage) {
  const key = selfUidStorageKey(convId)
  if (!key || !storage) return ''
  try {
    return storage.getItem(key) || ''
  } catch {
    return ''
  }
}

//: 记下某个会话的视角；uid 为空表示「清除选择、回到自动」。
export function writeStoredSelfUid(convId, uid, storage = globalThis.localStorage) {
  const key = selfUidStorageKey(convId)
  if (!key || !storage) return
  try {
    if (uid) storage.setItem(key, uid)
    else storage.removeItem(key)
  } catch {}
}

//: 清掉老版本那个会串会话的全局键（升级后跑一次）。
export function clearLegacyGlobalSelfUid(storage = globalThis.localStorage) {
  if (!storage) return
  try {
    storage.removeItem(LEGACY_GLOBAL_KEY)
  } catch {}
}
