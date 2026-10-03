/**
 * Media playback fallbacks.
 *
 * Douyin's IM videos are mostly HEVC/H.265, which the browser driving the chat
 * viewer cannot decode on its own. The backend can convert such a clip to H.264
 * on demand (`backend/media_files.py`), but only when asked, so the first play
 * costs a conversion and every play after that is a plain file send.
 *
 * The awkward part is *detecting* it. Chromium does **not** reliably fire
 * `error` for a codec it lacks: measured with an H.265 clip on Edge/Windows
 * (fix.md 2026-09-27 第三轮), `loadedmetadata`, `loadeddata`, `canplay` and
 * `playing` all fire, the `play()` promise even resolves and `currentTime`
 * advances — while the element decodes exactly zero frames and reports
 * `videoWidth === 0`. So the retry hangs off `videoWidth`, not off an event
 * that may never come.
 */

const RETRY_FLAG = 'tcRetried'

/** Append the "please convert this for me" flag to a media URL. */
export function transcodeUrl(src) {
  if (!src || typeof src !== 'string') return src
  // Already flagged (or a URL we did not build) — leave it alone.
  if (/[?&]tc=1(&|$)/.test(src)) return src
  return src + (src.includes('?') ? '&' : '?') + 'tc=1'
}

/**
 * Retry a failed <video> through the on-demand transcoder.
 *
 * Returns true when a retry was started. Deliberately fires at most once per
 * element: a second failure means transcoding cannot help (the file is missing
 * or genuinely unsupported), and retrying forever would hammer the server.
 */
export function retryVideoWithTranscode(video) {
  if (!video) return false
  const current = video.currentSrc || video.getAttribute?.('src') || video.src || ''
  if (!current) return false
  if (video.dataset && video.dataset[RETRY_FLAG] === '1') return false

  const next = transcodeUrl(current)
  if (next === current) return false

  if (video.dataset) video.dataset[RETRY_FLAG] = '1'
  video.src = next
  video.load?.()
  // The user already pressed play, so resume as soon as the (now decodable)
  // rendition arrives instead of making them click a second time. Autoplay
  // policy can still refuse; that is not an error worth surfacing.
  try { video.play?.()?.catch?.(() => {}) } catch { /* ignore */ }
  return true
}

/**
 * Retry through the transcoder when the browser is pretending to play.
 *
 * Safe to call from `play`/`playing`/`loadedmetadata`: it only acts when the
 * user has actually asked for playback (`paused === false`) and the metadata is
 * in, which keeps opening a conversation from kicking off a burst of
 * conversions for clips nobody opened.
 *
 * Returns true when a retry was started.
 */
export function retryVideoIfUndecodable(video) {
  if (!video) return false
  if (video.paused !== false) return false           // nobody asked it to play
  if ((video.readyState ?? 0) < 1) return false      // metadata not in yet
  if (video.videoWidth > 0) return false             // it can decode after all
  if (video.dataset?.[RETRY_FLAG] === '1') return false
  return retryVideoWithTranscode(video)
}

export function stopDialogVideos(root) {
  if (!root?.querySelectorAll) return
  for (const video of root.querySelectorAll('video')) {
    video.pause?.()
    try { video.currentTime = 0 } catch {}
  }
}
