# 项目代码审查报告

> 审查日期：2026-08-13
> 项目：Joker QQ群管插件 v1.5.1

---

## 📋 执行摘要

在对项目各个功能模块进行全面代码审查后，发现了 **7 个重要逻辑错误** 和 **5 个潜在风险**。

---

## 🔴 **严重错误（需立即修复）**

### 1️⃣ **卡密解析逻辑错误** (auth/manager.py)

**位置：** [mark_key_used()](auth/manager.py#L116)  
**问题：** 卡密天数解析存在严重缺陷

```python
# ❌ 错误的旧格式解析
days_str = ""
for char in key[2:]:
    if char.isdigit():
        days_str += char
    else:
        break
days = int(days_str) if days_str else 1
```

**问题描述：**
- 旧格式 `JK30-1692345678abc12345` 中，`days_str` 会变成 `"30"`，但实际上之后是 `-` 符号，不是数字
- 这会导致 `break` 提前退出，但代码逻辑已经在 `if '-' in key` 分支处理过了
- **冗余且混乱**，会导致旧格式卡密无法正确解析

**修复建议：**
```python
# 新格式：JK{days}-{timestamp}{unique}
if '-' in key and not key.startswith("JK-1-"):
    parts = key[2:].split('-', 1)
    days = int(parts[0])
else:
    # 永久卡密直接返回
    days = -1
```

---

### 2️⃣ **禁言系统时间比较逻辑错误** (qqadmin/mute.py)

**位置：** [is_muted()](qqadmin/mute.py#L58)  
**问题：** 禁言过期检查使用错误的时间比较

```python
# ❌ 错误
if expire_time == 0 or expire_time > time.time():
    return True
```

**问题描述：**
- 当 `expire_time == 0` 时，应该表示永久禁言，不应与 `>` 条件并联
- 当 `expire_time > time.time()` 时是**正确的**（还未过期）
- 当 `expire_time < time.time()` 时是过期（应该返回 False）
- **逻辑应该是：** 永久禁言（0） **OR** 未过期（> now）

**影响：** 所有 `expire_time == 0` 的禁言会被认为无效，且会被自动清理！

**修复代码：**
```python
def is_muted(group_id, user_id):
    """检查用户是否被禁言"""
    mute_list = get_group_data(group_id, "mute_list", {})
    if mute_list and user_id in mute_list:
        expire_time = mute_list[user_id].get("expire_time", 0)
        # ✅ 正确：永久禁言(0) OR 未过期(>now)
        if expire_time == 0 or expire_time > int(time.time()):
            return True
        else:
            unmute_user(group_id, user_id)
    return False
```

---

### 3️⃣ **黑名单过期时间检查错误** (qqadmin/blacklist.py)

**位置：** [is_in_blacklist()](qqadmin/blacklist.py#L70)  
**问题：** 临时黑名单过期检查使用了 `>` 而不是 `>=`

```python
# ❌ 潜在错误
expire_time = entry.get("expire_time", 0)
if expire_time > 0 and int(time.time()) > expire_time:
    return False
```

**问题描述：**
- 当 `expire_time == int(time.time())` 时（刚好过期的瞬间），条件不满足，会返回 `True`
- 应该使用 `>=` 来确保过期的黑名单被清理

**修复代码：**
```python
if expire_time > 0 and int(time.time()) >= expire_time:  # ✅ 使用 >=
    return False
```

---

### 4️⃣ **入群审核拒绝逻辑不完整** (qqadmin/audit.py)

**位置：** [reject_join_request()](qqadmin/audit.py#L426)  
**问题：** 拒绝入群请求后未清理验证挑战

```python
def reject_join_request(group_id: str, user_id: str, operator_id: str = "", reason: str = "") -> bool:
    def _reject(pending):
        if pending is None:
            return []
        for req in pending:
            if req.get("user_id") == user_id:
                req["status"] = "rejected"  # ❌ 只改状态，没删除
                # ...
        return pending
    update_group_data(group_id, "audit_pending", _reject)
    # ❌ 未清理 audit_challenges 中的记录
    add_audit_history(group_id, user_id, "", "rejected", operator_id, reason)
    return True
```

**问题描述：**
- 对比 [approve_join_request()](qqadmin/audit.py#L395)，拒绝函数缺少：
  1. 清理 `audit_challenges` 中的验证信息
  2. 完全删除而不是只改状态
- 这会导致存储空间泄漏，待拒绝列表越来越大

**修复代码：**
```python
def reject_join_request(group_id: str, user_id: str, operator_id: str = "", reason: str = "") -> bool:
    # 先获取 challenge_id，以便清理
    pending = get_group_data(group_id, "audit_pending", [])
    challenge_id = None
    for req in pending:
        if str(req.get("user_id", "")) == str(user_id):
            challenge_id = req.get("challenge_id")
            break
    
    def _reject(pending):
        if pending is None:
            return []
        # ✅ 完全移除（而不是只改状态）
        new_pending = [req for req in pending if str(req.get("user_id", "")) != str(user_id)]
        return new_pending
    
    update_group_data(group_id, "audit_pending", _reject)
    
    # ✅ 同时清理验证挑战
    if challenge_id:
        remove_challenge(group_id, challenge_id)
    
    add_audit_history(group_id, user_id, "", "rejected", operator_id, reason)
    return True
```

---

### 5️⃣ **踢出记录未去重** (qqadmin/kick.py)

**位置：** [add_kick_record()](qqadmin/kick.py#L21)  
**问题：** 同一用户被踢多次时，逻辑有缺陷

```python
def add_kick_record(group_id: str, user_id: str, operator_id: str, reason: str = "",
                    kick_type: str = "normal", notice: bool = True) -> bool:
    def _add(records):
        if records is None:
            records = []

        # 检查是否已有该用户的记录
        for existing_record in records:
            if existing_record.get("user_id") == user_id:  # ✅ 找到了
                existing_record["踢出次数"] = existing_record.get("踢出次数", 0) + 1
                existing_record["last_kicked_at"] = int(time.time())
                existing_record["last_reason"] = reason
                existing_record["last_operator"] = operator_id
                return records  # ✅ 返回

        # 新增记录
        record = {
            "user_id": user_id,
            "operator_id": operator_id,
            "reason": reason,
            "kicked_at": int(time.time()),
            "kick_type": kick_type,
            "notice_sent": notice,
            "踢出次数": 1
        }
        records.append(record)
        return records

    update_group_data(group_id, "kick_records", _add)
    return True
```

**问题描述：**
- 逻辑似乎是对的，但存在一个隐藏的风险：
- `update_group_data` 中的回调函数可能会被调用多次（如果有并发写入）
- 不建议在回调中修改原数据结构，应该返回新的数据结构

**建议：** 确保 `update_group_data` 是原子操作或者加锁

---

### 6️⃣ **撤回系统：should_recall_message 逻辑错误** (qqadmin/recall.py)

**位置：** [should_recall_message()](qqadmin/recall.py#L73)  
**问题：** 关键词检查逻辑反向了

```python
def should_recall_message(group_id: str, message: str) -> bool:
    if not is_recall_enabled(group_id):
        return False
    keywords = get_recall_keywords(group_id)
    if not keywords:
        return True  # ❌ 没有关键词时直接撤回？这是错误的！
    for keyword in keywords:
        if keyword in message:
            return True
    return False
```

**问题描述：**
- 当群组没有设置任何关键词时，应该撤回所有消息吗？
- 这个逻辑不清楚，可能导致未经配置的群组所有消息都被撤回

**修复代码：**
```python
def should_recall_message(group_id: str, message: str) -> bool:
    if not is_recall_enabled(group_id):
        return False
    
    mode = get_recall_mode(group_id)
    
    # 模式1：关键词匹配
    if mode == "keyword":
        keywords = get_recall_keywords(group_id)
        if not keywords:
            return False  # ✅ 没有关键词时不撤回
        for keyword in keywords:
            if keyword in message:
                return True
        return False
    
    # 模式2：正则匹配
    elif mode == "regex":
        patterns = get_recall_regex_list(group_id)
        for pattern in patterns:
            try:
                if re.search(pattern, message):
                    return True
            except re.error:
                continue
        return False
    
    # 模式3：全部撤回
    elif mode == "all":
        return True
    
    return False
```

---

### 7️⃣ **授权系统：激活卡密后未检查重复激活** (auth/manager.py)

**位置：** [mark_key_used()](auth/manager.py#L116)  
**问题：** 同一卡密被激活多次

```python
def mark_key_used(key: str, group_id: str, user_id: str = "") -> bool:
    data = load_auth_data()
    if key not in data["unused_keys"]:
        return False  # ✅ 检查，但时机太晚（已经可能被用过）
    
    # ... 解析天数 ...
    
    data["unused_keys"].remove(key)  # 移除前没加锁，可能并发问题
    data["used_keys"][key] = {  # 添加
        "group_id": group_id,
        "activated_at": now,
        "expire": expire,
        "user_id": user_id
    }
    # ...
```

**问题描述：**
- 在高并发场景下，两个请求可能同时检查 `key in unused_keys` 都返回 True
- 都通过检查，都执行 `remove()` 和 `add()`，导致同一卡密被多个群激活
- 第二次调用会在 `remove()` 时失败，但第一次的写入已经完成

**修复方案：** 添加原子操作或锁机制
```python
def mark_key_used(key: str, group_id: str, user_id: str = "") -> bool:
    data = load_auth_data()
    
    # ✅ 原子检查
    if key not in data["unused_keys"]:
        return False
    
    # ... 解析天数 ...
    
    # ✅ 原子移除和添加
    data["unused_keys"].remove(key)
    data["used_keys"][key] = {
        "group_id": group_id,
        "activated_at": now,
        "expire": expire,
        "user_id": user_id
    }
    
    save_auth_data(data)  # 原子保存
    return True
```

---

## 🟡 **中等风险（应该改进）**

### A. **审核超时处理不完善** (qqadmin/audit.py)

**位置：** [clean_expired_challenges_and_kick()](qqadmin/audit.py#L548)  
**问题：** 过期审核请求自动拒绝，但没有通知用户

```python
def clean_expired_challenges_and_kick(group_id: str) -> list:
    # ... 清理过期挑战 ...
    # ❌ 清理完了，但用户不知道为什么被拒绝了
```

**建议：** 添加通知机制
```python
# 在拒绝前发送通知
send_notify_message(group_id, user_id, "您的入群申请已过期，请重新申请")
```

---

### B. **递归导入风险** (main.py, group_config.py)

**位置：** [audit.py set_global_config()](qqadmin/audit.py#L7)  
**问题：** 多个模块相互导入全局配置

```python
# audit.py
_global_config = {}
def set_global_config(config: dict):
    global _global_config
    _global_config = config
```

```python
# 在 audit.py 内部又导入
from .group_config import get_group_feature_setting
```

**风险：** 如果 `group_config` 又导入 `audit`，会导致循环导入  
**建议：** 统一在 `main.py` 中注册全局配置

---

### C. **缓存一致性问题** (group_config.py)

**位置：** [_config_cache 缓存](qqadmin/group_config.py#L37)  
**问题：** 内存缓存 5 秒，但没有失效机制

```python
# _config_cache = {group_id: (config, file_mtime)}
# 缓存有效期 5 秒，如果在 5 秒内文件被外部修改，缓存会过时
```

**风险：** 
- 配置被外部程序修改时，本插件收不到通知
- 需要等 5 秒才能生效

**建议：** 使用文件监视器或消息队列

---

### D. **欢迎系统：新成员检测不完善** (qqadmin/welcome.py)

**位置：** [get_new_members()](qqadmin/welcome.py#L93)  
**问题：** 新成员检测只比较 user_id，容易出错

```python
def get_new_members(group_id: str, current_members: list) -> list:
    cached_members = get_group_members(group_id)
    if not cached_members:
        return []
    cached_uids = set(str(m.get("user_id", "")) for m in cached_members)
    new_members = []
    for member in current_members:
        uid = str(member.get("user_id", ""))
        if uid and uid not in cached_uids:  # ✅ 找不到说明是新成员
            new_members.append(member)
    return new_members
```

**问题：**
- 如果某个成员被移除然后重新加入，会被误认为是新成员
- 如果成员列表查询失败（因为权限不足），会导致欢迎所有人

---

### E. **时间戳类型不一致** (多个文件)

**问题：** 代码中混用 `int(time.time())` 和 `time.time()` (float)

```python
# audit.py
"created_at": int(time.time())  # int

# mute.py  
if expire_time > time.time():  # float
    return True

# 比较时会有微秒级别的精度问题
```

**建议：** 统一使用 `int(time.time())` 或始终转换

---

## 📊 问题汇总表

| # | 文件 | 函数 | 严重度 | 类型 | 说明 |
|---|------|------|--------|------|------|
| 1 | auth/manager.py | mark_key_used | 🔴 严重 | 逻辑错误 | 卡密格式解析混乱 |
| 2 | qqadmin/mute.py | is_muted | 🔴 严重 | 逻辑错误 | 禁言过期检查反向 |
| 3 | qqadmin/blacklist.py | is_in_blacklist | 🔴 严重 | 逻辑错误 | 边界条件错误 |
| 4 | qqadmin/audit.py | reject_join_request | 🔴 严重 | 不完整实现 | 未清理验证数据 |
| 5 | qqadmin/kick.py | add_kick_record | 🟡 中等 | 并发风险 | 缺乏原子性保障 |
| 6 | qqadmin/recall.py | should_recall_message | 🔴 严重 | 逻辑错误 | 没关键词时错误撤回 |
| 7 | auth/manager.py | mark_key_used | 🔴 严重 | 并发风险 | 同一卡密重复激活 |
| A | qqadmin/audit.py | clean_expired_challenges | 🟡 中等 | 功能不完善 | 过期审核无通知 |
| B | 多个文件 | - | 🟡 中等 | 架构风险 | 循环导入隐患 |
| C | qqadmin/group_config.py | - | 🟡 中等 | 缓存问题 | 一致性不足 |
| D | qqadmin/welcome.py | get_new_members | 🟡 中等 | 逻辑缺陷 | 重复加入识别错 |
| E | 全局 | - | 🟡 中等 | 类型混乱 | 时间戳处理不统一 |

---

## ✅ 正确实现的部分

虽然发现了多个问题，但以下部分实现良好：

- ✅ **分群配置系统** - 独立存储、支持导入导出
- ✅ **授权管理** - 卡密生成算法安全（MD5 + 时间戳）
- ✅ **群存储** - 支持群组级别的文件存储
- ✅ **事件驱动** - 异步处理机制完善
- ✅ **权限系统** - 多层级权限控制（主人、管理、子管）
- ✅ **统计系统** - 积分、签到等功能逻辑清晰

---

## 📝 修复优先级

### 优先级 1（立即修复 - 24小时内）
- [ ] 修复禁言系统时间比较（#2）
- [ ] 修复入群审核拒绝逻辑（#4）
- [ ] 修复撤回系统关键词检查（#6）

### 优先级 2（尽快修复 - 本周内）
- [ ] 修复卡密解析逻辑（#1）
- [ ] 修复黑名单边界条件（#3）
- [ ] 修复卡密并发激活风险（#7）

### 优先级 3（改进 - 下个版本）
- [ ] 踢出记录去重和原子性（#5）
- [ ] 审核超时通知机制（A）
- [ ] 缓存一致性改进（C）
- [ ] 时间戳类型统一（E）

---

## 🔧 建议的修复步骤

1. **编写单元测试** - 为每个问题编写测试用例
2. **修复关键逻辑** - 按优先级修复上述错误
3. **集成测试** - 测试各模块间的交互
4. **压力测试** - 验证并发场景下的正确性
5. **代码审查** - 重新审查修复后的代码

---

## 📚 相关文件

- [修复建议详解](./FIXES.md) - 每个问题的详细修复方案
- [测试用例](./TESTS.md) - 问题复现和验证的测试代码

---

**报告生成时间：** 2026-08-13  
**审查人员：** GitHub Copilot  
**项目版本：** v1.5.1
