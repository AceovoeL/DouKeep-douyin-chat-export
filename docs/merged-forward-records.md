# 合并转发记录的格式与支持范围

针对 [issue #36](https://github.com/TeamBreakerr/douyin-chat-export/issues/36) 的第四项，当前浏览器支持识别 `aweType=13600`，显示卡片并展开已取得的正文，包括嵌套转发、图片、视频分享、用户名片及普通文本。

## 已验证的数据格式

2026-09-07 对既有归档进行只读检查，发现内嵌正文与仅摘要两类消息；issue 的第 8 张附图还提供了远端资源格式的线索：

| 字段 | 用途 |
| --- | --- |
| `title` | 卡片标题 |
| `list_content` | 预览摘要，包含 `msgid`、`nick_name`、`text`，不能据此认定正文完整 |
| `msg_ids` | 被转发的消息列表，包含 `msg_id`、`uid`、`awe_type`、`create_time` 等 |
| `inline_content` | 新格式携带的完整 MessageBody 列表，包含 `server_message_id`、`sender`、`content`、`create_time` 等 |
| `is_new_mr_share`、`new_mr_share_version` | 新格式标识 |
| `upload_key_list` | 资源列表，每项包含 `key`、`count`；支持数组或 JSON 编码数组，key 前缀为 `douyin-im-merge-share/v01/` |
| `skey` | 消息顶层的十六进制 AES 密钥，与 `upload_key_list` 同级 |
| `origin_conv_id`、`root_id`、`prev_id` | 旧格式保留的来源／转发链标识，不等同于消息正文 |

实际新格式样本的 `list_content` 只有 3 条，但 `msg_ids` 与 `inline_content` 均有 4 条；解析后已能展示全部 4 条。旧格式样本只有 ID 和摘要，不含正文，且原消息未在本地归档中找到。

详情解析在 Python 端进行，保留 64 位消息 ID 和用户 ID 的精度。优先使用本地原消息，以复用已下载媒体；没有本地原消息时读取 `inline_content`。仍缺少正文的条目保留摘要并标记 `detail_missing`，不会用摘要冒充完整内容。嵌套展开有循环检测、5 层深度及总计 1000 条解析上限。

## 远端补抓的研究结论

初期检查网页 IM 模块未找到正文获取调用。进一步在公开的 Android 客户端反编译代码中定位到了完整资源获取与解密流程：

1. [ChatRoomApi](https://github.com/cxxsheng/TiktokSource/blob/main/com/ss/android/ugc/aweme/im/business/chat/root/roomapi/ChatRoomApi.java) 定义两个 POST 接口：`/aweme/v1/im_communication/merge_msg_card/get/`（旧版正文）和 `/aweme/v1/im_communication/merge_msg_card/get_object_url/`（新版资源 URL）。
2. [ShareMergeDownloader](https://github.com/cxxsheng/TiktokSource/blob/main/com/ss/android/ugc/aweme/im/business/sharemergepage/util/ShareMergeDownloader.java) 对每个资源 key 请求 URL。[请求体定义](https://github.com/cxxsheng/TiktokSource/blob/main/X/C20950082n.java) 是 `{"uri": key, "access_chain": [...]}`；访问链可选，各项包含 `msg_id`、`conv_id`（会话 short ID）。响应成功条件为 `status_code == 0`，URL 在顶层 `url`。
3. [解密实现](https://github.com/cxxsheng/TiktokSource/blob/main/X/C445980HUb.java) 将顶层 `skey` 从十六进制转为字节，文件前 12 字节作为 nonce，其余字节作为 AES-GCM 密文与认证标签。没有额外 AAD。
4. [资源结构](https://github.com/cxxsheng/TiktokSource/blob/main/X/C503920Jil.java) 是 `{"Messages": [...]}`，正文对象为 [MergeMsgInfo](https://github.com/cxxsheng/TiktokSource/blob/main/com/ss/android/ugc/aweme/im/sdk/chat/model/MergeMsgInfo.java)，与已支持的内嵌正文结构一致。

2026-09-07 继续在已登录的抖音私信网页中验证，找到了可用的网页接口：

```text
POST https://www.douyin.com/aweme/v1/web/im_communication/merge_msg_card/get_object_url/?aid=6383&device_platform=webapp
Content-Type: application/json

{"uri":"douyin-im-merge-share/v01/...","access_chain":[{"msg_id":123,"conv_id":456}]}
```

- 复用现有 Playwright 页面里的 `fetch` 和网页登录态，不需要 Android SDK，也不需要向 Android 服务域名转发 Cookie。
- **网页成功响应没有 `status_code` 字段**，直接返回 `url`、`extra`、`log_pb`；参数错误会返回 `status_code: 5`。这与 Android 客户端的成功判断不同。
- 验证得到的签名资源 URL 位于 `p数字-aweme-im-merge-share-sign.byteimg.com`。资源下载不携带登录 Cookie，限制 HTTPS、已验证的 CDN 主机、单文件 16 MiB；不自动跟随重定向。
- 使用新发送的真实合并转发，完整跑通「网页读取原始消息 → 获取资源链接 → 下载 → AES-GCM 解密 → 解析 → 展示」。样本没有 `inline_content`，有 13 条索引；解密得到 13 条且 ID 全部匹配，其中包括文本、两条语音、两条表情图片和两条视频分享。实际浏览器界面无缺失提示，两个语音控件与两张分享卡片均正常生成。语音媒体链接返回 HTTP 200、文件为 M4A；测试用 Chromium 不支持 AAC，不能据此确认音频解码播放。
- 整个资源处理保留 Python 整数精度；访问链 JSON 在 Python 序列化后原样交给浏览器，避免 64 位 ID 经过 JS Number。

`extractor/forwarded.py` 实现网络请求、资源解密、分片排序与完整性校验。抓取会话结束后补全该会话带 `upload_key_list` 的归档，将完整正文写入 `raw_data.forwarded_bodies`，保留原始 `content_json`。已成功的记录直接复用；全量重抓时从临时消息备份复用内容一致的缓存。接口拒绝、超时、解密失败或条数不符均不会覆盖原始消息。详情接口优先使用本地原消息，其次使用内嵌或下载正文。

### 仍有限制的格式

旧版只有 `msg_ids`、`root_id`、`prev_id` 而没有资源 key 的卡片，暂时只能从本地原消息补齐。网页 `/aweme/v1/web/im_communication/merge_msg_card/get/` 实测 HTTP 404（`Unsupported path(Janus)`）；直接使用移动版路径则为 HTTP 504。公开源码中旧版请求 DTO 缺失，仍需独立研究。

网页 JS SDK 的通用 `getMessageByServerId` 也不能替代合并转发详情接口：它需要源会话上下文，真实旧版样本的来源会话不在当前账号会话列表中。不能据此承诺能取回所有旧版转发。

正文中的媒体继续复用现有展示逻辑与本地原消息；没有本地媒体时使用正文携带的预览／媒体链接，资源过期和抖音权限限制仍可能影响播放。此处的合并记录资源下载不等同于任意聊天文件附件下载。

## 搜索时间边界

`/api/search` 的 `start_time`、`end_time` 为 Unix 秒，采用开始包含、结束不包含的区间。界面按日期查找使用右侧连续月份日历，标出有消息的日期；点击日期定位至该日首条消息。图片／视频在同一侧栏内按日期分组为缩略图网格。`media_type=media` 同时查找图片与视频，`image` 和 `video` 分开查找；分享视频卡片仍属于分享类型。
