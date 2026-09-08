# astrbot_plugin_bilibili

这是一个为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 设计的多功能 Bilibili 插件。

## ✨ 功能特性

  - **Bilibili 视频解析**：自动识别消息中的 `BV` 号，并返回视频的详细信息。
  - **UP 主动态订阅**：
      - 支持订阅 `视频动态`、`图文动态` 和 `直播`。
      - 提供灵活的关键词和类型过滤。
      - 订阅成功/更新、订阅列表均以卡片图片展示，并标注过滤规则与图片转发开关状态。
      - 多图动态可选「原图合并转发」：推送卡片后以合并消息附带全部原图，跟随每条订阅的 `img_forward` 设置，可批量开关。
      - 内置 `bili_style_test` 样式测试指令，快速预览各类型推送卡片样式。
      - 默认每个 UP 主检测周期为 `300` 秒（5 分钟），任务间最小间隔为 `20` 秒，可根据需要在插件配置中修改。
   - **推荐番剧**
      - 试着对 LLM 说 `推荐一些催泪的番剧，2016年之后的`。
      - 支持类别、番剧起始年份、番剧结束年份、番剧季度（一月番等）
      - 需要支持函数调用的 LLM。如 gpt-4o-mini
   - **自然语言视频/番剧搜索**
      - 对 LLM 说 `帮我找一个XXX视频` 返回单个视频图文；`帮我找下XXX相关的视频` 返回视频列表图。视频结果均带发布时间信息。
      - 番剧推荐/搜索（如 `推荐一些催泪的番剧`）同样：单个返回图文，多个返回列表图。
      - 可通过配置项 `enable_video_image` / `enable_bangumi_image` 分别关闭视频/番剧的列表图片渲染，改用纯文本（单个结果始终返回图文）。
  - **QQ 小程序解析**：自动识别并解析 QQ 聊天中分享的 Bilibili 小程序，提取并返回直链。
  - 后续还会增加更多功能！！

![image](https://github.com/user-attachments/assets/972b2b99-b801-45cf-a882-6d841c9e8137)
## 🚀 安装

- 在插件市场下载
- 通过以下指令进行安装：

```shell
plugin i https://github.com/Soulter/astrbot_plugin_bilibili
```

## ⚙️ 配置

插件至少需要配置 `sessdata` 才能正常获取 Bilibili 数据。
以下是两种配置方式：
1. 参考 [此指南](https://nemo2011.github.io/bilibili-api/#/get-credential) 获取你的 `sessdata`。

<img width="1453" alt="image" src="https://github.com/user-attachments/assets/d5342767-8e5c-4222-81da-f1cdb4b30c95">

2. 使用`/bili_login`指令获取登录二维码，扫码登录后插件会自动获取并保存凭据。
此方式有利于解决[issue #58](https://github.com/Soulter/astrbot_plugin_bilibili/issues/58)所述412问题。不推荐使用主账号登录。

其他配置项（轮询间隔、图片渲染开关、渲染模板、代理、AI 摘要等）可在 AstrBot 的插件配置面板中调整。其中 `enable_video_image` / `enable_bangumi_image` 分别控制自然语言搜索的视频/番剧列表是否渲染为图片（默认开启）；`img_forward` 为多图原图合并转发的功能总开关（默认关闭，关闭时任何订阅都不转发原图）。


## 📖 使用说明

### 动态订阅指令

| 指令 | 参数 | 说明 | 别名 |
| :--- | :--- | :--- | :--- |
| **bili_sub** | `<B站UID> [过滤器...]` | 订阅指定 UP 主的动态。可以添加多个过滤器（以空格分隔）以排除不感兴趣的内容。订阅成功后返回一张展示 UP 主信息、「订阅成功」标题与过滤规则（过滤类型[英文]/过滤正则/直播@全体/推送@全体/图片转发）的卡片图片（不带链接）。再次订阅同一 UP 主更新过滤条件时，同样返回卡片图片，标题为「订阅更新」。**更新为局部生效**：只改动指令中给出的项，其余保持不变（详见下方"局部更新"）。 | `订阅动态` |
| **bili_sub_list** | (无) | 以卡片图片形式显示当前会话的所有订阅（含 UID、会话类型 Group/Private、过滤条件等），每条订阅卡片显示「图片转发」开关状态（显示最终生效结果）。 | `订阅列表` |
| **bili_sub_del** | `<B站UID>` | 删除当前会话中对指定 UP 主的订阅，成功后回复包含 UP 主名称与 UID，如 `删除成功：xxx (UID: 123456)`。 | `订阅删除` |
| **bili_global_del** | `<SID>` | **[管理员]** 删除指定会话的所有订阅。使用 `/sid` 指令可查看会话 UMO。建议以「」包裹SID，形如`/bili_global_del 「QQ 12345:FriendMessage:67890」` | `全局删除` |
| **bili_global_list** | (无) | **[管理员]** 以卡片图片形式查看所有会话的订阅情况。 | `全局列表` |
| **bili_global_sub** | `<SID> <B站UID> [过滤器...]` | **[管理员]** 为指定会话（UMO）添加对 UP 主的订阅。建议以「」包裹SID，形如`/bili_global_sub 「QQ 12345:FriendMessage:67890」 123456` | `全局订阅` |
| **bili_clear** | `[SID]` | **[管理员]** 清空所有订阅；带可选 `SID` 参数时仅清空指定会话的订阅。 | `清空订阅` |
| **bili_sub_test** | `<B站UID>` | **[管理员]** 测试订阅功能。返回指定 UID 最新一条未被过滤的动态并渲染为图片（不受已推送状态影响），不保存订阅信息。 | `订阅测试` |
| **bili_card_style** | `[样式名]` | **[管理员]** 切换动态卡片渲染样式。不带参数查看可用样式列表。 | `卡片样式` |
| **bili_img_forward** | `on\|off` | 批量开关**当前会话所有订阅**的图片转发（多图动态推送时以合并消息附带原图，默认关闭）。也可用 `/bili_sub <UID> img_forward=on\|off` 单独设置某条订阅。 | `图片转发` |
| **bili_img_forward_global** | `<UMO> on\|off` | **[管理员]** 批量开关**指定会话所有订阅**的图片转发。UMO 格式：`<平台名>:<消息类型>:<会话ID>`（平台名可能包含空格，需用「」包裹），可用 `/sid` 查看。 | `全局图片转发` |
| **bili_style_test** | `[样式名]` | **[管理员]** 渲染样式测试卡片：视频/图文/转发/直播开播/下播五张动态卡片，外加订阅成功、订阅更新、订阅列表卡片及订阅删除、图片转发状态的文本示例，用于快速验收新样式。 | `样式测试` |
| **bili_login** | (无) | **[管理员]** 获取二维码以登录。仅支持在私聊中触发。 | (无) |
| **bili_logout** | (无) | **[管理员]** 删除已保存的登录凭据，转而采用配置项中的sessdata（如果有） | (无) |

#### 参数说明

**1. 过滤器（过滤不感兴趣的内容）**

  - `forward`：过滤掉转发动态。
  - `lottery`：过滤掉互动抽奖动态。
  - `video`：过滤掉视频发布动态。
  - `article`：过滤掉专栏动态。
  - `draw`：过滤掉图文动态。
  - `live`：过滤掉直播动态。
  - `forward_lottery`：过滤掉转发的互动抽奖动态。
  - **正则表达式**：任何不属于上述保留关键字的字符串都将被视为正则表达式，用于过滤动态文本内容。

**2. 提醒选项（控制群聊 @ 提醒）**

  - `at_all`：开启后，在群聊中检测到该 UP 主发布动态或开播时，将尝试 `@全体成员`。
  - `live_atall`：开启后，仅在检测到该 UP 主**开播**时尝试 `@全体成员`（发布普通动态时不会）。
  - `at_sub`：设置后，会将当前发送指令的用户加入特定提醒列表。UP 主推送动态或开播时会专门 `@该用户`。
  - `unat_sub`：取消当前用户对该 UP 主的特定 `@` 提醒。

> **⚠️ 注意**：
> - 使用 `@全体成员` 相关的配置（`at_all` 和 `live_atall`）需要发送指令的用户拥有群管理员及以上权限，且机器人自身也必须具备群管理员权限，否则将降级为普通推送。
> - 为避免打扰群友，在检测到 UP 主**下播**时，不会触发任何形式的 `@` 提醒。

**示例**：
`/订阅动态 123456 lottery 关注`
`/bili_sub 123456 lottery 关注`
这条指令会订阅 UID 为 `123456` 的 UP 主，但会过滤掉**抽奖动态**以及动态内容中包含“**关注**”二字的动态。

> **提示**：该指令也用于更新已订阅 UP 主的过滤条件。**更新为局部生效**：只改动指令中给出的项，其余保持不变。

**3. 局部更新（v1.8.1 新增）**

更新已有订阅时，无需重写全部规则，只写要改的项：

- `type=video,draw`：覆盖类型过滤（逗号分隔）；`type=` 清空全部类型。
- `+type=video` / `-type=video`：追加 / 移除单个类型。
- **单类型开关**：`lottery=1`（或 true/on）单独加上某类型；`lottery=0`（或 false/off，留空视为关）单独移除。
- `regex=关键词1,关键词2`：覆盖正则关键词（逗号分隔）；`regex=` 清空；`+regex=xx` / `-regex=xx` 追加 / 移除。
- `live_atall=on|off`：显式开关“开播@全体”（不带此项则保持原值不变）。
- `img_forward=on|off|clear`：设置**本订阅**的多图原图转发（优先于会话级设置）；`clear` 恢复跟随会话/全局设置。裸写 `img_forward`（不带 `=`）等同于 `img_forward=on`。
- 旧的裸参数写法（如 `/bili_sub 123 video 抽奖`）仍然有效：整体覆盖类型与正则两个列表。

示例：
`/bili_sub 123456 +regex=新词 img_forward=on` —— 只追加一条关键词，并单独为本订阅开启图片转发，其余设置不动。
`/bili_sub 123456 lottery=0` —— 只把「抽奖」从过滤类型中移除。

## 适用平台/适配器

  - aiocqhttp
  - nakuru

## 常见问题

1. 渲染图片失败  
一般是公共接口不稳定性导致，详见[issue43](https://github.com/Soulter/astrbot_plugin_bilibili/issues/43)

2. 错误代码-352 / 412  
先查看以下issue中解决方案[issue34](https://github.com/Soulter/astrbot_plugin_bilibili/issues/34)、[issue58](https://github.com/Soulter/astrbot_plugin_bilibili/issues/58)、[issue72](https://github.com/Soulter/astrbot_plugin_bilibili/issues/72)

3. AstrBot更新到4.0版本后订阅失效  
UMO结构发生了变化，已为"全局列表"指令添加了具体订阅信息，使用该指令查看后重新订阅即可。  
简便的方法是进入data/plugin_data/astrbot_plugin_bilibili文件夹修改UMO的第一部分（使用"/sid"指令了解区别）。

4. 使用新渲染模板发不出图片  
由于图文动态布局采用了纵向布局，如果图片过长，受限于qq本身机制，需以文件形式发送。  
你很可能需要在AstrBot"配置文件-系统配置"配置"对外可达的回调接口地址"

5. 生成的图片被错误裁剪或有多余区域  
始终推荐[自部署](https://docs.astrbot.app/others/self-host-t2i.html)，并且由于t2i服务更新，推荐及时更新到最新的镜像。  
本插件会始终在合适时支持更新的版本。

## 模板开发

详见[PR#53](https://github.com/Soulter/astrbot_plugin_bilibili/pull/53)
```bash
# 启动UI开发模式
cd astrbot_plugin_bilibili
python dev_ui.py
```

[astrbot-t2i-playground](https://github.com/AstrBotDevs/astrbot-t2i-playground) 也可以帮助开发和调试模板。

## Contributors

<a href="https://github.com/soulter/astrbot_plugin_bilibili/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=soulter/astrbot_plugin_bilibili" />
</a>

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md)
