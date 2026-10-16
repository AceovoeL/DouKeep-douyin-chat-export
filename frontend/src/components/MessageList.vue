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
        <!-- 只要这个会话有两个以上的人就可以设置「我」：群聊同样需要
             （以前只在正好两个人的会话里出现，群聊里根本点不到，于是群里
             所有消息都挤在左边）。这个选择存在浏览器里，所有会话共用；
             没手工选过时，用后端从聊天记录里认出来的本机账号当默认值。 -->
        <button v-if="effectiveSelfUid" class="msg-pick-self picked" @click="showPicker = true"
                :title="selfUid ? '点一下可以改' : '按聊天记录认出来的，点一下可以改'">
          我: {{ selfLabel }}<span v-if="!selfUid" class="msg-pick-self-auto">（自动）</span>
        </button>
        <button v-else-if="pickerOptions.length > 1" class="msg-pick-self" @click="showPicker = true">
          设置"我"
        </button>
      </div>

      <div v-if="referenceError" role="status" class="msg-loading" @click="referenceError = ''">{{ referenceError }}</div>

      <!-- UID 选择弹窗 -->
      <div v-if="showPicker" class="picker-overlay" @click.self="showPicker = false">
        <div class="picker-dialog">
          <div class="picker-title">选择哪个是你自己</div>
          <div class="picker-hint">选择后消息会区分左右显示</div>
          <div
            v-for="s in pickerOptions"
            :key="s.sender_uid"
            class="picker-option"
            :class="{ active: effectiveSelfUid === s.sender_uid }"
            @click="pickSelf(s.sender_uid)"
          >
            <span class="picker-uid">{{ pickerLabel(s) }}</span>
            <span class="picker-count">{{ s.discovered ? '本机账号（本会话没有它的消息）' : `${s.msg_count} 条消息` }}</span>
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
        <!-- 时间分隔条：和上一条隔了 5 分钟以上时，在两条消息中间居中补一个
             完整时间（年月日时分），布局跟系统通知一样。
             消息本身不画（镜像通知、空载荷）时也不插 —— 没有内容却挂一颗时间
             会让人以为漏了消息。 -->
        <div
          v-if="needsTimeDivider(index) && _shouldShow(msg) && !duplicateSystemIds.has(msg.msg_id)"
          class="msg-time-divider"
          role="separator"
          aria-label="发送时间"
        >
          <span>{{ formatFullTime(msg.timestamp) }}</span>
        </div>
        <div
          v-if="_shouldShow(msg) && !duplicateSystemIds.has(msg.msg_id)"
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
                <!-- 「陪伴天数」卡 (aweType=110270 · card_key=msg_daily ·
                     card_type=im_msg_daily_fire)：居中、整张卡由界面自己画
                     （载荷只给文字、天数和头像的摆放，没有背景图）。
                     上面两个叠着的头像固定是「对方在左、本机账号在右」，**不随视角变**；
                     中间一行「一起度过了人生的 🔥 365 天」；下面一颗按钮，按钮左侧是
                     **当前视角的人**的头像 + 一圈蓝绿渐变光环。
                     火花图标和光环都用项目自带素材（assets/fire.png、
                     assets/head_wrapper_circle.png）—— 载荷给的链接会过期。
                     见 lib/dailyFireCard.js。 -->
                <div v-else-if="dailyFire(msg)" class="msg-daily-card">
                  <!-- 上方两个头像：右边那个后画，压住左边一点（和见面礼卡一个摆法） -->
                  <div class="msg-daily-avatars">
                    <div class="msg-daily-avatar msg-daily-avatar-left">
                      <img
                        v-if="dailyAvatar(msg, 'left')"
                        :src="dailyAvatar(msg, 'left')"
                        :alt="dailyAvatarAlt(msg, 'left')"
                        :loading="imgLoading"
                        @error="onImgError"
                      />
                    </div>
                    <div class="msg-daily-avatar msg-daily-avatar-right">
                      <img
                        v-if="dailyAvatar(msg, 'right')"
                        :src="dailyAvatar(msg, 'right')"
                        :alt="dailyAvatarAlt(msg, 'right')"
                        :loading="imgLoading"
                        @error="onImgError"
                      />
                    </div>
                  </div>
                  <!-- 中间那行字：前文 + 火花图标 + 橙色天数 + 后文 -->
                  <div class="msg-daily-row">
                    <span v-if="dailyFire(msg).before" class="msg-daily-text">{{ dailyFire(msg).before }}</span>
                    <img class="msg-daily-flame" :src="dailyFlame" alt="" />
                    <span v-if="dailyFire(msg).days" class="msg-daily-days">{{ dailyFire(msg).days }}</span>
                    <span v-if="dailyFire(msg).after" class="msg-daily-text">{{ dailyFire(msg).after }}</span>
                  </div>
                  <!-- 底下那颗按钮：左边是当前视角的人的头像 + 渐变光环，右边是文字；
                       只做展示、点不动，只保留鼠标悬停反馈 -->
                  <div class="msg-daily-btn" aria-disabled="true">
                    <span class="msg-daily-ring" :style="dailyRingStyle">
                      <img
                        v-if="dailyRingAvatar(msg)"
                        :src="dailyRingAvatar(msg)"
                        :alt="dailyRingAvatarAlt(msg)"
                        :loading="imgLoading"
                        @error="onImgError"
                      />
                    </span>
                    <span class="msg-daily-btn-label">{{ dailyFire(msg).button }}</span>
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
                  v-html="renderText(renderSystemMsg(msg, effectiveSelfUid))"
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
              <div class="msg-sender">
                <span>{{ displayName(msg) }}</span>
                <!-- 小火人（精灵 AI）后面挂一枚灰底小签，写它的性格设定（如「腹黑傲娇」） -->
                <span v-if="spiritStyle(msg)" class="msg-spirit-style">{{ spiritStyle(msg) }}</span>
              </div>
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
              <ForwardRecords v-else-if="getForwardInfo(msg)" :message="msg" :selfUid="effectiveSelfUid" />
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
                <!-- 实况图：点左上角「实况」小标播放，电脑上悬停也会动，见 LivePhoto.vue -->
                <LivePhoto
                  v-else-if="getLivePhotoVideo(msg) && getImageSrc(msg)"
                  :cover="getImageSrc(msg)"
                  :video="getLivePhotoVideo(msg)"
                  :loading="imgLoading"
                  @click="openLivePhoto(msg)"
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
              <!-- 「聊天火花」关系卡（card_key=msg_fire_invitation 的邀请 / 接受纪念两种）：
                   不居中，跟着发送者分左右（对方发的在左、我发的在右），和别的卡片一致。
                   卡片外观是 assets/request.png、assets/accept.png（抖音客户端里那张卡的
                   底纹 + 底部「聊天火花」条），头像、那颗火花和「接受邀请」按钮叠在上面 ——
                   按钮只做展示、点不动；位置按抖音客户端量出来的摆（见 lib/fireInviteCard.js）。
                   姐妹 / 兄弟 / 伴侣 / 死党火花共用这两个 card_type，**只有那颗火花图标不同**，
                   所以图标一律用载荷里那张（采集时就存下来了）。
                   ⚠️ 必须排在商品卡 / 分享卡前面：那两支的兜底判据很宽（只要有 top +
                   content_top + content_content 就算商品卡），会先把这张卡截走、画成
                   一个「[商品]」空壳。 -->
              <div
                v-else-if="fireInvite(msg)"
                class="msg-fire-card"
                :style="fireCardStyle(msg)"
              >
                <!-- 接受纪念卡：两个头像夹一颗火花（左=发卡的人、右=对方） -->
                <template v-if="fireInvite(msg).kind === 'accept'">
                  <div class="msg-fire-avatar msg-fire-avatar-left">
                    <img
                      v-if="fireAvatar(msg, 'left')"
                      :src="fireAvatar(msg, 'left')"
                      :alt="fireAvatarAlt(msg, 'left')"
                      :loading="imgLoading"
                      @error="onImgError"
                    />
                  </div>
                  <img
                    v-if="fireSpark(msg)"
                    class="msg-fire-spark"
                    :src="fireSpark(msg)"
                    alt=""
                    @error="onFireSparkError"
                  />
                  <div class="msg-fire-avatar msg-fire-avatar-right">
                    <img
                      v-if="fireAvatar(msg, 'right')"
                      :src="fireAvatar(msg, 'right')"
                      :alt="fireAvatarAlt(msg, 'right')"
                      :loading="imgLoading"
                      @error="onImgError"
                    />
                  </div>
                </template>
                <!-- 邀请卡：上面只有一颗火花 -->
                <img
                  v-else-if="fireSpark(msg)"
                  class="msg-fire-spark msg-fire-spark-invite"
                  :src="fireSpark(msg)"
                  alt=""
                  @error="onFireSparkError"
                />
                <!-- 主标题 + 副标题：文字压在背景图左侧，右边留给那颗按钮 -->
                <div class="msg-fire-title">{{ fireInvite(msg).title }}</div>
                <div class="msg-fire-sub">
                  <span>{{ fireInvite(msg).subtitle }}</span>
                  <!-- 这个「›」是用 CSS 的两条边画出来的（见 .msg-fire-arrow），
                       所以里面不要再放一个「›」文字 —— 那会被一起旋转 45°，
                       看起来就是一支歪着的小箭头。 -->
                  <span
                    v-if="fireInvite(msg).arrow"
                    class="msg-fire-arrow"
                    aria-hidden="true"
                  ></span>
                </div>
                <!-- 「接受邀请」：背景图是干净的，按钮由这里画；接受纪念卡的背景图上
                     已经画好了那颗「纪念一下」，不再叠一个。 -->
                <div
                  v-if="!fireInvite(msg).buttonBakedIn"
                  class="msg-fire-btn"
                  :class="{ 'msg-fire-btn-primary': fireInvite(msg).buttonPrimary }"
                  aria-disabled="true"
                >{{ fireInvite(msg).button }}</div>
              </div>
              <!-- 「合养小火人」卡片（card_key=msg_fire_invitation 的 joint_* 三种）：
                   和「聊天火花」一样不居中，跟着发送者分左右（对方发的在左、我发的在右）。
                   卡片外观是 assets/request-firebaby.png 与 assets/accept-firebaby.png
                   （顶上那颗小火人和底部「合养精灵」那一条都印在图里），文字和那颗按钮
                   叠在上面 —— 按钮只做展示、点不动，位置按抖音客户端量出来的摆
                   （见 lib/fireBabyCard.js）。
                   标题/副标题**不省略**：抖音客户端会把「对方邀请你一起养小火人」截成
                   「对方邀请你一起养小…」，这里让它在左侧那点宽度里换行显示完整。
                   收到的那张图（accept）自带「我们开始养小火人啦／连续聊天让精灵快快长大」，
                   文案正好对得上就只补一颗按钮；对不上（比如 AI 精灵那款）就换回空白底纹
                   自己画，免得两层字叠在一起（见 fireBabyBackground）。
                   ⚠️ 必须排在商品卡 / 分享卡前面：那两支的兜底判据很宽（只要有 top +
                   content_top + content_content 就算商品卡），会先把这张卡截走、画成
                   一个「[商品]」空壳。 -->
              <div
                v-else-if="fireBaby(msg)"
                class="msg-fire-baby-card"
                :style="fireBabyStyle(msg)"
              >
                <!-- 文字：锚在内容区底部、往上长，标题换行也不会挤掉副标题 -->
                <div v-if="!fireBaby(msg).textBakedIn" class="msg-fire-baby-body">
                  <div class="msg-fire-baby-title">{{ fireBaby(msg).title }}</div>
                  <div class="msg-fire-baby-sub">
                    <span>{{ fireBaby(msg).subtitle }}</span>
                    <!-- 这个「›」是 CSS 两条边画的，不要再放「›」文字（会被一起转 45°） -->
                    <span
                      v-if="fireBaby(msg).arrow"
                      class="msg-fire-baby-arrow"
                      aria-hidden="true"
                    ></span>
                  </div>
                </div>
                <!-- 那颗按钮：三款卡都有，只做展示、点不动 -->
                <div
                  class="msg-fire-baby-btn"
                  :class="{ 'msg-fire-baby-btn-primary': fireBaby(msg).buttonPrimary }"
                  aria-disabled="true"
                >{{ fireBaby(msg).button }}</div>
              </div>
              <!-- 「喂精灵」卡（card_key=msg_fire_invitation 的 feeding 一种）：
                    和别的卡片一样不居中，跟着发送者分左右。这族卡片有**两版版式**，
                    靠载荷里有没有背景图区分（见 lib/feedCard.js）：
                    ① 老版（7 月那批，载荷没背景图）：整张卡用项目自带的
                       assets/invite-feed.png（浅色渐变底 + 底部「合养精灵」条，尺寸和
                       抖音那张一模一样），上面叠精灵图（载荷里的 top.content，就是那根
                       辣条）和载荷里的标题、副标题。
                    ② 新版（8 月起）：封面由背景图 + 食物图叠出来，下半部分是
                       assets/feed-card.png，两行文字固定。
                    两版都只有一颗「去喂食」按钮，点不动 —— 喂食卡不等人接受，接受后
                    会另外发一条消息，所以按钮只有这一种文案。
                    ⚠️ 必须排在商品卡 / 分享卡前面：那两支的兜底判据很宽（只要有 top +
                    content_top + content_content 就算商品卡），会先把这张卡截走、画成
                    一个「[商品]」空壳。 -->
              <div
                v-else-if="feedCard(msg)"
                class="msg-feed-card"
                :class="{ 'msg-feed-card--old': !feedCard(msg).background }"
                :style="feedCardStyle(msg)"
              >
                <!-- 老版卡片上的精灵图：载荷里那张（原图 252×252，四周各留了 5px 透明边），
                     位置和大小是按抖音参考图量的。同样走 iconSrc() 取本地缓存。 -->
                <img
                  v-if="!feedCard(msg).background && feedCard(msg).spirit"
                  class="msg-feed-spirit"
                  :src="feedSpiritSrc(msg)"
                  alt=""
                  :loading="imgLoading"
                  @error="onFeedFoodError"
                />
                <!-- 新版封面：背景图 + 居中叠上去的食物图。两张都是抖音的临时链接，
                     走 iconSrc() 取本地缓存（采集时已存下来，沿用官方原名）。 -->
                <div v-if="feedCard(msg).background" class="msg-feed-cover">
                  <img
                    v-if="feedCard(msg).food"
                    class="msg-feed-food"
                    :src="feedFoodSrc(msg)"
                    alt=""
                    :loading="imgLoading"
                    @error="onFeedFoodError"
                  />
                </div>
                <!-- 主标题 + 副标题：固定两句话，压在素材图左侧，右边留给那颗按钮 -->
                <div class="msg-feed-title">{{ feedCard(msg).title }}</div>
                <div class="msg-feed-sub">{{ feedCard(msg).subtitle }}</div>
                <!-- 「去喂食」：只做展示、点不动 -->
                <div class="msg-feed-btn" aria-disabled="true">{{ feedCard(msg).button }}</div>
              </div>
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
              <!-- 群邀请卡（type_code=58）：跟普通消息一样按发送者分左右（对方拉的你在左、
                   你被拉进群的那条也在发送者那一侧），卡片里上头大字是群名，
                   下头小字是「谁 添加你进群」；这个群已归档时底下多一个「发消息」。 -->
              <div v-else-if="inviteCard(msg)" class="msg-invite-card" @contextmenu="selectMsgContent">
                <div class="msg-invite-card-inner">
                  <img
                    v-if="cardIcon(msg, inviteCard(msg).icon)"
                    class="msg-invite-card-icon"
                    :src="cardIcon(msg, inviteCard(msg).icon)"
                    :alt="inviteCard(msg).groupName"
                    :loading="imgLoading"
                    @error="onCardIconError(msg)"
                  />
                  <div class="msg-invite-card-body">
                    <div class="msg-invite-card-name">{{ inviteCard(msg).groupName }}</div>
                    <div class="msg-invite-card-desc">{{ inviteCard(msg).description }}</div>
                  </div>
                </div>
                <!-- 群不在归档里就不画按钮：点了也跳不过去 -->
                <div v-if="groupCache[inviteCard(msg).convId]" class="msg-invite-card-foot">
                  <button type="button" class="msg-invite-card-btn" @click="openInviteGroup(msg)">发消息</button>
                </div>
              </div>
              <!-- 豆包分享卡（aweType=6001）：正方形封面 + 标题，封面那一侧标着来源
                   「豆包」。对方发来的封面在左，我发出去的封面在右（见下面的 CSS）。 -->
              <div v-else-if="musicCard(msg)" class="msg-music-card" @click="openMusicCard(msg)">
                <img
                  v-if="cardIcon(msg, musicCard(msg).cover)"
                  class="msg-music-cover"
                  :src="cardIcon(msg, musicCard(msg).cover)"
                  :alt="musicCard(msg).title"
                  :loading="imgLoading"
                  @error="onCardIconError(msg)"
                />
                <div class="msg-music-body">
                  <div v-if="musicCard(msg).title" class="msg-music-title">{{ musicCard(msg).title }}</div>
                  <div v-if="musicCard(msg).source" class="msg-music-source">{{ musicCard(msg).source }}</div>
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
                    v-html="renderText(reactionDetail(msg))"
                  ></span>
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

    <MessageLightbox v-model="lightboxSrc" :live="lightboxLive" />
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { renderRichText as _renderRichText } from '@/lib/highlight'
import { resolveAvatarUrl, iconSrc } from '@/lib/media'
import MessageLightbox from './MessageLightbox.vue'
import LivePhoto from './LivePhoto.vue'
import {
  clearCjCache, getContentJson, tryParseJson, tryParseShareContent, extractShareTitle,
  isJsonSystemMsg, isJsonSticker, getStickerUrl, shouldShow, renderSystemMsg, getWatchTogether, getProfileCard, getForwardInfo, isSystemMsg, duplicateSystemMessageIds,
  extractServerMsgIds, isVideoComment, isJsonShare, getShareInfo, shareCardTitle, getInlinePic,
  isLooseEmoji, isLooseImage, isShareCard,
  isVideoMsg, hasLocalVideo, isJsonVideo, getVideoPoster, getVideoDuration,
  getImageSrc, getEmojiSrc, getLivePhotoVideo, isViewOnce, getViewOnceText, getModifyKinds, getModifyReactions,
  MODIFY_KIND_LABELS, MODIFY_KIND_TITLES, isVoiceMsg, getVoiceUrl, getVoiceDuration,
  getRefMsg, getRefContent, getRefNickname,
  systemNoticeSide, peerRelationNotice,
  getGroupNotice,
} from '@/lib/douyinMessage'
import { getGoodsCard, clearGoodsCardCache } from '@/lib/goodsCard'
import { getFlameGiftCard, isFlameGiftCard, pickFlameAvatar, clearFlameCardCache } from '@/lib/flameCard'
import { getCallShopCard, isCallShopCard, clearCallShopCardCache } from '@/lib/callShopCard'
import { getFireInviteCard, isFireInviteCard, clearFireInviteCardCache } from '@/lib/fireInviteCard'
import { getFireBabyCard, isFireBabyCard, clearFireBabyCardCache } from '@/lib/fireBabyCard'
import { getFeedCard, isFeedCard, clearFeedCardCache } from '@/lib/feedCard'
import { getDailyFireCard, isDailyFireCard, clearDailyFireCardCache } from '@/lib/dailyFireCard'
import { getInviteCard, isInviteCard, getMusicCard, clearSystemCardCache } from '@/lib/cardKinds'
import { retryVideoWithTranscode, retryVideoIfUndecodable } from '@/lib/mediaPlayback'
import { senderDisplayName, selfPickerOptions, pickerOptionLabel } from '@/lib/selfIdentity'
import { getSpiritAi } from '@/lib/spiritAi'
import { readStoredSelfUid, writeStoredSelfUid, clearLegacyGlobalSelfUid } from '@/lib/selfView'
// 消息时间的两处写法（消息下方带年份、时间分隔条带年份）和「隔多久算断开」
// 都放在 lib/messageTime.js 里，那边能单测。
import { formatFullTime, exceedsGap } from '@/lib/messageTime'

// 卡片要用的图（项目 assets 里的原图）：火花卡背景 + 火花图标 + 联系门店卡外观 +
// 「聊天火花」关系卡的两张外观（邀请 / 接受纪念）。
// 注意：关系卡上那颗火花**不在**这里 —— 姐妹 / 兄弟 / 伴侣 / 死党各有各的图，
// 一律用载荷里那张（见 fireSpark）。
import flameCardBackground from '@/assets/background.png'
import flameCardFire from '@/assets/fire.png'
import callShopBackground from '@/assets/call-to-shop.png'
import fireRequestBackground from '@/assets/request.png'
import fireAcceptBackground from '@/assets/accept.png'
import fireBabyRequestBackground from '@/assets/request-firebaby.png'
import fireBabyAcceptBackground from '@/assets/accept-firebaby.png'
import feedCardHalf from '@/assets/feed-card.png'
import feedCardInvite from '@/assets/invite-feed.png'
// 「陪伴天数」卡自己画：火花图标和按钮上那圈渐变光环都用项目自带的素材
// （卡片载荷里也给这两张图的链接，但都是会过期的临时地址，自带一份更稳）。
import dailyFireFlame from '@/assets/fire.png'
import dailyFireRing from '@/assets/head_wrapper_circle.png'

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
const emit = defineEmits(['jumped', 'staticLoaded', 'openConversation'])

const messages = ref([])
const total = ref(0)
const loading = ref(false)
let scrollLocked = false  // 防止程序化滚动触发无限加载
const hasOlder = ref(false)
const hasNewer = ref(false)
const listRef = ref(null)
const senders = ref([])
// 「我是谁」的选择**按会话各记一份**（见 lib/selfView.js）：以前只存一个全局键，
// 在某个会话里切过视角之后，点开另一个会话还沿用上一次的选择（用户 2026-10-11 反馈）。
const selfUid = ref(
  props.selfUidOverride || readStoredSelfUid(props.conversation?.conv_id) || ''
)
// 老版本那个会串会话的全局键，升级后清掉一次。
clearLegacyGlobalSelfUid()
const showPicker = ref(false)
// 后端从聊天记录里认出来的本机账号（见 common/owner.py）。用户没手工选过「我」时
// 就拿它当默认值：单聊里自己发的消息才不会被当成对方、也不用每次手动选一遍。
// 认不出来时是空的，行为退回「让用户自己选」。
const detectedSelf = reactive({ uid: '', name: '' })
// 真正拿来判断「这条是不是我发的」的那个 uid：手工选的优先，其次自动认出来的
const effectiveSelfUid = computed(() => selfUid.value || detectedSelf.uid || '')
const pickerOptions = computed(
  () => selfPickerOptions(senders.value, effectiveSelfUid.value, { name: detectedSelf.name })
)
// 顶栏按钮上的名字：users 表里的昵称 → 自动认出来时后端给的名字 → uid 后四位
const selfLabel = computed(() => {
  const uid = effectiveSelfUid.value
  if (!uid) return ''
  return userCache[uid]?.nickname || detectedSelf.name || uid.slice(-4)
})
const isStatic = computed(() => !!props.staticRange || !!props.embeddedMessages)
const duplicateSystemIds = computed(() => duplicateSystemMessageIds(messages.value))
const imgLoading = computed(() => (isStatic.value ? 'eager' : 'lazy'))

// ── 列表进场动画 ──
// 只在「整屏换内容」时播放（切会话 / 搜索定位 / 引用跳转），
// 翻页追加的消息保持静止，避免破坏滚动位置和滚动锚定。
// 用两个交替的动画名，保证连续两次换内容都能重新播放（同名动画不会自动重启）。
const enterEpoch = ref(0)
let enterTimer = null
// 等 listRef 就绪的那个轮询定时器（见 onMounted / onUnmounted 里的 clearTimeout）
let bindTimer = null
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
  if (!effectiveSelfUid.value) return false
  return msg.sender_uid === effectiveSelfUid.value
}

// 表情快捷回复的明细：谁的什么表情。写在「表情快捷回复」标签右边，
// 形如「[爱心] | 小明」，多条回应用逗号隔开。
// 表情记号由模板交给 renderText() 渲染，本地有图的会像正文一样变成图片表情
// （拿不到昵称等纯文本部分照样转义，不会因为 v-html 漏出标签）。
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
  // 群邀请卡（type_desc=群聊邀请）虽然解析上算"卡片"，但位置跟普通消息一样：
  // 对方拉的你在左边、自己在右边，自带头像和昵称（见下面的 invite 卡片模板）。
  if (isInviteCard(msg)) return false
  // 姐妹火花卡（card_key=msg_fire_invitation 的邀请 / 接受纪念两种）同理：它在抖音里
  // 是**跟着发送者走**的一条消息（对方发的在左、我发的在右），不是居中的系统提示，
  // 所以也不能走系统提示那一套（见下面的 fire 卡片模板）。
  if (isFireInviteCard(msg)) return false
  // 「合养小火人」卡（同一个 card_key 的 joint_* 三种）也是跟着发送者走的卡片，
  // 同样不能居中（见下面的 fire-baby 卡片模板）。
  if (isFireBabyCard(msg)) return false
  // 「喂精灵」卡（card_key=msg_fire_invitation 的 feeding 一种）同理：抖音里它就是
  // 一条跟着发送者走的卡片消息（见下面的 feed 卡片模板），不是居中的系统提示。
  if (isFeedCard(msg)) return false
  // 「陪伴天数」卡（aweType=110270）虽然和见面礼卡同一个 card_key，但用户 2026-10-11
  // 明确要求它**居中**显示，所以它跟系统提示一起走（见下面的 daily 卡片模板）——
  // 不要因为它是"卡片"就挪到普通消息那一侧。
  if (isDailyFireCard(msg)) return true
  // 「获得火花见面礼」卡（aweType=110408）解析上算分享卡，但排版是系统提示样式，
  // 也要居中、不要头像和昵称，所以跟系统提示一起走。
  // 「联系门店」引导卡（aweType=110284）同理：抖音里就是会话中间的一条系统卡片。
  // 群公告（type_code=1004）也是居中的卡片，卡片自带头像和"谁发布了"，所以同样走这里。
  return isSystemMsg(msg) || isFlameGiftCard(msg) || isCallShopCard(msg)
}

// 这条消息画不画。invite/music 卡片的排版自己保证有内容可看，所以不需要
// douyinMessage.js 的 shouldShow 再认一遍（那边只认"正文是不是空的"）。
function _shouldShow(msg) {
  return shouldShow(msg) || isInviteCard(msg) || !!musicCard(msg)
    || isFireInviteCard(msg) || isFireBabyCard(msg) || isFeedCard(msg)
    || isDailyFireCard(msg)
}

// 群公告卡片：返回 { title, body } 或 null（见 lib/douyinMessage.js）。
function groupNotice(msg) {
  return getGroupNotice(msg)
}

// 「查看 JSON」贴在系统提示的哪一侧：随"我是谁"渲染出的人称变化。
function jsonToggleSide(msg) {
  return systemNoticeSide(msg, effectiveSelfUid.value)
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
  return exceedsGap(prev, cur)  // 隔了 5 分钟以上
}

// 这条消息前面要不要插一颗居中的完整时间（年月日时分）。
//
// 判据和「另起一组」是同一个（exceedsGap），这样「断开」这件事在视觉上只有一种
// 解释：多出的空隙、重新出现的头像昵称、正中间的时间，三者总是一起出现。
// 某些消息（镜像通知、空载荷）压根不画，拿它们当基准会算出假的间隔，所以往前找
// **上一条真正画出来的**消息来比。
function needsTimeDivider(index) {
  if (index <= 0) return false
  const cur = messages.value[index]
  if (!cur) return false
  const prev = previousDisplayed(index)
  if (!prev) return false
  return exceedsGap(prev, cur)
}

// 往前找上一条会画出来的消息；找不到（列表开头全是隐藏消息）时返回 null。
// 隐藏判定必须和模板上的 v-if 保持同一套（duplicateSystemIds + _shouldShow）。
function previousDisplayed(index) {
  for (let i = index - 1; i >= 0; i--) {
    const msg = messages.value[i]
    if (!msg) continue
    if (duplicateSystemIds.value.has(msg.msg_id)) continue
    if (!_shouldShow(msg)) continue
    return msg
  }
  return null
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
  // 认不出"我是谁"时分不清哪段是自己，就不猜（名字退回会话名 = 单聊里的对方昵称）。
  if (!effectiveSelfUid.value) return ''
  return first === effectiveSelfUid.value ? second : first
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

// 一条消息该显示谁的名字：规则在 lib/selfIdentity.js 里（那里能单测），
// 这里只把当前会话、当前「我是谁」、用户缓存喂进去。
//
// localUid 是「这台电脑的账号」，和当前选中的视角无关：会话名（= 对方的备注名）
// 只能套在本机账号对面那个人身上，视角切到对方之后本机账号那条不能再用会话名，
// 否则两边会显示成同一个名字（见 lib/selfIdentity.js 的说明）。
function displayName(msg) {
  if (peerNotice(msg)) return noticePeerName()
  return senderDisplayName(msg, {
    conversationName: props.conversation?.name || '',
    isGroup: isGroupConv.value,
    embedded: !!props.embeddedMessages,
    selfUid: effectiveSelfUid.value,
    localUid: detectedSelf.uid,
    users: userCache,
  })
}

// 小火人（精灵 AI）消息上要挂的性格小签；不是精灵消息时是空串。
function spiritStyle(msg) {
  return getSpiritAi(msg)?.style || ''
}

function getAvatarUrl(msg) {
  if (peerNotice(msg)) return noticePeerAvatar()
  return resolveAvatarUrl(userCache[msg.sender_uid]?.avatar_url)
}

// 火花见面礼卡片（aweType=110408）。头像优先跟聊天查看器用同一份最新头像
// （users 表里的，按 uid 取），那个人还没资料时才用卡片自带的那张。
function flameCard(msg) {
  return getFlameGiftCard(msg, effectiveSelfUid.value)
}

function flameAvatar(msg, which) {
  const card = flameCard(msg)
  if (!card) return ''
  const uid = which === 'self' ? card.selfUid : card.peerUid
  return pickFlameAvatar(card, which, resolveAvatarUrl(userCache[uid]?.avatar_url))
}

// 卡片背景是项目 assets/background.png（构建时打进 dist/assets）。
const flameCardStyle = { backgroundImage: `url("${flameCardBackground}")` }

// 「聊天火花」关系卡（card_key=msg_fire_invitation）：卡片外观是抖音客户端里那张卡的截图
// （assets/request.png 邀请 / assets/accept.png 接受纪念），文字、那颗火花和按钮由界面按
// 量出来的位置叠上去（见 lib/fireInviteCard.js）。
function fireInvite(msg) {
  return getFireInviteCard(msg, effectiveSelfUid.value)
}

// 卡片背景图：邀请卡一张、接受纪念卡一张，图上已经带好了底部的「聊天火花」条。
function fireBackground(msg) {
  const card = fireInvite(msg)
  return card?.kind === 'accept' ? fireAcceptBackground : fireRequestBackground
}

function fireCardStyle(msg) {
  return { backgroundImage: `url("${fireBackground(msg)}")` }
}

// 卡片上那颗火花（姐妹 / 兄弟 / 伴侣 / 死党各有各的图，都在载荷里）。抖音给的是带签名
// 的临时链接，所以一律走 iconSrc()：采集时已经存进 data/media/card_icons/，本地没有
// 且链接还没过期时后端会补拉一次（见 common/card_icons.py 的 is_fire_card）。
function fireSpark(msg) {
  const card = fireInvite(msg)
  if (!card || !card.icon) return ''
  return iconSrc(card.icon)
}

// 这颗火花拉不到时（载荷里没有图标、或那张图的链接已经过期且本地也没存下）就把 <img>
// 摘掉，卡片其余部分照常显示 —— 总比在卡片上留一个碎图标好。
function onFireSparkError(event) {
  const img = event?.target
  if (img) img.style.display = 'none'
}

// 卡片上的两个头像和见面礼卡一个道理：库里存的是**发消息那天**的快照，
// users 表里的是每次抓取都会刷新的最新头像，能用新的就用新的。
function fireAvatar(msg, which) {
  const card = fireInvite(msg)
  if (!card) return ''
  const uid = which === 'left' ? card.leftUid : card.rightUid
  const latest = resolveAvatarUrl(userCache[uid]?.avatar_url)
  return latest || (which === 'left' ? card.leftAvatar : card.rightAvatar)
}

function fireAvatarName(msg, which) {
  const card = fireInvite(msg)
  if (!card) return '对方'
  const uid = which === 'left' ? card.leftUid : card.rightUid
  if (uid && String(uid) === String(effectiveSelfUid.value)) return '我'
  return userCache[uid]?.nickname || '对方'
}

function fireAvatarAlt(msg, which) {
  return fireAvatarName(msg, which) + '的头像'
}

// 「合养小火人」卡（card_key=msg_fire_invitation 的 joint_* 三种）：卡片外观是
// assets/request-firebaby.png（空白底纹，只有那颗小火人）和 assets/accept-firebaby.png
// （已经印好「我们开始养小火人啦／连续聊天让精灵快快长大」）。见 lib/fireBabyCard.js。
function fireBaby(msg) {
  return getFireBabyCard(msg, effectiveSelfUid.value)
}

// 用哪张底纹：
//   * 邀请 / 纪念两种 → 空白那张，文字和按钮都由界面画；
//   * 开始合养且文案和图上印的一模一样 → 直接用它（图上的字就是对的）；
//   * 开始合养但文案对不上（比如 AI 精灵那款）→ 退回空白那张，免得图上印着不相干的话。
function fireBabyBackground(msg) {
  const card = fireBaby(msg)
  if (!card) return fireBabyRequestBackground
  return card.textBakedIn ? fireBabyAcceptBackground : fireBabyRequestBackground
}

function fireBabyStyle(msg) {
  return { backgroundImage: `url("${fireBabyBackground(msg)}")` }
}

// 「喂精灵」卡（card_key=msg_fire_invitation 的 feeding 一种）有**两版版式**：
// ① 新版（载荷带背景图）= assets/feed-card.png（下半部分，标题/副标题/底部「合养精灵」
//    条印在图里）+ 上面补一截封面（背景图 + 那根辣条叠上去）；
// ② 老版（7 月那批载荷没有背景图）= 整张卡就是 assets/invite-feed.png（浅色渐变底 +
//    底部「合养精灵」条，787×509，和抖音那张一模一样），上面叠精灵图和载荷里的文字。
// 见 lib/feedCard.js。
function feedCard(msg) {
  return getFeedCard(msg)
}

// 新版封面：背景图由卡片自己给（whole_card.ui_info.background_image@light），
// 走 iconSrc() 交给后端缓存 —— 抖音给的是带签名的临时链接，过期就只剩碎图。
// 采集时已经把它存进 data/media/card_icons/ 了（沿用官方文件名）。
// 下半部分那张图（标题 / 副标题 / 底部「合养精灵」条）用项目自带的 assets/feed-card.png，
// 贴在卡片底部；卡片给的那张只当封面。两张分开画，封面那截才能自己补上方圆角。
// 老版没有背景图，整张卡改用 assets/invite-feed.png 打底。
function feedCardStyle(msg) {
  const card = feedCard(msg)
  const style = {
    backgroundImage: `url("${card?.background ? feedCardHalf : feedCardInvite}")`,
  }
  if (card?.background) style['--feed-cover'] = `url("${iconSrc(card.background)}")`
  return style
}

// 把那根辣条叠到背景上。拉不到就只留背景（总比留个碎图标好）。
function feedFoodSrc(msg) {
  const card = feedCard(msg)
  return card?.food ? iconSrc(card.food) : ''
}

// 老版卡片上那张精灵图（载荷里的 top.content）。
function feedSpiritSrc(msg) {
  const card = feedCard(msg)
  return card?.spirit ? iconSrc(card.spirit) : ''
}

function onFeedFoodError(event) {
  const img = event?.target
  if (img) img.style.display = 'none'
}

// ── 「陪伴天数」卡（aweType=110270，见 lib/dailyFireCard.js）──
// 这张卡整张由界面自己画：上面两个叠着的头像 + 中间那行字 + 底下一颗带光环的按钮。
// 卡片上的位置和大小都是照抖音客户端量的。
function dailyFire(msg) {
  return getDailyFireCard(msg, detectedSelf.uid)
}

// 上方两个头像（固定顺序，不随视角变）：左边是对方、右边是本机账号。
// 头像本身取自 users 表（每次抓取都会刷新），拿不到就留空、让那个圆保持底色。
function dailyAvatar(msg, which) {
  const card = dailyFire(msg)
  if (!card) return ''
  const uid = which === 'left' ? card.leftUid : card.rightUid
  return resolveAvatarUrl(uid ? userCache[uid]?.avatar_url : '')
}

function dailyAvatarName(msg, which) {
  const card = dailyFire(msg)
  const uid = card ? (which === 'left' ? card.leftUid : card.rightUid) : ''
  if (!uid) return '对方'
  if (String(uid) === String(effectiveSelfUid.value)) return '我'
  return userCache[uid]?.nickname || '对方'
}

function dailyAvatarAlt(msg, which) {
  return dailyAvatarName(msg, which) + '的头像'
}

// 按钮里那颗头像：**跟着当前视角走**（载荷的 bottom_left_content 指向 user_info）。
// 本机账号那一侧可能没有资料，拿不到时退回上方那两个头像里的本机账号那张。
function dailyRingAvatar(msg) {
  const card = dailyFire(msg)
  if (!card) return ''
  const selfUid = effectiveSelfUid.value || detectedSelf.uid
  return resolveAvatarUrl(selfUid ? userCache[selfUid]?.avatar_url : '')
}

function dailyRingAvatarAlt(msg) {
  const card = dailyFire(msg)
  const selfUid = card && (effectiveSelfUid.value || detectedSelf.uid)
  return (selfUid ? (userCache[selfUid]?.nickname || '我') : '我') + '的头像'
}

// 卡片上的火花图标和按钮光环都用项目自带素材（载荷给的是会过期的临时链接）。
const dailyFlame = dailyFireFlame
// 光环那张由模板用 :style 传给 .msg-daily-ring（构建后会变成带哈希的文件名，
// 写死在 CSS 里的话打包就找不到了）。
const dailyRingStyle = { backgroundImage: `url("${dailyFireRing}")` }

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

// ── 群邀请卡 / 豆包卡（见 lib/cardKinds.js）──
// 邀请卡上的「发消息」只在被邀请的那个群**已经归档**时才画出来：群聊的名字要问
// 后端才知道（卡片里带的是拉你进群那会儿的群名，可能已经改过）。
const groupCache = reactive({})

// 加载不出来的卡片图，按消息记着，别再画那个碎图占位。
const brokenCardIcons = reactive({})

function inviteCard(msg) {
  return getInviteCard(msg)
}

function musicCard(msg) {
  return getMusicCard(msg)
}

// 卡片图（群头像 / 豆包卡封面）的地址：
//   1. 采集时存下来的那份（raw_data.card_icon_dir → /media/card_icons/…）；
//   2. 没存过就走 iconSrc()，后端发现链接还没过期会顺手补拉一份存下来。
//      —— 抖音给的图是带签名的临时链接，过期后 <img> 直接就碎了，所以第一次
//      看这张卡时就把图"固化"到本地（见 common/card_icons.py）。
function cardIcon(msg, url) {
  if (brokenCardIcons[msg.msg_id]) return ''
  // 采集时存过的那两份都认：media_local_path（消息落库时记的）和
  // raw_data.card_icon_dir（同一次抓取里另存一份摘要）。
  for (const saved of [msg.media_local_path, msg.card_icon_dir]) {
    if (typeof saved === 'string' && saved.startsWith('card_icons/')) return '/media/' + saved
  }
  return iconSrc(url)
}

function onCardIconError(msg) {
  // 已经存到本地的那份坏了不退回网络（链接早过期了，只会再失败一次），直接记成坏图。
  if (!msg.media_local_path && !msg.card_icon_dir && !brokenCardIcons[msg.msg_id]) {
    brokenCardIcons[msg.msg_id] = true
  }
}

// 打开豆包卡视频：卡片自带 open_url（v.douyin.com 短链），没有就不给点。
function openMusicCard(msg) {
  const card = musicCard(msg)
  if (card?.url) window.open(card.url, '_blank', 'noopener')
}

function openInviteGroup(msg) {
  const card = inviteCard(msg)
  if (card) emit('openConversation', { conv_id: card.convId, name: card.groupName })
}

async function loadGroupConversations(msgList) {
  const ids = new Set()
  for (const msg of msgList) {
    const card = getInviteCard(msg)
    if (card && groupCache[card.convId] === undefined) ids.add(card.convId)
  }
  await Promise.all(Array.from(ids).map(async (convId) => {
    try {
      const res = await fetch(`/api/conversations/${encodeURIComponent(convId)}`)
      groupCache[convId] = res.ok ? await res.json() : null
    } catch {
      groupCache[convId] = null
    }
  }))
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
    const card = getFlameGiftCard(msg, effectiveSelfUid.value)
    if (card) {
      for (const uid of [card.selfUid, card.peerUid]) {
        if (uid && !userCache[uid]) uids.add(uid)
      }
    }
    // 「陪伴天数」卡上三个头像（上面两个 + 按钮里那个）同样要查。
    const daily = getDailyFireCard(msg, detectedSelf.uid)
    if (daily) {
      for (const uid of [daily.leftUid, daily.rightUid, effectiveSelfUid.value || detectedSelf.uid]) {
        if (uid && !userCache[uid]) uids.add(uid)
      }
    }
  }
  // 「我是谁」那个人的昵称/头像也提前查一次：顶栏按钮和弹窗都用得上。
  if (effectiveSelfUid.value && !userCache[effectiveSelfUid.value]) {
    uids.add(effectiveSelfUid.value)
  }
  await Promise.all(Array.from(uids).map(uid => fetchUserInfo(uid)))
}

// 「设置我」弹窗里一行显示的名字：本机账号那一条的昵称来自 /api/owner。
function pickerLabel(option) {
  return pickerOptionLabel(option, { users: userCache })
}

// 问后端「这台电脑的抖音账号是谁」（见 common/owner.py）。认不出来就保持空，
// 界面退回让用户自己选。
async function fetchDetectedSelf() {
  if (props.selfUidOverride) return          // 截图等场景已经指定了人，不必再猜
  try {
    const res = await fetch('/api/owner')
    if (!res.ok) return
    const data = await res.json()
    detectedSelf.uid = String(data?.uid || '')
    detectedSelf.name = String(data?.name || '')
  } catch {}
}

function pickSelf(uid) {
  selfUid.value = uid || ''
  // 记在**当前会话**名下；清除选择 = 回到「自动认出来的那个」。
  writeStoredSelfUid(props.conversation?.conv_id, uid || '')
  showPicker.value = false
}

// 系统消息引用的视频：异步加载分享消息的标题和封面
async function loadSysRefs(msgList) {
  loadGroupConversations(msgList)
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
// 放大查看的如果是实况图，这里放着它的小视频地址（普通图片是 null）。
const lightboxLive = ref(null)
function openLightbox(src) {
  if (!src) return
  lightboxLive.value = null
  lightboxSrc.value = src
}
// 点击实况图：放大后仍然按同一套悬停规则播放（见 LivePhoto.vue）。
function openLivePhoto(msg) {
  const cover = getImageSrc(msg)
  if (!cover) return
  lightboxLive.value = getLivePhotoVideo(msg)
  lightboxSrc.value = cover
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
  // 静态渲染（截图 / 合并转发）也要先知道「我是谁」，否则单聊两边会显示成同一个人
  fetchDetectedSelf()
  if (isStatic.value) return
  // 延迟绑定，等 listRef 准备好
  const tryBind = () => {
    bindTimer = null
    if (listRef.value) {
      listRef.value.addEventListener('scroll', onListScroll, { passive: true })
    } else {
      bindTimer = setTimeout(tryBind, 200)
    }
  }
  tryBind()
})
onUnmounted(() => {
  // 轮询 listRef 的那个定时器也要停：组件在 listRef 就绪前就被卸载时，它会一直
  // 空转，甚至在某次 listRef 又变成非空时往一个已经没人管的元素上挂 scroll 监听
  // （onUnmounted 早就执行过，没人再摘得下来）。
  clearTimeout(bindTimer)
  if (listRef.value) {
    listRef.value.removeEventListener('scroll', onListScroll)
  }
  stopFollowBottom()
  clearTimeout(enterTimer)
  for (const audio of voiceAudioRefs.values()) audio.pause()
  voiceAudioRefs.clear()
  playingVoiceId.value = null
})

// 每条消息下方的时间。以前只写「时分」和「10月14日 时分」，翻跨年的记录时
// 分不清是哪年；用户 2026-10-14 要求带上年份，所以一律写完整时间
// （年月日时分），和两颗消息中间那颗时间分隔条用同一个格式。
function formatTime(ts) {
  return formatFullTime(ts)
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
  clearFireInviteCardCache()
  clearFireBabyCardCache()
  clearFeedCardCache()
  clearDailyFireCardCache()
  clearSystemCardCache()
  for (const key of Object.keys(groupCache)) delete groupCache[key]
  for (const key of Object.keys(brokenCardIcons)) delete brokenCardIcons[key]
}

watch(() => props.conversation, (conv) => {
  referenceError.value = ''
  loadError.value = ''
  messageRequestId++
  stopFollowBottom()
  // 换会话就把视角换回这个会话自己记的（没记过 = 自动认出来的那个）。截图等场景
  // 已经指定了人（selfUidOverride），那种情况不跟着会话走。
  if (!props.selfUidOverride) {
    selfUid.value = readStoredSelfUid(conv?.conv_id)
  }
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
  gap: 8px 12px;
  /* 会话名长、屏幕窄的时候让这行自己换行：以前不许换行，名字会把
     「设置"我"」那颗按钮挤出屏幕右边，手机上就再也点不到了。 */
  flex-wrap: wrap;
  flex-shrink: 0;
}
.msg-header h3 {
  font-size: 15px;
  font-weight: 600;
  /* 能被压窄 + 省略号，长名字才不会把整行撑爆 */
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.msg-total {
  font-size: 12px;
  color: var(--text-muted);
  flex-shrink: 0;
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
  flex-shrink: 0;
}
.msg-pick-self:hover { border-color: var(--accent); }
.msg-pick-self.picked { border-color: var(--accent); color: var(--accent); }
/* 名字是自动认出来的（用户还没手工选过）时，标记一下 —— 点开随时能改 */
.msg-pick-self-auto { color: var(--text-muted); font-size: 11px; }

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
  /* 群聊里人选可能有一长串，超出屏幕时要能滚，别把对话框顶出去 */
  max-height: 80vh;
  overflow-y: auto;
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
/* 时间分隔条：两条消息隔了 5 分钟以上时，把下面那条消息的完整时间（年月日时分）
   摆在正中间，外观和系统通知一样（浅底圆角小胶囊），一眼能看出「这里断开了」。
   下面那条消息若是另起一组（group-start）会带上 16px 上边距，这里把它吃回来，
   否则分隔条 + 头像昵称两段空隙叠在一起，断开会显得过分夸张。
   用 :has(+ ...) 而不是只吃空隙：没有 group-start 时还要保留一点上下呼吸。 */
.msg-time-divider {
  text-align: center;
  margin: var(--msg-group-gap) 0;
  user-select: none;
}
.msg-time-divider:has(+ .msg-item.group-start) {
  margin-top: 6px;
  margin-bottom: calc(var(--msg-group-gap) * -1 + var(--msg-gap));
}
.msg-time-divider > span {
  display: inline-block;
  max-width: 100%;
  padding: 4px 14px;
  border-radius: 999px;
  background: var(--system-bg);
  color: var(--system-text-color);
  font-size: 12px;
  overflow-wrap: anywhere;
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
.msg-system-line > .msg-daily-card,
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
/* 「陪伴天数」卡（aweType=110270）：居中显示（见 _isSystem），整张卡由界面自己画
   —— 载荷只给文字、天数和头像的摆放，没有背景图。
   宽 278px、圆角 14px，白底 + 轻投影，和见面礼卡一个观感。
   卡片上的间距和元素大小都照抖音客户端量出来的比例摆。 */
.msg-daily-card {
  box-sizing: border-box;
  width: 278px;
  max-width: 100%;
  padding: 14px 16px 16px;
  border-radius: 14px;
  background: #fff;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.12);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  overflow: hidden;
}
/* 上方两个头像：右边那个后画，压住左边一点 —— 抖音里就是左边那个被咬掉一个月牙。
   右边那个带一圈白边，咬痕才看得出来（不然两张同色底的图粘成一块）。 */
.msg-daily-avatars {
  display: flex;
  align-items: center;
  justify-content: center;
  flex: none;
  width: 100%;
  height: 62px;
}
.msg-daily-avatar {
  flex: none;
  width: 58px;
  height: 58px;
  border-radius: 50%;
  overflow: hidden;
  background: #f1f2f4;
}
.msg-daily-avatar img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.msg-daily-avatar-left {
  z-index: 1;
}
/* 右边那个压住左边一点：抖音里左边头像的右侧被咬掉一牙月牙。
   右边这个带一圈白边，咬痕才看得出来（两张同色底的图会粘成一块）。 */
.msg-daily-avatar-right {
  z-index: 2;
  margin-left: -10px;
  border: 2px solid #fff;
  box-sizing: content-box;
}
/* 中间那行字：前文 + 火花图标 + 橙色天数 + 后文，整体居中 */
.msg-daily-row {
  display: flex;
  align-items: center;
  justify-content: center;
  flex-wrap: wrap;
  gap: 1px;
  width: 100%;
  font-size: 16px;
  font-weight: 600;
  color: #1f2023;
  line-height: 1.3;
}
.msg-daily-text {
  white-space: pre-wrap;
}
.msg-daily-flame {
  width: 17px;
  height: 21px;
  object-fit: contain;
  margin: 0 2px;
}
.msg-daily-days {
  color: #ff9224;
  font-size: 17px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  margin: 0 3px;
}
/* 底下那颗按钮：左边是当前视角的人的头像 + 一圈蓝绿渐变光环，右边才是文字。
   只做展示、点不动，只保留鼠标悬停反馈。 */
.msg-daily-btn {
  box-sizing: border-box;
  position: relative;
  width: 100%;
  height: 41px;
  border-radius: 7px;
  background: #f4f4f4;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: #1f2023;
  font-size: 14px;
  font-weight: 600;
  cursor: default;
  user-select: none;
  transition: background var(--dur-fast) var(--ease-out);
}
.msg-daily-btn:hover {
  background: #e9e9e9;
}
/* 光环：项目自带的 assets/head_wrapper_circle.png（蓝→绿渐变的一圈），
   图片地址由模板用 :style 传进来（构建后会变成带哈希的文件名）。
   图本身只有那一圈渐变，中间是透明的，头像压在它正中间。
   抖音里这一圈比头像大一圈，中间留出白底，看起来像「照片镶在光环里」。 */
.msg-daily-ring {
  position: relative;
  flex: none;
  width: 33px;
  height: 33px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background-position: center;
  background-repeat: no-repeat;
  background-size: 100% 100%;
}
.msg-daily-ring img {
  display: block;
  width: 23px;
  height: 23px;
  border-radius: 50%;
  object-fit: cover;
}
.msg-daily-btn-label {
  white-space: nowrap;
}
/* 「聊天火花」关系卡（card_key=msg_fire_invitation）：整张卡就是 assets/request.png /
   assets/accept.png 那张抖音客户端的截图（787×509，连底部「聊天火花」条一起带上了），
   文字、按钮、头像按量出来的相对位置叠在上面。那颗火花不在图里，是载荷给的图片
   （姐妹/兄弟/伴侣/死党各不相同），由 <img> 铺在这个位置。
   坐标与字号统一按「原图 787×509 → 卡片 278×180」换算（比例 0.353），
   百分比负责位置、px 负责字号，这样改宽度时位置跟着走、字号仍然清晰。
   卡片不居中：它跟着发送者分左右（见 _isSystem / 模板里的注释）。 */
.msg-fire-card {
  position: relative;
  /* 787 : 509 ≈ 278 : 180 */
  width: 278px;
  max-width: 100%;
  height: 180px;
  border-radius: 13px;
  background-color: #fff;
  background-size: 100% 100%;
  background-repeat: no-repeat;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.12);
  overflow: hidden;
}
/* 顶上那颗火花。原图取的是整张 450×450 的图标（画面上只有中间一块是实心的），
   所以这里的框比看得见的那颗大一圈。 */
.msg-fire-spark {
  position: absolute;
  left: 41.64%;
  top: 9.14%;
  width: 17.72%;
  height: auto;
  pointer-events: none;
  user-select: none;
}
/* 邀请卡上那颗大一号（原图 x280-506 y4-232） */
.msg-fire-spark-invite {
  left: 35.55%;
  top: 0.84%;
  width: 28.78%;
}
/* 接受纪念卡的两个头像：原图两个圆各 174px，左圈 x156、右圈 x459，都在 y27 起 */
.msg-fire-avatar {
  position: absolute;
  top: 5.30%;
  width: 22.11%;
  aspect-ratio: 1;
  border-radius: 50%;
  overflow: hidden;
  background: #fff;
}
.msg-fire-avatar img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.msg-fire-avatar-left {
  left: 19.82%;
}
.msg-fire-avatar-right {
  left: 58.32%;
}
/* 主标题：原图左边距 38、字高一 14.7（换算到卡片就是 15.6px 的字） */
.msg-fire-title {
  position: absolute;
  left: 4.83%;
  right: 4.83%;
  top: 52.6%;
  font-size: 15.6px;
  font-weight: 600;
  line-height: 1.2;
  color: #16181c;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
/* 副标题：原图字高 12.7，后面常跟一个 › */
.msg-fire-sub {
  position: absolute;
  left: 4.83%;
  right: 4.83%;
  top: 67.4%;
  display: flex;
  align-items: center;
  gap: 1px;
  font-size: 13.6px;
  line-height: 1.2;
  color: #73767d;
  white-space: nowrap;
  overflow: hidden;
}
.msg-fire-sub > span:first-child {
  overflow: hidden;
  text-overflow: ellipsis;
}
.msg-fire-arrow {
  flex: none;
  /* 方块边长为 7.6px：两条边转 45° 后正好是参考图里那个「›」（约 5.4 宽、10.8 高） */
  width: 7.6px;
  height: 7.6px;
  border-right: 1.7px solid currentColor;
  border-bottom: 1.7px solid currentColor;
  transform: rotate(-45deg) translate(-1px, -1px);
  margin: 0 1px 0 2px;
}
/* 「接受邀请」只做展示，点不动；只保留鼠标悬停的阴影反馈。
   按钮压在原图那颗钮的位置：x521-750 y282-377（230×96）。 */
.msg-fire-btn {
  position: absolute;
  left: 66.2%;
  top: 55.4%;
  width: 29.22%;
  height: 18.86%;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  background: #f0f1f5;
  color: #a4a7ad;
  font-size: 13.6px;
  font-weight: 600;
  white-space: nowrap;
  cursor: default;
  user-select: none;
  transition: background var(--dur-fast) var(--ease-out), box-shadow var(--dur-fast) var(--ease-out);
}
/* 灰按钮（待接受 / 已接受 / 已失效）之上，这一档是「给我的邀请」：抖音用主色配红字 */
.msg-fire-btn-primary {
  background: #fdeef1;
  color: #f0476a;
}
.msg-fire-btn:hover {
  background: #e4e7ea;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
}
.msg-fire-btn-primary:hover {
  background: #fbdfe5;
}
/* 「合养小火人」卡片（card_key=msg_fire_invitation 的 joint_* 三种）：整张卡就是
   assets/request-firebaby.png / assets/accept-firebaby.png 那张客户端截图
   （787×509，顶上那颗小火人和底部「合养精灵」条都印在图里）。文字和按钮叠在上面。
   布局锚在「内容区底部往上长」：标题换行时往上吃空白，不会把副标题挤出卡片。
   坐标与字号统一按「原图 787×509 → 卡片 278×180」换算（比例 0.3532，原图 787px 就是
   客户端在 2.8325 倍缩放下那张卡的宽度），百分比负责位置、px 负责字号。 */
.msg-fire-baby-card {
  position: relative;
  /* 787 : 509 ≈ 278 : 180 */
  width: 278px;
  max-width: 100%;
  height: 180px;
  border-radius: 13px;
  background-color: #fff;
  background-size: 100% 100%;
  background-repeat: no-repeat;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.12);
  overflow: hidden;
}
/* 文字块：宽度让到按钮左边（按钮从 66.2% 起），底部贴着内容区下沿。
   用 bottom 定位 + 由下往上堆，标题折行时向上长（小火人上方那片是空的）。 */
.msg-fire-baby-body {
  position: absolute;
  left: 4.83%;
  right: 34%;
  bottom: 22.4%;
  display: flex;
  flex-direction: column;
  gap: 7px;
}
/* 主标题：原图字距 45px（÷2.8325 ≈ 16px），但抖音在客户端里会把长标题截成
   「对方邀请你一起养小…」。这里要**把话说完**：字号收到 15px，让最常见的
   11 个字「对方邀请你一起养小火人」正好排得下一行；万一更长就换行，绝不省略。 */
.msg-fire-baby-title {
  font-size: 15px;
  font-weight: 600;
  line-height: 20px;
  color: #161823;
  /* 抖音把长标题截成「对方邀请你一起养小…」，这里要求把话说完 */
  overflow-wrap: anywhere;
}
/* 副标题：原图字距 39.5px（≈14px），后面常跟一个 ›。同样不省略：
   一行排不下就换行（左下方那片是空的，往上长不会压到小火人）。 */
.msg-fire-baby-sub {
  display: flex;
  align-items: flex-start;
  gap: 1px;
  font-size: 14px;
  line-height: 1.15;
  color: #737679;
}
.msg-fire-baby-sub > span:first-child {
  overflow-wrap: anywhere;
}
.msg-fire-baby-arrow {
  flex: none;
  /* 方块边长 7.6px：两条边转 45° 后正好是参考图里那个「›」。
     跟第一行文字对齐（margin-top 把方块压到字高的中间）。 */
  width: 7.6px;
  height: 7.6px;
  border-right: 1.7px solid currentColor;
  border-bottom: 1.7px solid currentColor;
  transform: rotate(-45deg) translate(-1px, -1px);
  margin: 4px 1px 0 2px;
}
/* 按钮：压在客户端那颗钮的位置（原图 x521-750 y282-377，230×96），只做展示、点不动。 */
.msg-fire-baby-btn {
  position: absolute;
  left: 66.2%;
  top: 55.4%;
  width: 29.22%;
  height: 18.86%;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  background: #f4f4f4;
  color: #a9aaae;
  font-size: 15px;
  font-weight: 600;
  white-space: nowrap;
  cursor: default;
  user-select: none;
  transition: background var(--dur-fast) var(--ease-out), box-shadow var(--dur-fast) var(--ease-out);
}
/* 主色按钮（等对方接受的邀请 / 合养成功那两张）：抖音用淡粉底配红字 */
.msg-fire-baby-btn-primary {
  background: #fbebf0;
  color: #e1395a;
}
.msg-fire-baby-btn:hover {
  background: #e7e7e7;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
}
.msg-fire-baby-btn-primary:hover {
  background: #f7dfe6;
}
/* 「喂精灵」卡（card_key=msg_fire_invitation 的 feeding 一种）有**两版版式**：
   ① 新版（载荷带背景图）= 上面一截封面 + 下面 assets/feed-card.png。参考图量出来是
      788×511（封面 233 + 下半 278），按 278px 宽等比换算正好是 180px 高，和别的卡片一致。
      封面是**拼的**：背景图（小火人 + 爱心光晕）垫底，那根辣条居中偏左叠上去。
      下半那张图（由内联样式挂在卡片底部）只有下方两个圆角，所以封面这截必须自己补
      左上/右上圆角，否则卡片顶部是方角、和底部对不上。
   ② 老版（7 月那批载荷没有背景图：whole_card.ui_info 是空的）= 整张卡就是
      assets/invite-feed.png（787×509，浅色渐变底 + 底部「合养精灵」条，和抖音那张
      1:1 一样），卡片自己不用再拼封面；上面只叠精灵图和载荷里的标题 / 副标题 / 按钮。
      尺寸按 278px 宽等比换算 = 179.8px 高，和 assets 里的原图比例完全对齐。 */
.msg-feed-card {
  position: relative;
  /* 788 : 511 ≈ 278 : 180 */
  width: 278px;
  max-width: 100%;
  height: 180px;
  border-radius: 13px;
  background-color: #fff;
  /* 下半部分那张图贴在卡片底部（278×98，只带下方两个圆角） */
  background-repeat: no-repeat;
  background-position: 0 100%;
  background-size: 100% auto;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.12);
  overflow: hidden;
}
/* 老版：整张卡就是 invite-feed.png，铺满卡片（背景是渐变 + 底部那条页脚）。 */
.msg-feed-card--old {
  /* 787 : 509 ≈ 278 : 179.8 */
  height: 179.8px;
  background-position: 0 0;
  background-size: 100% 100%;
}
/* 老版卡片上的精灵图（载荷里那张 252×252 的图，四周各留 5px 透明边）：
   位置 / 大小按抖音参考图量的 —— 拿小火人两只眼睛的距离反推缩放（约 0.906），
   图片框左上落在原图 (289.5, 51.7) 处，框宽 228 / 787 = 28.98%。 */
.msg-feed-spirit {
  position: absolute;
  left: 31.2%;
  top: 5.85%;
  width: 26.7%;
  height: auto;
  pointer-events: none;
}
/* 封面那截：占卡片高度的 45.67%（233 / 511）。下半部分那张图没有上方圆角，
   所以这里自己补两个，拼起来才是一张四周都有圆角的卡。 */
.msg-feed-cover {
  position: absolute;
  left: 0;
  top: 0;
  width: 100%;
  height: 45.67%;
  border-radius: 13px 13px 0 0;
  overflow: hidden;
  background-color: #fff;
  /* 卡片给的背景图（whole_card.ui_info.background_image@light）。铺满整截：
     它和封面区的比例几乎一样，拉满不会有明显变形。 */
  background-image: var(--feed-cover, none);
  background-repeat: no-repeat;
  background-size: 100% 100%;
}
/* 那根辣条：按参考图量出来的位置叠上去 —— 原图 788 宽下 228px、左起 35.66%，
   垂直上沿在封面里占 2.58%。食物图自带透明通道，直接压在小火人左侧。 */
.msg-feed-food {
  position: absolute;
  left: 35.66%;
  top: 2.58%;
  width: 28.93%;
  height: auto;
  pointer-events: none;
}
/* 主标题：固定「邀请你一起喂精灵」，压在素材图左侧（原图字高 41px ≈ 14.5px）。
   top 用「字的**墨迹**上沿」换算：参考图里那行字的墨迹在卡片高的 53.6% 处，
   而 CSS 的 top 定的是行盒上沿（比墨迹高约 1.5%），所以这里减去这个差值。 */
.msg-feed-title {
  position: absolute;
  left: 4.95%;
  top: 52.1%;
  font-size: 15.6px;
  font-weight: 600;
  line-height: 1.2;
  color: #1d1c21;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
/* 副标题：固定「精灵爱吃这个，你也来喂」（原图字高 36px ≈ 12.7px）。同理按墨迹上沿换算。 */
.msg-feed-sub {
  position: absolute;
  left: 4.82%;
  top: 67.2%;
  font-size: 13.6px;
  line-height: 1.2;
  color: #7a7a7a;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
/* 「去喂食」：压在参考图那颗钮的位置（原图 230×96，左起 522），淡粉底配红字，
   只做展示、点不动。喂食卡不等人接受（接受后会另外发一条消息），所以**只有这一种
   文案**，没有待接受 / 已失效那些状态，也没有主色和灰色的区分。 */
.msg-feed-btn {
  position: absolute;
  left: 66.24%;
  top: 55.7%;
  width: 29.19%;
  height: 18.8%;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  background: #ffeff2;
  color: #d1435d;
  font-size: 15px;
  font-weight: 600;
  white-space: nowrap;
  cursor: default;
  user-select: none;
  transition: background var(--dur-fast) var(--ease-out), box-shadow var(--dur-fast) var(--ease-out);
}
.msg-feed-btn:hover {
  background: #fbe0e6;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
}
/* 老版卡片（载荷没有背景图）：上面的坐标全是按 787×509 那张参考图量的，换算成
   278px 卡宽后字号 / 位置都和新版不一样，所以这里整体覆盖一遍。
   标题和副标题的字取自载荷原文（见 lib/feedCard.js），颜色也是从参考图上取的。 */
.msg-feed-card--old .msg-feed-title {
  left: 4.83%;
  top: 52.4%;
  font-size: 15.7px;
  font-weight: 600;
  color: #0d0e12;
}
.msg-feed-card--old .msg-feed-sub {
  left: 4.83%;
  top: 67.8%;
  font-size: 13.7px;
  color: #686868;
}
/* 老版的按钮比新版略高一点（93 / 509 = 18.27%），底色 / 红色也照参考图取。 */
.msg-feed-card--old .msg-feed-btn {
  left: 66.17%;
  top: 55.58%;
  width: 28.92%;
  height: 18.46%;
  border-radius: 7px;
  background: #fbebf1;
  color: #ce3c60;
  font-size: 15.7px;
}
.msg-feed-card--old .msg-feed-btn:hover {
  background: #f7dde5;
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
/* 我发的消息：正文列（昵称、气泡、图片、卡片、页脚）一律贴右边对齐 */
.msg-item.msg-self > .msg-body {
  align-items: flex-end;
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

/* 头像旁边这一列：昵称 / 正文 / 页脚（时间 + 标签）从上到下排。
   用纵向 flex 是为了让每个子块各自贴住**自己那一侧**——
   对方的消息贴左，我发的消息贴右（见下面的 .msg-item.msg-self）。
   普通块级排版做不到这一点：正文块的宽度会被同一列里更宽的那一行撑开，
   而正文自己是 fit-content，于是短句（正文比时间行还窄）会往另一边倒。
   实测不带标签的短句要偏十几像素，带上「表情快捷回复」明细能偏出一百多像素。 */
.msg-body {
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
}

.msg-sender {
  font-size: var(--msg-sender-size);
  color: var(--text-muted);
  margin-bottom: 3px;
  display: flex;
  align-items: center;
  gap: 6px;
}
/* 小火人（精灵 AI）的性格小签：灰底、方角、比名字小一号 */
.msg-spirit-style {
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--bg-tertiary);
  color: var(--text-secondary);
  font-size: calc(var(--msg-sender-size) - 2px);
  line-height: 1.5;
}
.msg-item.msg-self > .msg-body > .msg-sender {
  text-align: right;
  justify-content: flex-end;
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

/* 群邀请卡（居中）：左边群头像，右边群名（大字加粗）+ 一行「谁 添加你进群」。
   群已归档时卡片底部再画一个「发消息」。 */
.msg-invite-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  background: var(--card-bg);
  border-radius: 10px;
  border-top-left-radius: 2px;
  padding: 12px 14px;
  max-width: var(--share-card-width);
  text-align: left;
}
/* 自己发的贴右边：圆角反过来，底色用我方气泡色（跟分享卡一个规矩） */
.msg-item.msg-self > .msg-body > .msg-invite-card {
  background: var(--bg-message-self);
  border-top-left-radius: 10px;
  border-top-right-radius: 2px;
}
.msg-invite-card-inner {
  display: flex;
  align-items: center;
  gap: 12px;
}
.msg-invite-card-icon {
  width: 48px;
  height: 48px;
  border-radius: 10px;
  object-fit: cover;
  flex-shrink: 0;
}
.msg-invite-card-body {
  min-width: 0;
}
.msg-invite-card-name {
  font-size: 15px;
  font-weight: 700;
  line-height: 1.35;
  color: var(--text-primary);
  word-break: break-word;
}
.msg-invite-card-desc {
  margin-top: 3px;
  font-size: 12px;
  font-weight: 400;
  color: var(--text-secondary);
  word-break: break-word;
}
.msg-invite-card-foot {
  display: flex;
  justify-content: flex-end;
}
.msg-invite-card-btn {
  padding: 5px 14px;
  border: 1px solid var(--accent);
  border-radius: 14px;
  background: transparent;
  color: var(--accent);
  font-size: 12px;
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}
.msg-invite-card-btn:hover {
  background: var(--accent);
  color: var(--text-on-self);
}

/* 豆包分享卡（aweType=6001）：正方形封面 + 标题，来源「豆包」紧贴在封面那一侧。
   封面在左（对方发的）/ 在右（我发的）靠 flex-direction 翻转，封面一律 object-fit: cover。 */
.msg-music-card {
  display: flex;
  flex-direction: row-reverse;
  align-items: center;
  gap: 10px;
  background: var(--card-bg);
  border-radius: 10px;
  border-top-left-radius: 2px;
  padding: 10px 12px;
  max-width: var(--share-card-width);
  cursor: pointer;
  transition: filter 0.15s;
}
.msg-item.msg-self > .msg-body > .msg-music-card {
  flex-direction: row;
  background: var(--bg-message-self);
  border-top-left-radius: 10px;
  border-top-right-radius: 2px;
}
.msg-music-card:hover {
  filter: brightness(1.1);
}
.msg-music-cover {
  width: 72px;
  height: 72px;
  border-radius: 6px;
  object-fit: cover;
  flex-shrink: 0;
}
.msg-music-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.msg-music-title {
  font-size: 13px;
  line-height: 1.4;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-word;
}
.msg-music-source {
  font-size: 11px;
  color: var(--text-muted);
  margin-top: auto;
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
  /* 调试用的 JSON 面板照旧铺满整列（别的子块是贴边对齐，它是按列宽撑开） */
  align-self: stretch;
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

/* ── 手机：一屏又窄又矮，东西挤在一起就分不清谁是谁 ──
   上下多留空白把消息分开，左右让出一点给正文；头部的间距也比桌面松一档。 */
@media (max-width: 768px) {
  .msg-header { padding: 12px 16px; gap: 6px 10px; }
  .msg-header h3 { font-size: 16px; }
  .msg-list { padding: 20px 16px 48px; }
  .msg-item { gap: 12px; }
  /* 不同发言人之间多留一点：比桌面默认的 --msg-group-gap 更明显 */
  .msg-item.group-start { margin-top: 20px; }
  .msg-jump-fab { right: 16px; }
  .msg-jump-top { bottom: 88px; }
  .msg-jump-bottom { bottom: 30px; }
}
</style>
