# 智能体自主创造力发言（Creative Musing）

## 背景

当前智能体完全被动——只在用户开口后才回应。缺乏自主表达的能力，无法体现"好奇心"或"有独立思想"。本项目已具备几个可直接复用的基础设施：

- `facial_capture_loop` 的 `should_capture()` 闲置判定（已含 `client_is_speaking` 处理，TODO-5）
- `visual_context` 的 VLM 场景描述缓存（`get_current_description` 不限新鲜度）
- `mood_state` 的心情状态机（三层 EMA + 惰性衰减，注入 LLM 上下文）
- `blink_loop` 的后台 asyncio 任务模式（per-connection、`close()` 时 cancel、尽力而为）
- `_recover_hung_round` 的简化 TTS 路径（`tts_one_sentence` + `tts_text_queue`，不经过 `chat()`）

## 目标

在用户从活跃转入非活跃时（复用 VLM 拍照循环的闲置判定），以最后 VLM 输出 + 短期对话记忆 + 当前心情为上下文，让 LLM（高温度、无工具）自主发起一段简短发言——讲笑话、提问、分享感受、表达想法。目标是体现"GPT 创造力"，让智能体展现出好奇心和自主表达能力。

## 核心决策

| 维度 | 决策 |
|---|---|
| 触发时机 | 复用 `should_capture()` 从 True→False 的下降沿（60s 闲置阈值），与停止拍照同源 |
| 交付方式 | TTS 语音播报，走正常 TTS 链路 |
| 上下文 | 最后 VLM 输出 + 最近 5 轮对话历史 + mood_state 心情注入 + 高自由度 system prompt |
| LLM 配置 | 和对话复用同一 LLM，仅调高 temperature（默认 1.2） |
| 对话历史 | 创造力发言入库 `dialogue.put`（后续对话可引用） |
| 心情更新 | 创造力发言的 LLM 回复也提取 emoji → 更新 mood_state（但不触发舵机表情动画） |
| 反骚扰 | TODO（冷却时间已做，次数上限/打断退避留待后续） |

## 架构

### 触发：复用 `facial_capture_loop` 的闲置判定下降沿

`facial_capture_loop` 每 2s 调用 `should_capture()`，已内含正确的 `client_is_speaking` 处理。加一个局部状态变量 `was_active` 追踪上一轮是否活跃，检测下降沿：

```
facial_capture_loop 每次醒来：
  is_active = should_capture(...)   # 已有逻辑

  if was_active and not is_active and not running:
      _maybe_trigger_creative_musing(conn)   # 下降沿触发（不 await）
  was_active = is_active

  if is_active and not running and now >= deadline:
      ...  # 原有拍照逻辑不变
```

**优点**：
- 单一真相源：闲置判定逻辑只在 `should_capture` 一处
- `client_is_speaking` 的 TODO-5 修复自动继承
- 零新增轮询
- 精确边沿：只在"刚好变不活跃"那一刻触发一次

### 整体流程

```
facial_capture_loop 检测到 is_active: True → False
  │
  └─ _maybe_trigger_creative_musing(conn)
       │
       ├─ 门控：CREATIVE_MUSING_ENABLED? mood_state.enabled? 冷却?
       │
       └─ asyncio.create_task(_do_creative_musing(conn))
            │
            ├─ 1. _build_creative_messages(conn)
            │     - system prompt（角色设定：高自由度）
            │     - mood line（来自 mood_state 的心情注入）
            │     - 最近 5 轮对话历史
            │     - 当前画面（最后 VLM 描述）
            │
            ├─ 2. loop.run_in_executor → _call_creative_llm(conn, messages)
            │     高温度 (1.2)、无工具 (tools=None)、短 token (80)、流式
            │     client_abort 时中止 → 不入库不播报
            │
            ├─ 3. get_emotion(text) → mood_state.update()
            │     （只更新心情，不触发舵机表情动画）
            │
            ├─ 4. dialogue.put(Message(role="assistant", content=text))
            │     （入库，后续对话可引用）
            │
            ├─ 5. tts_one_sentence() + tts_text_queue
            │     （语音播报，走简化 TTS 路径）
            │
            └─ 6. last_musing_time = time.monotonic()
                  （更新冷却计时）
```

### 与 `chat()` 的关键区别

| | chat() | _do_creative_musing |
|---|---|---|
| 触发方 | 用户输入 | 系统自主（活跃→非活跃边沿） |
| 工具调用 | 有（function_call） | 无（tools=None） |
| emotion 舵机表情 | emoji → 舵机动画 | 无（不调 `emotion_to_expression`/`enqueue_expression`） |
| mood_state 更新 | 有 | 有（仅心情，不含动画） |
| client_abort 检查 | 流式过程中 | 同 |
| dialogue.put | 有 | 有 |
| 对话轮次计数 | 有（trim_history 等） | 不参与 |
| PERF_ROUND | 有 | 无（不污染延迟度量） |
| 上下文装配 | `get_llm_dialogue_with_memory()`（含工具规则、memory） | `_build_creative_messages()`（独立构造，不含工具规则） |

## 配置项

| env 变量 | 默认 | 说明 |
|---|---|---|
| `CREATIVE_MUSING_ENABLED` | `"1"` | 总开关，`"0"` / `"false"` 关闭 |
| `CREATIVE_MUSING_TEMPERATURE` | `"1.2"` | LLM 温度（高于对话默认） |
| `CREATIVE_MUSING_MAX_TOKENS` | `"80"` | 最大输出 token |
| `CREATIVE_MUSING_COOLDOWN` | `"300"` | 两次发言最小间隔（秒，默认 5 分钟） |
| `CREATIVE_MUSING_HISTORY_TURNS` | `"5"` | 注入的对话历史轮数 |
| `CREATIVE_MUSING_SYSTEM_PROMPT` | 见上文 | 可覆盖 system prompt |

闲置阈值复用 `facial_expression_idle_timeout`（默认 60s），不新增独立配置项。

所有旋钮通过 `core.utils.env._env_flag` / `_env_int` / `_env_float` 在连接建立时读一次 env，连接生命周期内不变。不支持 manager-api 动态覆盖。

## 边界情况与风险

| 场景 | 处理 |
|---|---|
| 创造力 LLM 调用期间用户开口 | `client_abort` 检查 → 中止生成，不播不入库 |
| 创造力发言播放期间用户开口 | `client_is_speaking=True` + VAD 命中 → abort 中断播放（已有机制） |
| 无摄像头 / VLM 从未输出 | `visual_context` 为 None → 跳过画面注入，仅用对话历史 |
| mood_state 未上线（`MOOD_ENABLED=0`） | 跳过心情注入 + 心情更新，其余正常 |
| VLM 描述已过期（>30s TTL） | `get_current_description` 不限新鲜度，仍注入（旧画面 > 无画面） |
| 连接关闭竞态 | `conn.musing_task` 在 close() 中按 blink_task 模式显式 cancel+timeout（对标现有三个后台任务）；executor 在 close() 末尾 shutdown |
| LLM 返回空/纯符号 | 不入库不播报（`if not text.strip()`） |
| 连续多次下降沿（抖动） | 下降沿检测 + 冷却时间双重防护 |
| 创造力发言失败 | 记日志，不影响 `facial_capture_loop` 和对话主链路 |

## TODO：反骚扰机制

| 项目 | 说明 |
|---|---|
| 每连接最大自主发言次数 | 默认 3 次，env `CREATIVE_MUSING_MAX_PER_SESSION` |
| 用户打断后退避 | 若创造力发言被 abort 打断，冷却时间翻倍或暂停该连接后续触发 |
| 深夜模式 | 可考虑跟随设备本地时间（需要通过 private_config 下发），暂不实现 |

## 非目标

- 不创建新的事件通道（复用 `facial_capture_loop` 的轮询）
- 不影响 `chat()` 链路（不加分支、不改参数）
- 不影响舵机表情动画（创造力发言不触发表情）
- 不接入 manager-api / manager-web 配置界面（纯 env）
- 不跨连接持久化（每次连接独立计数）
