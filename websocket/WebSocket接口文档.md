# 内置机器人 WebSocket 接口文档

> 版本：按 2026-08-25 当前源码整理  
> 源码：`内置机器人.py`、`socketio_client.py`  
> 说明：本文描述的是客户端代码当前实际接受和发送的数据，不代表中转服务器或 SCUM 插件的完整服务端规范。

## 1. 协议总览

内置机器人中存在两套正在使用的实时通信协议，以及一套仍保留在源码中的旧协议。

| 通道 | 当前用途 | 地址 | 协议 |
| --- | --- | --- | --- |
| 手机/主机器人/副机器人消息总线 | 手机控制、玩家列表、主副机器人 RPC | `https://im3.vipscum.cn` | Socket.IO over WebSocket |
| 本地插件网关 | SCUM 插件命令、查询、实时事件 | `ws://<host>:<port>/ws?apiKey=<token>` | 原生 WebSocket，JSON 文本帧 |
| 旧消息总线 | 旧版兼容代码，当前 `start_websocket()` 不会启动它 | `wss://im2.vipscum.cn:2347` | 原生 WebSocket，JSON 文本帧 |

重要区别：`https://im3.vipscum.cn` 是 **Socket.IO 服务**，不能直接用普通 WebSocket 客户端按裸 JSON 协议连接。接入方应使用 Socket.IO 客户端。

## 1.1 Joker 网页的两条令牌链路

Joker 的 Linux 网页与 Windows 插件另有两套独立凭据，均不属于 im3 的 `key`：

| 凭据 | 用途 | 配置位置 |
| --- | --- | --- |
| 网页同步令牌 | 保护 Windows 主动发起的 HTTPS 配置同步 | Linux `JOKER_WEB_SYNC_TOKEN` 与 Windows `web_sync.sync_token` 必须相同 |
| 游戏 WebSocket 入口令牌 | 保护 Windows 连接 `/api/sync/v1/ws` 和后端游戏调用 | Linux 后台保存“游戏 WebSocket”时自动生成，加密下发为 Windows `web_sync.game_websocket_token` |

这两串令牌不能共用。im3 用户卡密只用于 `join_room`，与上述两条 Joker 通道均不同。

## 2. 主 Socket.IO 消息总线

### 2.1 连接和身份

服务地址：

```text
https://im3.vipscum.cn
```

连接参数：

| 参数 | 当前值/行为 |
| --- | --- |
| 使用传输 | `websocket` |
| 首次连接超时 | 10 秒 |
| 自动重连 | 开启，无限次数 |
| 重连延迟 | 1 秒开始，最大 16 秒；外层失败重试最大 30 秒 |

机器人身份类型：

| 身份 | `type` | `key` 来源 |
| --- | --- | --- |
| 主机器人 | `bot_main` | `secertKey`，源码沿用此拼写 |
| 副机器人 | `bot_child_<machine-guid>` | 主机器人配置中的 `BOT_main_Secret` |
| 手机/H5 调用方 | 通常为 `phone_chat` | 由消息中转服务和手机端约定 |

副机器人身份仅在以下条件同时成立时启用：

```text
botCurrentType == 2
BOT_main_type == "2"
BOT_main_Secret 非空
```

否则机器人按 `bot_main` 身份连接。

### 2.2 加入房间

Socket.IO 连接成功后，客户端发送事件 `join_room`：

```json
{
  "key": "服务器密钥或主机器人密钥",
  "type": "bot_main"
}
```

参数说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `key` | string | 是 | 房间密钥。不要写入日志、文档或前端公开代码 |
| `type` | string | 是 | 当前客户端身份，如 `bot_main`、`bot_child_<guid>` |

服务端应返回 `joined` 事件并包含非空 `room`。当前公开 im3 服务的实际行为是：
对任意符合格式的 `key` 都会创建房间，且普通 `joined` 回执不包含 `authorized`、
`authenticated`、`valid`、`key_valid`、`card_valid` 或 `auth` 等独立卡密授权字段。
因此后台传输层检测可以记录“已确认入房”，但用户卡密验证必须额外要求授权字段为布尔值 `true`；只有收到该字段才会保存卡密。当前公开协议不满足这一条件时，网页会报告“授权回执未提供”并拒绝保存。要安全地区分任意两个合法格式的卡密，必须由 im3 服务端在 `join_room` 中返回允许或拒绝结果；客户端无法从当前公开协议自行推断。

### 2.3 发送消息

发送 Socket.IO 事件名：

```text
send_message
```

事件数据：

```json
{
  "targetType": "bot_main",
  "content": {
    "actType": "sendMsgToGame",
    "content": "服务器广播内容"
  },
  "action": "send",
  "messageId": "1787654321000"
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `targetType` | string | 是 | 目标身份，如 `bot_main`、`phone_chat`、`bot_child_<guid>` |
| `content` | any | 是 | 业务消息。机器人接收时要求它是包含 `actType` 的对象 |
| `action` | string | 是 | 当前固定为 `send` |
| `messageId` | string | 是 | 默认使用当前毫秒时间。机器人不使用它做幂等或响应关联 |

### 2.4 接收消息

机器人监听 Socket.IO 事件：

```text
message
```

推荐的服务端下发结构：

```json
{
  "type": "phone_chat",
  "targetType": "bot_main",
  "content": {
    "actType": "phoneOnline"
  }
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `type` | string | 是 | 发送方身份；机器人会把它记录为 `fromType` |
| `targetType` | string | 是 | 接收方身份，推荐明确填写当前机器人的 `type` |
| `content` | object | 是 | 业务消息，必须包含受支持的 `actType` |

当前过滤条件为：

```text
data.type == 当前 ws_type
或
data.targetType == 当前 ws_type
```

正常接入应使用 `targetType` 指向接收者，并让 `type` 保持发送者身份。否则响应可能被发送到错误的 `fromType`。

## 3. 手机/H5 业务消息

业务消息位于 Socket.IO 外层的 `content` 中。当前支持以下 `actType`：

| `actType` | 方向 | 用途 |
| --- | --- | --- |
| `sendMsgToGame` | 手机 -> 主机器人 | 向游戏命令/广播队列写入文本 |
| `sendCmdToGame` | 手机 -> 主机器人 | 执行输入命令、机器人管理命令或玩家数据命令 |
| `getDataFromGame` | 手机 -> 主机器人 | 查询游戏数据，目前仅支持玩家列表 |
| `phoneOnline` | 手机 -> 主机器人 | 通知 H5/手机端在线 |
| `childSendMsgToMainBot` | 副机器人 -> 主机器人 | 心跳和 RPC 请求 |
| `mainSendMsgToChildBotToExcuCmd` | 主机器人 -> 副机器人 | 执行任务或返回 RPC 数据 |

未知 `actType` 会被忽略，不会返回协议错误。

### 3.1 `sendMsgToGame`

请求：

```json
{
  "actType": "sendMsgToGame",
  "content": "欢迎来到服务器"
}
```

`content` 支持：

| 类型 | 处理方式 |
| --- | --- |
| string | 直接加入 `botWaitToExcCmds` |
| object | 按 `msg`、`message`、`text`、`content` 的优先级取第一个存在的字段 |
| 其他类型 | 调用 `str()` 后加入队列 |

对象写法示例：

```json
{
  "actType": "sendMsgToGame",
  "content": {
    "message": "欢迎来到服务器"
  }
}
```

该接口没有结构化响应。文本进入游戏发送队列不等于已被游戏成功执行。

### 3.2 `phoneOnline`

请求：

```json
{
  "actType": "phoneOnline"
}
```

无需业务参数。机器人只把 `phoneIsOnline` 设置为 `true`。

### 3.3 `getDataFromGame`

当前唯一支持的请求：

```json
{
  "actType": "getDataFromGame",
  "content": "getPlayerListFromGame"
}
```

响应会发回请求消息的 `type`：

```json
{
  "type": "sendMsgToPhone",
  "dataType": "playerList",
  "data": [
    {
      "steamid": "7656119xxxxxxxxxx",
      "xiongb": 100,
      "vip_int": 20,
      "title_int": 10,
      "custom_title": "称号",
      "vip_level": "1",
      "title_level": "2",
      "weapon_card": [],
      "skill": [],
      "kill": 3,
      "dead": 2,
      "kd": "1.5",
      "vip_end_time": "2026-12-31",
      "pass": [],
      "wp_card_list": [],
      "coins": 50,
      "fragments": 8
    }
  ]
}
```

`data` 中每一项会保留 `getAllPlayerList()` 返回的原始玩家字段，并补充：

| 字段 | 说明 |
| --- | --- |
| `xiongb` | 熊币余额 |
| `vip_int` | VIP 积分 |
| `title_int` | 普通称号积分 |
| `custom_title` | 自定义称号 |
| `vip_level` | VIP 等级 |
| `title_level` | 普通称号等级 |
| `weapon_card` | 玩家个人礼品卡/武器卡数据 |
| `skill` | 玩家技能数据 |
| `kill` | 击杀数 |
| `dead` | 死亡数 |
| `kd` | KD |
| `vip_end_time` | VIP 到期时间，缺失时为 `无` |
| `pass` | 通行证数据 |
| `wp_card_list` | 玩家个人礼品卡列表；当前与 `weapon_card` 使用同一数据源 |
| `coins` | 金币余额 |
| `fragments` | 碎片余额 |

### 3.4 `sendCmdToGame`

通用结构：

```json
{
  "actType": "sendCmdToGame",
  "content": {
    "cmdType": "inputCmd",
    "cmdCont": "#ListPlayers"
  }
}
```

#### 3.4.1 `inputCmd`

`cmdCont` 必须为要加入游戏执行队列的字符串：

```json
{
  "actType": "sendCmdToGame",
  "content": {
    "cmdType": "inputCmd",
    "cmdCont": "#ListPlayers"
  }
}
```

#### 3.4.2 `botCmd`

结构：

```json
{
  "actType": "sendCmdToGame",
  "content": {
    "cmdType": "botCmd",
    "cmdCont": {
      "type": "restartBot"
    }
  }
}
```

支持的 `type`：

| `type` | 作用 |
| --- | --- |
| `restartBot` | 重启主机器人，并向在线副机器人发送重启命令 |
| `initTitle` | 重新更新全部玩家名称/称号 |
| `resetHongbao` | 重置红包数据 |
| `cleanDirty` | 执行垃圾清理 |
| `refreshBan` | 拉取封禁列表并执行封禁 |

#### 3.4.3 `dataCmd`

结构：

```json
{
  "actType": "sendCmdToGame",
  "content": {
    "cmdType": "dataCmd",
    "cmdCont": {
      "type": "addCoins",
      "steamid": "7656119xxxxxxxxxx",
      "value": "100"
    }
  }
}
```

`cmdCont` 的三个字段在当前实现中都会被直接索引，因此 **即使某个命令不使用 `value`，也必须传入该字段**。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `type` | string | 是 | 数据命令类型 |
| `steamid` | string | 是 | 目标玩家 SteamID |
| `value` | string/number | 是 | 数量、等级、日期、目标玩家或命令附加值；不使用时传空字符串 |

支持的 `type`：

| `type` | `value` 含义 | 当前动作 |
| --- | --- | --- |
| `addXiongb` | 增加数量 | 增加熊币 |
| `redXiongb` | 扣减数量 | 扣减熊币 |
| `addCoins` | 增加数量 | 增加金币 |
| `redCoins` | 扣减数量 | 扣减金币 |
| `addFragments` | 增加数量 | 增加碎片 |
| `redFragments` | 扣减数量 | 扣减碎片 |
| `setPlayer8555` | 未使用，传 `""` | 设置玩家四维为 8/5/5/5 |
| `changeTitleLevel` | 目标称号等级 | 调用配置中的 `ACMD_changeTitleLevel` |
| `resetTitleLevel` | 未使用，传 `""` | 调用 `ACMD_resetTitle` |
| `changeVIPLevel` | 目标 VIP 等级 | 调用 `ACMD_changeVipLevel` |
| `givePlayerPass` | 通行证附加值 | 调用 `ACMD_adminGivePass` |
| `givePlayerPersonGiftCard` | 礼品卡标识/值 | 调用 `ACMD_setGiftCard` |
| `ungivePlayerPersonGiftCard` | 礼品卡标识/值 | 调用 `ACMD_cancelGiftCard` |
| `cancelPlayerPass` | 未使用，传 `""` | 调用 `ACMD_adminCancelPass` |
| `resetVIPLevel` | 未使用，传 `""` | 调用 `ACMD_resetVip` |
| `changeVipEndTime` | 新的 VIP 到期时间或配置命令要求的值 | 调用 `ACMD_ChangeVipEndTime` |
| `setCustomTitle` | 自定义称号 | 调用 `ACMD_setCustomTitle` |
| `resetCustomTitle` | 未使用，传 `""` | 调用 `ACMD_cancelCustomTitle` |
| `setGodTime` | 管理员时长/配置命令要求的值 | 调用 `ACMD_setAdmin` |
| `setCarInsur` | 车险附加值 | 调用 `carInsurAdminKeyword` |
| `transToPlayer` | 传送目标玩家标识 | 入队 `#TeleportTo <value> <steamid>` |

处理结束后，机器人会向游戏文本队列加入：

```text
小程序管理命令执行成功
```

这条文本会在未知 `type` 或内部命令尚未真正完成时也被加入，不能作为可靠的执行确认。

## 4. 主机器人与副机器人协议

### 4.1 副机器人心跳

副机器人发送给 `bot_main`：

```json
{
  "actType": "childSendMsgToMainBot",
  "content": {
    "cmdType": "childHeartToMainBot",
    "cmdCont": {
      "childBotKey": "bot_child_<machine-guid>",
      "currentTime": 1787654321.123,
      "isWorking": "0"
    },
    "currentTime": 1787654321.123,
    "childBotKey": "bot_child_<machine-guid>"
  }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `childBotKey` | string | 副机器人身份，格式为 `bot_child_<guid>` |
| `currentTime` | number | Unix 时间戳，秒；主机器人当前不校验该值 |
| `isWorking` | string | `0` 表示空闲，其他值表示工作中 |

主机器人以收到消息的本地时间更新在线状态。超过 20 秒未收到心跳的副机器人会被判定离线。

心跳响应使用普通 RPC 响应结构，但 `taskType` 和 `taskId` 为空，`taskRespData` 为 `success`。

### 4.2 主机器人下发执行任务

发送目标：具体的 `bot_child_<guid>`。

```json
{
  "actType": "mainSendMsgToChildBotToExcuCmd",
  "content": {
    "cmdType": "mainToChildToExcuCmd",
    "cmdCont": [
      {
        "id": "7656119xxxxxxxxxx",
        "cmdCont": "@查询",
        "name": "玩家名",
        "time": "2026-08-25 12:00:00"
      }
    ]
  }
}
```

`cmdCont` 的特殊值：

| 值 | 作用 |
| --- | --- |
| `"mainToChildRestart"` | 重启副机器人 |
| array | 追加到副机器人的 `currentBotNeedExcuCmd` 执行队列 |

除重启字符串外，`cmdCont` 应为数组；传单个对象或普通字符串可能触发类型错误。

### 4.3 副机器人 RPC 请求

副机器人请求主机器人数据：

```json
{
  "actType": "childSendMsgToMainBot",
  "content": {
    "cmdType": "getDataFromMainBot",
    "cmdCont": {
      "taskType": "getPlayerUserData",
      "taskId": 101,
      "taskValue": "7656119xxxxxxxxxx"
    },
    "currentTime": 1787654321.123,
    "childBotKey": "bot_child_<machine-guid>"
  }
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `taskType` | string | 是 | RPC 方法名，大小写敏感 |
| `taskId` | number/string | 是 | 请求关联 ID；响应原样返回 |
| `taskValue` | any | 是 | 方法参数。无参数任务也建议传空字符串 |
| 外层 `childBotKey` | string | 建议 | 用于更新该副机器人的最后在线时间 |

当前副机器人调用封装会重新分配独立递增的 `taskId`，并等待最多 10 秒。超时返回空字符串。

### 4.4 RPC 响应

主机器人发送到请求副机器人：

```json
{
  "actType": "mainSendMsgToChildBotToExcuCmd",
  "content": {
    "cmdType": "mainToChildToRespData",
    "cmdCont": {
      "taskType": "getPlayerUserData",
      "taskId": 101,
      "taskRespData": []
    }
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `taskType` | 与请求一致 |
| `taskId` | 与请求一致，用于唤醒对应等待任务 |
| `taskRespData` | 方法返回值；可能是对象、数组、数字、布尔值、字符串或 `null` |

### 4.5 RPC 方法表

以下名称中的 `fatch` 是现有协议拼写，接入时不能改成 `fetch`。

#### 玩家、卡密和货币

| `taskType` | `taskValue` | 返回/说明 |
| --- | --- | --- |
| `checkAndRegisterCommandUseLimit` | `{command, steamId?, playerName?, requestKey}` | 次数限制判定对象；`requestKey` 必填，`steamId` 为空时尝试按 `playerName` 解析 |
| `getGiftCardData` | 卡密字符串 | 卡密记录数组 |
| `getPlayerUserData` | SteamID 字符串 | `user_list` 玩家记录数组 |
| `insertDataIntoUser` | 玩家对象，见下表 | 无业务返回，默认响应 `success` |
| `updateDataToUser` | 至少包含 `steam_id` 的更新对象 | 无业务返回，字段名直接映射数据库列 |
| `updateGiftCardData` | `{userSteamId, userName, cardId}` | 把卡密标记为已使用 |
| `fhbDeductLocalCurrency` | `{steamId, priceType, amount, transactionKey}` | 复活币流程的本地货币扣减结果 |
| `fhbChangeLocalCurrency` | `{steamId, priceType, amount, direction, transactionKey, relatedTransactionKey}` | 本地货币增减结果 |
| `fhbRecordUsage` | `{steamId, playerName, requestKey, status?, countAsUse?}` | 使用记录结果；`status` 默认 `success`，`countAsUse` 默认 `true` |
| `fatchUserKdRankList` | SteamID 字符串 | `{rankNum, KDStr}` |

`insertDataIntoUser.taskValue` 当前直接读取以下字段：

```json
{
  "user_name": "玩家名",
  "nick_name": "显示称号",
  "amount": "0",
  "reg_time": "2026-08-25 12:00:00",
  "total_time": "0",
  "last_log_time": "2026-08-25 12:00:00",
  "vip_level": "0",
  "vip_end_time": "0",
  "integral": "0",
  "steam_id": "7656119xxxxxxxxxx",
  "normal_integral": "0",
  "normal_vip_level": "0"
}
```

#### 礼包领取记录

| `taskType` | `taskValue` | 返回/说明 |
| --- | --- | --- |
| `insertDataIntoSendedGift` | `&&&&&` 分隔字符串 | 写入领取记录 |
| `insertToSendedGiftData` | `&&&&&` 分隔字符串 | 更新主机器人内存领取缓存 |
| `fatchSendedDataByUserByTotal` | `{keyword, usercode}` | 指定用户、指定关键词的全部记录 |
| `fatchSendedDataByAllByTotal` | `{localtime, keyword}` | 指定日期、指定关键词的全部用户记录 |
| `fatchSendedDataByUserByDay` | `{localtime, keyword, usercode}` | 指定日期和用户的记录 |
| `getSquadGiftClaimCounts` | `{keyword, localtime}` | `{ok, total, day}` |

分隔字符串当前按以下位置读取：

```text
server_time&&&&&local_time&&&&&keyword&&&&&username&&&&&usercode&&&&&steamid
```

部分旧调用会产生 7 段数据，此时 `insertDataIntoSendedGift` 使用第 7 段作为 SteamID。

#### 大喇叭

| `taskType` | `taskValue` | 返回/说明 |
| --- | --- | --- |
| `getDlbNum` | SteamID 字符串 | 大喇叭余额整数 |
| `addDlbNum` | `{id: SteamID, num: 数量}` | 增加余额 |
| `reduceDlbNum` | `{id: SteamID, num: 数量}` | 减少余额 |

#### 车险保单

| `taskType` | `taskValue` | 返回/说明 |
| --- | --- | --- |
| `getCarInsurPolicyBuyCarid` | 车辆 ID | 按 `last_car_id` 查询 |
| `getCarInsurPolicyBySteamid` | SteamID | 按玩家查询 |
| `getCarInsurPolicyTotalList` | 空字符串 | 全部保单 |
| `createCarInsurPolicy` | 10 项数组，见下方顺序 | 新建保单 |
| `updateCarInsurPolicy` | 包含 `insur_id` 的字段对象 | 按保单 ID 更新，键名直接映射数据库列 |

`createCarInsurPolicy.taskValue` 数组顺序：

```text
[player_name, player_steamid, insurd_time, end_insur_time,
 last_car_id, car_type, get_time_space, total_max, day_max, is_destory]
```

#### 车险领取记录

| `taskType` | `taskValue` | 返回/说明 |
| --- | --- | --- |
| `createCarInsurGetLog` | 10 项数组，见下方顺序 | 新建领取记录 |
| `getCarInsurUserGetListByInsurId` | 保单 ID | 按保单查询领取记录 |
| `getCarInsurUserGetListBySteamId` | SteamID | 按玩家查询领取记录 |

`createCarInsurGetLog.taskValue` 数组顺序：

```text
[player_name, player_steamid, insur_id, get_time, car_type,
 get_location, pre_car_id, pre_car_owner, cur_car_id, is_destoryed]
```

#### 签到

| `taskType` | `taskValue` | 返回/说明 |
| --- | --- | --- |
| `createSigninLog` | `[player_name, player_steamid, sign_date, sign_time]` | 新建签到记录 |
| `getSigninLogBySteamidAndDate` | `{id: SteamID, date: 日期}` | 按玩家和日期查询 |
| `getSigninLogBySteamid` | SteamID | 按玩家查询 |
| `deleteSigninLogById` | SteamID | 删除该玩家签到记录；名称中的 `Id` 实际指 SteamID |
| `getTotalSigninLogList` | 空字符串 | 设计为查询全部记录；当前实现存在缺陷，见第 8 节 |

#### 悬赏

| `taskType` | `taskValue` | 返回/说明 |
| --- | --- | --- |
| `createXuanshangLog` | 12 项数组，见下方顺序 | 新建悬赏；副机器人封装当前存在任务名错误 |
| `updateXuanshangLog` | 包含 `id` 的字段对象 | 更新悬赏，键名直接映射数据库列 |
| `deleteXuanshangLog` | 悬赏记录 ID | 删除记录 |
| `selectXuanshangLogById` | SteamID | 查询作为发布者或目标的记录 |
| `selectXuanshangLogByTargetId` | SteamID | 查询未完成且目标为该玩家的记录 |
| `selectXuanshangSelfLogById` | SteamID | 查询该玩家发布且未完成的记录 |
| `selectXuanshangBangList` | 空字符串 | 未完成悬赏榜；当前实现存在缺陷 |
| `selectAllXuanshangLog` | 空字符串 | 全部悬赏；当前实现存在缺陷 |

`createXuanshangLog.taskValue` 数组顺序：

```text
[req_name, req_steamid, tar_name, tar_steamid,
 create_time, end_time, xs_amount, xs_amount_type,
 finisher_name, finisher_steamid, finish_time, finish_state]
```

#### 白名单

| `taskType` | `taskValue` | 返回/说明 |
| --- | --- | --- |
| `addWhiteList` | `[player_name, player_steamid, start_time, end_time, status]` | 新增白名单 |
| `getWhiteListUserGetListBySteamId` | SteamID | 按玩家查询 |
| `getWhiteListUserGetListByStatus` | 状态值 | 按状态查询 |
| `getWhiteListUserGetListBySteamIdAndStatus` | `{id: SteamID, status}` | 按玩家和状态查询 |
| `updateWhiteList` | 包含 `player_steamid` 的字段对象 | 更新白名单，键名直接映射数据库列 |

#### 自定义称号池

| `taskType` | `taskValue` | 返回/说明 |
| --- | --- | --- |
| `titlePoolGetPlayerTotal` | SteamID | 返回去重后的称号字符串数组 |
| `titlePoolAddTitle` | `{id: SteamID, title: string或array}` | 增加称号，可一次传多个 |
| `titlePoolDeleteTitle` | `{id: SteamID, title: string}` | 删除一个称号 |
| `titlePoolClearTitle` | SteamID | 清空称号池 |

## 5. 本地插件 WebSocket 网关

### 5.1 地址发现和认证

连接模板：

```text
ws://<host>:<port>/ws?apiKey=<URL编码后的控制令牌>
```

当前代码只构建 `ws://`，没有构建 `wss://`。该通道应限制在本机或受信任局域网中。

主机候选来源：

```text
127.0.0.1
localhost
pluginHost
pluginIp
pluginGatewayHost
pluginGatewayIp
ftpIp
serverIp
ip
publicIp
```

端口候选来源：

```text
pluginPort, webPort, oxygenPort, pluginGatewayPort,
gamePort, serverPort, port, ftpGamePort
```

固定回退端口：

```text
18080, 7011, 7088, 7000, 7021, 8080, 7777, 7776
```

每个候选端口还会追加 `端口 - 1`。代码先通过 HTTP `GET /ping` 探测可用网关，再连接 WebSocket。

连接行为：

| 参数 | 当前值 |
| --- | --- |
| `ping_interval` | 10 秒 |
| `ping_timeout` | 10 秒 |
| `close_timeout` | 3 秒 |
| 最大消息 | 4 MiB |
| 断线重连 | 2 秒后重试，并清除网关发现缓存 |

同一令牌用于 HTTP 回退请求时，会放入：

```http
X-API-Key: <token>
Authorization: <token>
```

### 5.2 请求帧

原生 WebSocket 文本帧：

```json
{
  "id": "op-0123456789abcdef0123456789abcdef",
  "method": "POST",
  "path": "/broadcast",
  "body": "{\"text\":\"服务器公告\",\"color\":\"Yellow\",\"requestKey\":\"req-1\"}"
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | `op-` 表示等待响应，`ff-` 表示 fire-and-forget |
| `method` | string | 是 | 当前使用 `GET` 或 `POST` |
| `path` | string | 是 | 包含查询字符串的相对路径 |
| `body` | string | 是 | JSON 序列化后的字符串；GET/无请求体时为空字符串 |

注意：`body` 不是嵌套 JSON 对象，而是 **JSON 字符串**。服务端需要再做一次 JSON 解析。

### 5.3 响应帧

推荐响应：

```json
{
  "id": "op-0123456789abcdef0123456789abcdef",
  "status": 200,
  "body": "{\"ok\":true,\"data\":{}}"
}
```

响应关联 ID 可使用以下任一字段：

```text
id
requestId
requestID
correlationId
correlationID
```

服务端应始终原样回传请求 ID。没有关联 ID 时，客户端只会在“当前恰好只有一个等待请求”时把响应配给该请求，并发下不可靠。

`body` 可以是对象，也可以是 JSON 字符串。客户端会自动解开顶层 `body`。

当前成功判定兼容以下形式：

```text
ok == true
success == true
sent == true
duplicate == true 且没有 error
status/statusCode 在 200..299 且没有 error
嵌套 body/result/data/items/results 可递归判定成功
```

`ok == false`、`success == false` 或 HTTP 状态大于等于 400 会判定失败。

### 5.4 超时和幂等

默认请求超时为 8 秒，各业务可覆盖为 4 到 300 秒。

| 模式 | `id` 前缀 | 行为 |
| --- | --- | --- |
| 等待响应 | `op-` | 等待对应 ID 的响应；超时后返回“已发送但未回包”的内部结果 |
| Fire-and-forget | `ff-` | 非 GET 请求发送成功后立即返回，不等待响应 |

对于插件模式下的 POST 请求，直连 WebSocket 不可用时不会自动改走 HTTP，以避免重复执行管理命令。请求已经提交但响应不明确时也不会重试。需要幂等的命令应由服务端按 `requestKey` 去重。

## 6. 插件网关端点

本节列出 `内置机器人.py` 当前通过插件请求层调用的端点。

### 6.1 查询端点

| 方法和路径 | 参数 | 客户端使用的返回字段 |
| --- | --- | --- |
| `GET /server-log-lines?kind=<kind>&limit=<n>` | `kind` 日志类别，`limit` 最大行数 | `{ok, logs: string[]}` |
| `GET /audit-logs?limit=<n>` | `limit` 最大条数 | `{ok, logs: any[]}` |
| `GET /admin-command-result?requestKey=<key>` | 管理命令请求键 | 命令结果；未找到时常见 `error=result_not_found` |
| `GET /server-basic` | 无 | 服务器基础数据；当前读取 `timeOfDay` |
| `GET /players?detail=basic|full&steamId=<id>` | `detail` 默认 `basic`；`steamId` 可选 | `{players:[...]}` 或 `{data:[...]}` |
| `GET /server-flags` | 无 | `{ok:true, items:[...]}` |
| `GET /server-vehicles` | 无 | `{ok:true, items:[...]}` |
| `GET /server-config-file?name=<name>` | 配置文件名 | `{ok, content, sha256}` |
| `GET /player-stats?steamId=<id>` | SteamID | 玩家四维、技能和相关统计数据 |

`/server-squads` 和 `/player-squad-flag` 在当前源码中被刻意放到独立 HTTP 连接执行，避免慢查询占用命令/事件 WebSocket，因此不属于当前 WebSocket 调用清单。

### 6.2 `/run-command` 和 `/give-item`

请求体：

```json
{
  "mode": "command",
  "command": "#ListPlayers",
  "commands": ["#ListPlayers"],
  "target": "global",
  "steamId": "",
  "timeoutSeconds": 15,
  "bypassPluginCompat": true,
  "requestKey": "唯一请求键"
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `mode` | string | 建议 | 当前固定为 `command`；部分内部调用省略 |
| `command` | string | 是 | 第一条或唯一命令 |
| `commands` | string[] | 是 | 顺序执行的命令数组 |
| `target` | string | 是 | `global` 或 `player` |
| `timeoutSeconds` | number | 是 | 插件内部命令完成窗口 |
| `bypassPluginCompat` | boolean | 是 | 当前固定为 `true` |
| `requestKey` | string | 是 | 幂等、审计和后续结果查询键 |

当 `target == "player"` 时，当前客户端会同时发送以下同值字段以兼容不同插件版本：

```json
{
  "steamId": "7656119xxxxxxxxxx",
  "targetSteamId": "7656119xxxxxxxxxx",
  "playerSteamId": "7656119xxxxxxxxxx",
  "playerId": "7656119xxxxxxxxxx",
  "userId": "7656119xxxxxxxxxx"
}
```

`/give-item` 与 `/run-command` 使用同类请求体；批量发货时 `commands` 可包含多条命令，并要求等待完整响应。

### 6.3 `/broadcast`

```json
{
  "text": "服务器公告",
  "color": "Yellow",
  "requestKey": "唯一请求键"
}
```

| 字段 | 说明 |
| --- | --- |
| `text` | 广播内容 |
| `color` | 当前机器人固定传 `Yellow` |
| `requestKey` | 幂等和审计键 |

### 6.4 `/player-revive`

预检查：

```json
{
  "steamId": "7656119xxxxxxxxxx",
  "dryRun": true
}
```

执行复活：

```json
{
  "steamId": "7656119xxxxxxxxxx",
  "dryRun": false,
  "confirm": "REVIVE",
  "requestKey": "唯一请求键",
  "deathGeneration": 123
}
```

`deathGeneration` 用于绑定本次死亡状态，避免旧复活请求误作用于新的死亡事件。

### 6.5 `/server-config-file`

写入请求：

```json
{
  "name": "ServerSettings.ini",
  "content": "完整文件内容",
  "expectedSha256": "读取时得到的sha256",
  "dryRun": false,
  "confirm": "WRITE"
}
```

| 字段 | 说明 |
| --- | --- |
| `name` | 允许操作的服务器配置文件名 |
| `content` | 完整新内容 |
| `expectedSha256` | 乐观锁；用于避免覆盖别人刚刚写入的版本 |
| `dryRun` | 当前执行写入时为 `false` |
| `confirm` | 当前固定为 `WRITE` |

成功响应当前读取 `newSha256`。

### 6.6 `/player-stats-op`

四维修改：

```json
{
  "steamId": "7656119xxxxxxxxxx",
  "strength": 8.0,
  "constitution": 5.0,
  "dexterity": 5.0,
  "intelligence": 5.0,
  "dryRun": false,
  "confirm": "UPDATE_PLAYER_STATS"
}
```

技能修改：

```json
{
  "steamId": "7656119xxxxxxxxxx",
  "skillName": "RiflesSkill",
  "skillLevel": 4,
  "skillExperience": 9999999,
  "dryRun": false,
  "confirm": "UPDATE_PLAYER_STATS"
}
```

客户端把 `ok == true` 或 `partial == true` 都视为本次调用可接受。

### 6.7 `/actor-op`

通用确认字段：

```json
{
  "dryRun": false,
  "confirm": "SPAWN_MOVE_DESTROY_ACTOR"
}
```

支持的当前操作：

| `operation` | 主要参数 |
| --- | --- |
| `spawn` | `type`, `customAssetPath`, `x`, `y`, `z`, `pitch`, `yaw`, `roll` |
| `get-location` | `actorId`, `actorPtr` |
| `move` | `actorId`, `actorPtr`, `direction`, `meters`, `distanceUnits`, `x`, `y`, `z` |
| `destroy` | `actorId`, `actorPtr` |

生成示例：

```json
{
  "operation": "spawn",
  "dryRun": false,
  "confirm": "SPAWN_MOVE_DESTROY_ACTOR",
  "type": "Sentry",
  "customAssetPath": "",
  "x": 100.0,
  "y": 200.0,
  "z": 300.0,
  "pitch": 0,
  "yaw": 0,
  "roll": 0
}
```

### 6.8 `/robot-control-ack`

机器人处理 `robot-control` 事件后发送：

```json
{
  "action": "restart",
  "source": "plugin",
  "eventId": "evt-123",
  "running": true,
  "starting": false,
  "mainThreadId": 10,
  "success": true,
  "message": "restart completed"
}
```

## 7. 插件实时事件

### 7.1 事件外层

```json
{
  "type": "event",
  "eventName": "player-chat",
  "eventId": "evt-123",
  "payload": {}
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `type` | string | 是 | 固定为 `event` |
| `eventName` | string | 是 | 事件名 |
| `eventId` | string | 强烈建议 | 去重键，可放在外层或 `payload.eventId` |
| `payload` | object | 是 | 事件数据 |

事件 ID 还会从外层 `id`、`payload.id`、`payload.payload.eventId`、`payload.payload.id` 中尝试读取。机器人最多保留约 1000 个已处理 ID，超过后移除较早的 300 个。

### 7.2 玩家聊天

接受的事件名：

```text
player-chat
player-message
chat
chat-message
playerchat
playermessage
```

名称规范化后同时包含 `chat` 和 `player` 也会被识别。

推荐负载：

```json
{
  "type": "event",
  "eventName": "player-chat",
  "eventId": "chat-123",
  "payload": {
    "message": "@查询",
    "time": "2026-08-25T12:00:00+08:00",
    "channel": "Global",
    "player": {
      "steamId": "7656119xxxxxxxxxx",
      "fakeName": "玩家名"
    }
  }
}
```

字段兼容：

| 语义 | 可接受字段，按优先级 |
| --- | --- |
| 消息 | `message`, `text`, `content` |
| SteamID | `player.steamId`, `player.userId`, `payload.steamId`, `payload.userId` |
| 玩家名 | `player.fakeName`, `payload.fakeName`, `player.playerName`, `payload.playerName`, `player.name`, `payload.name` |
| 时间 | `time`，缺失时使用机器人当前时间 |
| 频道 | `channel`, `channelName`, `channelType`, `chatChannel`, `chatChannelName`, `chatType`, `messageType`, `type`, `scope`, `mode`；也会检查嵌套 `chat/messageInfo/messageData/data/payload` |

频道会归一化为：

```text
Global
Local
Squad
Admin
```

无法识别时默认为 `Global`。有 `eventId` 时按事件 ID 去重；没有时按 `steamId|time|message` 的 MD5 去重。

### 7.3 玩家上线/下线

事件名：

```text
player-connected
player-disconnected
```

推荐负载：

```json
{
  "time": "2026-08-25T12:00:00+08:00",
  "player": {
    "steamId": "7656119xxxxxxxxxx",
    "fakeName": "玩家名"
  }
}
```

玩家基础字段从以下位置读取：

| 语义 | 可接受字段 |
| --- | --- |
| SteamID | `player.steamId`, `player.userId`, `payload.steamId` |
| 玩家名 | `player.fakeName`, `player.name`, `payload.name` |

下线事件会计算本次在线分钟数，并触发在线玩家列表刷新。

### 7.4 玩家死亡

事件名：

```text
player-death
```

推荐负载：

```json
{
  "time": "2026-08-25T12:00:00+08:00",
  "player": {
    "steamId": "7656119xxxxxxxxxx",
    "fakeName": "死亡玩家"
  },
  "killerId": "7656119yyyyyyyyyy",
  "killerName": "击杀玩家",
  "reason": "Weapon_AK47",
  "distance": 80.5
}
```

| 字段 | 说明 |
| --- | --- |
| `player`/`steamId` | 死亡玩家信息 |
| `killerId` 或 `killerSteamId` | 击杀者 SteamID；用于最近死亡缓存时也接受 `killer.steamId/userId` |
| `killerName` | 击杀者名称；最近死亡缓存也接受 `killer.fakeName/name` |
| `reason` 或 `deadType` | 死亡原因/武器 |
| `distance` | 距离，米 |
| `time` | 事件时间 |

为保证击杀播报兼容，建议始终提供顶层 `killerId` 和 `killerName`。

### 7.5 开锁事件

事件名：

```text
player-lockpick
```

推荐负载：

```json
{
  "time": "2026-08-25T12:00:00+08:00",
  "player": {
    "steamId": "7656119xxxxxxxxxx",
    "fakeName": "开锁玩家",
    "location": "X=1 Y=2 Z=3"
  },
  "result": "success",
  "failCount": 2,
  "target": "BP_Lock_123",
  "ownerSteamId": "7656119yyyyyyyyyy",
  "ownerName": "锁主人",
  "location": "X=1 Y=2 Z=3"
}
```

| 字段 | 说明 |
| --- | --- |
| `result` | `1`, `true`, `success`, `succeeded` 视为成功，其他值视为失败 |
| `failCount` | 失败次数，默认 0 |
| `target` 或 `targetId` | 被开锁对象 |
| `ownerSteamId` | 所有者 SteamID |
| `ownerName` | 所有者名称 |
| `location` | 坐标；优先使用 `player.location` |

### 7.6 机器人控制

事件名：

```text
robot-control
```

负载：

```json
{
  "action": "restart",
  "source": "control-panel"
}
```

动作别名：

| 归一化动作 | 接受的 `action` |
| --- | --- |
| `start` | `start`, `start-robot`, `run` |
| `stop` | `stop`, `end`, `close`, `stop-robot`, `end-robot` |
| `restart` | `restart`, `restart-robot`, `reboot` |
| `reload-config` | `reload`, `reload-config`, `reload-options`, `read-config`, `read-options` |
| `status` | `status`, `state` |

处理后通过 `POST /robot-control-ack` 回传状态。进程启动 30 秒内，来源为 `rcon-console` 的 `stop` 会被当作过期事件忽略，但仍返回成功 ACK。

## 8. 当前实现缺陷和对接注意事项

以下内容是按当前源码确认的行为，接入方不应把它们当成正常协议能力：

1. `dataCmd` 中存在第二个重复的 `resetCustomTitle` 分支。该分支原本看起来应调用 `ACMD_cancelAdmin`，但永远不可达；协议中也没有可用的 `resetGodTime` 分支。
2. `getUserGiftLogsByKey` 会由副机器人发起，但主机器人 RPC 分发器没有对应处理分支，当前会收到默认的 `success`，而不是查询结果。
3. `createXuanshangLog()` 的副机器人封装错误地发送 `taskType=deleteXuanshangLog`。外部直接发送正确的 `createXuanshangLog` 可进入主分发器，但现有内部调用路径不正确。
4. `getTotalSigninLogList` 的主分发器给无参数函数多传了一个 `taskValue`，当前会触发 `TypeError`。
5. `selectXuanshangBangList` 和 `selectAllXuanshangLog` 同样给无参数函数多传了 `taskValue`，当前会触发 `TypeError`。
6. 未识别的 RPC `taskType` 默认仍会响应 `taskRespData="success"`，不能据此判断任务存在或执行成功。
7. 手机 `sendCmdToGame/dataCmd` 无论是否匹配到有效命令，结尾都可能加入“执行成功”文本，没有可靠 ACK。
8. `updateDataToUser`、`updateCarInsurPolicy`、`updateXuanshangLog`、`updateWhiteList` 将对象键直接拼入 SQL 列名。只应允许可信内部调用，并在服务边界做字段白名单验证。
9. Socket.IO 的 `messageId` 当前不参与去重。会产生资产或余额变化的调用必须在业务层增加幂等键。
10. 插件 WebSocket 令牌放在 URL 查询参数中，可能出现在代理访问日志。不得把完整连接 URL 写入公开日志。
11. 插件事件应始终携带稳定且唯一的 `eventId`。缺失 ID 时的内容哈希只能做有限去重。
12. 插件响应应始终回传关联 ID；无 ID 响应在并发请求下无法可靠匹配。

## 9. 旧版原生 WebSocket

源码仍保留旧连接：

```text
wss://im2.vipscum.cn:2347
```

旧版连接建立后发送：

```json
{
  "key": "密钥",
  "type": "bot_main",
  "keepalive": true
}
```

旧版参数：

| 参数 | 值 |
| --- | --- |
| Ping 间隔 | 10 秒 |
| Ping 超时 | 5 秒 |
| 关闭超时 | 5 秒 |
| 最大消息 | 1 MiB |
| 压缩 | 关闭 |
| 连接超时 | 10 秒 |

当前 `start_websocket()` 实际调用 `init_socketio_client()`，不会启动该旧连接循环。新接入不要依赖此旧地址。

## 10. 最小调用示例

### 10.1 手机端向主机器人发送游戏文本

```javascript
import { io } from "socket.io-client";

const socket = io("https://im3.vipscum.cn", {
  transports: ["websocket"]
});

socket.on("connect", () => {
  socket.emit("join_room", {
    key: process.env.BOT_SECRET,
    type: "phone_chat"
  });

  socket.emit("send_message", {
    targetType: "bot_main",
    content: {
      actType: "sendMsgToGame",
      content: "欢迎来到服务器"
    },
    action: "send",
    messageId: String(Date.now())
  });
});

socket.on("message", (message) => {
  console.log(message);
});
```

具体手机端 `key/type` 是否允许自行注册仍取决于中转服务器的鉴权实现；机器人仓库中只包含客户端行为。

### 10.2 插件网关查询玩家

发送原生 WebSocket 文本帧：

```json
{
  "id": "op-example-1",
  "method": "GET",
  "path": "/players?detail=full&steamId=7656119xxxxxxxxxx",
  "body": ""
}
```

服务端响应：

```json
{
  "id": "op-example-1",
  "status": 200,
  "body": "{\"ok\":true,\"players\":[]}"
}
```

## 11. 源码定位

| 内容 | 文件/符号 |
| --- | --- |
| Socket.IO 客户端、连接、事件和发送封装 | `socketio_client.py` / `SocketIOClient` |
| 主 Socket.IO 初始化 | `内置机器人.py` / `start_websocket` |
| Socket.IO 消息入口 | `内置机器人.py` / `handle_socketio_message` |
| `actType` 白名单和业务分发 | `内置机器人.py` / `socketMsgTypesArr`, `doSocketRecMsgs`, `doSendAdminNeedSendMsgs` |
| 副机器人同步 RPC 等待 | `内置机器人.py` / `wxSkt_GetDataFromMainBot` |
| 插件 WebSocket 客户端 | `内置机器人.py` / `PluginRealtimeClient` |
| 插件事件分发 | `内置机器人.py` / `plugin_handle_event` |
| 插件请求路由和 HTTP 回退 | `内置机器人.py` / `plugin_request` |
| 插件网关地址探测 | `内置机器人.py` / `get_plugin_gateway_config` |
