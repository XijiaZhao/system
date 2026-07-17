# VLM 场景描述注入 LLM 上下文、表情由 LLM 决定 

- **日期**：2026-07-17
- **分支**：`visual-context-llm-expression`（已推送到 `mine` 远端，**未并回 main**）
- **提交**：`ac826010`（13 个文件，+719/−144）
- **状态**：代码完成、全量测试 185 通过；

---

## 1. 需求

​	现在的 VLM 仅识别表情并下发表情指令；修改为：VLM 描述看到的东西缓存并注入到下一个 LLM 的上下文中（需要保证消息的新鲜度，丢弃不新鲜的消息），由 LLM 来决定是否下发表情指令和下发怎样的表情指令。

## 2. 改动前现状

- `facialCaptureHandle.facial_capture_loop`（2026-07-16 刚落地）：设备活跃期间每 `facial_expression_interval`（默认 10s）触发 `conn.spawn_facial_capture()`。
- `_capture_facial_expression`：MCP 工具 `self_camera_take_photo`（question=表情识别提示词）→ 设备拍照 HTTP 上传 → VLLM（默认 glm-4v-flash）→ 文本经 `PendingVisionRegistry` Future 返回 → `match_expression()` 关键词匹配 8 种表情 → `enqueue_expression` → 舵机动画 `{"type":"expression",...}` 下发设备。
- **VLM 结果从不进入对话历史/LLM 上下文**，纯粹驱动动画。
- 三个关键现成机制（本次设计的基石）：
  1. `dialogue.py` 的【实时信息】尾注：当前时间/记忆在装配期拼到最新 user 消息末尾、不写持久历史（保护 LLM prefix cache，本项目实测性能红线）。
  2. LLM 已被系统提示词要求每轮回复开头带 21 种 emoji 之一，服务器取首 token emoji 下发 `{"type":"llm","emotion":...}`——现成的"LLM 表达情绪"通道。
  3. 分层惯例：`core/utils` 从不 import `core/handle`（只有反向），`connection.py` 是唯一组合根。

## 3. 需求决策

| 问题 | 决定 |
|---|---|
| LLM 如何决定/下发表情指令 | **emoji 映射**：复用回复开头情绪 emoji，映射到 8 种舵机表情。零延迟、零工具调用（弃 set_expression function-call 工具：要么罐头话术替代自然回复、要么多一次 LLM 往返，且本地 2B 有误调工具前科） |
| 缓存条数与新鲜度 | **仅最新 1 条**，TTL 默认 30 秒可配；过期静默丢弃 |
| 旧"VLM→关键词→直接下发"链路 | **直接删除**（行为变化：用户不说话时不再有表情镜像，表情只随对话轮次出现） |
| VLM 提示词侧重 | **通用场景描述**（人物+环境+物品均衡，40 字内） |

隐含默认（无异议执行）：期望 VLM 描述允许进入长期记忆。

## 4. 架构方案

1. 新模块 `core/utils/visual_context.py`、映射表归 `expressionActionHandle`、connection.py 单协程调度、删死代码、标识符不改名；
2. 通用尾注参数 `extra_realtime_lines`（dialogue.py 保持特性无关，后续功能可复用）；
3. 提示词加 env 覆盖 `XIAOZHI_FACIAL_EXPRESSION_PROMPT`（key 名不动；本部署 config.yaml 无效，无 env 则调提示词要改代码）；
4. 截断上限 120 字符。

## 5. 实现明细

### 5.1 新数据流

```
facial_capture_loop（未动，10s/活跃判定）
  → _capture_facial_expression：MCP拍照 → VLM 通用场景描述
  → visual_context.store(conn, text)  # 冻结dataclass整体替换 = 跨线程原子快照
        ⋮（最多 TTL=30s 后）
chat()（worker 线程）
  → visual_context.get_realtime_line(conn)  # 新鲜→"当前画面（约N秒前）：…"，过期→None
  → dialogue.get_llm_dialogue_with_memory(..., extra_realtime_lines=[行])
        # 装配期拼到最新 user 消息末尾，不进持久历史
  → LLM 流式回复，首个非空 chunk：
      _handle_emotion_and_expression（事件循环线程，单协程调度）
        ├─ get_emotion：发 {"type":"llm","text":emoji,"emotion":词}（原有）＋返回情绪词（新）
        └─ EMOTION_TO_EXPRESSION 映射 → enqueue_expression → 舵机表情动画（原有机制复用）
```

线程安全：写方在事件循环线程、读方在 worker 线程；靠"单属性赋值一个不可变对象"的 GIL 原子性，禁止拆成 description/timestamp 两属性分别读写。

### 5.2 emoji→舵机表情映射（10 词映射 / 11 词不动画）

| 情绪词 | 表情 | | 情绪词 | 表情 |
|---|---|---|---|---|
| funny 😂 / laughing 😆 | 大笑 | | crying 😭 / sad 😔 | 悲伤 |
| angry 😠 | 愤怒 | | surprised 😲 | 惊讶 |
| shocked 😱 | 恐惧 | | thinking 🤔 / confused 🙄 | 疑惑 |
| loving 😍 | 快乐 | | **happy 🙂（兜底）** | **None（必须，防每轮都动）** |

relaxed/sleepy/neutral/embarrassed/winking/cool/delicious/kissy/confident/silly → None（宁缺毋滥）；**厌恶**无可信来源，设计内不可达（其它调用方仍可直接入队）。

## 6. 已知边界与后续可选项

- 用户不说话时不再有表情镜像反应（需求预期内）；若想要"无对话也做表情"，需另立由 VLM/服务端触发的通道。
- "厌恶"表情经 emoji 通道不可达；若需要可给 LLM 提示词扩充 emoji 表或加显式工具。
- 注入行每轮增加 ~130 字符 prefill（描述 120 上限+前缀），对本地栈首 token 影响可忽略但可观测（PERF_ROUND 的 prefill 字段自动含它）。
- 场景描述是外部 VLM 自由文本首次进入对话 LLM 上下文，属轻度提示注入面（与 memory_str 同级，未额外消毒，长度上限已兜底）。
