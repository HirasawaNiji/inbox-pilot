# InboxPilot YAML 规则配置指南

本文档说明如何安全地修改 InboxPilot 的邮件优先级规则。规则配置文件位于：

```text
config/rules.yaml
```

规则引擎读取邮件后，按照以下公式计算优先级：

```text
最终分数 = 基础分 + 所有命中规则的分数变化
```

最终分数会被限制在 `0～100`，再通过 `thresholds` 映射为 P1～P5。

修改规则后应运行完整回归测试，确认新的行为符合预期：

```powershell
Set-Location E:\inbox-pilot
uv run pytest
uv run ruff check .
uv run mypy src
```

## 配置文件结构

```yaml
policy_version: rules-v1
base_score: 30

user_context: ...
bulk_recipient_prefixes: ...
bulk_recipient_threshold: ...

keywords: ...
weights: ...
thresholds: ...

review_margin: 0
```

各部分职责如下：

| 配置段 | 作用 |
| --- | --- |
| `policy_version` | 标识当前规则版本 |
| `base_score` | 设置所有邮件的初始分数 |
| `user_context` | 配置用户邮箱、可信发件人和可信域名 |
| `bulk_recipient_prefixes` | 通过收件地址识别群发邮件 |
| `bulk_recipient_threshold` | 通过收件人数识别群发邮件 |
| `keywords` | 定义不同类型的关键词 |
| `weights` | 定义每个信号的加减分 |
| `thresholds` | 将分数映射为 P1～P5 |
| `review_margin` | 设置优先级边界附近的人工复核范围 |

## `policy_version`：规则版本

```yaml
policy_version: rules-v1
```

规则版本不参与评分，用于记录一封邮件是由哪一版规则评估的。明显修改关键词、权重或阈值后，建议升级版本：

```yaml
policy_version: rules-v2
```

版本名只能使用字母、数字、点号、下划线和连字符，例如：

```yaml
policy_version: rules-v1.1
policy_version: campus-rules-v2
```

## `base_score`：基础分

```yaml
base_score: 30
```

每封邮件都从基础分开始计算。提高基础分会让所有邮件整体进入更高优先级，降低基础分则会让普通邮件更容易落入 P4/P5。

例如：

```text
基础分                 30
可信发件人            +15
直接发送给本人         +5
七天内截止            +20
--------------------------------
最终分数               70
```

基础分允许范围为 `0～100`。建议优先调整具体关键词和权重，最后再考虑修改基础分。

## `user_context`：用户与信任配置

```yaml
user_context:
  mailbox_addresses:
    - student@example.edu
  trusted_senders:
    - registrar@example.edu
    - teacher.zhang@example.edu
  trusted_domains:
    - example.edu
  timezone: Asia/Shanghai
```

### `mailbox_addresses`

属于用户本人的邮箱地址。规则引擎用它判断邮件是否直接发送给用户。

如果有多个别名，可以全部配置：

```yaml
mailbox_addresses:
  - student@example.edu
  - student.name@example.edu
  - student-id@example.edu
```

邮箱地址会被转为小写，至少需要配置一个地址。

### `trusted_senders`

特别可信或通常比较重要的具体发件人，例如任课老师、导师、教务处、课程平台和信息安全中心：

```yaml
trusted_senders:
  - registrar@example.edu
  - teacher.zhang@example.edu
  - security@example.edu
```

不要把所有学校地址都放入这个列表，否则“具体可信发件人”将失去区分能力。

### `trusted_domains`

可信机构域名：

```yaml
trusted_domains:
  - example.edu
```

以下地址都会匹配 `example.edu`：

```text
library@example.edu
career@example.edu
international@example.edu
```

域名配置中不要包含协议或完整邮箱：

```yaml
# 错误
- https://example.edu
- user@example.edu

# 正确
- example.edu
```

具体发件人与可信域名分数不会叠加。当前逻辑为：先匹配 `trusted_senders`；未匹配时，才使用 `trusted_domains`。

### `timezone`

```yaml
timezone: Asia/Shanghai
```

用于保存用户时区，为后续截止时间转换、每日摘要和 LLM 上下文做准备。当前规则引擎计算截止时间时，主要使用邮件 `received_at` 中自带的时区。

## 群发邮件识别

### `bulk_recipient_prefixes`

```yaml
bulk_recipient_prefixes:
  - all-
  - class-
  - newsletter-
```

规则引擎检查收件地址 `@` 前面的本地部分。例如：

```text
all-students@example.edu
class-ai101@example.edu
newsletter-subscribers@example.edu
```

分别匹配 `all-`、`class-` 和 `newsletter-`。

如果学校使用其他列表地址，可以追加前缀：

```yaml
bulk_recipient_prefixes:
  - all-
  - class-
  - newsletter-
  - undergraduate-
  - students-
```

这里只填写本地部分的开头，不要包含 `@`。

### `bulk_recipient_threshold`

```yaml
bulk_recipient_threshold: 10
```

如果 To 和 Cc 中明确列出的收件人数量达到该值，也会识别为群发。判断条件为：

```text
收件人数达到阈值
或者
某个收件地址匹配群发前缀
```

群发只是一项负向信号，不等同于低优先级。考试变更、安全警报等群发邮件仍可通过其他信号进入 P1。

## `keywords`：关键词配置

```yaml
keywords:
  urgent: ...
  security: ...
  bulk: ...
  deadline: ...
  action: ...
  opportunity: ...
  no_action: ...
  unsubscribe: ...
```

关键词匹配不区分英文大小写。同一关键词组即使命中多个词，也只应用一次该组权重，但所有命中词都会保存在解释结果中。

### `urgent`：紧急关键词

```yaml
urgent:
  - 紧急
  - 补交
  - 地点变更
  - 课程取消
  - 逾期
  - 未提交
```

适合加入明确代表紧急行动的短语：

```yaml
- 考试变更
- 注册失败
- 材料缺失
- 账号冻结
```

避免添加过于宽泛的词，例如“重要”“通知”“时间”。也不要使用单独的“取消”，否则可能误匹配“取消订阅”。

### `security`：安全关键词

```yaml
security:
  - 安全警报
  - 钓鱼
  - 修改密码
```

适合加入：

```yaml
- 密码泄露
- 异常登录
- 账户被盗
- 可疑链接
```

安全关键词优先于紧急关键词。同一封邮件同时命中两者时，只应用 `security_keyword`，避免重复高额加分。

### `bulk`：活动、简报和推广关键词

```yaml
bulk:
  - 新闻简报
  - 推广
  - 优惠
  - 活动
  - 社团
  - 招聘
  - 双选会
  - 文化周
```

`bulk_recipient_prefixes` 判断收件地址，`keywords.bulk` 判断标题和正文内容。避免添加“课程”“学校”“学生”等高频通用词，否则正常邮件也会被扣分。

### `deadline`：截止语言

```yaml
deadline:
  - 截止
  - 到期
  - 逾期
  - 前提交
  - 前补交
  - 前完成
  - 前报名
  - 前预约
  - 前登录
  - 前上传
```

只有包含这些词的句子或分句，其中的日期才会被视为截止日期。因此：

```text
开放时间：2026 年 8 月 10 日；
截止时间：2026 年 8 月 21 日。
```

只会提取 8 月 21 日作为截止时间。

不要单独添加“前”，它会误匹配“请提前阅读”。建议使用“前确认”“前填写”等完整短语。

当前支持的明确日期格式包括：

```text
2026 年 8 月 8 日 20:00
2026-08-08 20:00
2026/08/08 20:00
```

暂时不能精确解析“明天晚上”“下周五”“三天后”等相对日期。检测到截止语言但无法提取未来日期时，会使用 `deadline_without_date` 权重。

### `action`：行动关键词

```yaml
action:
  - 补交
  - 提交
  - 确认
  - 缴纳
  - 报名
  - 预约
```

可以根据需要增加：

```yaml
- 填写
- 上传
- 回复
- 签署
- 续借
```

规则引擎会处理简单否定。例如“无需提交”命中 `no_action` 后，不会再因为“提交”获得行动加分。

### `opportunity`：学习和职业机会

```yaml
opportunity:
  - 实习
  - 招聘
  - 双选会
  - 奖学金
```

可以增加“科研项目”“交换生”“夏令营”“推免”“竞赛”等。避免使用“学习”“工作”“项目”等过于宽泛的词。

### `no_action`：无需行动

```yaml
no_action:
  - 无需回复
  - 无需提交
  - 无需报名
  - 不要求立即回复
  - 自愿参加
  - 自行决定
  - 仅用于提醒
  - 请忽略本通知
```

该组既产生负分，也会抑制对应的正向行动词。可以增加“无需处理”“不必回复”“仅供参考”等表达。

### `unsubscribe`：退订信息

```yaml
unsubscribe:
  - unsubscribe
  - 取消订阅
  - 退订
```

用于识别订阅简报和推广邮件。建议保留中英文版本。

## `weights`：评分权重

权重允许范围为 `-100～100`。正数提高优先级，负数降低优先级，`0` 表示保留解释但不改变分数。

### 信任与收件人

```yaml
trusted_sender: 15
trusted_domain: 10
direct_recipient: 5
```

| 权重 | 含义 |
| --- | --- |
| `trusted_sender` | 命中具体可信发件人 |
| `trusted_domain` | 来自可信学校域名，但未命中具体发件人 |
| `direct_recipient` | To 中包含用户邮箱 |

### 紧急与安全

```yaml
urgent_keyword: 40
security_keyword: 40
```

误报过多时，应先收窄关键词，再考虑降低权重。通常不要因为添加了宽泛关键词而直接大幅降低所有紧急邮件分数。

### 截止日期

```yaml
deadline_within_two_days: 35
deadline_within_seven_days: 20
deadline_later: 0
deadline_without_date: 5
```

| 权重 | 含义 |
| --- | --- |
| `deadline_within_two_days` | 截止时间不超过 48 小时 |
| `deadline_within_seven_days` | 截止时间在 2～7 天内 |
| `deadline_later` | 截止时间超过 7 天 |
| `deadline_without_date` | 有截止语言但未解析出未来日期 |

`deadline_later` 当前为 `0`，表示记录解释但不立即提升较远截止事项。改为正数可能让普通选课通知进入更高优先级。

### 行动与机会

```yaml
action_keyword: 10
opportunity_keyword: 20
```

正在找实习时可以适当提高 `opportunity_keyword`；如果招聘群发邮件过多，可以降低它。

### 邮件提供方信号

```yaml
high_importance: 10
low_importance: -5
has_attachment: 10
```

高重要性和附件都只是辅助信号。不要把 `high_importance` 设置得过高，否则推广方可能通过高重要性标记绕过其他规则。

### 群发、推广与退订

```yaml
bulk_mail: -10
bulk_keyword: -10
unsubscribe: -20
```

三项可以叠加，但紧急教务通知也可以通过可信发件人、紧急关键词和近期截止时间抵消这些负分。

### 其他信号

```yaml
no_action_required: -5
external_sender: -10
empty_subject: -10
sender_mismatch: 0
```

| 权重 | 含义 |
| --- | --- |
| `no_action_required` | 邮件明确表示无需操作 |
| `external_sender` | 发件人不属于可信域名或可信列表 |
| `empty_subject` | 邮件没有标题 |
| `sender_mismatch` | From 与实际 Sender 不同 |

`sender_mismatch` 当前为 `0`，仅保留解释。学校自动系统经常使用不同的 From 和 Sender，不应仅凭这一点扣分。

## `thresholds`：优先级阈值

```yaml
thresholds:
  P1: 80
  P2: 50
  P3: 35
  P4: 10
```

当前映射为：

```text
80～100 → P1
50～79  → P2
35～49  → P3
10～34  → P4
0～9    → P5
```

阈值必须满足：

```text
P1 > P2 > P3 > P4
```

降低某个阈值会让更多邮件进入对应优先级；提高阈值则会减少对应邮件数量。权重和阈值应结合调整。

## `review_margin`：边界复核范围

```yaml
review_margin: 0
```

当前值 `0` 表示关闭“阈值附近自动复核”。如果设置为：

```yaml
review_margin: 2
```

则距离 `80`、`50`、`35`、`10` 任一阈值不超过 2 分的邮件都会标记为需要人工复核。

## 不能仅通过 YAML 修改的逻辑

以下行为目前写在 `src/inbox_agent/rule_engine.py` 中：

- 具体可信发件人与可信域名分数不叠加；
- 安全关键词优先于紧急关键词，两组高分不叠加；
- 同一关键词组无论命中多少词，只应用一次权重；
- `无需提交` 等否定表达会抑制对应行动词；
- 空标题会强制进入人工复核；
- 同时包含活动/推广关键词和截止要求，但没有紧急或安全关键词时，会进入人工复核；
- 日期只从包含截止语言的句子或分句中提取；
- 最终分数固定限制在 `0～100`。

如果要改变这些行为，需要同时修改 Python 代码和测试，不能只调整 YAML。

## YAML 编写注意事项

### 使用两个空格缩进

```yaml
keywords:
  urgent:
    - 紧急
    - 补交
```

不要使用 Tab。

### 特殊字符串使用引号

包含 `#`、`:`、`{}` 等字符时建议加引号：

```yaml
urgent:
  - "截止："
  - "#紧急通知"
```

否则 `#` 后面的内容可能被 YAML 解释为注释。

### 权重使用数字

```yaml
# 正确
bulk_mail: -10

# 不推荐
bulk_mail: "-10"
```

## 推荐修改顺序

当分类结果不符合预期时，建议依次检查：

1. 邮件是否命中了错误的关键词；
2. 是否缺少更精确的关键词或可信发件人；
3. 单项权重是否过高或过低；
4. 最后再考虑调整基础分和全局阈值。

例如“取消订阅”错误触发紧急规则时，正确处理方式是把“取消”收窄成“课程取消”，而不是降低所有紧急邮件的权重。

## 修改后的回归检查

运行：

```powershell
uv run pytest
```

其中以下测试会比较当前 50 封主 demo 和人工标签：

```text
test_all_sample_priorities_match_human_labels
```

如果测试失败，会指出哪些邮件的预测优先级发生变化。这不一定意味着修改错误，需要判断：

- 新结果是否更符合真实使用习惯；
- 如果新结果合理，是否应该同步修改人工标签；
- 如果新结果不合理，是否应调整关键词或权重。

人工标签位于：

```text
data/eval/expected_results.json
```

不要为了让测试通过而直接修改人工标签。只有在确认原有人工判断不符合实际需求时，才应更新标签及其解释。

## 修改示例

假设希望实习和招聘邮件更受重视，可以将：

```yaml
opportunity_keyword: 20
```

修改为：

```yaml
opportunity_keyword: 25
```

然后运行测试，观察 `sample-020-career-fair-registration` 等邮件是否进入更高优先级。如果普通招聘群发邮件也被大量提升，应考虑增加更精确的机会关键词，而不是继续提高全局权重。
