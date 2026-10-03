<template>
  <div class="msg-panel" :class="{ 'msg-panel-static': isStatic }">
    <div v-if="!conversation" class="msg-empty">
      <div class="msg-empty-icon">💬</div>
      <div>选择一个会话查看聊天记录</div>
    </div>
    <template v-else>
      <div class="msg-header" v-if="!isStatic">
        <h3>{{ conversation.name || '未命名' }}</h3>
        <span class="msg-total">{{ total }} 条消息</span>
        <button v-if="!selfUid && senders.length === 2" class="msg-pick-self" @click="showPicker = true">
          设置"我"
        </button>
        <button v-if="selfUid" class="msg-pick-self picked" @click="showPicker = true">
          我: {{ selfUid.slice(-4) }}
        </button>
      </div>

      <div v-if="referenceError" role="status" class="msg-loading" @click="referenceError = ''">{{ referenceError }}</div>

      <!-- UID 选择弹窗 -->
      <div v-if="showPicker" class="picker-overlay" @click.self="showPicker = false">
        <div class="picker-dialog">
          <div class="picker-title">选择哪个是你自己</div>
          <div class="picker-hint">选择后消息会区分左右显示</div>
          <div
            v-for="s in senders"
            :key="s.sender_uid"
            class="picker-option"
            :class="{ active: selfUid === s.sender_uid }"
            @click="pickSelf(s.sender_uid)"
          >
            <span class="picker-uid">{{ userCache[s.sender_uid]?.nickname || ('UID ...' + s.sender_uid.slice(-6)) }}</span>
            <span class="picker-count">{{ s.msg_count }} 条消息</span>
          </div>
          <button v-if="selfUid" class="picker-clear" @click="pickSelf(null)">清除选择</button>
        </div>
      </div>

      <Transition name="fab">
        <button v-if="!isStatic && hasOlder" class="msg-jump-fab msg-jump-top" @click="jumpToTop">
          ↑ 最早消息
        </button>
      </Transition>
      <Transition name="fab">
        <button v-if="!isStatic && hasNewer" class="msg-jump-fab msg-jump-bottom" @click="jumpToBottom">
          ↓ 最新消息
        </button>
      </Transition>
      <div class="msg-list" :class="[{ 'msg-list-static': isStatic }, enterClass]" ref="listRef">
        <div v-if="loading" class="msg-loading" role="status">
          <span class="ui-spinner" aria-hidden="true"></span>加载中…
        </div>
        <div v-else-if="loadError" role="alert" class="msg-loading">
          {{ loadError }} <button @click="fetchMessages(conversation.conv_id)">重试</button>
        </div>
        <div v-if="hasOlder && !loading" class="msg-load-more" @click="loadOlder">
          ⬆ 加载更早消息
        </div>
        <template v-for="(msg, index) in messages" :key="msg.msg_id">
        <div
          v-if="shouldShow(msg) && !duplicateSystemIds.has(msg.msg_id)"
          class="msg-item"
          :data-msgid="msg.msg_id"
          :style="enterDelay(index)"
          :class="{
            'msg-self': isSelf(msg),
            'msg-system': _isSystem(msg),
            'msg-highlight': highlightMsgId === msg.msg_id,
            'group-start': isGroupStart(index),
            'msg-grouped': !isGroupStart(index),
          }"
        >
          <!-- 系统消息居中显示：「查看 JSON」跟在提示主语同侧（我 → 左，对方 → 右），分不清则放下面 -->
          <template v-if="_isSystem(msg)">
            <div class="msg-system-block">
              <div class="msg-system-line">
                <!-- 一起看视频邀请卡片 (aweType=9000) -->
                <div v-if="getWatchTogether(msg)" class="msg-watch-card">
                  <div class="msg-watch-icon">▶</div>
                  <div class="msg-watch-body">
                    <div class="msg-watch-title">{{ getWatchTogether(msg).title }}</div>
                    <div v-if="getWatchTogether(msg).subtitle" class="msg-watch-sub">{{ getWatchTogether(msg).subtitle }}</div>
                  </div>
                </div>
                <!-- 获得火花见面礼卡片 (aweType=110408)：居中、背景图、两个头像、领取按钮 -->
                <div v-else-if="flameCard(msg)" class="msg-flame-card" :style="flameCardStyle">
                  <!-- 两个头像左右并排：对面在左、本视角的人在右，只叠一点点边 -->
                  <div class="msg-flame-avatars">
                    <div class="msg-flame-avatar msg-flame-avatar-peer">
                      <img
                        v-if="flameAvatar(msg, 'peer')"
                        :src="flameAvatar(msg, 'peer')"
                        alt="对方头像"
                        :loading="imgLoading"
                        @error="onImgError"
                      />
                    </div>
                    <div class="msg-flame-avatar msg-flame-avatar-self">
                      <img
                        v-if="flameAvatar(msg, 'self')"
                        :src="flameAvatar(msg, 'self')"
                        alt="我的头像"
                        :loading="imgLoading"
                        @error="onImgError"
                      />
                    </div>
                  </div>
                  <div class="msg-flame-row">
                    <span class="msg-flame-days">
                      <img class="msg-flame-icon" :src="flameCardFire" alt="" />
                      <span class="msg-flame-num">{{ flameCard(msg).days }}</span>
                    </span>
                    <span class="msg-flame-title">{{ flameCard(msg).title }}</span>
                  </div>
                  <!-- 按钮只做展示：点不动，只有鼠标悬停反馈 -->
                  <div class="msg-flame-claim" aria-disabled="true">{{ flameCard(msg).button }}</div>
                </div>
                <!-- 联系门店引导卡片 (aweType=110284 · card_type=life_bar_link_private_msg_guide)：
                     居中显示，卡片外观是 assets/call-to-shop.png，右侧空白处叠一个「联系」按钮 -->
                <div v-else-if="callShopCard(msg)" class="msg-call-shop-card">
                  <img
                    class="msg-call-shop-bg"
                    :src="callShopBackground"
                    :alt="callShopAlt(msg)"
                    :loading="imgLoading"
                  />
                  <!-- 按钮只做展示：点不动，只有鼠标悬停反馈 -->
                  <div class="msg-call-shop-btn" aria-disabled="true">{{ callShopCard(msg).button }}</div>
                </div>
                <!-- 群公告（type_code=1004）：头像 +「某某 发布了群公告」+ 公告正文。
                     抖音把这条消息推给全群成员，消息里的 sender_uid 就是改公告的人。 -->
                <div v-else-if="groupNotice(msg)" class="msg-notice-card" @contextmenu="selectSystemContent">
                  <div class="msg-notice-head">
                    <div class="msg-notice-face">
                      <img v-if="getAvatarUrl(msg)" :src="getAvatarUrl(msg)" alt="" @error="onImgError" />
                      <span
                        v-else
                        :style="{ background: isSelf(msg) ? 'var(--accent)' : 'var(--bg-tertiary)', color: isSelf(msg) ? '#fff' : 'var(--text-secondary)' }"
                      >{{ displayName(msg)[0] }}</span>
                    </div>
                    <span class="msg-notice-who">{{ displayName(msg) }}</span>
                    <span class="msg-notice-verb">发布了群公告</span>
                    <span class="msg-notice-time">{{ formatTime(msg.timestamp) }}</span>
                  </div>
                  <div v-if="groupNotice(msg).body" class="msg-notice-body">{{ groupNotice(msg).body }}</div>
                </div>
                <!-- 搜索框里搜到的词也让它在提示里高亮（群通知的句子是渲染出来的，
                     跟内容搜索用的是同一份规则，高亮才对得上） -->
                <div
                  v-else
                  class="msg-system-text"
                  @contextmenu="selectSystemContent"
                  v-html="renderText(renderSystemMsg(msg, selfUid))"
                ></div>
                <!-- 调试：展开查看 raw_data -->
                <div
                  v-if="!isStatic"
                  class="msg-system-side"
                  :class="`msg-system-side-${jsonToggleSide(msg)}`"
                >
                  <button
                    type="button"
                    class="msg-json-toggle"
                    :class="{ open: expandedRaw[msg.msg_id] }"
                    @click.stop="toggleRaw(msg.msg_id)"
                  >
                    {{ expandedRaw[msg.msg_id] ? '收起 JSON' : '查看 JSON' }}
                  </button>
                </div>
              </div>
              <!-- 引用的分享视频卡片 -->
              <div
                v-if="sysRefCache[msg.msg_id]"
                class="msg-system-ref"
                @click="openSystemReference(msg)"
              >
                <img
                  v-if="sysRefCache[msg.msg_id].cover"
                  :src="sysRefCache[msg.msg_id].cover"
                  class="msg-system-ref-cover"
                  @error="onImgError"
                />
                <span class="msg-system-ref-title">{{ sysRefCache[msg.msg_id].title }}</span>
              </div>
              <pre v-if="!isStatic && expandedRaw[msg.msg_id]" class="msg-json-body">{{ formatRaw(msg) }}</pre>
            </div>
          </template>
          <!-- 普通消息 -->
          <template v-else>
            <div class="msg-avatar" :class="{ 'msg-avatar-img': getAvatarUrl(msg) }">
              <img v-if="getAvatarUrl(msg)" :src="getAvatarUrl(msg)" @error="onImgError" />
              <span v-else :style="{ background: isSelf(msg) ? 'var(--accent)' : 'var(--bg-tertiary)', color: isSelf(msg) ? '#fff' : 'var(--text-secondary)' }">{{ displayName(msg)[0] }}</span>
            </div>
            <div class="msg-body" @contextmenu="selectMsgContent">
              <div class="msg-sender">{{ displayName(msg) }}</div>
              <!-- 引用/回复消息 -->
              <div v-if="getRefMsg(msg)" class="msg-ref-quote" @click="jumpToRefMsg(getRefMsg(msg))">
                <span v-if="getRefNickname(getRefMsg(msg))" class="msg-ref-name">{{ getRefNickname(getRefMsg(msg)) }}：</span>
                <span class="msg-ref-content" v-html="renderText(getRefContent(getRefMsg(msg)))"></span>
              </div>
              <!-- 互相关注提示（「我们已互相关注，可以开始聊天了」/「我们已成为朋友」）：
                   抖音客户端里这句是"对方发过来的"，所以按对方的消息显示（左边、带头像和昵称），不居中。 -->
              <div
                v-if="peerNotice(msg)"
                class="msg-bubble msg-notice-bubble"
                v-html="renderText(peerNotice(msg))"
              ></div>
              <a v-else-if="getProfileCard(msg)" class="msg-profile-card" :href="getProfileCard(msg).url" target="_blank" rel="noopener noreferrer">
                <img v-if="getProfileCard(msg).avatar" :src="getProfileCard(msg).avatar" alt="用户头像" :loading="imgLoading" @error="onImgError" />
                <div><strong>{{ getProfileCard(msg).name }}</strong>
                  <div v-if="getProfileCard(msg).description">抖音号：{{ getProfileCard(msg).description }}</div>
                  <div v-if="getProfileCard(msg).followers !== null">粉丝：{{ getProfileCard(msg).followers }}</div>
                  <small>用户名片 · 查看主页</small>
                </div>
              </a>
              <ForwardRecords v-else-if="getForwardInfo(msg)" :message="msg" :selfUid="selfUid" />
              <!-- 表情包（含 type=0 的 515/517/520 漏网） -->
              <div v-else-if="(msg.msg_type === 2 || isLooseEmoji(msg)) && getEmojiSrc(msg)" class="msg-media msg-media-emoji">
                <img :src="getEmojiSrc(msg)" :alt="msg.content" :loading="imgLoading" @click="openLightbox(getEmojiSrc(msg))" @error="onImgError" />
              </div>
              <!-- 图片/视频：优先本地原文件，回退到 inline_pic 缩略图 -->
              <div v-else-if="msg.msg_type === 3 || isLooseImage(msg)" class="msg-media msg-media-image">
                <video
                  v-if="isVideoMsg(msg)"
                  :src="'/media/' + msg.media_local_path"
                  controls
                  preload="metadata"
                  :poster="getInlinePic(msg)"
                  @error="onVideoError"
                  @play="onVideoPlayAttempt"
                  @playing="onVideoPlayAttempt"
                />
                <img
                  v-else-if="getImageSrc(msg)"
                  :src="getImageSrc(msg)"
                  alt="图片"
                  :loading="imgLoading"
                  @click="openLightbox(getImageSrc(msg))"
                />
                <div v-else class="msg-media-missing">[图片已失效]</div>
              </div>
              <!-- 仅看一次消息（aweType=10401 + 正文，非分享卡片） -->
              <div v-else-if="isViewOnce(msg)" class="msg-bubble msg-view-once-bubble" v-html="renderText(getViewOnceText(msg))"></div>
              <!-- 商品分享卡片：上图 + 描述（最多 3 行）+ 原价/券后价 -->
              <div v-else-if="getGoodsCard(msg)" class="msg-goods-card" @click="openShare(msg)">
                <div class="msg-goods-cover-wrap">
                  <img
                    v-if="getGoodsCard(msg).cover"
                    :src="getGoodsCard(msg).cover"
                    class="msg-goods-cover"
                    :alt="getGoodsCard(msg).title"
                    :loading="imgLoading"
                    @error="onImgError"
                  />
                  <span v-else class="msg-goods-cover-missing">商品图片已失效</span>
                </div>
                <div class="msg-goods-body">
                  <div v-if="getGoodsCard(msg).title" class="msg-goods-title">{{ getGoodsCard(msg).title }}</div>
                  <div v-if="getGoodsCard(msg).couponPrice !== ''" class="msg-goods-price">
                    <span v-if="getGoodsCard(msg).originalPrice !== ''" class="msg-goods-price-num">￥{{ getGoodsCard(msg).originalPrice }}</span>
                    <span class="msg-goods-price-num">券后价￥{{ getGoodsCard(msg).couponPrice }}</span>
                  </div>
                </div>
              </div>
              <!-- 分享卡片（含 type=0/1 漏网的作品/地点/直播卡） -->
              <div v-else-if="isShareCard(msg)" class="msg-share-card" @click="openShare(msg)">
                <div v-if="getShareInfo(msg).comment || getShareInfo(msg).commentImg" class="msg-share-comment">
                  <span v-if="getShareInfo(msg).commentUser" class="msg-share-comment-user">{{ getShareInfo(msg).commentUser }}：</span>{{ getShareInfo(msg).comment }}
                  <img v-if="getShareInfo(msg).commentImg" :src="getShareInfo(msg).commentImg" class="msg-share-comment-img" :loading="imgLoading" @error="onImgError" />
                </div>
                <div class="msg-share-card-inner">
                  <div class="msg-share-card-body">
                    <!-- 没有标题的作品分享不写占位文字，标题行整行留白 -->
                    <div v-if="shareCardTitle(msg)" class="msg-share-card-title">{{ shareCardTitle(msg) }}</div>
                    <div v-if="getShareInfo(msg).author" class="msg-share-card-author">
                      @ {{ getShareInfo(msg).author }}
                    </div>
                  </div>
                  <img
                    v-if="getShareInfo(msg).cover"
                    :src="getShareInfo(msg).cover"
                    class="msg-share-card-cover"
                    :loading="imgLoading"
                    @error="onImgError"
                  />
                </div>
              </div>
              <!-- 视频消息：本地有 .mp4 → 真播放；否则展示封面 + 时长 -->
              <div v-else-if="isJsonVideo(msg)" class="msg-media msg-video-poster">
                <video
                  v-if="hasLocalVideo(msg)"
                  :src="'/media/' + msg.media_local_path"
                  :poster="getVideoPoster(msg)"
                  controls
                  preload="none"
                  class="msg-video-player"
                  @error="onVideoError"
                  @play="onVideoPlayAttempt"
                  @playing="onVideoPlayAttempt"
                />
                <template v-else>
                  <img
                    v-if="getVideoPoster(msg)"
                    :src="getVideoPoster(msg)"
                    :loading="imgLoading"
                    @click="openLightbox(getVideoPoster(msg))"
                  />
                  <div v-else class="msg-media-missing">[视频]</div>
                  <div class="msg-video-overlay">
                    <span class="msg-video-play">▶</span>
                    <span v-if="getVideoDuration(msg)" class="msg-video-dur">{{ getVideoDuration(msg) }}</span>
                  </div>
                </template>
              </div>
              <!-- msg_type=1 但实际是贴纸/表情 JSON -->
              <div v-else-if="isJsonSticker(msg)" class="msg-media msg-media-emoji">
                <img v-if="getStickerUrl(msg)" :src="getStickerUrl(msg)" :loading="imgLoading" @error="onImgError" />
                <div v-else class="msg-media-missing">[贴纸]</div>
              </div>
              <!-- msg_type=1 但实际是分享卡片（JSON content 含 content_title） -->
              <div v-else-if="isJsonShare(msg)" class="msg-share-card" @click="openShare(msg)">
                <div v-if="getShareInfo(msg).comment || getShareInfo(msg).commentImg" class="msg-share-comment">
                  <span v-if="getShareInfo(msg).commentUser" class="msg-share-comment-user">{{ getShareInfo(msg).commentUser }}：</span>{{ getShareInfo(msg).comment }}
                  <img v-if="getShareInfo(msg).commentImg" :src="getShareInfo(msg).commentImg" class="msg-share-comment-img" :loading="imgLoading" @error="onImgError" />
                </div>
                <div class="msg-share-card-inner">
                  <div class="msg-share-card-body">
                    <!-- 没有标题的作品分享不写占位文字，标题行整行留白 -->
                    <div v-if="shareCardTitle(msg)" class="msg-share-card-title">{{ shareCardTitle(msg) }}</div>
                    <div v-if="getShareInfo(msg).author" class="msg-share-card-author">
                      @ {{ getShareInfo(msg).author }}
                    </div>
                  </div>
                  <img
                    v-if="getShareInfo(msg).cover"
                    :src="getShareInfo(msg).cover"
                    class="msg-share-card-cover"
                    :loading="imgLoading"
                    @error="onImgError"
                  />
                </div>
              </div>
              <!-- 语音消息 -->
              <div v-else-if="isVoiceMsg(msg)" class="msg-bubble msg-voice-bubble">
                <div class="msg-voice-player" :class="{ playing: isVoicePlaying(msg) }">
                  <button
                    type="button"
                    class="msg-voice-play"
                    :class="{ playing: isVoicePlaying(msg) }"
                    :disabled="!getVoiceUrl(msg)"
                    :aria-label="isVoicePlaying(msg) ? '暂停语音' : '播放语音'"
                    @click.stop="toggleVoice(msg)"
                  >
                    <svg v-if="!isVoicePlaying(msg)" viewBox="0 0 24 24" aria-hidden="true">
                      <path d="M8 5.2v13.6a1 1 0 0 0 1.55.83l9.25-6.8a1 1 0 0 0 0-1.66l-9.25-6.8A1 1 0 0 0 8 5.2Z" />
                    </svg>
                    <svg v-else viewBox="0 0 24 24" aria-hidden="true">
                      <path d="M7 5.5h3.4v13H7v-13Zm6.6 0H17v13h-3.4v-13Z" />
                    </svg>
                  </button>
                  <span class="msg-voice-wave" aria-hidden="true">
                    <i v-for="n in 13" :key="n"></i>
                  </span>
                  <span class="msg-voice-dur">{{ getVoiceDuration(msg) }}″</span>
                  <audio
                    :ref="el => setVoiceAudioRef(msg.msg_id, el)"
                    preload="none"
                    :src="getVoiceUrl(msg)"
                    @ended="onVoiceEnded(msg.msg_id)"
                  ></audio>
                </div>
                <div v-if="msg.voice_transcription" class="msg-voice-divider"></div>
                <div v-if="msg.voice_transcription" class="msg-voice-transcript">
                  <span v-html="renderText(msg.voice_transcription)"></span>
                </div>
              </div>
              <!-- 评论引用视频（aweType=700，文本+关联视频） -->
              <div v-else-if="isVideoComment(msg)" class="msg-share-card" @click="openVideoReference(msg)">
                <div class="msg-share-comment" v-html="renderText(msg.content)"></div>
                <div class="msg-share-card-inner msg-share-card-ref">
                  <span class="msg-share-card-ref-icon">▶</span>
                  <span class="msg-share-card-ref-text">引用的视频</span>
                </div>
              </div>
              <!-- 文本消息 -->
              <div v-else class="msg-bubble" v-html="renderText(msg.content)"></div>
              <div class="msg-time">
                <template v-for="(kind, index) in getModifyKinds(msg)" :key="kind">
                  <span v-if="index" class="msg-modify-sep">/</span>
                  <span
                    class="msg-modify-tag"
                    :class="'msg-modify-' + kind"
                    :title="MODIFY_KIND_TITLES[kind]"
                  >{{ MODIFY_KIND_LABELS[kind] }}</span>
                  <span
                    v-if="kind === 'reaction' && reactionDetail(msg)"
                    class="msg-reaction-detail"
                  >{{ reactionDetail(msg) }}</span>
                </template>
                {{ formatTime(msg.timestamp) }}
              </div>
              <!-- 调试：展开查看 raw_data -->
              <template v-if="!isStatic">
                <button
                  type="button"
                  class="msg-json-toggle"
                  :class="{ open: expandedRaw[msg.msg_id] }"
                  @click.stop="toggleRaw(msg.msg_id)"
                >
                  {{ expandedRaw[msg.msg_id] ? '收起 JSON' : '查看 JSON' }}
                </button>
                <pre v-if="expandedRaw[msg.msg_id]" class="msg-json-body" @click.stop>{{ formatRaw(msg) }}</pre>
              </template>
            </div>
          </template>
        </div>
        </template>
        <div v-if="hasNewer && !loading" class="msg-load-more" @click="loadNewer">
          ⬇ 加载更新消息
        </div>
        <div v-if="messages.length === 0 && !loading && !loadError" class="msg-no-data">
          暂无消息
        </div>
      </div>
    </template>

    <MessageLightbox v-model="lightboxSrc" />
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { renderRichText as _renderRichText } from '@/lib/highlight'
import { resolveAvatarUrl } from '@/lib/media'
import MessageLightbox from './MessageLightbox.vue'
import {
  clearCjCache, getContentJson, tryParseJson, tryParseShareContent, extractShareTitle,
  isJsonSystemMsg, isJsonSticker, getStickerUrl, shouldShow, renderSystemMsg, getWatchTogether, getProfileCard, getForwardInfo, isSystemMsg, duplicateSystemMessageIds,
  extractServerMsgIds, isVideoComment, isJsonShare, getShareInfo, shareCardTitle, getInlinePic,
  isLooseEmoji, isLooseImage, isShareCard,
  isVideoMsg, hasLocalVideo, isJsonVideo, getVideoPoster, getVideoDuration,
  getImageSrc, getEmojiSrc, isViewOnce, getViewOnceText, getModifyKinds, getModifyReactions,
  MODIFY_KIND_LABELS, MODIFY_KIND_TITLES, isVoiceMsg, getVoiceUrl, getVoiceDuration,
  getRefMsg, getRefContent, getRefNickname,
  systemNoticeSide, peerRelationNotice,
  getGroupNotice,
} from '@/lib/douyinMessage'
import { getGoodsCard, clearGoodsCardCache } from '@/lib/goodsCard'
import { getFlameGiftCard, isFlameGiftCard, pickFlameAvatar, clearFlameCardCache } from '@/lib/flameCard'
import { getCallShopCard, isCallShopCard, clearCallShopCardCache } from '@/lib/callShopCard'
import { retryVideoWithTranscode, retryVideoIfUndecodable } from '@/lib/mediaPlayback'

// 卡片要用的图（项目 assets 里的原图）：火花卡背景 + 火花图标 + 联系门店卡外观。
import flameCardBackground from '@/assets/background.png'
import flameCardFire from '@/assets/fire.png'
import callShopBackground from '@/assets/call-to-shop.png'

import ForwardRecords from './ForwardRecords.vue'

const props = defineProps({
  conversation: Object,
  searchHighlight: String,
  jumpToSeq: Number,
  // 截图模式：只加载 [startSeq, endSeq] 区间，隐藏交互 chrome，不绑定滚动
  staticRange: Object,
  embeddedMessages: Array,
  selfUidOverride: String,
})
const emit = defineEmits(['jumped', 'staticLoaded'])

const messages = ref([])
const total = ref(0)
const loading = ref(false)
let scrollLocked = false  // 防止程序化滚动触发无限加载
const hasOlder = ref(false)
const hasNewer = ref(false)
const listRef = ref(null)
const senders = ref([])
const selfUid = ref(props.selfUidOverride || localStorage.getItem('selfUid') || '')
const showPicker = ref(false)
const isStatic = computed(() => !!props.staticRange || !!props.embeddedMessages)
const duplicateSystemIds = computed(() => duplicateSystemMessageIds(messages.value))
const imgLoading = computed(() => (isStatic.value ? 'eager' : 'lazy'))

// ── 列表进场动画 ──
// 只在「整屏换内容」时播放（切会话 / 搜索定位 / 引用跳转），
// 翻页追加的消息保持静止，避免破坏滚动位置和滚动锚定。
// 用两个交替的动画名，保证连续两次换内容都能重新播放（同名动画不会自动重启）。
const enterEpoch = ref(0)
let enterTimer = null
const enterClass = computed(() => {
  if (!enterEpoch.value) return ''
  return enterEpoch.value % 2 ? 'msg-enter-a' : 'msg-enter-b'
})

// 新消息从下往上依次浮现：只给末尾 8 条排延迟，避免长列表整体被推迟。
function enterDelay(index) {
  if (!enterEpoch.value) return null
  const fromEnd = messages.value.length - 1 - index
  if (fromEnd < 0 || fromEnd > 7) return null
  return { '--enter-delay': `${(7 - fromEnd) * 22}ms` }
}

function reducedMotion() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

function playEnter() {
  if (isStatic.value || reducedMotion()) return
  clearTimeout(enterTimer)
  enterEpoch.value += 1
  // 最长的一段是「延迟 154ms + 动画 320ms」，留出余量再摘掉类名
  enterTimer = setTimeout(() => { enterEpoch.value = 0 }, 700)
}

// ── 初次落地后短暂跟随底部 ──
// 消息里的图片 / 表情 / 视频是异步才拿到尺寸的：贴底的那一刻列表还是「矮版本」，
// 等媒体量好尺寸，内容会明显长高，而浏览器的滚动锚定只保证画面不跳、
// 不保证仍然贴着最新消息（实测会停在离底部两三条消息的位置）。
// 所以落地后的一小段时间里继续贴底，用户一旦自己滚动就立刻交还控制权。
const FOLLOW_BOTTOM_MS = 1000  // 最长跟随时间
const FOLLOW_SETTLE_MS = 250   // 行高连续这么久没变化就提前收工
let followActive = false
let followDeadline = 0
let followSettleTimer = null
let bottomObserver = null
let lastPinnedTop = 0
const rowHeights = new Map()

function pinToBottom() {
  const list = listRef.value
  if (!list) return
  const max = list.scrollHeight - list.clientHeight
  if (Math.abs(list.scrollTop - max) > 1) list.scrollTop = max
  lastPinnedTop = list.scrollTop
}

function stopFollowBottom() {
  if (!followActive && !bottomObserver) return
  followActive = false
  clearTimeout(followSettleTimer)
  followSettleTimer = null
  if (bottomObserver) bottomObserver.disconnect()
  bottomObserver = null
  rowHeights.clear()
  const list = listRef.value
  if (!list) return
  list.removeEventListener('wheel', stopFollowBottom)
  list.removeEventListener('pointerdown', stopFollowBottom)
  list.removeEventListener('touchstart', stopFollowBottom)
  window.removeEventListener('keydown', stopFollowBottom)
  list.removeEventListener('scroll', onFollowScroll)
}

// 只有「往上跑」才算用户操作：跟随期间浏览器锚定自己也会改 scrollTop（往底部方向）。
function onFollowScroll() {
  const list = listRef.value
  if (!list || !followActive) return
  if (list.scrollTop < lastPinnedTop - 1) stopFollowBottom()
}

function startFollowBottom() {
  stopFollowBottom()
  const list = listRef.value
  if (isStatic.value || !list) return
  pinToBottom()
  if (typeof ResizeObserver === 'undefined') return

  followActive = true
  followDeadline = performance.now() + FOLLOW_BOTTOM_MS
  bottomObserver = new ResizeObserver((entries) => {
    if (!followActive) return
    // 首次回调只是「确认元素存在」，只有行高真的变了才算内容长开
    let changed = false
    for (const entry of entries) {
      const height = entry.contentRect.height
      if (rowHeights.get(entry.target) !== height) {
        rowHeights.set(entry.target, height)
        changed = true
      }
    }
    if (!changed) return
    if (performance.now() > followDeadline) { stopFollowBottom(); return }
    pinToBottom()
    clearTimeout(followSettleTimer)
    followSettleTimer = setTimeout(stopFollowBottom, FOLLOW_SETTLE_MS)
  })
  for (const row of list.querySelectorAll(':scope > .msg-item')) bottomObserver.observe(row)

  list.addEventListener('wheel', stopFollowBottom, { passive: true })
  list.addEventListener('pointerdown', stopFollowBottom, { passive: true })
  list.addEventListener('touchstart', stopFollowBottom, { passive: true })
  window.addEventListener('keydown', stopFollowBottom)
  list.addEventListener('scroll', onFollowScroll, { passive: true })
  followSettleTimer = setTimeout(stopFollowBottom, FOLLOW_BOTTOM_MS)  // 兜底收工
}

// 用户信息缓存 { uid: { nickname, avatar_url, unique_id } }
const userCache = reactive({})

// Keep native audio elements behind the compact voice bubble and allow only
// one voice message to play at a time.
const voiceAudioRefs = new Map()
const playingVoiceId = ref(null)

function setVoiceAudioRef(msgId, el) {
  if (el) voiceAudioRefs.set(msgId, el)
  else voiceAudioRefs.delete(msgId)
}

function isVoicePlaying(msg) {
  return playingVoiceId.value === msg.msg_id
}

async function toggleVoice(msg) {
  const audio = voiceAudioRefs.get(msg.msg_id)
  if (!audio || !getVoiceUrl(msg)) return
  if (!audio.paused) {
    audio.pause()
    playingVoiceId.value = null
    return
  }
  for (const [msgId, other] of voiceAudioRefs) {
    if (msgId !== msg.msg_id && !other.paused) other.pause()
  }
  try {
    await audio.play()
    playingVoiceId.value = msg.msg_id
  } catch {
    playingVoiceId.value = null
  }
}

function onVoiceEnded(msgId) {
  if (playingVoiceId.value === msgId) playingVoiceId.value = null
}

// content_json 解析缓存
// 系统消息引用的分享视频缓存 { msg_id: { title, cover, itemId } }
const sysRefCache = reactive({})

// 调试用：{ msg_id: true } 表示该条消息的 raw_data 已展开
const expandedRaw = reactive({})

function toggleRaw(msgId) {
  expandedRaw[msgId] = !expandedRaw[msgId]
}

// 调试展示原始 raw_data：优先解析后美化缩进，解析失败则原样输出
function formatRaw(msg) {
  const raw = msg.raw_data
  if (raw === null || raw === undefined || raw === '') return '（无 raw_data）'
  if (typeof raw === 'object') {
    try { return JSON.stringify(raw, null, 2) } catch { return String(raw) }
  }
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return raw
  }
}

function isSelf(msg) {
  // 互相关注那句提示是对方发出来的，固定显示在对方那一侧（左边）。
  if (peerNotice(msg)) return false
  if (!selfUid.value) return false
  return msg.sender_uid === selfUid.value
}

// 表情快捷回复的明细：谁的什么表情。写在「表情快捷回复」标签右边，
// 形如「[爱心] | 小明」，多条回应用逗号隔开。
function reactionDetail(msg) {
  const reactions = getModifyReactions(msg)
  if (!reactions.length) return ''
  return reactions
    .map(item => {
      const emoji = item.emoji || '[表情]'
      const name = reactionAuthor(item.uid)
      return name ? `${emoji} | ${name}` : emoji
    })
    .join('，')
}

// 回应者一律显示昵称，不显示「我 / 对方」这类代词（自己也要显示自己的昵称）。
// 昵称走用户接口；拿不到时，1:1 会话用会话名（也就是对方的备注名）兜底。
function reactionAuthor(uid) {
  if (!uid) return ''
  const cached = userCache[uid]
  if (cached?.nickname) return cached.nickname
  if (String(uid) === String(noticePeerUid.value) && props.conversation?.name) {
    return props.conversation.name
  }
  return `用户${String(uid).slice(-6)}`
}

function _isSystem(msg) {
  // 「我们已互相关注，可以开始聊天了」/「我们已成为朋友」是对方发来的消息，
  // 抖音客户端也是按对方消息显示，所以不进系统提示（不居中）。
  if (peerNotice(msg)) return false
  // 「获得火花见面礼」卡（aweType=110408）解析上算分享卡，但排版是系统提示样式，
  // 也要居中、不要头像和昵称，所以跟系统提示一起走。
  // 「联系门店」引导卡（aweType=110284）同理：抖音里就是会话中间的一条系统卡片。
  // 群公告（type_code=1004）也是居中的卡片，卡片自带头像和"谁发布了"，所以同样走这里。
  return isSystemMsg(msg) || isFlameGiftCard(msg) || isCallShopCard(msg)
}

// 群公告卡片：返回 { title, body } 或 null（见 lib/douyinMessage.js）。
function groupNotice(msg) {
  return getGroupNotice(msg)
}

// 「查看 JSON」贴在系统提示的哪一侧：随"我是谁"（selfUid）渲染出的人称变化。
function jsonToggleSide(msg) {
  return systemNoticeSide(msg, selfUid.value)
}

// A message starts a new visual group unless it continues the previous
// message's sender within a short window. Grouped messages hide the repeated
// avatar + name and sit tighter. System messages always break a group.
function isGroupStart(index) {
  if (index <= 0) return true
  const cur = messages.value[index]
  const prev = messages.value[index - 1]
  if (!prev || _isSystem(cur) || _isSystem(prev)) return true
  if (prev.sender_uid !== cur.sender_uid) return true
  return Math.abs((cur.timestamp || 0) - (prev.timestamp || 0)) > 300  // >5 min
}

// 单聊的 conv_id 形如 "0:1:uidA:uidB"，群聊是纯数字雪花 ID。
const isGroupConv = computed(() => {
  const id = props.conversation?.conv_id || ''
  return !!id && !id.includes(':')
})

// 互相关注那句提示（「我们已互相关注，可以开始聊天了」/「我们已成为朋友」）在抖音里
// 是对方发来的消息，所以固定按"对方"那一侧渲染：单聊的 conv_id 去掉自己就是对面。
const noticePeerUid = computed(() => {
  const parts = String(props.conversation?.conv_id || '').split(':')
  if (parts.length < 4) return ''
  const [first, second] = parts.slice(-2)
  // 没选过"我是谁"时分不清哪段是自己，就不猜（名字退回会话名 = 单聊里的对方昵称）。
  if (!selfUid.value) return ''
  return first === selfUid.value ? second : first
})

function peerNotice(msg) {
  return peerRelationNotice(msg)
}

function noticePeerName() {
  const uid = noticePeerUid.value
  const cached = uid ? userCache[uid] : null
  if (cached?.nickname) return cached.nickname
  // 单聊的会话名就是对方昵称，兜底够用。
  return props.conversation?.name || '对方'
}

function noticePeerAvatar() {
  const uid = noticePeerUid.value
  return resolveAvatarUrl(uid ? userCache[uid]?.avatar_url : '')
}

function displayName(msg) {
  if (peerNotice(msg)) return noticePeerName()
  if (selfUid.value && msg.sender_uid === selfUid.value) {
    const u = userCache[msg.sender_uid]
    return u?.nickname || '我'
  }
  const u = userCache[msg.sender_uid]
  if (u?.nickname) return u.nickname
  if (msg.sender_name && msg.sender_name !== '__self__') return msg.sender_name
  // 群聊里回退成会话名 = 把每个不认识的成员都显示成群名，不同的人会糊成同一个。
  // 单聊没这个问题（会话名就是对方昵称）。
  if (props.embeddedMessages || isGroupConv.value) {
    return msg.sender_uid ? `用户${msg.sender_uid.slice(-6)}` : '群成员'
  }
  return props.conversation?.name || '对方'
}

function getAvatarUrl(msg) {
  if (peerNotice(msg)) return noticePeerAvatar()
  return resolveAvatarUrl(userCache[msg.sender_uid]?.avatar_url)
}

// 火花见面礼卡片（aweType=110408）。头像优先跟聊天查看器用同一份最新头像
// （users 表里的，按 uid 取），那个人还没资料时才用卡片自带的那张。
function flameCard(msg) {
  return getFlameGiftCard(msg, selfUid.value)
}

function flameAvatar(msg, which) {
  const card = flameCard(msg)
  if (!card) return ''
  const uid = which === 'self' ? card.selfUid : card.peerUid
  return pickFlameAvatar(card, which, resolveAvatarUrl(userCache[uid]?.avatar_url))
}

// 卡片背景是项目 assets/background.png（构建时打进 dist/assets）。
const flameCardStyle = { backgroundImage: `url("${flameCardBackground}")` }

// 联系门店引导卡片（aweType=110284）：卡片外观用 assets/call-to-shop.png，
// 卡片自己带的主标题/副标题只当图片说明（alt）用，文字已经画在图片里了。
function callShopCard(msg) {
  return getCallShopCard(msg)
}

function callShopAlt(msg) {
  const card = callShopCard(msg)
  if (!card) return ''
  return [card.title, card.subtitle].filter(Boolean).join('，') || '联系门店'
}

async function fetchUserInfo(uid) {
  if (!uid || userCache[uid]) return
  try {
    const res = await fetch(`/api/users/${uid}`)
    if (res.ok) {
      const data = await res.json()
      userCache[uid] = data
    }
  } catch {}
}

async function loadUserInfoForMessages(msgList) {
  const uids = new Set()
  for (const msg of msgList) {
    if (msg.sender_uid && !userCache[msg.sender_uid]) uids.add(msg.sender_uid)
    // 表情快捷回复要显示「谁回应的」，回应者可能不是这条消息的发送者。
    for (const item of getModifyReactions(msg)) {
      if (item.uid && !userCache[item.uid]) uids.add(item.uid)
    }
    // 互相关注提示要显示对方的头像/昵称，所以对面那个 uid 也得查一次。
    if (peerRelationNotice(msg) && noticePeerUid.value && !userCache[noticePeerUid.value]) {
      uids.add(noticePeerUid.value)
    }
    // 火花卡里的两个人（我 / 对面）也要按 uid 查一次，卡片才拿得到最新头像。
    const card = getFlameGiftCard(msg)
    if (card) {
      for (const uid of [card.selfUid, card.peerUid]) {
        if (uid && !userCache[uid]) uids.add(uid)
      }
    }
  }
  await Promise.all(Array.from(uids).map(uid => fetchUserInfo(uid)))
}

function pickSelf(uid) {
  selfUid.value = uid || ''
  if (uid) {
    localStorage.setItem('selfUid', uid)
  } else {
    localStorage.removeItem('selfUid')
  }
  showPicker.value = false
}

// 系统消息引用的视频：异步加载分享消息的标题和封面
async function loadSysRefs(msgList) {
  for (const msg of msgList) {
    if (msg.msg_type !== 0 || sysRefCache[msg.msg_id]) continue
    const smids = extractServerMsgIds(msg)
    if (!smids.length) continue
    for (const smid of smids) {
      try {
        const res = await fetch(`/api/messages/srv_${smid}`)
        if (!res.ok) continue
        const refMsg = await res.json()
        const info = getShareInfo(refMsg)
        if (info.title || info.cover) {
          sysRefCache[msg.msg_id] = { ...info, serverId: smid }
          break
        }
      } catch {}
    }
  }
}

function openSystemReference(msg) {
  const target = sysRefCache[msg.msg_id]
  if (props.embeddedMessages) {
    if (target?.itemId) window.open(`https://www.douyin.com/video/${target.itemId}`, '_blank')
  } else if (target?.serverId) jumpToRefMsg({ server_id: target.serverId })
}

async function openVideoReference(msg) {
  if (props.embeddedMessages) return openShare(msg)
  const ref = getRefMsg(msg)
  if (ref?.server_id) return jumpToRefMsg(ref)
  const convId = props.conversation?.conv_id
  try {
    const res = await fetch(`/api/messages/${encodeURIComponent(msg.msg_id)}/referenced-video`)
    if (!res.ok) throw new Error()
    const target = await res.json()
    if (props.conversation?.conv_id !== convId) return
    return jumpToRefMsg({ server_id: target.msg_id.replace(/^srv_/, '') })
  } catch {
    if (props.conversation?.conv_id === convId) referenceError.value = '引用的消息未归档，无法定位'
  }
}

// 右键消息体 → 全选其内容（让浏览器原生右键菜单的"复制"直接生效）
function selectMsgContent(e) {
  const body = e.currentTarget
  if (!body) return
  // 优先精确选中消息内容容器（避开 sender / time）
  const content = body.querySelector(
  ':scope > .msg-bubble, :scope > .msg-share-card, :scope > .msg-goods-card, :scope > .msg-share-comment, :scope > .msg-media'
  )
  const target = content || body
  const range = document.createRange()
  range.selectNodeContents(target)
  const sel = window.getSelection()
  sel.removeAllRanges()
  sel.addRange(range)
  // 不阻止默认事件 —— 浏览器原生菜单照常出现，"Copy" 即可
}

// 右键系统消息 → 全选
function selectSystemContent(e) {
  const el = e.currentTarget
  if (!el) return
  const range = document.createRange()
  range.selectNodeContents(el)
  const sel = window.getSelection()
  sel.removeAllRanges()
  sel.addRange(range)
}

// 高亮的消息 ID
const highlightMsgId = ref(null)

// Lightbox state (the overlay + ESC handling live in MessageLightbox)
const loadError = ref('')
const referenceError = ref('')
let messageRequestId = 0
const lightboxSrc = ref(null)
function openLightbox(src) {
  if (src) lightboxSrc.value = src
}

async function jumpToRefMsg(ref) {
  if (!ref || !ref.server_id || !props.conversation) return
  referenceError.value = ''
  const convId = props.conversation.conv_id
  const canonicalId = `srv_${ref.server_id}`
  const embedded = props.embeddedMessages?.find(m => m.msg_id === canonicalId || m.msg_id.endsWith('/' + canonicalId))
  const msgId = embedded?.msg_id || canonicalId
  // 锁定滚动，防止无限加载干扰
  scrollLocked = true
  try {
    // 先检查当前已加载的消息中是否有目标
    const existing = listRef.value?.querySelector(`[data-msgid="${msgId}"]`)
    if (existing) {
      existing.scrollIntoView({ block: 'center' })
      highlightMsgId.value = msgId
      setTimeout(() => { highlightMsgId.value = null }, 2000)
      return
    }
    if (isStatic.value) {
      referenceError.value = '引用的消息不在当前记录中'
      return
    }
    // 需要加载目标消息附近的消息
    const res = await fetch(`/api/messages/${msgId}`)
    if (!res.ok) throw new Error()
    const msg = await res.json()
    if (props.conversation?.conv_id !== convId) return
    if (msg.conv_id !== convId || !msg.seq) throw new Error()
    const targetSeq = Math.max(0, msg.seq - 50)
    loading.value = true
    const url = `/api/conversations/${props.conversation.conv_id}/messages?page_size=100&after_seq=${targetSeq}`
    const res2 = await fetch(url)
    if (!res2.ok) throw new Error()
    const data = await res2.json()
    if (props.conversation?.conv_id !== convId) return
    resetParseCaches()
    loading.value = false
    messages.value = data.items
    total.value = data.total
    hasOlder.value = data.has_older ?? true
    hasNewer.value = data.has_newer ?? true
    playEnter()
    loadUserInfoForMessages(data.items)
    loadSysRefs(data.items)
    await nextTick()
    const el = listRef.value?.querySelector(`[data-msgid="${msgId}"]`)
    if (el) {
      el.scrollIntoView({ block: 'center' })
      highlightMsgId.value = msgId
      setTimeout(() => { highlightMsgId.value = null }, 2000)
    }
  } catch {
    referenceError.value = '引用的消息未归档或加载失败，无法定位'
  } finally {
    loading.value = false
    setTimeout(() => { scrollLocked = false }, 300)
  }
}

// 打开分享视频
function openShare(msg) {
  const info = getShareInfo(msg)
  if (info.productUrl) {
    window.open(info.productUrl, '_blank')
  } else if (info.itemId) {
    window.open(`https://www.douyin.com/video/${info.itemId}`, '_blank')
  }
}

async function fetchSenders(convId) {
  try {
    const res = await fetch(`/api/conversations/${convId}/senders`)
    senders.value = await res.json()
    // 预加载发送者的用户信息
    for (const s of senders.value) {
      fetchUserInfo(s.sender_uid)
    }
  } catch {}
}

async function fetchMessages(convId, beforeSeq = null, afterSeq = null, replace = false) {
  const requestId = ++messageRequestId
  // 任何新的加载都结束上一轮的贴底跟随，避免和翻页的滚动补偿打架
  stopFollowBottom()
  loading.value = true
  loadError.value = ''
  let url = `/api/conversations/${convId}/messages?page_size=100`
  if (beforeSeq !== null) url += `&before_seq=${beforeSeq}`
  if (afterSeq !== null) url += `&after_seq=${afterSeq}`
  let data
  try {
    const res = await fetch(url)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    data = await res.json()
    if (requestId !== messageRequestId || props.conversation?.conv_id !== convId) return
    if (!Array.isArray(data.items)) throw new Error('消息响应格式错误')
  } catch (e) {
    if (requestId !== messageRequestId || props.conversation?.conv_id !== convId) return
    loadError.value = `消息加载失败（${e.message}），请重试；持续失败请查看服务端日志`
    // Don't leave the spinner (and scroll lock) stuck forever on a failed load.
    loading.value = false
    scrollLocked = false
    console.error('加载消息失败', e)
    return
  }
  loading.value = false

  // 清理缓存
  resetParseCaches()

  scrollLocked = true
  if (beforeSeq === null && afterSeq === null) {
    // 初始加载（最新消息）
    messages.value = data.items
    hasOlder.value = !!data.has_older
    hasNewer.value = !!data.has_newer
    playEnter()
    await nextTick()
    startFollowBottom()
  } else if (replace) {
    // 从搜索、日期或引用结果跳入会话中间；页面两侧都可能还有消息。
    messages.value = data.items
    hasOlder.value = !!data.has_older
    hasNewer.value = !!data.has_newer
    playEnter()
    await nextTick()
    if (afterSeq === 0 && listRef.value) listRef.value.scrollTop = 0
  } else if (afterSeq !== null) {
    // 向下加载更新的消息。
    messages.value = [...messages.value, ...data.items]
    hasNewer.value = data.items.length > 0 && !!data.has_newer
  } else {
    // 加载更早的消息（向上加载更多）
    const list = listRef.value
    const prevHeight = list ? list.scrollHeight : 0
    messages.value = [...data.items, ...messages.value]
    hasOlder.value = data.items.length > 0 && !!data.has_older
    await nextTick()
    if (list) list.scrollTop = list.scrollHeight - prevHeight
  }
  setTimeout(() => { scrollLocked = false }, 200)

  total.value = data.total
  // 异步加载用户信息（头像、昵称）
  loadUserInfoForMessages(data.items)
  // 异步加载系统消息引用的视频
  loadSysRefs(data.items)
}

function loadOlder() {
  if (!props.conversation || !messages.value.length || !hasOlder.value || loading.value) return
  const minSeq = Math.min(...messages.value.map(m => m.seq))
  fetchMessages(props.conversation.conv_id, minSeq)
}

function loadNewer() {
  if (!props.conversation || !messages.value.length || !hasNewer.value || loading.value) return
  const maxSeq = Math.max(...messages.value.map(m => m.seq))
  fetchMessages(props.conversation.conv_id, null, maxSeq)
}

// 截图模式：一次性加载闭区间消息，等用户信息与引用卡片就绪后通知父组件
async function fetchStaticRange(convId) {
  loading.value = true
  try {
    const { startSeq, endSeq } = props.staticRange
    const res = await fetch(
      `/api/conversations/${convId}/messages/range?start_seq=${startSeq}&end_seq=${endSeq}`
    )
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = await res.json()
    resetParseCaches()
    messages.value = data.items
    total.value = data.total
    hasOlder.value = false
    hasNewer.value = false
    await loadUserInfoForMessages(data.items)
    await loadSysRefs(data.items)
  } catch (e) {
    console.error('加载消息失败', e)
  } finally {
    loading.value = false
  }
  await nextTick()
  emit('staticLoaded')
}

async function jumpToTop() {
  if (!props.conversation) return
  await fetchMessages(props.conversation.conv_id, null, 0, true)
}

async function jumpToBottom() {
  if (!props.conversation) return
  // 重新加载最新消息（无 beforeSeq / afterSeq）
  await fetchMessages(props.conversation.conv_id)
}

// 无限滚动：滚动到顶部或底部时自动加载更多
let scrollDebounce = null
function onListScroll() {
  if (scrollDebounce || scrollLocked) return
  const list = listRef.value
  if (!list || loading.value) return
  if (!props.conversation) return  // scroll event after the conversation was cleared
  const threshold = 100
  // 滚到顶部附近 → 加载更早消息
  if (list.scrollTop < threshold && hasOlder.value) {
    scrollDebounce = true
    loadOlder()
    setTimeout(() => { scrollDebounce = false }, 500)
  }
  // 滚到底部附近 → 加载更新消息。
  if (list.scrollHeight - list.scrollTop - list.clientHeight < threshold && hasNewer.value) {
    scrollDebounce = true
    loadNewer()
    setTimeout(() => { scrollDebounce = false }, 500)
  }
}

onMounted(() => {
  if (isStatic.value) return
  // 延迟绑定，等 listRef 准备好
  const tryBind = () => {
    if (listRef.value) {
      listRef.value.addEventListener('scroll', onListScroll, { passive: true })
    } else {
      setTimeout(tryBind, 200)
    }
  }
  tryBind()
})
onUnmounted(() => {
  if (listRef.value) {
    listRef.value.removeEventListener('scroll', onListScroll)
  }
  stopFollowBottom()
  clearTimeout(enterTimer)
  for (const audio of voiceAudioRefs.values()) audio.pause()
  voiceAudioRefs.clear()
  playingVoiceId.value = null
})

function formatTime(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const now = new Date()
  const isToday = d.toDateString() === now.toDateString()
  const time = d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  if (isToday) return time
  return d.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' }) + ' ' + time
}

// 渲染一段消息正文：搜索词高亮 + 本地有图的文字式表情换成图片。
function renderText(text) {
  return _renderRichText(text, props.searchHighlight)
}

function onImgError(e) {
  e.target.style.display = 'none'
}

// 视频解码失败（典型是 H.265）：带 ?tc=1 重试一次，让后端按需转成 H.264。
// retryVideoWithTranscode 自身保证每个元素只重试一次，避免死循环。
function onVideoError(e) {
  retryVideoWithTranscode(e?.target)
}

// Chromium 遇到解不了的编码**不一定**抛 error：它会照常触发 loadedmetadata /
// playing、currentTime 也在走，但一帧都不解、videoWidth 恒为 0（见 fix.md 第三轮）。
// 所以还要在 play/playing 时按 videoWidth 再判一次；只有用户真的点了播放才会命中，
// 因此打开会话不会批量触发转码。
function onVideoPlayAttempt(e) {
  retryVideoIfUndecodable(e?.target)
}

// 消息列表整体换掉时，丢弃按行缓存的内容/卡片解析结果。
function resetParseCaches() {
  clearCjCache()
  clearGoodsCardCache()
  clearFlameCardCache()
  clearCallShopCardCache()
}

watch(() => props.conversation, (conv) => {
  referenceError.value = ''
  loadError.value = ''
  messageRequestId++
  stopFollowBottom()
  if (props.embeddedMessages) return
  if (conv) {
    messages.value = []
    hasOlder.value = false
    hasNewer.value = false
    resetParseCaches()
    if (isStatic.value) {
      fetchStaticRange(conv.conv_id)
      return
    }
    fetchSenders(conv.conv_id)
    // 如果有 jumpToSeq，由 jumpToSeq watcher 处理加载
    if (!props.jumpToSeq) {
      fetchMessages(conv.conv_id)
    }
  }
}, { immediate: true })

watch(() => props.embeddedMessages, (items) => {
  if (!items) return
  messages.value = items
  total.value = items.length
  hasOlder.value = false
  hasNewer.value = false
  loadUserInfoForMessages(items)
}, { immediate: true })

watch(() => props.selfUidOverride, (uid) => { selfUid.value = uid || '' })

watch(() => props.jumpToSeq, async (seq) => {
  if (seq && props.conversation) {
    messages.value = []
    resetParseCaches()
    const targetSeq = Math.max(0, seq - 50)
    await fetchMessages(props.conversation.conv_id, null, targetSeq, true)
    await nextTick()
    // 精确滚动到目标消息并高亮
    const targetMsg = messages.value.find(m => m.seq === seq)
    if (targetMsg && listRef.value) {
      const el = listRef.value.querySelector(`[data-msgid="${targetMsg.msg_id}"]`)
      if (el) {
        el.scrollIntoView({ block: 'center' })
        highlightMsgId.value = targetMsg.msg_id
        setTimeout(() => { highlightMsgId.value = null }, 3000)
      }
    }
    emit('jumped')
  }
})
</script>

<style scoped>
.msg-profile-card { display: flex; align-items: center; gap: 12px; width: 280px; max-width: 100%; padding: 14px; background: var(--bg-secondary); color: var(--text-primary); border: 1px solid var(--border-color); border-radius: var(--radius); text-decoration: none; }
.msg-profile-card img { width: 56px; height: 56px; border-radius: 50%; object-fit: cover; }
.msg-profile-card strong { font-size: 15px; }
.msg-profile-card div div, .msg-profile-card small { font-size: 12px; color: var(--text-secondary); margin-top: 5px; }

.msg-panel {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  background: var(--bg-primary);
  position: relative;
}

.msg-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: var(--text-muted);
  gap: 12px;
}
.msg-empty-icon {
  font-size: 48px;
}

.msg-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  border-bottom: 1px solid var(--border-color);
  background: var(--bg-secondary);
  gap: 12px;
}
.msg-header h3 {
  font-size: 15px;
  font-weight: 600;
}
.msg-total {
  font-size: 12px;
  color: var(--text-muted);
}

.msg-pick-self {
  margin-left: auto;
  padding: 4px 12px;
  border: 1px solid var(--border-color);
  border-radius: 14px;
  background: var(--bg-tertiary);
  color: var(--text-primary);
  font-size: 12px;
  cursor: pointer;
}
.msg-pick-self:hover { border-color: var(--accent); }
.msg-pick-self.picked { border-color: var(--accent); color: var(--accent); }

/* UID 选择弹窗 */
.picker-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.5);
  z-index: 200;
  display: flex;
  align-items: center;
  justify-content: center;
  animation: overlay-in var(--dur-fast) var(--ease-out);
}
.picker-dialog {
  background: var(--bg-secondary);
  border-radius: 12px;
  padding: 24px;
  min-width: 280px;
  box-shadow: 0 8px 30px rgba(0,0,0,0.4);
  animation: dialog-in var(--dur-base) var(--ease-out);
}
@keyframes overlay-in {
  from { opacity: 0; }
  to { opacity: 1; }
}
@keyframes dialog-in {
  from { opacity: 0; transform: translateY(8px) scale(0.96); }
  to { opacity: 1; transform: none; }
}
.picker-title {
  font-size: 16px;
  font-weight: 600;
  margin-bottom: 4px;
}
.picker-hint {
  font-size: 12px;
  color: var(--text-muted);
  margin-bottom: 16px;
}
.picker-option {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 14px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  margin-bottom: 8px;
  cursor: pointer;
  transition: border-color 0.15s;
}
.picker-option:hover { border-color: var(--accent); }
.picker-option.active { border-color: var(--accent); background: var(--bg-tertiary); }
.picker-uid { font-family: monospace; font-size: 13px; }
.picker-count { font-size: 12px; color: var(--text-muted); }
.picker-clear {
  width: 100%;
  margin-top: 8px;
  padding: 6px;
  border: none;
  background: none;
  color: var(--text-muted);
  font-size: 12px;
  cursor: pointer;
}
.picker-clear:hover { color: var(--accent); }

.msg-list {
  flex: 1;
  overflow-y: auto;
  padding: var(--msg-list-pad-y) var(--msg-list-pad-x) 32px;
}

/* 截图模式：容器随内容自然撑开，不滚动，供整页截图 */
.msg-panel-static {
  flex: none;
  min-height: auto;
  height: auto;
}
.msg-list-static {
  flex: none;
  overflow: visible;
  height: auto;
}

.msg-loading, .msg-no-data {
  text-align: center;
  color: var(--text-muted);
  padding: 20px;
  font-size: 13px;
}

.msg-load-more {
  text-align: center;
  color: var(--accent);
  padding: 16px 10px;
  margin: 8px 0;
  cursor: pointer;
  font-size: 13px;
  flex-shrink: 0;
  transition: color var(--dur-fast), transform var(--dur-fast) var(--ease-out);
}
.msg-load-more:hover {
  color: var(--accent-hover);
  transform: translateY(-1px);
}

.msg-jump-fab {
  position: absolute;
  right: 32px;
  z-index: 100;
  padding: 8px 18px;
  background: var(--accent);
  color: #fff;
  border: none;
  border-radius: 20px;
  font-size: 13px;
  cursor: pointer;
  box-shadow: 0 2px 10px rgba(0,0,0,0.35);
  transition: opacity 0.15s, transform 0.15s var(--ease-out), filter 0.15s;
}
.msg-jump-fab:hover { filter: brightness(1.08); }
.msg-jump-fab:active { transform: scale(0.96); }
/* 两个浮动按钮可能同时出现（定位到中间某条消息），上下错开避免互相遮挡 */
.msg-jump-top { bottom: 78px; }
.msg-jump-bottom { bottom: 28px; }

/* 浮动按钮进出场：从下往上淡入 */
.fab-enter-active, .fab-leave-active {
  transition: opacity var(--dur-base) var(--ease-out), transform var(--dur-base) var(--ease-out);
}
.fab-enter-from, .fab-leave-to {
  opacity: 0;
  transform: translateY(10px);
}

/* 系统消息 */
.msg-item.msg-system {
  display: block;
  max-width: 100%;
}
/* 提示行：左槽位 / 居中的提示 / 右槽位（或下方按钮）。两侧等宽，所以提示永远是正中间。 */
.msg-system-line {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, auto) minmax(0, 1fr);
  align-items: center;
  column-gap: 10px;
  row-gap: 6px;
  width: 100%;
}
.msg-system-line > .msg-system-text,
.msg-system-line > .msg-watch-card,
.msg-system-line > .msg-flame-card,
.msg-system-line > .msg-call-shop-card,
.msg-system-line > .msg-notice-card {
  grid-column: 2;
  grid-row: 1;
}
/* 悬停时才显示按钮，但槽位一直占位，保证文字不被挤偏 */
.msg-system-side {
  display: flex;
  align-items: center;
  min-width: 0;
}
/* 按钮自身那 4px 上边距是为行内布局留的，放进槽位后要清掉，否则会偏离提示中心 2px */
.msg-system-side > .msg-json-toggle {
  margin-top: 0;
}
/* 我相关的提示：按钮贴在左侧 */
.msg-system-side-left {
  grid-column: 1;
  grid-row: 1;
  justify-content: flex-end;
}
/* 对方相关的提示：按钮贴在右侧 */
.msg-system-side-right {
  grid-column: 3;
  grid-row: 1;
  justify-content: flex-start;
}
/* 主语分不清：按钮居中放到提示下方 */
.msg-system-side-below {
  grid-column: 1 / -1;
  grid-row: 2;
  justify-content: center;
}
.msg-system-block {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  max-width: 100%;
}
.msg-system-text {
  font-size: 12px;
  color: var(--system-text-color);
  background: var(--system-bg);
  padding: 4px 14px;
  border-radius: 999px;
  text-align: center;
  max-width: 100%;
  overflow-wrap: anywhere;
}
/* 群公告卡片（type_code=1004）：抬头一行是 头像 + 昵称 +「发布了群公告」+ 时间 */
.msg-notice-card {
  width: 100%;
  max-width: 440px;
  padding: 12px 14px;
  background: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  box-shadow: var(--shadow-sm);
  text-align: left;
}
.msg-notice-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 8px;
}
.msg-notice-face {
  flex: none;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  overflow: hidden;
  background: var(--bg-tertiary);
  color: var(--text-secondary);
  font-size: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.msg-notice-face img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.msg-notice-who {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
}
.msg-notice-verb {
  font-size: 13px;
  color: var(--text-secondary);
  white-space: nowrap;
}
.msg-notice-time {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-muted);
  white-space: nowrap;
}
.msg-notice-body {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--border-color);
  font-size: 13px;
  line-height: 1.55;
  color: var(--text-primary);
  overflow-wrap: anywhere;
}
/* 一起看视频邀请卡片 */
.msg-watch-card {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 14px 8px 10px;
  background: color-mix(in srgb, var(--accent) 10%, var(--bg-secondary));
  border: 1px solid color-mix(in srgb, var(--accent) 30%, transparent);
  border-radius: var(--radius);
  max-width: 280px;
}
.msg-watch-icon {
  flex-shrink: 0;
  width: 30px;
  height: 30px;
  border-radius: 50%;
  background: var(--accent);
  color: #fff;
  font-size: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding-left: 2px;
}
.msg-watch-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}
.msg-watch-sub {
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 1px;
}
/* 火花见面礼卡片：宽高比接近正方形的长方形，背景是 assets/background.png */
.msg-flame-card {
  box-sizing: border-box;
  width: 278px;
  max-width: 100%;
  padding: 18px;
  border-radius: 14px;
  background-color: #fff;
  background-size: cover;
  background-position: center;
  background-repeat: no-repeat;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.12);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  overflow: hidden;
}
.msg-flame-avatars {
  display: flex;
  align-items: center;
  justify-content: center;
  flex: none;
  width: 100%;
  height: 72px;
}
.msg-flame-avatar {
  flex: none;
  width: 64px;
  height: 64px;
  border-radius: 50%;
  overflow: hidden;
  background: #f1f2f4;
  border: 2px solid #fff;
  box-shadow: 0 1px 5px rgba(0, 0, 0, 0.14);
}
.msg-flame-avatar img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
}
/* 左右并排：对面的人在左、本视角的人在右；右边那个后画，压住左边头像约 14px */
.msg-flame-avatar-peer {
  z-index: 1;
}
.msg-flame-avatar-self {
  z-index: 2;
  margin-left: -14px;
}
/* 文字那一行：火花图标 + 天数在左，文案在右，两段挨着放在中间 */
.msg-flame-row {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  width: 100%;
  font-size: 13px;
  font-weight: 600;
  color: #2f3033;
}
.msg-flame-days {
  display: inline-flex;
  align-items: center;
  gap: 1px;
  flex: none;
}
.msg-flame-icon {
  width: 15px;
  height: 17px;
  object-fit: contain;
}
.msg-flame-num {
  font-size: 15px;
  font-weight: 700;
  color: #ff7a1a; /* 火花天数用橙色数字 */
  font-variant-numeric: tabular-nums;
}
.msg-flame-title {
  flex: 0 1 auto;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
/* 「立即领取」只做展示，点不动；只保留鼠标悬停的颜色/阴影反馈 */
.msg-flame-claim {
  box-sizing: border-box;
  width: 100%;
  height: 40px;
  line-height: 40px;
  text-align: center;
  border-radius: 8px;
  background: #f2f3f5;
  color: #2f3033;
  font-size: 14px;
  font-weight: 600;
  cursor: default;
  user-select: none;
  transition: background var(--dur-fast) var(--ease-out), box-shadow var(--dur-fast) var(--ease-out);
}
.msg-flame-claim:hover {
  background: #e4e7ea;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
}
/* 联系门店引导卡片（aweType=110284）：卡片外观是 assets/call-to-shop.png
   （原图 824×212，左边电话图标 + 两行文案，右边一片空白），
   「联系」按钮就叠在那片空白里，靠右、垂直居中。 */
.msg-call-shop-card {
  position: relative;
  width: 320px;
  max-width: 100%;
  border-radius: 12px;
  overflow: hidden;
  background: #fff;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.12);
  font-size: 0; /* 去掉图片下方的行内基线空隙 */
}
.msg-call-shop-bg {
  display: block;
  width: 100%;
  height: auto;
}
/* 「联系」只做展示，点不动；只保留鼠标悬停的颜色/阴影反馈 */
.msg-call-shop-btn {
  position: absolute;
  right: 12px;
  top: 50%;
  transform: translateY(-50%);
  box-sizing: border-box;
  min-width: 62px;
  height: 30px;
  padding: 0 14px;
  line-height: 28px;
  text-align: center;
  border: 1px solid #e3e5e8;
  border-radius: 8px;
  background: #f2f3f5; /* 灰白底色 */
  color: #2f3033;
  font-size: 13px;
  font-weight: 600;
  white-space: nowrap;
  cursor: default;
  user-select: none;
  transition: background var(--dur-fast) var(--ease-out), box-shadow var(--dur-fast) var(--ease-out);
}
.msg-call-shop-btn:hover {
  background: #e4e7ea;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.12);
}
.msg-system-ref {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  background: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  cursor: pointer;
  max-width: 280px;
  transition: border-color 0.15s;
}
.msg-system-ref:hover {
  border-color: var(--accent);
}
.msg-system-ref-cover {
  width: 36px;
  height: 36px;
  border-radius: 4px;
  object-fit: cover;
  flex-shrink: 0;
}
.msg-system-ref-title {
  font-size: 12px;
  color: var(--text-secondary);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  line-height: 1.3;
}

.msg-item {
  display: flex;
  gap: 10px;
  margin-bottom: var(--msg-gap);
  max-width: var(--msg-max-width);
  transition: background 0.3s;
}
/* first message of a sender group gets breathing room above it */
.msg-item.group-start {
  margin-top: var(--msg-group-gap);
}
/* Target only this row: forwarded dialogs contain another MessageList. */
/* continuation messages hide the repeated avatar + name and sit tight */
.msg-item.msg-grouped > .msg-avatar {
  visibility: hidden;
}
.msg-item.msg-grouped > .msg-body > .msg-sender {
  display: none;
}
.msg-item.msg-highlight {
  background: color-mix(in srgb, var(--highlight) 22%, transparent);
  border-radius: var(--radius);
  animation: highlight-fade 2s ease-out;
}
@keyframes highlight-fade {
  0% { background: color-mix(in srgb, var(--highlight) 40%, transparent); }
  100% { background: transparent; }
}

/* 整屏换内容（切会话 / 搜索定位 / 引用跳转）时消息逐条浮现：
   末尾 8 条依次延迟，视觉重心落回最新消息。
   两个动画名交替使用 —— 同名动画不会自动重播，换名才能每次都重新开始。
   高亮行不参与进场，把动画让给 highlight-fade。
   只动 opacity、不做位移：位移（transform）会让浏览器把这一行排除在滚动锚点
   候选之外，而刚插入的图片稍后才撑开高度，锚定失效就会把视图留在旧消息上。 */
.msg-enter-a > .msg-item:not(.msg-highlight) {
  animation: msg-enter-a 0.32s var(--ease-out) both;
  animation-delay: var(--enter-delay, 0ms);
}
.msg-enter-b > .msg-item:not(.msg-highlight) {
  animation: msg-enter-b 0.32s var(--ease-out) both;
  animation-delay: var(--enter-delay, 0ms);
}
@keyframes msg-enter-a {
  from { opacity: 0; }
  to { opacity: 1; }
}
@keyframes msg-enter-b {
  from { opacity: 0; }
  to { opacity: 1; }
}

.msg-item.msg-self {
  flex-direction: row-reverse;
  margin-left: auto;
}

.msg-avatar {
  width: var(--msg-avatar-size);
  height: var(--msg-avatar-size);
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: calc(var(--msg-avatar-size) * 0.39);
  font-weight: 600;
  flex-shrink: 0;
  margin-top: 2px;
  overflow: hidden;
}
.msg-avatar img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  border-radius: 50%;
}
.msg-avatar span {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
}

.msg-body {
  min-width: 0;
}

.msg-sender {
  font-size: var(--msg-sender-size);
  color: var(--text-muted);
  margin-bottom: 3px;
}
.msg-item.msg-self > .msg-body > .msg-sender {
  text-align: right;
}

.msg-bubble {
  background: var(--bg-message-other);
  padding: var(--bubble-pad-y) var(--bubble-pad-x);
  border-radius: var(--bubble-radius);
  border-top-left-radius: 4px;
  font-size: var(--msg-font-size);
  line-height: var(--msg-line-height);
  /* 外观设置里的「文字字间距」；负值时用 fit-content 让气泡跟着文字收紧 */
  letter-spacing: var(--msg-letter-spacing);
  width: fit-content;
  max-width: 100%;
  word-break: break-word;
  white-space: pre-wrap;
  box-shadow: var(--shadow-sm);
  border: 1px solid color-mix(in srgb, var(--text-primary) 5%, transparent);
}
.msg-item.msg-self > .msg-body > .msg-bubble {
  background: var(--bg-message-self);
  color: var(--text-on-self);
  border: none;
  border-top-left-radius: var(--bubble-radius);
  border-top-right-radius: 4px;
}

/* 互相关注提示（「我们已互相关注，可以开始聊天了」）：抖音客户端里这是对方发来的消息，
   外观跟对方的普通文字消息一致，类名只用来标记它。 */
.msg-notice-bubble {
  white-space: pre-wrap;
}

/* 商品分享卡片：竖长方形（高 > 宽），上图 / 中描述（最多 3 行）/ 下价格 */
.msg-goods-card {
  width: var(--goods-card-width);
  max-width: 100%;
  display: flex;
  flex-direction: column;
  background: var(--card-bg);
  border-radius: 10px;
  border-top-left-radius: 2px;
  border: 1px solid var(--card-border);
  box-shadow: var(--shadow-sm);
  overflow: hidden;
  cursor: pointer;
  transition: filter 0.15s;
}
.msg-item.msg-self > .msg-body > .msg-goods-card {
  /* 己方气泡是彩色渐变，商品卡保持中性底色，否则红色价格看不清 */
  background: var(--bg-message-other);
  border-top-left-radius: 10px;
  border-top-right-radius: 2px;
}
.msg-goods-card:hover {
  filter: brightness(1.08);
}
.msg-goods-cover-wrap {
  width: 100%;
  aspect-ratio: 1 / 1;
  background: var(--bg-tertiary);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.msg-goods-cover {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.msg-goods-cover-missing {
  font-size: 12px;
  color: var(--text-muted);
}
.msg-goods-body {
  /* 描述行数不足 3 行时也撑够高度，保证卡片始终是高 > 宽的长方形 */
  min-height: 86px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 10px 10px;
}
.msg-goods-title {
  font-size: 13px;
  line-height: 1.45;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-word;
}
.msg-goods-price {
  margin-top: auto;
  display: flex;
  align-items: baseline;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  /* 原价与券后价统一红色（各主题用 --price-color 保持对比度） */
  color: var(--price-color);
}
.msg-goods-price-num {
  white-space: nowrap;
}

/* 分享卡片 */
.msg-share-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: var(--card-bg);
  border-radius: 10px;
  border-top-left-radius: 2px;
  padding: 10px 12px;
  cursor: pointer;
  transition: filter 0.15s;
  max-width: var(--share-card-width);
  border-left: 3px solid var(--accent);
}
.msg-share-comment {
  font-size: 14px;
  line-height: 1.5;
  word-break: break-word;
}
.msg-share-comment-user {
  font-weight: 600;
  color: var(--accent);
}
.msg-share-comment-img {
  display: block;
  max-width: 180px;
  max-height: 180px;
  border-radius: 6px;
  margin-top: 6px;
  object-fit: contain;
}
.msg-share-card-inner {
  display: flex;
  gap: 10px;
}
.msg-share-card:hover {
  filter: brightness(1.1);
}
.msg-item.msg-self > .msg-body > .msg-share-card {
  background: var(--bg-message-self);
  border-top-left-radius: 10px;
  border-top-right-radius: 2px;
}
.msg-share-card-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.msg-share-card-title {
  font-size: 13px;
  line-height: 1.4;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-word;
}
.msg-share-card-author {
  font-size: 11px;
  color: var(--text-muted);
  margin-top: auto;
}
.msg-share-card-cover {
  width: 60px;
  height: 60px;
  border-radius: 6px;
  object-fit: cover;
  flex-shrink: 0;
  align-self: center;
}
.msg-share-card-ref {
  background: var(--bg-tertiary);
  border-radius: 6px;
  padding: 6px 10px;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-secondary);
}
.msg-share-card-ref-icon {
  opacity: 0.6;
}

/* 引用/回复消息 */
.msg-ref-quote {
  padding: 6px 10px;
  margin-bottom: 4px;
  background: var(--bg-tertiary);
  border-left: 2px solid var(--text-muted);
  border-radius: 4px;
  font-size: 12px;
  color: var(--text-muted);
  max-width: 300px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  cursor: pointer;
  transition: background 0.2s;
}
.msg-ref-quote:hover {
  background: var(--border-color);
}
.msg-ref-name {
  font-weight: 600;
  color: var(--text-secondary);
}
.msg-ref-content {
  opacity: 0.85;
}

/* 语音消息：沿用普通消息气泡，并在播放器与转写之间分隔 */
.msg-voice-bubble {
  min-width: var(--voice-min-width);
  max-width: 360px;
  padding: 8px 10px 9px;
}
.msg-voice-player {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 32px;
}
.msg-voice-play {
  width: 32px;
  height: 32px;
  padding: 0;
  border: 0;
  border-radius: 50%;
  display: grid;
  place-items: center;
  background: var(--text-primary);
  color: var(--bg-primary);
  box-shadow: none;
  cursor: pointer;
  flex: 0 0 auto;
  transition: transform 0.15s, opacity 0.15s;
}
.msg-voice-play svg { width: 16px; height: 16px; fill: currentColor; }
.msg-voice-play:hover:not(:disabled),
.msg-voice-play.playing {
  transform: scale(1.03);
  opacity: 0.78;
}
.msg-item.msg-self > .msg-body > .msg-voice-bubble .msg-voice-play {
  background: color-mix(in srgb, var(--text-on-self) 94%, transparent);
  color: var(--accent);
}
.msg-item.msg-self > .msg-body > .msg-voice-bubble .msg-voice-play:hover:not(:disabled),
.msg-item.msg-self > .msg-body > .msg-voice-bubble .msg-voice-play.playing {
  opacity: 0.8;
}
.msg-voice-play:disabled {
  opacity: 0.45;
  cursor: default;
  box-shadow: none;
}
.msg-voice-play:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.msg-voice-wave {
  display: flex;
  align-items: center;
  gap: 3px;
  height: 22px;
  flex: 0 0 auto;
}
.msg-voice-wave i {
  display: block;
  width: 2.5px;
  height: 10px;
  border-radius: 999px;
  background: var(--text-primary);
  opacity: 0.88;
  transform-origin: center;
  transition: opacity 0.2s, background 0.2s;
}
.msg-voice-wave i:nth-child(1) { height: 8px; }
.msg-voice-wave i:nth-child(2) { height: 14px; }
.msg-voice-wave i:nth-child(3) { height: 11px; }
.msg-voice-wave i:nth-child(4) { height: 16px; }
.msg-voice-wave i:nth-child(5) { height: 10px; }
.msg-voice-wave i:nth-child(6) { height: 18px; }
.msg-voice-wave i:nth-child(7) { height: 12px; }
.msg-voice-wave i:nth-child(8) { height: 21px; }
.msg-voice-wave i:nth-child(9) { height: 14px; }
.msg-voice-wave i:nth-child(10) { height: 9px; }
.msg-voice-wave i:nth-child(11) { height: 17px; }
.msg-voice-wave i:nth-child(12) { height: 12px; }
.msg-voice-wave i:nth-child(13) { height: 19px; }
.msg-item.msg-self > .msg-body > .msg-voice-bubble .msg-voice-wave i {
  background: color-mix(in srgb, var(--text-on-self) 90%, transparent);
  opacity: 0.9;
}
@keyframes voice-wave-pulse {
  0%, 100% { transform: scaleY(0.58); opacity: 0.58; }
  50% { transform: scaleY(1); opacity: 1; }
}
.msg-voice-player.playing .msg-voice-wave i {
  animation: voice-wave-pulse 0.9s ease-in-out infinite alternate;
}
.msg-voice-player.playing .msg-voice-wave i:nth-child(2n) { animation-delay: -0.25s; }
.msg-voice-player.playing .msg-voice-wave i:nth-child(3n) { animation-delay: -0.5s; }
@media (prefers-reduced-motion: reduce) {
  .msg-voice-player.playing .msg-voice-wave i { animation: none; }
}
.msg-voice-bubble audio {
  display: none;
}
.msg-voice-dur {
  margin-left: 4px;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-muted);
  white-space: nowrap;
  text-align: right;
}
.msg-item.msg-self > .msg-body > .msg-voice-bubble .msg-voice-dur {
  color: color-mix(in srgb, var(--text-on-self) 76%, transparent);
}
.msg-voice-divider {
  border-top: 1px solid color-mix(in srgb, var(--text-primary) 16%, transparent);
  margin: 8px 0 7px;
}
.msg-item.msg-self > .msg-body > .msg-voice-bubble .msg-voice-divider {
  border-top-color: color-mix(in srgb, var(--text-on-self) 25%, transparent);
}
.msg-voice-transcript {
  color: inherit;
  font-size: var(--msg-font-size);
  line-height: var(--msg-line-height);
  white-space: pre-wrap;
  word-break: break-word;
}

/* 表情包/贴纸、图片、视频分开控制最大边长（外观设置里的三项） */
.msg-media-emoji img {
  max-width: var(--emoji-max-size);
  max-height: var(--emoji-max-size);
  border-radius: 8px;
  cursor: pointer;
}
.msg-media-image img {
  max-width: var(--media-max-size);
  max-height: var(--media-max-size);
  border-radius: 8px;
  cursor: pointer;
}
.msg-media video,
.msg-video-poster img {
  max-width: var(--video-max-size);
  max-height: calc(var(--video-max-size) * 1.3);
  border-radius: 8px;
  cursor: pointer;
}
.msg-media video {
  background: #000;
}
.msg-inline-pic {
  opacity: 0.85;
  filter: blur(0.5px);
}
.msg-media-missing {
  font-size: 12px;
  color: var(--text-muted);
  padding: 8px 12px;
  background: var(--bg-tertiary);
  border-radius: 8px;
}
/* 视频封面 + 时长 overlay */
.msg-video-poster {
  position: relative;
  display: inline-block;
  cursor: pointer;
}
.msg-video-player {
  max-width: var(--video-max-size);
  max-height: calc(var(--video-max-size) * 1.3);
  border-radius: 8px;
  background: #000;
}
.msg-video-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  pointer-events: none;
  border-radius: 8px;
  background: linear-gradient(rgba(0,0,0,0) 60%, rgba(0,0,0,0.5));
}
.msg-video-play {
  font-size: 36px;
  color: rgba(255,255,255,0.92);
  text-shadow: 0 2px 8px rgba(0,0,0,0.6);
  line-height: 1;
}
.msg-video-dur {
  position: absolute;
  right: 8px;
  bottom: 6px;
  background: rgba(0,0,0,0.55);
  color: #fff;
  font-size: 12px;
  padding: 2px 6px;
  border-radius: 3px;
}

.msg-time {
  font-size: var(--msg-time-size);
  color: var(--text-muted);
  margin-top: 3px;
}
.msg-item.msg-self > .msg-body > .msg-time {
  text-align: right;
}
/* 撤回 / 编辑 / 表情快捷回复：三种底色各不相同；不确定时并排显示、用 / 分隔。 */
.msg-modify-tag {
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 3px;
  border: 1px solid transparent;
  margin-right: 4px;
  cursor: help;
}
.msg-modify-recall {
  color: #e5534b;
  background: rgba(229, 83, 75, 0.16);
  border-color: rgba(229, 83, 75, 0.38);
}
.msg-modify-edit {
  color: #4a86dd;
  background: rgba(74, 134, 221, 0.16);
  border-color: rgba(74, 134, 221, 0.38);
}
.msg-modify-reaction {
  color: #c98a1e;
  background: rgba(201, 138, 30, 0.18);
  border-color: rgba(201, 138, 30, 0.40);
}
.msg-modify-view_once {
  color: #8b5cf6;
  background: rgba(139, 92, 246, 0.16);
  border-color: rgba(139, 92, 246, 0.40);
}
/* 表情快捷回复的明细：[表情] | 谁回应的。用跟前面标签同高、同圆角的灰色底，
   底色、边框统一走灰色，和三种彩色标签区分开。 */
.msg-reaction-detail {
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 3px;
  border: 1px solid rgba(138, 143, 156, 0.38);
  background: rgba(138, 143, 156, 0.16);
  color: var(--text-secondary);
  margin-right: 4px;
  white-space: nowrap;
}
.msg-modify-sep {
  font-size: 10px;
  color: var(--text-muted);
  margin-right: 4px;
}
.msg-view-once-tag {
  font-size: 10px;
  color: #8b5cf6;
  background: rgba(139, 92, 246, 0.1);
  padding: 1px 5px;
  border-radius: 3px;
  margin-right: 4px;
}
/* 仅看一次消息仍是普通文本气泡，用左侧细边标识，不做成分享卡片。 */
.msg-view-once-bubble {
  border-left: 2px solid rgba(139, 92, 246, 0.5);
}
/* 调试：查看 raw_data */
.msg-json-toggle {
  display: inline-block;
  margin-top: 4px;
  padding: 1px 7px;
  font-size: 10px;
  line-height: 16px;
  color: var(--text-muted);
  background: transparent;
  border: 1px solid color-mix(in srgb, var(--text-primary) 18%, transparent);
  border-radius: 999px;
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s, color 0.15s, border-color 0.15s;
}
.msg-item:hover > .msg-json-toggle,
.msg-item:hover > .msg-body > .msg-json-toggle,
.msg-item:hover > .msg-system-block .msg-json-toggle,
.msg-json-toggle:focus-visible,
.msg-json-toggle.open {
  opacity: 1;
}
.msg-json-toggle:hover {
  color: var(--accent);
  border-color: var(--accent);
}
.msg-item.msg-self > .msg-body > .msg-json-toggle {
  display: block;
  margin-left: auto;
}
.msg-json-body {
  max-width: 100%;
  max-height: 320px;
  overflow: auto;
  margin: 4px 0 0;
  padding: 8px 10px;
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--text-primary);
  background: var(--bg-tertiary);
  border: 1px solid color-mix(in srgb, var(--text-primary) 10%, transparent);
  border-radius: 6px;
  user-select: text;
  animation: json-in var(--dur-base) var(--ease-out);
}
@keyframes json-in {
  from { opacity: 0; transform: translateY(-4px); }
  to { opacity: 1; transform: none; }
}
.msg-item.msg-self > .msg-body > .msg-json-body {
  margin-left: auto;
}
/* 系统消息：展开的 JSON 居中显示在提示下方 */
.msg-system-block > .msg-json-body {
  margin-top: 0;
}
</style>
