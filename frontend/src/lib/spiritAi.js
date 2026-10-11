// 小火人（精灵 AI）消息的识别与「性格」取用。
//
// 抖音的小火人是个机器人，它发的消息在库里和真人消息混在一起，没有独立的消息类型。
// 能认出它的是载荷里的 ai_business_ext（其中的 petai_style 就是这颗精灵的性格设定，
// 例如「腹黑傲娇」），以及它专用的 aweType=716。
//
// 阅读端需要它的名字：机器人不是会话里的那个人，昵称不能跟着会话名走 —— 单聊里
// 会话名是「我给对方的备注」，套到精灵身上就成了对方的名字（用户 2026-10-11 反馈）。
// 这里只负责认出它、取出性格，显示成「精灵AI + 性格标签」由界面决定。
//
// 纯函数，便于单测。
import { payloadJson } from './douyinMessage'

//: 精灵 AI 专用消息类型号（数字或字符串都可能出现）。
const SPIRIT_AWE_TYPE = 716

//: 精灵在界面上的固定名字。
export const SPIRIT_NAME = '精灵AI'

// 认出精灵消息时返回 { name, style }；不是精灵则返回 null。
// style 取不到时是空字符串（老消息 / 开场白那几条没有这个字段）。
export function getSpiritAi(msg) {
  if (!msg || typeof msg !== 'object') return null
  const cj = payloadJson(msg)
  if (!cj || typeof cj !== 'object') return null
  const ext = cj.ai_business_ext
  const hasExt = !!ext && typeof ext === 'object' && !Array.isArray(ext)
  if (!hasExt && String(cj.aweType) !== String(SPIRIT_AWE_TYPE)) return null
  const style = hasExt && typeof ext.petai_style === 'string' ? ext.petai_style.trim() : ''
  return { name: SPIRIT_NAME, style }
}
