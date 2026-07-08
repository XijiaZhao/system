# 记忆数据流与可行性分析

## 1. mem_local_long运行时数据流

### 1.1 查询记忆（热路径 — 每轮对话触发）

**触发点**：LLM 请求组装前，同步阻塞。

```
用户说话 → ASR 识别 → chat()
  │
  ├─ ① query_memory(query="今天天气怎么样")
  │     ├─ 提取纯文本 query（解析 JSON 格式的 ASR 输出）
  │     └─ await self.memory_client.search(
  │             query=search_query,
  │             user_id=self.role_id,     # "AA:BB:CC:DD:EE:FF"
  │             limit=30
  │         )
  │           │
  │           └─ mem_local_long 内部：
  │               ❶ Embedding API ──→ query → [0.23, -0.71, 0.08, ...]
  │               ❷ sqlite-vec ──→ 余弦检索 top-30
  │               ❸ 艾宾浩斯衰减因子 × 相关性分数 → 最终排序
  │               ❹ 返回 {"results": [{memory, updated_at}, ...]}
  │           ~100-400ms（主要是 Embedding API 网络往返）
  │
  ├─ ② 拼入对话历史
  │     注入到最新 user 消息尾部：
  │       "今天天气怎么样\n\n【实时信息】\n当前时间：14:30\n
  │        相关记忆：\n- [2026-07-08 14:22] 用户喜欢喝咖啡\n
  │        - [2026-07-08 13:15] 用户叫张三"
  │
  └─ ③ 发送 LLM（带记忆的完整对话）
```

**延迟影响**：`query_memory` 通过 `asyncio.run_coroutine_threadsafe + .result()` 同步阻塞 LLM 请求线程。用户每轮对话在 LLM TTFB 之外额外等待 embedding API 往返时间（~100-400ms）。

### 1.2 保存记忆（冷路径 — 连接关闭时触发）

**触发点**：独立 daemon thread，fire-and-forget，不阻塞用户。

```
WebSocket 断开 → _save_and_close()
  │
  ├─ 立即关闭连接，不等待记忆保存
  └─ threading.Thread(daemon=True) → save_memory_task()
       └─ loop.run_until_complete(
              self.memory.save_memory(
                  self.dialogue.dialogue,    # 本次完整对话历史
                  self.session_id
              )
          )
            │
            ├─ 过滤 system 消息，保留 user/assistant/tool
            ├─ 转成 [{"role": "user", "content": "..."}, ...] 格式
            └─ await self.memory_client.add(messages=messages, user_id=self.role_id)
                  │
                  └─ mem_local_long 内部流水线：
                      ❶ LLM API ──→ 事实提取 + 重要性评分
                         "User likes coffee (importance: 0.7)"
                         "User's name is 张三 (importance: 0.9)"
                      ❷ 冲突检测（同名事实 → 合并更新，保留最新时间戳）
                      ❸ Embedding API ──→ 每条事实 → 向量
                      ❹ INSERT INTO sqlite (user_id, memory, embedding, ebbinghaus_schedule)
                      ❺ 可选：UserMemory 模式额外提取用户画像
```

### 1.3 完整时序

```
时间轴 ────────────────────────────────────────────────────────────►

[启动]               [每轮对话]                   [会话关闭]
  │                    │                            │
  │ AsyncMemory()      │ search(query)              │ add(messages)
  │ ├─ sqlite open     │ ├─ Embedding API(100-400ms)│ ├─ LLM 提取(1-3s)
  │ ├─ embedder ready  │ ├─ vec search(<10ms)       │ ├─ LLM 评分(<1s)
  │ └─ 常驻 ~50MB      │ └─ 格式化 + 衰减(<5ms)     │ ├─ Embedding(200-500ms)
  │                    │                            │ └─ sqlite INSERT(<10ms)
  │                    │                            │
  │ ~1s（一次）         │ +100-400ms（每次用户说话）       │ ~3s（后台线程）
  ▼                    ▼                            ▼
```

## 2. 数据存储

### 2.1 SQLite 结构（`vector_store.provider: sqlite`）

```
xiaozhi-server/
    ├── memories             ← 记忆事实表
    │   ├── user_id          ← device_id 隔离
    │   ├── agent_id         ← agent 隔离（多智能体场景）
    │   ├── session_id       ← 会话级隔离
    │   ├── memory           ← 事实文本
    │   ├── importance       ← 重要性评分 (0.0-1.0)
    │   ├── embedding        ← 向量
    │   └── metadata         ← 创建/更新时间、来源等
    │
    ├── embeddings           ← 向量索引（sqlite-vec）
    │
    └── ebbinghaus           ← 艾宾浩斯衰减调度
        ├── decay_factor     ← 当前衰减因子 R(t) = e^(-λt)
        ├── last_review      ← 最后复习时间
        └── next_review      ← 下次复习时间
```

### 2.2 多租户隔离

```
├── device_AA:BB:CC:DD:EE:FF
│   ├── "用户叫张三"
│   ├── "用户是程序员"
│   └── "用户喜欢喝咖啡"
│
├── device_11:22:33:44:55:66
│   ├── "用户叫李四"
│   └── "用户在北京"
│
└── ...（完全隔离，互不可查）
```

## 3. 开销评估

### 3.1 延迟

| 操作 | 触发频率 | 延迟来源 | 估算 |
|------|---------|---------|------|
| `query_memory` | **每轮用户说话** | Embedding API 网络往返 + sqlite 检索 | **100-400ms** |
| `save_memory` | 每次会话关闭 | LLM API + Embedding API | 2-5s（后台） |
| 模块初始化 | 服务启动 | sqlite 连接 + embedder 初始化 | ~1s |

**关键**：query_memory 的 100-400ms 是**热路径上的新增延迟**，直接加在用户感知的 TTFB 之前。对比当前 `nomem`（0ms），这是确定的性能倒退。

### 3.2 API 费用

#### 典型会话（5 轮对话）单次成本

| 操作 | 每次 Token | 频率 | 会话总量 |
|------|-----------|------|---------|
| query Embedding | ~200 tokens | ×5 轮 | ~1,000 tokens |
| save LLM 提取 | ~2,000 in + 500 out | ×1 | ~2,500 tokens |
| save Embedding | ~300 tokens/事实 × 5 条 | ×1 | ~1,500 tokens |
| **合计** | | | **~5,000 tokens** |

#### 各方案月度成本估算（按 100 次会话/天，30 天）

| 方案 | LLM | Embedding | 月成本 |
|------|-----|-----------|--------|
| 智谱 | glm-4.5-air ¥2.4/1M tok | embedding-3（免费） | **~¥0.50** |
| 阿里云百炼 | qwen-plus ¥0.004/1K tok | text-embedding-v4 ¥0.0007/1K tok | **~¥0.90** |
| OpenAI | gpt-4o-mini ¥0.15/1M tok | text-embedding-3-small ¥0.02/1M tok | **~¥0.03** |

**结论：费用可接受。**

### 3.3 系统资源

| 资源 | 影响 |
|------|------|
| 内存 | +50-100MB（embedding client + sqlite 连接池） |
| 磁盘 | powermem.db 初始 ~100KB，线性增长 |
| CPU | 可忽略（sqlite-vec 本地检索 < 10ms） |

### 3.4 故障面

| 故障点 | 概率 | 影响 | 容错 |
|--------|------|------|------|
| Embedding API 不可用 | 中 | `query_memory` 异常 → 返回 `""` | ✅ 降级为空记忆，不阻断对话 |
| LLM API 不可用 | 中 | `memory_client.add()` 失败 | ✅ 静默丢失本次记忆 |
| sqlite 文件损坏 | 低 | 全部操作失败，`use_powermem=False` | ✅ 降级为 nomem |

**容错策略合理：记忆模块故障不阻断主对话链路，始终降级到无记忆状态。**

## 4. 与现有方案对比

| 维度 | nomem | mem_local_short | mem_local_short |
|------|:---:|:---:|:---:|
| query 延迟 | 0ms | ~0ms（内存读） | **100-400ms** |
| save 延迟 | 0 | 2-5s（阻塞线程） | 2-5s（不阻塞） |
| 检索方式 | 无 | 全量返回 | 语义 top-K + 衰减排序 |
| 遗忘机制 | 无 | prompt 内字数淘汰 | 艾宾浩斯指数衰减 |
| 外部依赖 | 0 | 1（LLM） | **2（LLM + Embedding）** |
| 用户画像 | 无 | 无 | 可选（UserMemory 模式） |
| 多租户隔离 | N/A | YAML 文件（竞态风险） | SQLite 行级隔离 |
| 配置复杂度 | 1 行 | 填 1 个 LLM 名称 | 填 2 套 API 配置 |
