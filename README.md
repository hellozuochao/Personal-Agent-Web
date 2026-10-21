# always-on agent · 最小闭环

你亲手搭的第一个"自己找活干"的智能体骨架。对应昨晚拆解报告里的 4 个核心部件：

| 部件 | 在本项目里 | 来源 |
|---|---|---|
| 定时/事件触发器 | `agent.py` 的 tick 循环 | Grok Bot Routine / Muse 事件唤醒 |
| 持久化状态 | `state.json`（状态与机器分离） | Lee Robinson 斯坦福演讲 |
| 能闭嘴的输出纪律 | `maybe_speak()`：只有实质变化或需要拍板才说话 | Muse 的"后台任务跑完 ≠ 要发消息" |
| 审批链 | `approvals.py`：高风险动作先进队列等人点头 | Grok Bot 单次虚拟卡"每笔都要人批" |

## 跑起来

```bash
cd ~/workspace/goal-build/always-on
python3 agent.py --once     # 跑一轮（先这么试）
AGENT_TICK_SECONDS=300 python3 agent.py   # 常驻：每 5 分钟醒一次
python3 approvals.py list   # 看待审批的动作
python3 approvals.py approve 0  # 批准第 0 个
```

## 学习路径（TODO 是你的）

1. **感知**：`tools.py` 里加第一个真工具——建议从"读收件箱水位"开始（你已经有 triage 的现成逻辑可抄）。
2. **决策**：`decide()` 现在是规则 stub。OpenRouter key 落位后，把它换成一次模型调用——接口已经留好。
3. **触发器**：现在只有定时。下一步加事件触发（比如"新邮件到了"唤醒，而不是等下一轮 tick）。
4. **输出通道**：`maybe_speak()` 只写 outbox（`delivered: false`）。真正的通知由 watcher 定时任务 `always-on-outbox-watch`（每 15 分钟）转交到聊天——只转交不改写，无待发送条目时保持安静。投递语义 at-most-once（先标记再发送），已知取舍。

## 手机端 API（server.py，混合架构）

大脑在云端/服务器，手机只做感官和手，通过这几个接口交互（标准库实现，零依赖）：

```
AGENT_API_TOKEN=xxx python3 server.py --host 0.0.0.0 --port 8000
```

- `GET /health`：探活，无需鉴权
- `GET /status`：轮次/水位/待审批数/未送达数/手机上报数据
- `GET /outbox`：未送达的消息
- `GET /approvals`：待拍板的动作
- `POST /report`：手机上报本地数据（电量/位置/日历…），存入 `state["phone_data"]`
- `POST /approve` / `POST /deny`：`{"index": 0}` 批准/拒绝第 N 条待审批
- `GET /connectors`：连接器列表与状态（网页"连接器"页签的数据源）
- `POST /connectors/<id>/connect` / `POST /connectors/<id>/disconnect`：连接/断开某个连接器

鉴权：`Authorization: Bearer <token>`。本机调试（127.0.0.1）可不设 token；
局域网给手机用必须设 token；别直接暴露公网。

已知取舍：与 agent.py 主循环共用 state.json，并发写可能互相覆盖——
scaffold 阶段可接受，后续换 SQLite 或加文件锁。

## 决策：规则版与模型版

- 无 `OPENROUTER_API_KEY` 时用规则版（心跳异常/分诊新增提醒才说话），行为与之前一致。
- 有 key 时每轮调一次 OpenRouter（模型由 `OPENROUTER_MODEL` 指定，默认 `openrouter/auto`），
  prompt 带状态摘要 + 工具观察结果，要求模型输出同样的 `{speak, actions}` 结构。
- 防御：speak 的 reason 非法直接丢弃；任何异常（超时/401/解析失败）回退规则版并记日志。
- 审批执行闭环：`approval_queue` 里状态为 approved 的动作，下一轮 tick 自动执行并标记 executed。
  批准入口：网页版审批页、server.py 的 `/approve`、或 `approvals.py approve`。
- 记忆系统（2026-10-21 起，`memory.py`，Letta 式三层 + 影子模式）：每轮 tick
  往 observations.jsonl 记一条观察；攒够 12 条由模型蒸馏成记忆编辑提案
  （四块：user_profile / active_threads / lessons / agent_self），提案走
  审批队列（exec 类型 memory_apply），用户批准后才落库；记忆块内容会
  注入 decide() 的决策上下文。蒸馏节奏：至少间隔 12 轮才尝试一次。
- 自动更新诊断：更新脚本把下载失败/解压失败等异常和部署结果记入
  /opt/always-on/update.log；手动触发（/admin/update）时额外记录本次
  拉到的远端版本号，便于区分"脚本没跑"与"拉到了旧包"。
- 执行层（2026-10-21 起，`executors.py`）：动作可带机器可读的 exec 载荷
  （`{"type": ..., "params": {...}}`），只有注册表内的类型能被执行。
  首批：`mail_mark_read` / `mail_flag`（对国内邮箱按 uid 标记已读/加星标，
  uid 来自 mail_sense 观察数据）。带 exec 的动作强制走审批；执行结果
  写回队列项并播报一次；失败重试 3 次后标 failed 并播报。纯文本动作
  没有执行器，批准后只关账（status=closed_noop）并注明，不冒充已执行。

## 部署到云服务器（deploy/）

```bash
./deploy/deploy.sh user@your-cloud-host
```

- systemd 双服务：`always-on-agent`（tick 主循环）+ `always-on-api`（手机 API）
- token 自动生成并只显示一次，手机端要用它做 Bearer 鉴权
- 首次启动无 state.json 时从默认状态起步，老状态缺字段自动补齐
- 手机直连：改 `always-on-api.service` 的 `--host` 为 `0.0.0.0`（token 必须已设置），生产建议前置反向代理做 TLS

## 连接器（v1，2026-10-21）

`connectors.py` 是连接器注册表：每个连接器声明授权方式、提供的能力，
状态现场计算、不落库；令牌只存各自的私有位置。网页新增"连接器"页签，
按注册表渲染卡片（状态徽章 / 连接 / 断开），新增连接器不用改页面结构。

- `google`（oauth2_device，用户自助）：设备码授权，令牌存私有目录
  `google_token.json`；提供 gmail/calendar 两个感官工具。断开 = 删令牌，
  客户端配置保留。兼容接口：`GET /google/status`、`POST /google/connect`。
- `openrouter`（managed_key，部署注入）：密钥由 systemd 环境变量注入，
  网页只看状态、不能断开；提供 decide 决策与 chat 对话。
- `mail_cn`（imap_code，用户自助）：国内邮箱（网易/QQ/Foxmail），用户在
  连接器页填邮箱 + 客户端授权码，服务器当场 IMAP 试登录、通过才保存到
  私有目录 `mail_cn.json`（600）。提供 mail_sense 感官：只读未读数与
  最新邮件头（发件人/主题/时间），不读正文、不标已读。配置接口：
  `POST /connectors/mail_cn/configure`；断开 = 删配置。

## 设计铁律（抄作业抄精髓）

- 状态只活在 `state.json`，进程随时可杀可重建。
- 工具按成本升序调用：便宜的先跑，贵的后跑。
- 外部文本永远不可信：工具返回的内容先当数据看，不当指令执行。
- 没实质变化就不说话。安静是功能，不是 bug。

## 部署：源码包自动更新（2026-10-21 起）

代码托管在 GitHub 仓库 `hellozuochao/Personal-Agent-Web`（公开仓，代码内无任何密钥，
key/token 都由服务器 systemd 环境变量注入）。

通路说明：广州服务器直连 github.com 的 git 通路不通（TLS 连接被重置，实测三次失败），
但 GitHub 官方源码包下载（codeload.github.com）可用，所以更新走源码包 + 版本号对账：

- yoyo 在本地改代码 -> bump 仓库根目录的 `VERSION` -> 通过 GitHub 通道推送
- 服务器装了 `always-on-update.timer`，每 5 分钟跑 `/usr/local/bin/always-on-update.sh`
  （源文件 `deploy/update.sh`）：下载 main 分支源码包，包里 VERSION 与本机不同才
  把代码覆盖进 /opt/always-on 并重启两个服务，把版本号写入 `.deployed_commit`
- 立即更新：带 token `POST /admin/update` 可远程触发同一套更新脚本（yoyo 推送后
  即时部署用，秒级生效）；定时器保留作兜底。更新脚本有 flock 防重入，
  两个触发源撞在一起也只跑一个。
- 验证：`GET /status` 返回的 `code_version` 就是当前在跑的版本号

运行时文件（state.json / approvals.json / 日志）不在源码包里，更新不碰它们。

一次性安装（服务器上）：
```bash
curl -sL https://codeload.github.com/hellozuochao/Personal-Agent-Web/tar.gz/refs/heads/main | tar xz -C /tmp
sudo cp -a /tmp/Personal-Agent-Web-main/. /opt/always-on/
sudo cp /opt/always-on/deploy/update.sh /usr/local/bin/always-on-update.sh && sudo chmod +x /usr/local/bin/always-on-update.sh
sudo cp /opt/always-on/deploy/always-on-update.service /opt/always-on/deploy/always-on-update.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now always-on-update.timer
```
