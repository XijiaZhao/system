# 表情→舵机映射系统设计

**日期**: 2026-07-13

## 1. 动机

当前 `connection.py` 的 `_capture_facial_expression()` 已实现周期性拍照 + VLLM 表情识别，
VLLM 返回表情文本（如"开心"、"疑惑"）后触发 `on_facial_expression` 回调，但该回调未被
任何逻辑消费——表情识别结果只记日志，不驱动设备动作。

本设计建立「表情文本 → 28 通道舵机指令」的映射系统，在 VLM 识别到表情后自动向设备下发
**平滑表情动画**（中位→表情→中位），使人形机器人面部自然呈现对应表情。设计遵循模块化、
可复用原则，未来其他模块（如对话 LLM 触发表情、外部 API 调用）均可重用同一映射接口。

## 2. 架构

```
VLM文本 → [match_expression()] → 标准表情名 → [send_expression_animation()]
              ↑                        ↑                      ↑
       EXPRESSION_RULES        expression_servo_map.json   chunked WS send
                              + 中位 servo 位置            type:"expression"
```

三个新增构件，各司一职：

| 构件 | 文件 | 职责 |
|------|------|------|
| 数据 | `config/assets/expression_servo_map.json` | 中位姿态 + 8 种表情 × 28 通道目标舵机 |
| 逻辑 | `core/handle/expressionActionHandle.py` | 加载、关键词匹配、帧插值生成、动画下发 |
| 集成 | `connection.py`（改） | `_capture_facial_expression` 回调中内置表情→舵机下发 |

遵循现有 `blinkActionHandle.py` 的同款模式（数据文件 + 惰性缓存 + 环境变量门控 +
紧凑 JSON 序列化 + connection 任务），保持代码风格一致。

### 2.1 动画流程

```
中位姿态 ──[20帧, 每帧40ms, 线性插值]──→ 表情姿态 ──[保持3000ms]──→ 中位姿态
   ↑                    ↑                       ↑                    ↑
 phase:"in"        n_frames帧             asyncio.sleep       phase:"out"
(neutral→expression)                  (server-side hold)  (expression→neutral)
```

与原始设计的单次静态姿态不同，本修订改为**两段式平滑动画面**：
1. **进入段 (phase "in")**：从中位姿态平滑过渡到目标表情（默认 20 帧 × 40ms = 800ms）
2. **保持段**：服务器等待 `hold_ms`（默认 3000ms），表情保持在目标位置
3. **退出段 (phase "out")**：从目标表情平滑过渡回中位姿态（默认 20 帧 × 40ms = 800ms）

每段帧序列由服务器端线性插值生成，以紧凑分块消息下发设备。设备端按 `frame_offset` +
`frame_ms` 对齐播放时钟，无需自行插值。

## 3. 数据格式

### 3.1 JSON 配置文件

**路径**: `main/xiaozhi-server/config/assets/expression_servo_map.json`

```json
{
  "neutral": {
    "0": 1581, "1": 1389, "2": 1433, "3": 1581,
    "4": 1270, "5": 1685, "6": 1470, "7": 1478,
    "8": 1418, "9": 1633, "10": 1567, "11": 1352,
    "12": 1574, "13": 1441, "14": 1707, "15": 1085,
    "16": 1685, "17": 2041, "18": 1433, "19": 1025,
    "20": 1915, "21": 1385, "22": 1629, "23": 1638,
    "24": 1959, "25": 1391, "26": 1130, "27": 1567,
    "28": 1648
  },
  "expressions": {
    "快乐": {
      "servos": {
        "0": 1635, "1": 1335, "2": 1540, "3": 1474,
        "4": 1255, "5": 1700, "6": 1500, "7": 1448,
        "12": 1420, "13": 1595, "14": 1639,
        "15": 1200, "16": 1660, "17": 1985, "18": 1425,
        "19": 1001, "20": 1940, "21": 1516, "22": 1498,
        "23": 1680, "24": 2049, "25": 1349, "26": 1040, "27": 1580
      }
    },
    "悲伤": {
      "servos": {
        "0": 1650, "1": 1320, "2": 1270, "3": 1744,
        "4": 1350, "5": 1605, "6": 1515, "7": 1433,
        "12": 1495, "13": 1520, "14": 1680,
        "15": 1125, "16": 1680, "17": 1985, "18": 1480,
        "19": 1050, "20": 1891, "21": 1516, "22": 1498,
        "23": 1680, "24": 1710, "25": 1349, "26": 1379, "27": 1505
      }
    },
    "愤怒": {
      "servos": {
        "0": 1550, "1": 1420, "2": 1200, "3": 1814,
        "4": 1165, "5": 1790, "6": 1440, "7": 1508,
        "12": 1380, "13": 1635, "14": 1480,
        "15": 1050, "16": 1680, "17": 1985, "18": 1480,
        "19": 1050, "20": 1891, "21": 1516, "22": 1498,
        "23": 1416, "24": 1761, "25": 1613, "26": 1328, "27": 1505
      }
    },
    "恐惧": {
      "servos": {
        "0": 1620, "1": 1350, "2": 1245, "3": 1769,
        "4": 1400, "5": 1555, "6": 1400, "7": 1548,
        "12": 1574, "13": 1441, "14": 1460,
        "15": 1212, "16": 1680, "17": 2110, "18": 1480,
        "19": 1050, "20": 1891, "21": 1300, "22": 1714,
        "23": 1680, "24": 1761, "25": 1349, "26": 1328, "27": 1620
      }
    },
    "惊讶": {
      "servos": {
        "0": 1690, "1": 1280, "2": 1600, "3": 1414,
        "4": 1180, "5": 1775, "6": 1400, "7": 1548,
        "12": 1435, "13": 1580, "14": 1707,
        "15": 1212, "16": 1580, "17": 1985, "18": 1370,
        "19": 1050, "20": 1891, "21": 1516, "22": 1498,
        "23": 1416, "24": 2049, "25": 1613, "26": 1040, "27": 1685
      }
    },
    "厌恶": {
      "servos": {
        "0": 1605, "1": 1365, "2": 1180, "3": 1834,
        "4": 1540, "5": 1415, "6": 1500, "7": 1448,
        "12": 1360, "13": 1655, "14": 1520,
        "15": 1212, "16": 1580, "17": 2110, "18": 1370,
        "19": 1001, "20": 1940, "21": 1300, "22": 1714,
        "23": 1680, "24": 1761, "25": 1349, "26": 1328, "27": 1620
      }
    },
    "大笑": {
      "servos": {
        "0": 1555, "1": 1415, "2": 1580, "3": 1434,
        "4": 1205, "5": 1750, "6": 1538, "7": 1410,
        "12": 1320, "13": 1695, "14": 1520,
        "15": 1212, "16": 1580, "17": 1985, "18": 1370,
        "19": 1001, "20": 1940, "21": 1374, "22": 1640,
        "23": 1680, "24": 2049, "25": 1349, "26": 1040, "27": 1660
      }
    },
    "疑惑": {
      "servos": {
        "0": 1650, "1": 1320, "2": 1285, "3": 1729,
        "4": 1430, "5": 1525, "6": 1575, "7": 1373,
        "12": 1574, "13": 1441, "14": 1640,
        "15": 1050, "16": 1680, "17": 2110, "18": 1435,
        "19": 1050, "20": 1891, "21": 1360, "22": 1654,
        "23": 1629, "24": 1889, "25": 1400, "26": 1200, "27": 1505
      }
    }
  }
}
```

要点：
- **`neutral`** — 中位/放松姿态，包含全部 29 个通道（ID 0–28）。动画的起点和终点
- **`expressions.<name>.servos`** — 仅包含**该表情偏离中位的通道**。未列出的通道在插值时
  保持中位值不变，故 JSON 中无需列出所有 29 个通道
- **`servo key 用字符串`** — JSON 标准要求 key 为字符串；服务端加载后转为 int 索引
- **移除了 `duration_ms`**（原始设计）——过渡时长由 `n_frames × frame_ms` 决定，
  保持时长由 `hold_ms` 决定，均为全局可配参数（见 §3.3）
- **通道 28（ID 28）在所有表情中始终保持中位值**，不参与任何表情变化。表情 `servos` 中
  不得包含 ID 28；代码侧 `generate_frames` 对缺失通道自动补全 neutral 值，无需额外处理

### 3.2 关键词匹配规则

**白名单原则**：仅匹配已知关键词，其余一切文本（包括"平静"、"中性"、"未检测到人脸"、
"面无表情"、"neutral"等）静默视为无操作，不下发指令也不报错。

使用**显式有序列表**（非 dict 遍历）定义匹配优先级。较宽泛的同义词组排在后面，
防止误匹配（例如"大笑"须在"快乐"前检查，否则"欢笑"关键词会抢先捕获"大笑"）：

```python
EXPRESSION_RULES: list[tuple[str, list[str]]] = [
    ("惊讶", ["吃惊", "惊讶", "惊奇", "诧异", "震惊", "惊愕"]),
    ("恐惧", ["害怕", "恐惧", "惊恐", "畏惧", "恐慌", "惧怕"]),
    ("愤怒", ["生气", "愤怒", "发怒", "恼怒", "暴怒", "激怒"]),
    ("厌恶", ["厌恶", "讨厌", "嫌弃", "反感", "恶心", "厌烦"]),
    ("大笑", ["大笑", "爆笑", "狂笑", "狂喜", "笑开了"]),
    ("快乐", ["开心", "高兴", "快乐", "喜悦", "欢笑", "愉快", "欢乐"]),
    ("悲伤", ["难过", "悲伤", "伤心", "哭泣", "哀伤", "沮丧", "忧伤"]),
    ("疑惑", ["疑惑", "困惑", "不解", "疑问", "迷茫", "纳闷", "费解"]),
]
```

匹配逻辑：按列表顺序遍历，VLM 文本中包含任一关键词即返回对应标准表情名，停止后续检查。
全部不匹配 → 返回 `None`（不下发指令）。

**为什么用 list[tuple] 而非 dict**：Python 3.7+ 的 dict 插入顺序虽然稳定，但仅属"恰好保证"。
如果开发者重排 dict 代码（例如按字母排序），优先级会被悄然打破。显式 `list[tuple]`
使优先级成为源码级事实，无可误解，且能用 `for` 循环清晰表达"首个匹配即胜出"语义。

### 3.3 环境变量

遵循 blink/servo 的既有约定（`_env_flag` / `_env_int` helper，默认值，空字符串回落默认）。

| 变量 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `ENABLE_EXPRESSION_ACTION` | flag | **开** (1/true/yes/on) | 设 `0`/`false`/`no`/`off` 显式关闭；仅门控内置表情→舵机下发，不影响 `on_facial_expression` 外部回调 |
| `EXPRESSION_SERVO_MAP_PATH` | str | `config/assets/expression_servo_map.json` | 覆盖 JSON 映射文件路径；与 `BLINK_INSTRUCTION_SET_PATH` 同款模式 |
| `EXPRESSION_FRAMES_PER_PHASE` | int | 20 | 每个过渡段（进/出）的帧数；≥2（<2 回退默认） |
| `EXPRESSION_FRAME_MS` | int | 40 | 每帧间隔（毫秒）；夹紧 [20, 200] |
| `EXPRESSION_HOLD_MS` | int | 3000 | 表情保持时长（毫秒），即进入段结束到退出段开始之间的 server-side sleep；≥0（0=立即回中位） |
| `EXPRESSION_MAX_WAIT_SPEECH_MS` | int | 5000 | 若设备正在说话，等待语音结束的最大时长（毫秒）；0=不等待直接下发。等待期间以轮询 `client_is_speaking` + `audio_rate_controller.play_position` 估算剩余时间，取最小值提前唤醒 |

过渡动画总时长 = `FRAMES_PER_PHASE × FRAME_MS`（默认 800ms）。
设备端不自行插值——所有帧由服务端预计算并分块下发，设备仅按 `frame_offset` 对齐播放。

## 4. 消息格式

### 4.1 单块消息结构

由于每帧包含 29 个通道值，单个 WebSocket 文本帧无法承载完整 20 帧序列（估算约 3500 字节，
远超 ESP32 ~1024 字节 WS 接收缓冲）。采用与 `servoActionHandle` 相同的分块策略：
每条消息携带最多 `MAX_FRAMES_PER_CHUNK` 帧，设备端按 `frame_offset` 拼回完整时间线。

```json
{
  "type": "expression",
  "session_id": "xxx",
  "emotion": "快乐",
  "phase": "in",
  "frame_ms": 40,
  "n_frames": 20,
  "frame_offset": 0,
  "frames": [
    [1581, 1389, 1433, 1581, 1270, 1685, 1470, 1478, 1418, 1633,
     1567, 1352, 1574, 1441, 1707, 1085, 1685, 2041, 1433, 1025,
     1915, 1385, 1629, 1638, 1959, 1391, 1130, 1567, 1648],
    [1583, 1390, 1435, 1583, 1272, 1687, 1472, 1480, 1420, 1635,
     1569, 1354, 1576, 1443, 1709, 1087, 1687, 2043, 1435, 1027,
     1917, 1387, 1631, 1640, 1961, 1393, 1132, 1569, 1650]
  ]
}
```

字段说明：

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | `"expression"` — 区分于 blink/servo/llm 等其他消息类型 |
| `session_id` | string | 设备会话 ID（36 字符 UUID） |
| `emotion` | string | 标准表情名（"快乐"/"悲伤"/…），设备端可用于日志/显示 |
| `phase` | string | `"in"` = 中位→表情，`"out"` = 表情→中位 |
| `frame_ms` | int | 每帧持续时间（毫秒）；所有帧等间隔，故省略 per-frame `time_ms` |
| `n_frames` | int | 本 phase 的总帧数（所有 chunk 加总） |
| `frame_offset` | int | 本块首帧在 phase 时间线上的绝对下标；设备按 `frame_offset × frame_ms` 计算起始时间 |
| `frames` | int[][] | 帧数组，每帧为 `[ch0, ch1, …, ch28]` 共 29 个通道脉宽值；按位置索引对应通道 ID |

### 4.2 分块与体积

- `MAX_FRAMES_PER_CHUNK = 4`：每条消息最多 4 帧。
  - 4 帧 × 29 通道 ≈ 116 个数值 × ~6 字节/数 ≈ 700 字节 + JSON 开销 ≈ **≤850 字节**，安全
  - 5 帧 ≈ 1000+ 字节 → 接近/超过 1024 上限，故取 4
- 默认配置下每 phase = 20 帧 ÷ 4 = 5 块；完整一次表情动画 = 2 phases × 5 块 = **10 条 WS 消息**
- 使用 `json.dumps(..., separators=(",", ":"))` 紧凑序列化，无空格

### 4.3 帧插值算法

对每个通道 `ch`：
- 若表情 `servos` 中定义了 `ch`：`value[t] = round(neutral[ch] + (target[ch] - neutral[ch]) × t / (n_frames - 1))`
- 若表情 `servos` 中**未**定义 `ch`：`value[t] = neutral[ch]`（全程保持中位）

其中 `t ∈ [0, n_frames-1]` 为帧序号。phase "in" 时 `target = 表情servos`，
phase "out" 时反向（`from_servos = 表情servos, to_servos = neutral`）。

边界处理：`n_frames = 1` 时直接返回目标姿态单帧（退化情况，正常配置下不触发）。

## 5. 模块 API

`core/handle/expressionActionHandle.py` 对标 `blinkActionHandle.py` 结构：

### 5.1 `expression_action_enabled() -> bool`

- 读取 `ENABLE_EXPRESSION_ACTION` 环境变量
- 默认返回 `True`（开）；设 `0`/`false`/`no`/`off` 返回 `False`
- 空字符串视同未配置，回落默认（开）
- 遵循 `_env_flag("ENABLE_EXPRESSION_ACTION", default=True)` 模式，与
  `blink_action_enabled()` / `servo_action_enabled()` 完全一致

### 5.2 `load_expression_map() -> dict | None`

- 从 `EXPRESSION_SERVO_MAP_PATH` 或默认路径加载 JSON
- 惰性缓存：首次调用读盘，后续返回缓存（`_UNLOADED → dict → None` 三态，与 blink 一致）
- 校验：必须包含 `"neutral"` 和 `"expressions"` 两个顶层 key；
  `"neutral"` 须包含全部 29 个通道（ID 0–28）
- 失败（文件缺失/JSON 解析错误/格式校验失败）→ 打 error 日志，缓存 `None`，返回 `None`

### 5.3 `match_expression(text: str) -> str | None`

- 输入：VLM 返回的自由文本（如 "看起来很开心"、"表情有点难过"）
- 输出：标准表情名（"快乐" / "悲伤" / … 之一）或 `None`（无匹配）
- 逻辑：按 `EXPRESSION_RULES` 列表顺序遍历；`text` 中包含任一关键词即返回对应表情名，
  停止后续检查
- "平静" / "中性" / "未检测到人脸" / "面无表情" / "neutral" 等不匹配任何规则 →
  返回 `None`，不下发指令也不报错（白名单原则）

### 5.4 `generate_frames(from_servos: dict, to_servos: dict, neutral: dict, n_frames: int) -> list[list[int]]`

- 在两个舵机姿态之间生成 `n_frames` 帧线性插值
- `from_servos` / `to_servos` 仅含偏离中位的通道（稀疏）；缺失通道从 `neutral` 补全
- 返回 `list[list[int]]`，每帧 `[ch0, ch1, …, ch28]` 共 29 个值
- 纯函数，无副作用，可单测

### 5.5 `async send_expression_animation(conn, emotion: str) -> None`

- 完整的表情动画协程：等待语音结束 → 进入段 → 保持 → 退出段
- 流程：
  0. **等待语音结束**（新增）：若设备正在说话（`conn.client_is_speaking`），
     通过 `conn.audio_rate_controller` 估算剩余播放时长（`play_position - elapsed_ms`），
     sleep 该时长后轮询 `client_is_speaking`，最长等待 `EXPRESSION_MAX_WAIT_SPEECH_MS`。
     设备未在说话或等待超时→继续。此机制确保表情动画不会与 TTS 音频争抢舵机。
  1. `load_expression_map()` 获取 neutral + target servos
  2. `generate_frames(neutral, target, n_frames=FRAMES_PER_PHASE)` → phase "in" 帧序列
  3. 分块（≤`MAX_FRAMES_PER_CHUNK`）发送 phase "in" 所有帧
  4. `asyncio.sleep(EXPRESSION_HOLD_MS)`
  5. 若连接未关闭（`conn.stop_event` 检查）：`generate_frames(target, neutral, …)` → phase "out"
  6. 分块发送 phase "out" 所有帧
- 发送前检查 `conn.stop_event` 和 `conn.client_abort`，提前终止（不发送过期的舵机指令）
- 使用 `await conn.websocket.send(msg)` 直接在事件循环上发送（非跨线程，与 blink_loop
  一致；不同于 servo 的 `run_coroutine_threadsafe`）
- 尽力而为：任何步骤失败只记日志，不抛异常

### 5.6 `async _wait_for_speech_end(conn) -> None`

- 私有辅助协程：若设备正在说话则等待语音播放结束
- 实现：
  1. 检查 `not conn.client_is_speaking` → 立即返回（未在说话）
  2. 获取 `conn.audio_rate_controller`，若不可用 → 直接返回
  3. 估算剩余音频时长：`remaining_ms = max(0, play_position - elapsed_ms)`
  4. 夹紧到 `[0, EXPRESSION_MAX_WAIT_SPEECH_MS]`
  5. 若 `remaining_ms > 0`：`await asyncio.sleep(remaining_ms / 1000)`
  6. 轮询兜底：以 100ms 间隔检查 `client_is_speaking`，最多再轮询 `EXPRESSION_MAX_WAIT_SPEECH_MS - remaining_ms` 毫秒
  7. 连接关闭（`stop_event` / `client_abort`）→ 提前退出
- `EXPRESSION_MAX_WAIT_SPEECH_MS=0` → 跳过所有等待，直接返回（不等待语音结束）
- 依赖 `conn` 对象的两个属性（均为 `connection.py` 已有字段）：
  - `conn.client_is_speaking: bool` — 服务端正在向设备发送 TTS 音频时为 True
  - `conn.audio_rate_controller: AudioRateController` — 含 `play_position`（已排队的音频总毫秒数）
    和 `_get_elapsed_ms()`（从首个音频包发送起已过的实时毫秒数）

## 6. 集成点

### 6.1 connection.py 改动（核心集成）

在 `_capture_facial_expression()` 的结果处理处，将表情文本消费逻辑改为内置的
expression→servo 下发。**关键设计决策：外部回调先于内置下发执行**，外部回调可通过
返回 truthy 值来表示"已处理，跳过内置下发"（例如外部回调想修改表情名或自行控制舵机）：

```python
# connection.py 头部添加导入
from core.handle import expressionActionHandle

# connection.py _capture_facial_expression() 中，替换现有回调段：

# 外部回调优先：返回真值表示"已处理，跳过内置下发"
handled_externally = False
if self._facial_expression_callback:
    try:
        handled_externally = bool(self._facial_expression_callback(expression_text))
    except Exception as cb_err:
        self.logger.bind(tag=TAG).error(f"表情回调异常: {cb_err}")

# 内置：表情 → 舵机平滑动画面下发
if not handled_externally and expressionActionHandle.expression_action_enabled():
    emotion = expressionActionHandle.match_expression(expression_text)
    if emotion:
        try:
            await expressionActionHandle.send_expression_animation(self, emotion)
        except Exception as e:
            self.logger.bind(tag=TAG).warning(
                f"表情动画下发失败({emotion}): {e}"
            )
```

要点：
- **模块级导入**：`from core.handle import expressionActionHandle`，与 blink 的
  `from core.handle import blinkActionHandle` 风格一致
- **外部回调优先**：返回 truthy → 跳过内置下发。`bool()` 包裹使 `None`/无返回值的
  旧回调也能正确 fall through 到内置逻辑（向后兼容）
- **ENABLE_EXPRESSION_ACTION 门控**：仅关闭内置下发；外部回调始终触发（不受门控影响）
- **协程直接 await**：`send_expression_animation` 在事件循环上运行，直接
  `await`，不走 `run_coroutine_threadsafe`

### 6.2 未来扩展点

`match_expression()` 和 `generate_frames()` 作为独立公共 API，可被以下场景复用：
- **device_action 插件**：语音命令 "做个开心的表情" → 直接查映射并播放动画
- **对话 LLM 触发**：LLM 回复中包含表情标记时自动下发
- **外部 HTTP API**：第三方系统调用接口控制表情
- **manager-api 参数管理**：通过管理台动态更新关键词字典或舵机参数
