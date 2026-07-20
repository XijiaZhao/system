# 智能体心情状态机

## 背景

当前情绪系统（`textUtils.get_emotion` → `expressionActionHandle.emotion_to_expression`）完全无状态：每一轮从 LLM 回复文本扫 emoji，映射成舵机表情动画，然后就丢弃了。智能体没有"心情"——它不记得三分钟前用户逗过它，也无法在前一轮沉默后自然恢复基线。

本项目已具备三个现成模式可组合复用：

- `visual_context.py` 的 **per-connection 状态字段**（`conn.visual_context`，构造时初始化、连接生命周期内被覆盖/惰性过期）
- visual_context 的 **`extra_realtime_lines` 注入通道**（`connection.py:1152-1172`，`【实时信息】` 拼入本轮请求、不进对话历史）
- `blinkActionHandle.blink_loop` / `expressionActionHandle.expression_animation_loop` 的 **per-connection asyncio 后台任务**模式（创建 + cancel + `wait(timeout=2.0)` + 尽力而为只记日志）

## 目标

新增一个 per-connection 的心情状态机，实现以下动态：

- **受交互更新**：LLM 回复中的 emoji 情绪词作为智能体自我报告的情绪事件，累加进持久的心情状态
- **重复衰减（habituation）**：同一情绪词连续出现时每次贡献递减，防止纯自指回路漂移
- **一致性消费（congruence consume）**：心情已接近某情绪时再做对应的表情 → delta 被削弱，意味着"情绪被表达/消耗"而非持续叠加
- **分层衰减（tiered decay）**：不同类情绪以不同速率自然遗忘——冲动情绪（愤怒/惊喜）快速消退，弥漫情绪（悲伤）持续更久
- **影响回应基调**：当前心情被量化为自然语言描述行，注入 LLM 系统上下文，使回复语气受心情调节

## 核心概念

### 三层心情组分

每种情绪词不再直接操作一个共享的坐标点，而是按衰减半衰期归入三个独立的 EMA 分量：

| 层级 | 半衰期（可配置） | 收录情绪 | 心理特征 |
|---|---|---|---|
| `fast` | 90s | angry, surprised, shocked, laughing, funny, silly | 冲动型——来得快去得快 |
| `medium` | 5min | happy, loving, confident, cool, delicious, kissy, disgusted, winking, embarrassed, thinking, confused | 事件型——与具体情境绑定 |
| `slow` | 20min | sad, crying, sleepy, neutral, relaxed | 弥漫型——消退缓慢，构成情绪底色 |

每个分量是一个 `@dataclass(frozen=True)` 的不可变三元组 `(valence, arousal, last_updated)`，通过 EMA 独立更新、独立惰性衰减。`update()` 返回新的 `MoodState` 实例（整体替换 `conn.mood_state` 引用），与 `visual_context.store()` 的原子替换模式完全一致——读者要么见旧快照、要么见新快照，不会见部分更新的撕裂状态。读取时三个分量**求和再夹紧**得到有效心情点。

### EMA 更新（共享机制，三个分量各自运行）

```
component += α · (target − component)
```

其中 `α`（学习率）控制单次更新的步长。EMA 自带自限性：越靠近目标，移动越小，永远不越界。

### 递减基数（habituation）

每个情绪词维护一个"最近命中密度"计数器 `habit_count[e]`，按自身的半衰期（默认 3 分钟）惰性衰减。每次该情绪词命中时先做衰减再 +1。

实际 delta 除以 `(1 + habit_count[e] · habit_factor)`，其中 `habit_factor` 默认 0.5：

- 第 1 次命中 → 计数器 1 → 分母 1.5 → 有效 delta = 67% 基础值
- 第 5 次连中 → 计数器 5 → 分母 3.5 → 有效 delta = 29% 基础值
- 该情绪 3 分钟不再出现 → 计数器衰减近零 → 下一次命中接近满额

### 一致性消费（congruence consume）

更新前先计算当前 mood 与目标情绪坐标的**一致性**：

```
distance = sqrt((v_mood − v_target)² + (a_mood − a_target)²)
congruence = clamp(1 − distance / span, 0, 1)
```

其中 `span` 是"判定一致"的半径（默认 0.5，即 mood 在目标 ±0.5 范围时一致性从 1 线性降到 0）。越接近目标，一致性越高，消费越强。

有效 delta = 基础 delta × (1 − congruence · consume_factor)，其中 `consume_factor` 默认 0.8：

| 一致度 | 有效 delta 占比 |
|---|---|
| 1.0（mood = target） | 20%（几乎不增，表达已消耗情绪） |
| 0.5 | 60% |
| 0（mood 远离 target） | 100%（真正的情绪变化） |

### 分层衰减（tiered decay）

每个分量的 `last_updated` 记录上次更新时刻（`time.monotonic()`）。读取时先按实际流逝时间做惰性衰减：

```
decayed = component × (1/2) ^ (dt / half_life)
```

惰性衰减的好处：不创建新 asyncio 任务，没有"tick 粒度 vs 实际衰减精度"的 trade-off，计算精确到秒。

### 心情 → 提示行

有效心情 `(v, a)` 通过阈值切分映射为自然语言描述，注入 `extra_realtime_lines`（和 visual_context 实时行为同一通道，不进对话历史）：

| v ∈ | a ∈ | 示例描述 |
|---|---|---|
| [0.1, 1] | [0.3, 1] | 心情积极且精力充沛，表达可以活泼一些 |
| [0.1, 1] | [-0.4, 0.3) | 心情相对平和偏积极，回应可以温和 |
| [0.1, 1] | [−1, −0.4) | 心情满足但有点疲惫，回应用词可简练 |
| [−0.1, 0.1) | [0.3, 1] | 心情中性但精力充沛，回应可以活泼 |
| [−0.1, 0.1) | [−0.4, 0.3) | 心情中性平静，回应自然即可 |
| [−0.1, 0.1) | [−1, −0.4) | 心情中性但有点累，回应可以简练 |
| [−1, −0.1) | [−0.3, 1] | 心情有点低落但不算疲惫，回应可以温和带点安抚感 |
| [−1, −0.1) | [−1, −0.3) | 心情不太好且疲惫，回应用词可以简短低调 |

未命中任何分桶时（精确落在 0 值上）不注入行，等同于"无特殊心情背景"。

### 迟滞（hysteresis）

当心情值在分桶边界附近振荡时（如 valence 在 0.09 ↔ 0.11 之间反复横跳），注入文本可能在连续轮次中频繁跳变。`MoodState` 维护一个 `last_bucket` 字段（首次为 None），分桶判定规则：

- 若 `last_bucket` 为 None → 正常分桶，记录本次桶
- 若当前坐标仍在 `last_bucket` 区间内 → 保持原桶（正常）
- 若当前坐标离开 `last_bucket` 区间，但距离边界不足 ε（默认 0.05）→ 保持原桶（迟滞生效）
- 若当前坐标离开 `last_bucket` 区间，且距离边界 ≥ ε → 切换新桶，更新 `last_bucket`

ε 通过 env `MOOD_HYSTERESIS_EPSILON` 配置，默认 0.05。

## 22 情绪词 → 目标坐标与衰减层级

EMOJI_MAP 中的 22 个情绪词，每个映射到一个目标 `(valence, arousal)` 和衰减层级：

| 情绪词 | valence | arousal | 层级 |
|---|---|---|---|
| happy | +0.6 | +0.4 | medium |
| laughing | +0.7 | +0.8 | fast |
| funny | +0.7 | +0.6 | fast |
| loving | +0.8 | +0.3 | medium |
| confident | +0.5 | +0.5 | medium |
| cool | +0.4 | +0.1 | medium |
| delicious | +0.5 | +0.3 | medium |
| kissy | +0.6 | +0.2 | medium |
| winking | +0.3 | +0.4 | medium |
| relaxed | +0.3 | −0.2 | slow |
| neutral | 0.0 | 0.0 | slow |
| thinking | +0.1 | +0.1 | medium |
| confused | −0.1 | +0.2 | medium |
| embarrassed | −0.2 | +0.3 | medium |
| surprised | +0.1 | +0.9 | fast |
| shocked | −0.4 | +0.9 | fast |
| angry | −0.6 | +0.7 | fast |
| disgusted | −0.5 | +0.2 | medium |
| silly | +0.5 | +0.6 | fast |
| sleepy | +0.1 | −0.8 | slow |
| sad | −0.5 | −0.3 | slow |
| crying | −0.7 | +0.2 | slow |

## 配置项

### 新增 env 变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MOOD_ENABLED` | `"1"` | 心情状态机总开关，`"0"` / `"false"` 关闭（关闭时 `format_mood_line` 恒返回 None，`update` 返回原实例） |
| `MOOD_ALPHA` | `"0.3"` | EMA 学习率，0-1 |
| `MOOD_HABIT_HALF_LIFE` | `"180"` | habit 计数器半衰期（秒） |
| `MOOD_HABIT_FACTOR` | `"0.5"` | habit 递减强度 |
| `MOOD_CONSUME_FACTOR` | `"0.8"` | 一致性消费强度 |
| `MOOD_CONGRUENCE_SPAN` | `"0.5"` | 一致性判定半径 |
| `MOOD_FAST_HALF_LIFE` | `"90"` | fast 层半衰期（秒） |
| `MOOD_MEDIUM_HALF_LIFE` | `"300"` | medium 层半衰期（秒） |
| `MOOD_SLOW_HALF_LIFE` | `"1200"` | slow 层半衰期（秒） |
| `MOOD_HYSTERESIS_EPSILON` | `"0.05"` | 分桶迟滞带宽度，0 关闭迟滞 |

所有旋钮通过 `core.utils.env._env_float` / `_env_int` 在连接建立时读一次 env，存入 `MoodState` 实例（与 blink/servo/expression 各 handler 的 `_env_flag` 模式一致，共享同一份实现），连接生命周期内不变。不支持 manager-api 动态覆盖（初期没必要，纯 env 就够）。

## 边界情况

- **首次对话、无情绪历史**：三个分量均为 (0, 0)，惰性衰减跳过（dt ≈ 0），有效心情 (0, 0) 不在任何分桶区间 → `format_mood_line` 返回 None → 不注入，等同于"无心情背景"
- **长时间静默后恢复**：惰性衰减按实际 dt 计算，可能所有分量均已归零 → 同上
- **同一情绪词连续 10+ 次**：habit 计数器压制 → 有效 delta 极低 → 心情不极化；切换到新情绪则 habit 计数器仍在 3min 内残存影响新情绪 → 消费机制主导
- **连接关闭**：心情状态随连接对象一同销毁，无需显式清理（无 asyncio task/subscription）
- **mood 更新和读取的并发**：更新在事件循环线程（`_handle_emotion_and_expression`），读取在 worker 线程（`chat()` 的 prompt 装配阶段）。`MoodState` 为 `@dataclass(frozen=True)` 不可变对象——`update()` 返回新实例，调用方通过 `conn.mood_state = new_state` 整体替换引用。依赖 CPython 的"单个属性赋值不可变对象"原子性（与 visual_context 的线程安全约定完全一致），最坏情况是读到旧 snapshot（本轮或上一轮的心情），不会读到半更新的撕裂状态。
- **关闭功能后重启**：`MOOD_ENABLED=0` 时状态机不更新不注入，所有存量 `conn.mood_state` 静默冻结

## 非目标

- 不接入 manager-api / manager-web 配置界面（初期纯 env，有需求再加，同 facial_expression_* 的历史路径）
- 不跨连接持久化（连接断开心情重置，不做数据库/Redis）
- 不引入用户情绪判定（智能体心情信号源 = LLM 自我报告的 emoji，不读用户输入语气/摄像头表情）
- 不影响 TTS 语速/音调/音量（prosody 仅静态，本次不改）
- 不修改 `EMOTION_TO_EXPRESSION` 映射或舵机动画逻辑（表情动画路径完全原样保留）
- 不创建后台 asyncio 任务（惰性衰减不需要 tick）

## 为什么不做的事

**不做"断回路"（外部锚点）。** emoji 信号来自 LLM 输出、心情行注入 LLM 输入，回路确实存在且有意保留——这是智能体自我一致的心情，不是外部观察的。递减基数 + 一致性消费双重压制是"回路上的稳定器"，不是"切断回路的外锚"。如果实际运行中仍观察到极端漂移，再考虑加弱的外部修正项（如用户输入文本的情绪极性做微拉力）。但现阶段先靠内部动力学，不引入新信号源，保持改动面最小。

**不做 per-connection 后台衰减 tick。** 惰性衰减比定时器更精确（不受 tick jitter 影响）、更简单（无 task 创建/取消/stop_event 检查）、更省资源（无 CPU 唤醒）。唯一代价是"心情值不随时间自发变化直到下一次读"——但心情只在被读时（prompt 装配）才有意义，所以这不是代价。
