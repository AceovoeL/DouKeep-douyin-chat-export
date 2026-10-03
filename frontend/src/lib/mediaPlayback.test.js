import { describe, expect, it, vi } from 'vitest'
import {
  stopDialogVideos, transcodeUrl, retryVideoWithTranscode, retryVideoIfUndecodable,
} from './mediaPlayback'

describe('transcodeUrl', () => {
  it('adds the flag to a plain media path', () => {
    expect(transcodeUrl('/media/videos/a.mp4')).toBe('/media/videos/a.mp4?tc=1')
  })
  it('appends to an existing query string', () => {
    expect(transcodeUrl('/media/videos/a.mp4?v=2')).toBe('/media/videos/a.mp4?v=2&tc=1')
  })
  it('leaves an already-flagged or unusable value untouched', () => {
    expect(transcodeUrl('/media/videos/a.mp4?tc=1')).toBe('/media/videos/a.mp4?tc=1')
    expect(transcodeUrl('/x.mp4?a=1&tc=1&b=2')).toBe('/x.mp4?a=1&tc=1&b=2')
    expect(transcodeUrl('')).toBe('')
    expect(transcodeUrl(null)).toBe(null)
  })
})

describe('retryVideoWithTranscode', () => {
  it('swaps the source and reloads exactly once', () => {
    const video = {
      currentSrc: 'http://127.0.0.1:8000/media/videos/a.mp4',
      src: '',
      dataset: {},
      load: vi.fn(),
      play: vi.fn(() => Promise.reject(new Error('autoplay refused'))),
    }
    expect(retryVideoWithTranscode(video)).toBe(true)
    expect(video.src).toBe('http://127.0.0.1:8000/media/videos/a.mp4?tc=1')
    expect(video.load).toHaveBeenCalledTimes(1)
    expect(video.play).toHaveBeenCalledTimes(1)

    // The second failure must not loop back into the transcoder.
    expect(retryVideoWithTranscode(video)).toBe(false)
    expect(video.load).toHaveBeenCalledTimes(1)
  })
  it('falls back to the declared src and tolerates bare objects', () => {
    const video = { getAttribute: () => '/media/videos/b.mp4', src: '' }
    expect(retryVideoWithTranscode(video)).toBe(true)
    expect(video.src).toBe('/media/videos/b.mp4?tc=1')
    expect(retryVideoWithTranscode(null)).toBe(false)
    expect(retryVideoWithTranscode({})).toBe(false)
  })
  it('ignores a refused autoplay', () => {
    const video = {
      getAttribute: () => '/media/videos/c.mp4',
      src: '',
      play: () => { throw new Error('NotAllowedError') },
    }
    expect(() => retryVideoWithTranscode(video)).not.toThrow()
  })
})

// Chromium reports no `error` for a codec it cannot decode — it fires
// playing/loadedmetadata, advances currentTime, and decodes nothing. The only
// usable signal is videoWidth staying 0 (measured, fix.md 第三轮).
describe('retryVideoIfUndecodable', () => {
  const playing = (over = {}) => ({
    paused: false,
    readyState: 4,
    videoWidth: 0,
    getAttribute: () => '/media/videos/a.mp4',
    src: '',
    dataset: {},
    load: vi.fn(),
    play: vi.fn(),
    ...over,
  })

  it('retries a clip that is playing but decoding nothing', () => {
    const video = playing()
    expect(retryVideoIfUndecodable(video)).toBe(true)
    expect(video.src).toBe('/media/videos/a.mp4?tc=1')
    // ...and only once.
    expect(retryVideoIfUndecodable(video)).toBe(false)
  })

  it('leaves a video the browser can decode alone', () => {
    const video = playing({ videoWidth: 592, videoHeight: 1280 })
    expect(retryVideoIfUndecodable(video)).toBe(false)
    expect(video.src).toBe('')
    expect(video.load).not.toHaveBeenCalled()
  })

  it('waits for the user: a paused clip is never converted', () => {
    expect(retryVideoIfUndecodable(playing({ paused: true }))).toBe(false)
  })

  it('waits for metadata before judging', () => {
    expect(retryVideoIfUndecodable(playing({ readyState: 0 }))).toBe(false)
  })

  it('tolerates missing elements', () => {
    expect(retryVideoIfUndecodable(null)).toBe(false)
    expect(retryVideoIfUndecodable(undefined)).toBe(false)
  })
})

describe('stopDialogVideos', () => {
  it('pauses every video and rewinds to the start', () => {
    const videos = [
      { pause: vi.fn(), currentTime: 18.4 },
      { pause: vi.fn(), currentTime: 3 },
    ]
    stopDialogVideos({ querySelectorAll: () => videos })
    expect(videos.map(video => video.pause.mock.calls.length)).toEqual([1, 1])
    expect(videos.map(video => video.currentTime)).toEqual([0, 0])
  })
  it('ignores missing roots and videos that cannot seek yet', () => {
    stopDialogVideos(null)
    const video = {
      pause: vi.fn(),
      set currentTime(_value) { throw new Error('not seekable') },
    }
    stopDialogVideos({ querySelectorAll: () => [video] })
    expect(video.pause).toHaveBeenCalled()
  })
})
