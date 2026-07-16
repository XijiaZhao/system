# 人脸表情周期性拍照：从"绑定对话轮次"改为"设备活跃期间固定间隔"

## 背景

当前实现（`core/connection.py` + `core/handle/receiveAudioHandle.py` + `core/handle/sendAudioHandle.py`）里，人脸表情捕获（拍照 + VLLM 识别 + 匹配到的表情下发舵机动画）的触发时机是：

- 每一轮对话开始（`startToChat`）时立即拍一张，并把下次到期时间设为 `now + facial_expression_interval`
- 若单轮对话拖得很长，超过 `facial_expression_interval` 秒后 `_check_timeout` 循环会补拍一张
- 一轮对话的语音发送完（`sendAudioHandle` 发出 `SentenceType.LAST`）后，到期时间清零，定时器停摆

结果是拍照节奏完全跟着对话节奏走：用户说话越频繁，拍照越频繁；用户不说话，拍照就停。实测日志（`main/xiaozhi-server/tmp/server.log`）里两次拍照间隔从 15 秒到 90 秒以上都有，并非固定周期。

## 目标

拍照节奏改为：设备处于"活跃"状态期间，按**固定可配置间隔**周期性拍照，不再与对话轮次绑定。

"活跃"的定义（用户已确认）：WebSocket 连接建立时或对话开始时进入活跃状态；超过可配置的空闲时长（默认60秒）没有对话，进入非活跃状态，暂停拍照；活跃状态恢复后拍照恢复。

日志证据确认：该设备实际运行时从未触发过"唤醒词"相关的两条代码路径（`listenMessageHandler.py` 的 `state:"detect"` 分支、`helloHandle.checkWakeupWords`），而是持续监听/VAD 直接触发对话的模式，因此"活跃"不依赖唤醒词事件，改用连接活动时间戳判定。

## 架构

新增模块 `core/handle/facialCaptureHandle.py`，结构比照项目里已有的两个同类"空闲后台特性"模块（`blinkActionHandle.py` 的 `blink_loop`、`expressionActionHandle.py` 的 `expression_animation_loop`）：

- `facial_capture_enabled(conn) -> bool`：读 `conn.facial_expression_interval > 0`
- `facial_capture_loop(conn) -> None`：后台循环，只看 `conn.stop_event`；每次醒来判断是否"活跃"，活跃且到了拍照时间点就调用已有的 `conn.spawn_facial_capture()`

`_capture_facial_expression`（实际拍照 + VLLM 识别逻辑）完全不改，只改"什么时候调用它"。

任务生命周期比照 blink_task/expression_task：

- 创建：`connection.py` 里 blink_task、expression_task 创建的地方（约308-317行）新增 `self.facial_capture_task = asyncio.create_task(facialCaptureHandle.facial_capture_loop(self))`
- 取消：`close()` 里 blink_task/expression_task 取消的地方（约1482-1508行）新增同样的 cancel + `asyncio.wait(timeout=2.0)`

## 配置项

复用现有参数（不改名、不改解析方式，仅改默认值）：

- `facial_expression_interval`：活跃状态下拍照的固定周期。**默认值由 30 改为 10（秒）**。解析优先级不变：env `XIAOZHI_FACIAL_EXPRESSION_INTERVAL` > 每台设备的 `private_config.facial_expression_interval` > `config.yaml` 默认值。

新增参数：

- `facial_expression_idle_timeout`：判定"活跃"的空闲阈值，默认 `60`（秒）。解析优先级：env `XIAOZHI_FACIAL_EXPRESSION_IDLE_TIMEOUT` > `private_config.facial_expression_idle_timeout` > `config.yaml` 默认值 `60`。

  ⚠️ 命名说明：项目里已有一个含义完全不同的 `facial_expression_timeout`（默认15秒，指"单次拍照+VLLM识别调用本身的超时"）。新参数命名为 `facial_expression_**idle**_timeout` 以示区分，代码注释需明确写清两者差异，避免混淆。

不新增 manager-web 界面项：现有 `facial_expression_interval/prompt/timeout` 三个参数目前都不在 manager-web 界面上，只能走 env / config.yaml / private_config；新参数保持同样的接入方式。

## 活跃状态判定与数据流

直接复用现有字段 `conn.last_activity_time`（毫秒时间戳，检测到用户人声时刷新，已经在 `receiveAudioHandle.no_voice_close_connect` 里维护；连接建立时也已经在 `connection.py:298` 被设置为当前时间），不新增任何状态字段：

```python
idle_ms = time.time() * 1000 - conn.last_activity_time
active = idle_ms <= conn.facial_expression_idle_timeout * 1000
```

已知取舍：`last_activity_time` 只在检测到**用户**人声时刷新，assistant 自己讲话（TTS 播放中）不会刷新它。如果用户提问后一直不说话、assistant 却讲了一段超过 `idle_timeout` 的长回复，周期拍照会在这段独白期间暂停。这是复用现有信号带来的取舍——零新增状态，且和"连接超时关闭"用的是同一套"用户多久没说话"语义；代价是极端场景下会漏拍。本次先按此实现，不做进一步处理。

循环节奏：`facial_capture_loop` 每 **2 秒**醒一次做判定（模块内部常量，不开放配置，纯粹是"多久检查一次该不该拍"的轮询粒度，与"每隔 interval 秒拍一张"是两回事）：

```python
async def facial_capture_loop(conn):
    while not conn.stop_event.is_set():
        idle_ms = time.time() * 1000 - conn.last_activity_time
        active = idle_ms <= conn.facial_expression_idle_timeout * 1000
        if (
            active
            and not conn._facial_expression_running
            and time.monotonic() >= conn.facial_expression_deadline
        ):
            conn.facial_expression_deadline = time.monotonic() + conn.facial_expression_interval
            conn.spawn_facial_capture()
        await asyncio.sleep(2)
```

`facial_expression_deadline` 初始值 0.0（现有代码 `__init__` 里就是这么初始化），意味着连接一建立、循环第一次醒来（2秒内）只要判定为"活跃"就会立刻拍第一张，之后按 `facial_expression_interval` 周期走；不活跃期间自然跳过、不推进 deadline，用户重新开口后最多 2 秒内恢复拍照。

评估过用"订阅用户讲话事件+广播"替代轮询（用户主动提出），结论是不采用：需要的是"活跃窗口内即使没人说话也要按周期继续拍"，这个"无事件发生"的空档期本身仍需超时兜底（`wait_for(event, timeout=interval)`），定时器逻辑省不掉；事件机制只能把"用户重新开口后恢复拍照"的延迟从最多2秒缩短到接近0，收益小，且项目里所有同类后台特性（blink_loop、expression_animation_loop）都是朴素轮询，无先例的 pub/sub 抽象，不引入新同步原语。
