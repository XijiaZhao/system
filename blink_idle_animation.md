# 空闲眨眼动画 —— 设计文档

## 1. 需求背景

用户希望机器人在**空闲/日常状态**下，每隔 2~6 秒（随机、正态分布、每台设备独立）向所有活跃（已连接）设备下发一次眨眼动作，让设备看起来更"活着"，而不依赖用户说"眨个眼"才触发。

素材来源：仓库根目录新增的 `blink_instruction_set.json`——一段 460ms、42 帧、逐帧精确 PWM 脉宽的眨眼动画（通道 6=左上眼皮、通道 7=右上眼皮）。

## 2. 与既有"blink"能力的整合（覆盖，非并存）

`docs/dev-notes/设备动作指令需求文档.md` 记录了一条**已存在**的语音触发链路：用户说"眨个眼" → 意图识别命中 `device_action` 插件（`plugins_func/functions/device_action.py`）→ 服务端下发 `{"type":"llm","emotion":"blink","session_id":...}` → 固件执行**自己内置**的眨眼动画（无帧数据下发）。

按用户要求，本次改造把这条链路的 blink 分支**收编**到新方案：`device_action` 命中 `command == "blink"` 时，不再发旧的 `{"type":"llm","emotion":"blink"}`，改为调用与第 7 节定时任务共用的同一个消息构建函数，发新的 `{"type":"blink",...}` 逐帧消息（格式见第 5 节；改造细节见第 6 节）。语音回复文案（`COMMAND_RESPONSES["blink"] = "好的，眨眨眼~"`）不变，只换 WS 消息本身。

`device_action` 的其它分支（`eye_roll`/`smile`/`open_mouth`/`wink_left`/`wink_right`/`set_eye_speed`/`set_neck_speed`）不受影响，继续发旧的 `{"type":"llm","emotion":command,...}`——`blink_instruction_set.json` 只覆盖双眼同步眨眼这一个动作，其余动作没有对应帧数据可用。

**此切换不受 `ENABLE_BLINK_ACTION` 开关门控**（用户明确要求"无条件切换"）：一旦本功能代码上线，语音说"眨个眼"就会发新版 `type:"blink"` 消息，与第 7 节的定时空闲眨眼是否开启无关。这意味着**固件必须支持解析 `type:"blink"` 才能让语音触发的"眨个眼"继续有动画效果**——开关关闭时只是不影响语音回复文案，动画消息本身在固件升级前会被固件忽略（原有内置动画不再触发）。这是用户明确选择的取舍，此处记录以便追溯。

## 3. 架构选型

**独立的每连接 asyncio 任务**（新 `blink_loop(conn)` 协程，定义在 `core/handle/blinkActionHandle.py` 而非 ConnectionHandler 方法——与 `sendAudioHandle`/`receiveAudioHandle` 等既有 handler 一致的"模块函数收 conn"风格，`connection.py` 只负责 create_task / cancel 两处接线），仿照 `_facial_expression_tasks` 的生命周期管理：连接建立时创建、`close()` 时取消。任务运行在连接自己的事件循环上，可直接 `await conn.websocket.send(...)`，不需要像 `ServoActionWorker` 那样跨线程 `run_coroutine_threadsafe`。天然满足"每设备独立"——各任务的随机采样互不干扰。循环体放在 handler 模块的另一个收益：单元测试用轻量 fake conn 即可驱动完整循环，无需实例化重量级 ConnectionHandler。

## 4. 数据文件

`blink_instruction_set.json` 从仓库根目录移动到 `main/xiaozhi-server/config/assets/blink_instruction_set.json`——这是本仓库现有的静态资源存放约定（`wakeup_words.wav`、`bind_code/*.wav` 等已在此目录，会被 git 跟踪；而 `data/` 目录整体被 `.gitignore` 排除，是运行时状态而非可提交资源，不适合放这个文件）。

新模块 `core/handle/blinkActionHandle.py` 在**首次调用时惰性加载并缓存**该文件（模块级缓存，进程内只读盘一次，成功与失败均缓存；不在每次 tick / 每次发送时重复读盘）。选惰性加载而非模块导入时加载的原因：① `BLINK_INSTRUCTION_SET_PATH` 环境变量覆盖（见第 8 节）不受 import 顺序影响；② 单元测试重置缓存 + 改环境变量即可覆盖"文件缺失/解析失败"分支，无需 `importlib.reload`。asyncio 事件循环与 `device_action` 工作线程并发触发首次加载的竞态最多导致重复读一次盘，结果幂等，接受、不加锁。

默认路径 `config/assets/blink_instruction_set.json` 为**相对 CWD** 的路径——与 `receiveAudioHandle.py`/`helloHandle.py` 引用同目录资产的既有写法一致（服务始终从 `main/xiaozhi-server` 启动，pytest 的 rootdir 也是它）。

加载时做轻量一致性校验：源文件的 `num_frames` 与 `frames` 实际长度不符、或 `total_duration_ms` 与最后一帧 `time_ms` 不符时记 warning（不中断，消息字段以实际帧数组为准，见第 5 节）。文件缺失或解析失败：记一条 error 日志并缓存失败标记，`build_blink_message()`（见第 5 节）此后恒返回 `None`，两处调用方（第 6 节 `device_action`、第 7 节定时任务）各自按第 6/9 节描述的方式优雅降级，不影响服务启动、不影响其它功能。

## 5. 下发设备的消息格式

新增消息类型 `"type":"blink"`，与既有 `"type":"servo"`（AudioAction 口型，13 维 DoF 位置数组，两者语义/寻址方式完全不同，已确认无物理通道重叠）区分开：

```json
{"type":"blink","session_id":"<conn.session_id>","total_duration_ms":460,"n_frames":42,"frames":[[0,1470,1478],[10,1470,1478],"...",[460,1470,1478]]}
```

- `frames` 每项为 `[time_ms, channel_6_pulse, channel_7_pulse]`——**按位置**而非每帧重复 `{"6":...,"7":...}` 键值对，以压缩体积（固定通道顺序：索引 1 = 通道 6/左上眼皮，索引 2 = 通道 7/右上眼皮）。
- `n_frames` 取 `len(frames)` 实际长度、`total_duration_ms` 取源文件同名字段——**不直接信任**源文件的 `num_frames` 字段（两者不一致时以帧数组为准并在加载时 warning，见第 4 节）。
- 用 `json.dumps(separators=(",",":"))` 紧凑序列化。整段 42 帧序列化后**实测 778 字节**（含 36 字符 UUID `session_id`），落在 `servoActionHandle.py` 历史事故（ESP32 WS 接收缓冲 ~1024 字节，超大单帧曾导致固件 WS 传输层崩溃、整轮音频中断）已验证安全的 ~900 字节区间内，**当前不需要分块下发**。
- 保险措施：首次加载时用等长占位 `session_id` 计算一次序列化后的字节数，若未来该文件被换成更大/更密的动画导致超过安全阈值（900 字节），打印明显的 warning 日志提示需要引入分块——不预先为当前用不上的场景构建分块机制（YAGNI），但确保回归可被及时发现，不会重演静默超限的事故。
- 帧数据本身（含源文件里两组连续重复帧：160ms 和 200ms 各出现两次）原样透传，不做去重/改写，避免误改动画语义。
- 该消息由 `blinkActionHandle.build_blink_message(session_id: str) -> str | None` 统一构建（帧数据是惰性加载后缓存的模块级数据，函数只做 `session_id` 填充 + 序列化）。**两处调用方共用同一个函数**：第 7 节的定时任务、第 6 节 `device_action` 的 blink 分支——避免消息格式/加载逻辑重复两份、后续改格式只改一处。加载失败时返回 `None`（对应第 4 节的降级）。

## 6. device_action 语音触发链路改造

`plugins_func/functions/device_action.py` 的 `device_action()` 函数中，`command == "blink"` 分支专门处理：

```python
new_msg = None
if command == "blink":
    new_msg = blinkActionHandle.build_blink_message(conn.session_id)

if new_msg is not None:
    msg = new_msg
else:
    # 非 blink 指令；或 blink 动画文件加载失败的兜底（至少保留固件内置旧动画能继续触发）
    payload = {"type": "llm", "emotion": command, "session_id": conn.session_id}
    if params:
        payload["params"] = params
    msg = json.dumps(payload)
```

兜底路径与非 blink 分支共用同一段旧消息组装代码，**含 `params` 透传**——退化路径必须与改造前逐字节一致（旧固件对 blink 的 `count` 参数处理不变），不引入第二种"旧消息但缺 params"的中间形态。`device_action.py` 顶部新增 `from core.handle import blinkActionHandle`（该模块不反向依赖 `core.connection`，无循环导入）。

发送方式不变，沿用该函数现有的 `asyncio.run_coroutine_threadsafe(conn.websocket.send(msg), conn.loop)` 跨线程下发路径（`device_action()` 本身是同步函数，不能直接 `await`）；语音回复（`ActionResponse`/`COMMAND_RESPONSES`）逻辑完全不变。

**不支持 `count` 参数**：函数签名里 `params.count`（重复眨眼次数，默认 1，见需求文档 3.1 节）在旧的 `emotion` 触发模式下由固件自行处理重复；新的逐帧流式消息只发一次完整动画（42 帧、460ms），`count>1` 时**不会**重复拼接多次动画，本次改造范围内该参数对 blink 分支不生效（其它仍走 `emotion` 消息的分支不受影响）。若后续需要连续多次眨眼，需在 `build_blink_message` 里拼接多份帧序列并累加时间偏移，留作后续需求，本次不做。

## 7. 调度逻辑

每连接一个 `blinkActionHandle.blink_loop(conn)` 协程（模块函数收 conn，见第 3 节）：

```
while not conn.stop_event.is_set():
    interval = clamp(random.gauss(mu, sigma), min_s, max_s)   # mu=(min+max)/2, sigma=(max-min)/6，±3σ 覆盖配置区间；默认 [2,6] → mu=4, sigma≈0.667
    await asyncio.sleep(interval)
    若 conn.stop_event 仍未置位: 发送一次 blink 消息
```

- 存活判定**只看 `stop_event`**（`close()` 会置位它，且任务本身会被 cancel）。**明确不用 `client_abort`**——那是"用户打断本轮 TTS"的每轮标志（打断时置 True `connection.py:1438`、新一轮开始时复位 `:1108`），不是连接存活标志；若用它门控，用户打断一次后空闲眨眼会静默停摆到下一轮对话才恢复，违背下一条"眨眼独立于说话"的原则。
- mu/sigma 由第 8 节的 min/max **推导**而非硬编码——否则用户改配置区间后分布塌缩到边界（如 min=5/max=15 时 `gauss(4, 0.667)` 几乎恒被 clamp 成 5，抖动名存实亡）。min > max 视为配置错误：记 warning 并回退默认 `[2,6]`；min == max 退化为固定间隔，允许。
- 明确**不**依据 `client_is_speaking` 做门控——按用户确认，眨眼是独立的生理动作，说话时也应正常触发。
- 首次眨眼同样走一次随机采样（不做"连接建立立即眨眼"之类的特殊首帧逻辑），实现最简单。
- 任务创建时机：`handle_connection` 中 `register_connection`（`connection.py:280`）之后、与现有 `self.timeout_task = asyncio.create_task(self._check_timeout())`（`:296`）相邻处，保证 `device_id`/`session_id`/`websocket` 已就绪；仅当 `ENABLE_BLINK_ACTION` 开启才创建。`__init__` 中在 `self.timeout_task = None`（`:205`）旁初始化 `self.blink_task = None`。
- 任务清理：`close()` 中比照 `_facial_expression_tasks` 现有取消逻辑（`:1462-1467`），新增对 `self.blink_task` 的 cancel + `asyncio.wait({task}, timeout=2.0)`（带超时保护，避免 close() 被卡住）。

## 8. 配置项（纯环境变量，本部署 config.yaml 不生效的既有约定）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ENABLE_BLINK_ACTION` | **关闭**（`false`） | **仅门控第 7 节的定时空闲眨眼任务**是否创建/运行；不影响第 6 节 `device_action` 的 blink 分支（该分支无条件切换到新消息，见第 2 节）。`type:"blink"` 对定时任务而言是固件从未见过的全新触发面，默认关闭，待确认固件已支持后再由用户显式打开 |
| `BLINK_MIN_INTERVAL_SECONDS` | `2` | 抖动下界（秒，可为小数）。mu/sigma 由 min/max 推导（见第 7 节）；min > max 记 warning 回退默认 `[2,6]` |
| `BLINK_MAX_INTERVAL_SECONDS` | `6` | 抖动上界（秒，可为小数） |
| `BLINK_INSTRUCTION_SET_PATH` | 空（使用内置 `config/assets/blink_instruction_set.json`） | 可选覆盖动画文件路径（相对 CWD 或绝对路径） |

沿用 `servoActionHandle.py` 里的 `_env_flag`/`_int`/`_float` 小工具风格（该仓库目前是每个 handler 模块各自维护一份同款小工具，未抽公共 util，本次沿用现状不新增共享模块）。开关读取函数命名 `blink_action_enabled()`，对齐既有 `servo_action_enabled()`。读取时机：`ENABLE_BLINK_ACTION` 在连接建立时读一次（决定是否创建任务），区间参数在每次采样时读——env 是进程级、启动后不变，两者行为等价，差异仅是实现自然度。

## 9. 错误处理

与本仓库其它旁路功能（`ServoActionWorker` 等）一致的"尽力而为"原则：

- 定时任务 `blink_loop`：发送失败（`websocket.send` 抛异常、连接已关闭等，可能是**瞬时**故障）只记日志、跳过本次，循环继续等待下一个随机间隔；`build_blink_message()` 返回 `None`（动画加载失败，结果已缓存、**永久性**）则记一条 warning 后**退出循环**——没有可发的数据，留着任务每几秒空转没有意义。两种情况都绝不抛出到主收发消息循环、绝不影响音频/对话主链路。
- `device_action` 的 blink 分支：`build_blink_message()` 返回 `None` 时按第 6 节回退发旧 `emotion` 消息（含 `params` 透传，与改造前逐字节一致）；`websocket.send` 失败复用该函数现有的 try/except（返回"动作执行失败了"的 `ActionResponse`），不额外改动。
