# Axes 锁定键切换说明：仅用户名 → 用户名 + 客户端地址

适用对象：使用 `AxesDatabaseHandler`（默认）或 `AxesCacheHandler` 的部署。
本次切换**只改一个配置项**，不改模型、不写数据迁移、不改写入路径。

```python
# 旧配置（按用户名锁定）
AXES_LOCKOUT_PARAMETERS = ["username"]

# 新配置（用户名与客户端地址“组合”锁定，注意是嵌套列表 = AND）
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]
```

> 关键区别：`["username", "ip_address"]`（两个独立字符串）是 **OR**（用户名或 IP 任一命中即锁）；
> `[["username", "ip_address"]]`（嵌套列表）才是 **AND 组合**（同一用户名 + 同一 IP 才算同一个锁定主体）。
> 本需求要的是后者。解析逻辑见 `axes/helpers.py:333` 的 `get_client_parameters`。

---

## 1. 参数、写入键、读取条件的追踪结论

### 1.1 聚合参数从哪里来
- 配置入口：`axes/conf.py:39` 读取 `AXES_LOCKOUT_PARAMETERS`；`axes/conf.py:42` 的旧开关
  （`AXES_ONLY_USER_FAILURES` 等）只是在该项为 `None` 时的兼容回退，新部署应直接显式设置该项。
- 统一取值：`axes/helpers.py:313` `get_lockout_parameters` 返回原始结构（字符串=独立维度，列表=组合维度）。
- 转成查询条件：`axes/helpers.py:333` `get_client_parameters` 把每个维度映射成一个 filter dict：
  - 旧：`[{"username": <u>}]`
  - 新：`[{"username": <u>, "ip_address": <ip>}]`

### 1.2 写入键（两套配置完全相同，不受影响）
- 物理落库的唯一性由模型决定：`axes/models.py:51`
  `unique_together = [["username", "ip_address", "user_agent"]]`。
- 写入调用：`axes/handlers/database.py:190` 的
  `AccessAttempt.objects.select_for_update().get_or_create(username=..., ip_address=..., user_agent=..., ...)`，
  命中同一三元组就把 `failures_since_start` 加一（`database.py:226`），否则新建一行。
- 结论：**写入键恒定为 `(username, ip_address, user_agent)`，切换配置不会改变行的结构，也不需要迁移。**
  `AXES_LOCKOUT_PARAMETERS` 只影响“怎么读/怎么聚合”，不影响“怎么写”。

### 1.3 读取条件（这里发生变化）
- DB 读取：`axes/handlers/database.py:359` `filter_user_attempts` 对每个 filter dict 生成一个 queryset；
  `database.py:112` `get_failures` 对各 queryset 分别 `Sum("failures_since_start")` 再取 `max`；
  冷却窗口由 `database.py:381` `get_user_attempts` 叠加 `attempt_time__gte=阈值`。
- 缓存键：`axes/helpers.py:380` `make_cache_key_list` 对 filter dict 的值拼接后做 SHA-256，前缀 `axes-`。
  - 旧键只含用户名；新键含“用户名+IP”，因此**两套配置的缓存键天然不同、互不命中**。
- 判定门槛：`axes/handlers/base.py:125` `is_locked`，`failures >= AXES_FAILURE_LIMIT` 即锁定。

---

## 2. 合成数据 + SQLite 实测对照

复现脚本：`switch_verification/verify_lockout_switch.py`（内存 SQLite + LocMem，全程离线）。
完整输出：`switch_verification/verification_output.txt`，30 项断言全部通过（退出码 0）。

合成行（`failure_limit = 5`）：

| 行 | username | ip_address | user_agent | failures |
|----|----------|------------|------------|----------|
| R1 | alice | 10.0.0.1 | Chrome | 3 |
| R2 | alice | 10.0.0.2 | Chrome | 3 |
| R3 | bob | 10.0.0.1 | Chrome | 5 |
| R4 | carol | 10.0.0.9 | Firefox | 5 |
| R7 | carol | 10.0.0.9 | Safari | 2 |
| R5 | dave | 10.0.0.5 | Chrome | 2 |

判定对照（节选，完整见输出文件）：

| 探测请求 (user, ip, UA) | 旧 `["username"]` | 新 `[["username","ip_address"]]` |
|---|---|---|
| alice / 10.0.0.1 / Chrome | 匹配 R1+R2，Sum=6 → **锁定** | 仅 R1，Sum=3 → 放行 |
| alice / 10.0.0.2 / Chrome | 同一只读 username 键，Sum=6 → **锁定** | 仅 R2，Sum=3 → 放行 |
| alice / 10.0.0.77（新 IP） | 旧键仍命中 R1+R2 → **锁定** | 组合键无任何行，Sum=0 → 放行 |
| bob / 10.0.0.1 / Chrome | Sum=5 → **锁定** | Sum=5 → **锁定**（不变） |
| carol / 10.0.0.9 / Firefox | R4+R7，Sum=7 → **锁定** | R4+R7，Sum=7 → **锁定** |
| carol / 10.0.0.9 / **Chrome** | 读取不含 UA，仍 Sum=7 → **锁定** | 读取不含 UA，仍 Sum=7 → **锁定** |

要点：
- 读取维度里**没有 user_agent**，所以 carol 换浏览器仍是同一锁定主体（R4、R7 被合并聚合）。
- 新配置下每个 `(username, ip)` 生成独立缓存键；同 user+ip 不同 UA 的两个探测缓存键相同，证明键不含 UA。

---

## 3. 切换影响的三类边界（务必区分）

### 3.1 旧记录“失效”——其实是行还在、但不再参与判定
- DB 模式：旧表行**不会被删除，也不需要数据迁移**。变化的是读取过滤条件：旧的 username 聚合被拆成
  多个 `(username, ip)` 子集，像 alice 那样原本跨 IP 凑满阈值的，切换后每个子集都低于阈值，
  锁定“事实上解除”，但历史行仍保留，可在 admin/`axes_list_attempts` 中查到。
- 缓存模式：旧键（只哈希用户名）与新键（哈希用户名+IP）不同，旧计数不会被新键读到，等于自动“孤儿化”，
  只会按 `AXES_COOLOFF_TIME` 到期自然消失；无需手动 flush，但若想立即干净可执行一次 cache clear。
- 反向同理：若再切回 `["username"]`，旧 username 行又会重新被聚合，可能再次触发锁定。

### 3.2 多个记录“合并”——分两种，别混淆
- **跨 IP 拆分（新配置不再合并）**：旧配置把同一 username 的所有 IP 行 Sum 到一起；新配置按
  `(username, ip)` 分组，alice 的 R1、R2 从此各算各的。
- **跨 UA 仍然合并（两套都合并）**：因为读取键不含 `user_agent`。carol 的 R4(Firefox)+R7(Safari)
  在新旧配置下都被 Sum=7。物理上它们仍是两行（写入键含 UA），只是判定时被聚合。
- OR 形态 `["username", "ip_address"]` 的聚合是“各 queryset 取 Sum 后取 max”（不是相加），
  与 AND 组合语义不同；本需求不用这种形态。

### 3.3 管理员重置范围变化
重置底层：`axes/handlers/database.py:45` `reset_attempts`，命令封装在 `axes/management/commands/`，
工具函数 `axes/utils.py:19` `reset` / `axes/utils.py:33` `reset_request`。实测（脚本中用回滚事务，不破坏数据）：

| 操作 | 实际删除范围（基于上面的合成行） |
|---|---|
| `axes_reset_username alice` | 删 R1、R2（该用户**所有 IP**，共 2 行） |
| `axes_reset_ip 10.0.0.9` | 删 R4、R7（该 IP **所有用户/UA**，共 2 行） |
| `axes_reset_ip_username 10.0.0.1 alice` | AND：仅删 R1（1 行）——与新组合键精确对应 |
| `reset(ip=..., username=..., ip_or_username=True)` | OR：删 R1、R2、R3（3 行） |

- 重置命令按**字段**过滤，本身不读 `AXES_LOCKOUT_PARAMETERS`；`reset_request`（视图解锁入口）
  才会根据锁定参数推导该用 AND 还是 OR（`axes/utils.py:48`）。
- 新组合锁定下，**精准解锁单个主体应使用 `axes_reset_ip_username <ip> <username>`**；
  仍用 `axes_reset_username` 会把该用户在所有地址上的计数一并清掉，范围比锁定主体大。
- 缓存处理器限制：`AxesCacheHandler.reset_attempts` 在 `ip_or_username=True` 时直接抛
  `NotImplementedError`（`axes/handlers/cache.py:42`），OR 式重置在缓存后端不可用；AND/单维度重置可用。

---

## 4. 上线动作清单（无需接入线上地址或账户）

1. 配置：设置 `AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]`（嵌套列表）。
2. 无需模型迁移、无需手动改 `axes_accessattempt` 表；写入键 `(username, ip_address, user_agent)` 不变。
3. DB 后端：历史行保留，跨 IP 凑数的既有锁定会因读取拆分而自动解除（属预期）。
4. 缓存后端：旧 username 缓存键成为孤儿，等待 cooloff 过期；需要立即清空可做一次 cache clear。
5. 运维口径：单主体解锁改用 `axes_reset_ip_username`；向支持团队说明 `axes_reset_username` 现在是“跨所有 IP”的更大范围。
6. 验证：离线执行
   `./.venv/bin/python switch_verification/verify_lockout_switch.py`，看到 `TOTAL ASSERTION FAILURES: 0` 即符合预期。

## 5. 回归验证
- 离线对照脚本：30 项断言通过，退出码 0。
- 现有测试：`tests/test_handlers.py`、`tests/test_management.py`、`tests/test_helpers.py`
  合计 **213 passed, 44 subtests passed**（未改动任何 `axes/` 源码）。
