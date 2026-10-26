# SCUM BattleMetrics 中转站接口文档

版本：1.2.0  
更新时间：2026-10-19  
服务地址：`https://ba.yooyun.top`

## 1. 功能说明

本服务用于给其他程序调用 BattleMetrics 的 SCUM 公开网页数据，支持：

- 使用数字关键词，例如 `8084`；
- 使用中文关键词，例如 `建家`；
- 返回搜索到的服务器 ID、名称、在线人数、人数上限、IP、端口和地区等网页实际显示数据；
- 根据数字服务器 ID 查询详情和当前在线人数；
- 将服务器 ID 绑定为自定义名称，之后通过名称重复查询。

服务使用服务器上的浏览器读取 BattleMetrics 公开网页，不使用虚假的固定数据。每次搜索或详情请求都会重新读取网页；返回的在线人数可能随着游戏服务器状态变化。

> BattleMetrics 页面不提供 SCUM 玩家姓名列表，因此本服务不能返回真实玩家姓名，只能返回网页显示的服务器信息和在线人数。

## 2. 认证方式

除 `/health` 外，所有接口都需要在请求头中携带中转 Key：

```http
Authorization: Bearer YOUR_RELAY_KEY
```

必须包含 `Bearer` 和一个空格。下面的写法是错误的：

```http
Authorization: YOUR_RELAY_KEY
```

Key 保存在中转服务器的：

```text
/www/wwwroot/battlemetrics/.env
```

配置名称是：

```dotenv
RELAY_API_KEY=你的中转Key
```

调用程序应把 Key 放在后端环境变量或私密配置中，不要放进公开网页的 JavaScript、HTML 或客户端 App 中。

## 3. 健康检查

### 请求

```http
GET https://ba.yooyun.top/health
```

### 返回

```json
{
  "ok": true,
  "upstreamChecked": false
}
```

健康检查只代表中转 Node 服务正在运行，不代表 BattleMetrics 当前可以访问。

## 4. 关键词搜索

### 请求

```http
GET /api/search?q=关键词
```

完整地址示例：

```text
https://ba.yooyun.top/api/search?q=8084
https://ba.yooyun.top/api/search?q=建家
```

中文必须进行 URL 编码。使用 `curl --data-urlencode` 或程序的 URL 参数功能，不要手动拼接未编码的中文。

### 参数

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `q` | 是 | 搜索关键词，数字和中文都可以，最多 120 个字符 |
| `offset` | 否 | 分页偏移量，默认 `0`；下一页通常使用 `10` |
| `limit` | 否 | 请求条数，范围 `1-100`；网页模式以 BattleMetrics 页面实际分页为准，当前通常每页 10 条 |

### curl 示例

```sh
curl --get 'https://ba.yooyun.top/api/search' \\
  -H 'Authorization: Bearer YOUR_RELAY_KEY' \\
  --data-urlencode 'q=8084'
```

```sh
curl --get 'https://ba.yooyun.top/api/search' \\
  -H 'Authorization: Bearer YOUR_RELAY_KEY' \\
  --data-urlencode 'q=建家'
```

查询下一页：

```sh
curl --get 'https://ba.yooyun.top/api/search' \\
  -H 'Authorization: Bearer YOUR_RELAY_KEY' \\
  --data-urlencode 'q=建家' \\
  --data-urlencode 'offset=10'
```

### 返回示例

```json
{
  "source": "BattleMetrics",
  "fetchedAt": "2026-10-19T12:00:00.000Z",
  "keyword": "建家",
  "servers": [
    {
      "id": "41638507",
      "name": "3倍pvp/无赞助/魔改/回收/免费建家/免费855/9222",
      "status": null,
      "players": 118,
      "maxPlayers": 128,
      "ip": "47.113.218.7",
      "port": 9222,
      "country": "China",
      "updatedAt": null
    }
  ],
  "raw": {
    "data": [],
    "links": {}
  }
}
```

`raw` 是中转读取到的原始网页映射数据，字段可能随 BattleMetrics 页面结构变化。通常只需要使用 `servers`。

搜索列表中的 `status` 可能是 `null`，这是因为搜索页没有显示可确认的在线状态。需要在线状态时，再调用服务器详情接口。

## 5. 按服务器 ID 查询详情

### 请求

```http
GET /api/servers/{serverId}
```

例如查询服务器 ID `39243225`：

```sh
curl 'https://ba.yooyun.top/api/servers/39243225' \\
  -H 'Authorization: Bearer YOUR_RELAY_KEY'
```

服务器 ID 必须是数字。示例中的 ID 是 8 位数字，但接口实际允许 1 到 20 位数字，只要是 BattleMetrics 搜索结果中的服务器 ID 即可。

### 返回示例

```json
{
  "source": "BattleMetrics",
  "fetchedAt": "2026-10-19T12:01:00.000Z",
  "server": {
    "id": "39243225",
    "name": "SCUM Server",
    "status": "online",
    "players": 116,
    "maxPlayers": 128,
    "ip": "47.113.218.7",
    "port": 9222,
    "country": "China",
    "updatedAt": null
  },
  "playerData": null,
  "raw": {}
}
```

详情接口的 `status` 通常为 `online`、`offline` 或 `dead`。`players` 和 `maxPlayers` 是网页当时显示的实时人数与上限。

## 6. 绑定服务器 ID

绑定适合固定查询某个服务器。绑定名称可以使用中文、字母、数字、下划线和短横线，长度 1 到 80 个字符。

### 6.1 创建或更新绑定

```http
PUT /api/bindings/{bindingName}
Content-Type: application/json
```

示例：

```sh
curl -X PUT 'https://ba.yooyun.top/api/bindings/my-server' \\
  -H 'Authorization: Bearer YOUR_RELAY_KEY' \\
  -H 'Content-Type: application/json' \\
  -d '{"serverId":"39243225"}'
```

绑定名称也可以使用中文，例如：

```text
/api/bindings/我的服务器
```

### 6.2 查询绑定服务器

```sh
curl 'https://ba.yooyun.top/api/bindings/my-server' \\
  -H 'Authorization: Bearer YOUR_RELAY_KEY'
```

查询绑定会重新读取网页，不使用旧的在线人数缓存。

### 6.3 删除绑定

```sh
curl -X DELETE 'https://ba.yooyun.top/api/bindings/my-server' \\
  -H 'Authorization: Bearer YOUR_RELAY_KEY'
```

删除成功返回：

```json
{
  "deleted": "my-server"
}
```

## 7. 在其他程序中调用

### Node.js

```js
const relayKey = process.env.SCUM_RELAY_KEY;

async function searchScum(keyword) {
  const url = new URL('https://ba.yooyun.top/api/search');
  url.searchParams.set('q', keyword);

  const response = await fetch(url, {
    headers: { Authorization: `Bearer ${relayKey}` },
    signal: AbortSignal.timeout(60000)
  });
  const body = await response.json();

  if (!response.ok) {
    throw new Error(body.error || `HTTP ${response.status}`);
  }
  return body.servers;
}

console.log(await searchScum('建家'));
```

### Python

```python
import os
import requests

relay_key = os.environ["SCUM_RELAY_KEY"]
response = requests.get(
    "https://ba.yooyun.top/api/search",
    params={"q": "建家"},
    headers={"Authorization": f"Bearer {relay_key}"},
    timeout=60,
)
response.raise_for_status()
servers = response.json()["servers"]
print(servers)
```

查询指定 ID：

```python
response = requests.get(
    "https://ba.yooyun.top/api/servers/39243225",
    headers={"Authorization": f"Bearer {relay_key}"},
    timeout=60,
)
response.raise_for_status()
print(response.json()["server"])
```

### PHP

```php
<?php
$relayKey = getenv('SCUM_RELAY_KEY');
$url = 'https://ba.yooyun.top/api/search?' . http_build_query(['q' => '建家']);

$ch = curl_init($url);
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_TIMEOUT => 60,
    CURLOPT_HTTPHEADER => [
        'Authorization: Bearer ' . $relayKey,
        'Accept: application/json',
    ],
]);
$body = curl_exec($ch);
$status = curl_getinfo($ch, CURLINFO_HTTP_CODE);
curl_close($ch);

if ($body === false || $status < 200 || $status >= 300) {
    throw new RuntimeException('SCUM 中转请求失败：HTTP ' . $status);
}

$result = json_decode($body, true, 512, JSON_THROW_ON_ERROR);
print_r($result['servers']);
```

### Java 11+

```java
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;

String relayKey = System.getenv("SCUM_RELAY_KEY");
HttpClient client = HttpClient.newHttpClient();
HttpRequest request = HttpRequest.newBuilder()
    .uri(URI.create("https://ba.yooyun.top/api/servers/39243225"))
    .header("Authorization", "Bearer " + relayKey)
    .timeout(java.time.Duration.ofSeconds(60))
    .GET()
    .build();

HttpResponse<String> response = client.send(
    request, HttpResponse.BodyHandlers.ofString());
if (response.statusCode() < 200 || response.statusCode() >= 300) {
    throw new RuntimeException("SCUM 中转请求失败：HTTP " + response.statusCode());
}
System.out.println(response.body());
```

## 8. 错误返回

错误统一为 JSON：

```json
{
  "error": "中转服务密钥无效。"
}
```

常见状态码：

| HTTP 状态码 | 含义 |
| --- | --- |
| `400` | 参数错误，例如关键词为空或服务器 ID 不是数字 |
| `401` | Key 缺失或无效 |
| `404` | 接口不存在或绑定名称不存在 |
| `413` | 请求体过大 |
| `429` | 浏览器正在处理另一个查询，请稍后重试 |
| `502` | BattleMetrics 页面或返回结构无法识别、连接失败 |
| `503` | 浏览器需要网站验证或暂时无法连接 |
| `500` | 中转服务内部错误 |

调用方建议：

- HTTP 超时设置为至少 60 秒；
- 收到 `429` 时等待后重试，不要并发大量请求；
- 收到 `503` 时检查服务器上的浏览器是否需要人工验证；
- 不要把 `502` 或 `503` 当作在线人数为 0。

## 9. 安全注意事项

- 不要把 `RELAY_API_KEY` 写进公开前端代码；
- 不要把 Key 放在 URL 查询参数中；
- 如果 Key 泄露，在服务器 `.env` 中生成新值并重启 Node 项目；
- 反向代理应保持至少 60 秒读取超时；
- 服务器浏览器的调试端口只监听 `127.0.0.1`，不要开放到公网。

## 10. 与 BattleMetrics 页面对应关系

关键词搜索对应公开页面：

```text
https://www.battlemetrics.com/servers/scum?countries-bool-mode=or&sort=score&q=关键词
```

服务器详情对应公开页面：

```text
https://www.battlemetrics.com/servers/scum/{服务器ID}
```

中转接口返回的是网页当时能够确认的数据，不绕过订阅 API，也不伪造数据。BattleMetrics 页面或 Cloudflare 验证变化时，中转会返回错误，调用方应提示用户稍后重试或完成验证。
