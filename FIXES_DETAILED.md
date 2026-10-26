# 详细修复方案

---

## 🔧 修复 #1：卡密解析逻辑（auth/manager.py）

### 当前问题代码
```python
# auth/manager.py - mark_key_used() 函数
if '-' in remaining:
    # 新格式处理
    if remaining.startswith('-1-'):
        days = -1
        rest = remaining[3:]
    else:
        parts = remaining.split('-', 1)
        days_str = parts[0]
        rest = parts[1]
        if not days_str.isdigit():
            return {"valid": False, "error": "无效的卡密格式"}
        days = int(days_str)
else:
    # ❌ 旧格式处理有问题
    if len(remaining) < 21:
        return {"valid": False, "error": "无效的卡密格式"}
    
    unique = remaining[-8:]
    timestamp_str = remaining[-21:-8]
    days_str = remaining[:-21]  # 这里错了，应该是数字开头
    
    if not days_str.isdigit():
        return {"valid": False, "error": "无效的卡密格式"}
    if not timestamp_str.isdigit():
        return {"valid": False, "error": "无效的卡密格式"}
    
    days = int(days_str)
```

### ✅ 修复后的代码
```python
def verify_license_key(key: str, auth_key: str, group_id: str = "") -> dict:
    if not key.startswith("JK"):
        return {"valid": False, "error": "无效的卡密格式"}

    try:
        remaining = key[2:]

        # 永久卡密：JK-1-{timestamp}{unique}
        if remaining.startswith('-1-'):
            days = -1
            timestamp_str = remaining[3:16]  # 13位时间戳
            unique = remaining[16:24]  # 8位唯一码
        # 新格式：JK{days}-{timestamp}{unique}
        elif '-' in remaining:
            parts = remaining.split('-', 1)
            days_str = parts[0]
            rest = parts[1]

            if not days_str.isdigit():
                return {"valid": False, "error": "无效的卡密格式"}

            days = int(days_str)
            timestamp_str = rest[:13]
            unique = rest[13:21]
        # 旧格式（已弃用，但兼容）
        else:
            # 旧格式：JK{days}{timestamp}{unique}
            # 总长度应该是 2(JK) + X(days) + 13(timestamp) + 8(unique) >= 23
            if len(key) < 23:
                return {"valid": False, "error": "无效的卡密格式"}
            
            # 从后往前提取：最后8位是unique，前13位是timestamp
            unique = key[-8:].upper()
            timestamp_str = key[-21:-8]
            days_str = key[2:-21]
            
            if not days_str.isdigit() or not timestamp_str.isdigit():
                return {"valid": False, "error": "无效的卡密格式"}
            
            days = int(days_str)

        # 统一验证时间戳和唯一码
        if not timestamp_str.isdigit():
            return {"valid": False, "error": "无效的卡密格式"}

        timestamp = int(timestamp_str)
        
        # 验证哈希值（尝试 100 次以应对index变化）
        for i in range(100):
            expected_unique = hashlib.md5(
                f"{auth_key}{timestamp}{group_id}{i}".encode()
            ).hexdigest()[:8].upper()
            if unique == expected_unique:
                return {"valid": True, "days": days, "timestamp": timestamp}
        
        return {"valid": False, "error": "卡密验证失败"}
    
    except Exception as e:
        logger.error(f"验证卡密失败: {e}")
        return {"valid": False, "error": "卡密验证失败"}
```

---

## 🔧 修复 #2：禁言系统时间比较（qqadmin/mute.py）

### 当前问题
```python
def is_muted(group_id, user_id):
    """检查用户是否被禁言"""
    mute_list = get_group_data(group_id, "mute_list", {})
    if mute_list and user_id in mute_list:
        expire_time = mute_list[user_id].get("expire_time", 0)
        # ❌ 错误：expire_time == 0 时会返回 True
        # 但代码后面又检查过期，如果 expire_time == 0 则不会进入这个分支
        if expire_time == 0 or expire_time > time.time():
            return True
        else:
            unmute_user(group_id, user_id)
    return False
```

### ✅ 修复代码
```python
def is_muted(group_id, user_id):
    """检查用户是否被禁言
    
    返回值：
        True: 用户被禁言（永久或未过期）
        False: 用户未被禁言或禁言已过期
    """
    mute_list = get_group_data(group_id, "mute_list", {})
    if not mute_list or user_id not in mute_list:
        return False
    
    mute_info = mute_list[user_id]
    expire_time = mute_info.get("expire_time", 0)
    current_time = int(time.time())
    
    # 永久禁言（expire_time == 0）
    if expire_time == 0:
        return True
    
    # 临时禁言：检查是否过期
    if expire_time > current_time:
        return True
    else:
        # 禁言已过期，自动清理
        unmute_user(group_id, user_id)
        return False
```

---

## 🔧 修复 #3：黑名单边界条件（qqadmin/blacklist.py）

### 当前问题
```python
def is_in_blacklist(group_id: str, user_id: str, check_active: bool = True) -> bool:
    """检查用户是否在黑名单中"""
    blacklist = get_group_data(group_id, "blacklist", {})
    if not blacklist or user_id not in blacklist:
        return False

    if check_active:
        entry = blacklist[user_id]
        if not entry.get("is_active", True):
            return False

        expire_time = entry.get("expire_time", 0)
        # ❌ 错误：应该用 >= 而不是 >
        if expire_time > 0 and int(time.time()) > expire_time:
            return False

    return True
```

### ✅ 修复代码
```python
def is_in_blacklist(group_id: str, user_id: str, check_active: bool = True) -> bool:
    """检查用户是否在黑名单中
    
    Args:
        group_id: 群组ID
        user_id: 用户ID
        check_active: 是否检查黑名单是否生效（排除已解除的黑名单）
    
    Returns:
        bool: 用户是否在有效的黑名单中
    """
    blacklist = get_group_data(group_id, "blacklist", {})
    if not blacklist or user_id not in blacklist:
        return False

    entry = blacklist[user_id]
    
    if check_active:
        # 检查黑名单是否已被解除
        if not entry.get("is_active", True):
            return False

        # 检查临时黑名单是否过期
        expire_time = entry.get("expire_time", 0)
        if expire_time > 0:
            current_time = int(time.time())
            # ✅ 使用 >= 确保精确的过期时间
            if current_time >= expire_time:
                return False

    return True
```

---

## 🔧 修复 #4：入群审核拒绝逻辑（qqadmin/audit.py）

### 当前问题
```python
def reject_join_request(group_id: str, user_id: str, operator_id: str = "", reason: str = "") -> bool:
    def _reject(pending):
        if pending is None:
            return []
        for req in pending:
            # ❌ 只改状态，没有删除和清理验证数据
            if req.get("user_id") == user_id:
                req["status"] = "rejected"
                req["processed_at"] = int(time.time())
                req["operator_id"] = operator_id
                req["reason"] = reason
        return pending
    
    update_group_data(group_id, "audit_pending", _reject)
    # ❌ 缺少清理 audit_challenges
    add_audit_history(group_id, user_id, "", "rejected", operator_id, reason)
    return True
```

### ✅ 修复代码
```python
def reject_join_request(group_id: str, user_id: str, operator_id: str = "", reason: str = "") -> bool:
    """拒绝入群请求
    
    Args:
        group_id: 群组ID
        user_id: 用户ID
        operator_id: 操作者ID
        reason: 拒绝原因
    
    Returns:
        bool: 是否成功拒绝
    """
    # 第一步：获取待处理请求，找出关联的验证挑战ID
    pending = get_group_data(group_id, "audit_pending", [])
    challenge_id = None
    
    for req in pending:
        if str(req.get("user_id", "")) == str(user_id) and req.get("status") == "pending":
            challenge_id = req.get("challenge_id")
            break
    
    # 第二步：从待审核列表中完全移除该请求
    def _reject(pending):
        if pending is None:
            return []
        # ✅ 完全删除而不是只改状态（这样可以回收存储空间）
        new_pending = [
            req for req in pending 
            if str(req.get("user_id", "")) != str(user_id)
        ]
        return new_pending
    
    update_group_data(group_id, "audit_pending", _reject)
    
    # 第三步：清理关联的验证挑战
    if challenge_id:
        remove_challenge(group_id, challenge_id)
    
    # 第四步：记录到审核历史
    add_audit_history(group_id, user_id, "", "rejected", operator_id, reason)
    
    return True
```

---

## 🔧 修复 #6：撤回系统关键词检查（qqadmin/recall.py）

### 当前问题
```python
def should_recall_message(group_id: str, message: str) -> bool:
    if not is_recall_enabled(group_id):
        return False
    keywords = get_recall_keywords(group_id)
    # ❌ 如果没有关键词，返回 True，意味着所有消息都被撤回！
    if not keywords:
        return True
    for keyword in keywords:
        if keyword in message:
            return True
    return False
```

### ✅ 修复代码
```python
def should_recall_message(group_id: str, message: str) -> bool:
    """检查消息是否应该被撤回
    
    撤回逻辑取决于模式：
    - keyword: 匹配任何关键词时撤回
    - regex: 匹配任何正则表达式时撤回
    - all: 撤回所有消息
    - none: 不撤回任何消息
    """
    if not is_recall_enabled(group_id):
        return False
    
    mode = get_recall_mode(group_id)
    
    # 关键词撤回模式
    if mode == "keyword":
        keywords = get_recall_keywords(group_id)
        if not keywords:
            # ✅ 没有配置关键词时，不撤回
            return False
        for keyword in keywords:
            if keyword in message:
                return True
        return False
    
    # 正则表达式撤回模式
    elif mode == "regex":
        patterns = get_recall_regex_list(group_id)
        if not patterns:
            # ✅ 没有配置正则时，不撤回
            return False
        for pattern in patterns:
            try:
                if re.search(pattern, message):
                    return True
            except re.error as e:
                logger.warning(f"正则表达式错误: {pattern}, {e}")
                continue
        return False
    
    # 全部撤回模式
    elif mode == "all":
        return True
    
    # 无撤回模式或未知模式
    else:
        return False
```

---

## 🔧 修复 #7：卡密并发激活风险（auth/manager.py）

### 当前问题
```python
def mark_key_used(key: str, group_id: str, user_id: str = "") -> bool:
    data = load_auth_data()
    # ❌ 检查和移除之间没有原子性保证
    if key not in data["unused_keys"]:
        return False
    
    # ... 其他代码 ...
    
    # 两个请求可能同时通过上面的检查，然后都执行这行
    data["unused_keys"].remove(key)  # 第二次调用时会失败
    data["used_keys"][key] = {...}
    save_auth_data(data)
```

### ✅ 修复代码（方案1：使用锁）
```python
import threading

_auth_lock = threading.RLock()  # 在模块顶部

def mark_key_used(key: str, group_id: str, user_id: str = "") -> bool:
    """激活卡密（原子操作）"""
    with _auth_lock:  # ✅ 获取锁
        data = load_auth_data()
        
        # 再次检查（double-check locking）
        if key not in data["unused_keys"]:
            return False
        
        now = int(time.time())

        # 解析天数
        if '-' in key and not key.startswith("JK-1-"):
            parts = key[2:].split('-', 1)
            days = int(parts[0])
        elif key.startswith("JK-1-"):
            days = -1
        else:
            days = 1  # 默认1天

        # 计算过期时间
        if days == -1:
            expire = -1
        else:
            expire = now + (days * 86400)
        
        # ✅ 原子操作：移除和添加
        data["unused_keys"].remove(key)
        data["used_keys"][key] = {
            "group_id": group_id,
            "activated_at": now,
            "expire": expire,
            "user_id": user_id
        }
        
        # 更新群组授权信息
        if group_id not in data["groups"]:
            data["groups"][group_id] = {}
        data["groups"][group_id] = {
            "expire": expire,
            "activated_at": now,
            "key": key,
            "days": days,
            "user_id": user_id
        }
        
        # ✅ 原子保存
        save_auth_data(data)
        return True
```

### ✅ 修复代码（方案2：使用原子文件操作）
```python
import tempfile
import shutil

def mark_key_used(key: str, group_id: str, user_id: str = "") -> bool:
    """激活卡密（原子文件操作）"""
    auth_file = get_auth_file()
    
    try:
        # 读取现有数据
        data = load_auth_data()
        
        # 检查卡密是否已使用
        if key not in data["unused_keys"]:
            return False
        
        now = int(time.time())
        
        # ... 解析和计算逻辑 ...
        
        # 更新数据
        data["unused_keys"].remove(key)
        data["used_keys"][key] = {
            "group_id": group_id,
            "activated_at": now,
            "expire": expire,
            "user_id": user_id
        }
        
        # ✅ 原子写入：先写临时文件，再原子重命名
        temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(auth_file))
        try:
            with os.fdopen(temp_fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            # 原子重命名（在 Windows 和 Unix 上都是原子的）
            shutil.move(temp_path, auth_file)
            return True
        except:
            os.unlink(temp_path)
            raise
    
    except Exception as e:
        logger.error(f"激活卡密失败: {e}")
        return False
```

---

## 📋 修复检查清单

修复后需要验证以下内容：

- [ ] **单元测试**：为每个修复编写单元测试
- [ ] **集成测试**：测试模块间的交互
- [ ] **边界测试**：测试边界条件和异常情况
- [ ] **并发测试**：使用多线程/多进程测试并发安全性
- [ ] **回归测试**：确保修复不会破坏其他功能

### 建议的测试用例

```python
# test_mute_fix.py
import unittest
from qqadmin.mute import mute_user, is_muted, unmute_user
import time

class TestMuteFix(unittest.TestCase):
    def test_permanent_mute(self):
        """测试永久禁言"""
        mute_user("test_group", "user1", duration=0, mute_type="manual")
        assert is_muted("test_group", "user1") == True
    
    def test_temporary_mute_not_expired(self):
        """测试临时禁言（未过期）"""
        mute_user("test_group", "user2", duration=3600, mute_type="manual")
        assert is_muted("test_group", "user2") == True
    
    def test_temporary_mute_expired(self):
        """测试临时禁言（已过期）"""
        # 模拟过期的禁言
        import qqadmin.group_config as gc
        gc.set_group_data("test_group", "mute_list", {
            "user3": {
                "expire_time": int(time.time()) - 1,  # 已过期
                "duration": 3600,
                "muted_at": int(time.time()) - 3700,
                "mute_type": "manual"
            }
        })
        assert is_muted("test_group", "user3") == False
```

---

**总结：** 这 7 个修复涵盖了最关键的逻辑错误和并发问题，修复后应该能显著提高插件的稳定性和可靠性。
