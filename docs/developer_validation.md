# 开发者模拟验证与压力测试(仅开发者使用)

普通用户的安装、认证和日常流程只使用实盘(`rr cert`、`rr on`)。本页描述
**开发者专用**的模拟盘验证入口:代码改动后、尚未做实盘通道认证之前,用模拟
miniQMT 完整验证执行器的正常路径、崩溃恢复路径、下午编排以及接口承压能力,
确保代码鲁棒性。

模拟验证证书**不参与实盘启用门禁**。`rr on` 只接受 1000 元实盘快速通道证书;
模拟验证是开发者的质量门,不是普通用户的启用条件。

## 前置条件

- 已安装模拟 miniQMT(如 `D:\国金QMT交易端模拟`),勾选"独立交易"并至少成功
  登录一次,生成 `userdata_mini` 目录;
- 已在本机完成 `.\rr init`(实盘环境就绪);
- 本机 `config\runtime.local.json` 已有 `simulation_qmt_path` 且
  `config\repo_simulation_account_binding.local.json` 存在(用下面的
  `rr dev bind` 一键完成)。

## 命令一览

| 命令 | 作用 |
|---|---|
| `.\rr dev bind` | 交互配置模拟miniQMT路径并绑定模拟账户(只读查询,不下单) |
| `.\rr dev cert [日期]` | 部署单日模拟验证:正式上午/下午正常路径 + 隔离窗口崩溃恢复,当日15:31签发证书 |
| `.\rr dev cert stat` | 查看四项模拟验证任务状态 |
| `.\rr dev cert off` | 暂停模拟验证任务(保留定义;运行中拒绝强杀) |
| `.\rr dev cert del` | 删除模拟验证任务(运行中拒绝强杀) |
| `.\rr dev cert reset` | 归档并撤销模拟验证证书 |
| `.\rr dev stress [日期]` | 部署一次性模拟5Hz全链路压力测试 |
| `.\rr dev stress stat / off / del` | 查看 / 暂停 / 删除压力任务 |
| `.\rr dev signal [日期]` | 部署单日 GC001 早盘盘口信号模拟验证（09:27:30 启动，小额挂单走完整下单/撤单链路） |
| `.\rr dev signal stat / off / del` | 查看 / 暂停 / 删除信号模拟任务 |
| `.\rr dev signal smoke` | 立即连接检查：模拟行情订阅 + 交易通道 + 账户查询，不下单 |
| `.\rr dev status` | 汇总:模拟验证证书 + 认证任务 + 压力任务状态 |

## 推荐流程

```powershell
.\rr dev bind
.\rr off
.\rr dev cert 2026-08-10        # 当天或未来工作日;当天需保持模拟miniQMT登录
.\rr dev cert stat
# 交易日15:31后:
.\rr dev status                  # 确认模拟验证证书有效
```

模拟验证通过说明执行器在模拟柜台下完成了:第一次正常路径(资金快照→目标冻结
→意图持久化→提交→成交核对)、第二次正常路径(GC001/R-001 选优)、以及
独立时间窗口内的崩溃恢复(注入
`crash_after_broker_accept_before_response_journal` 故障)。证书还要求离线
穷举验证覆盖全部声明的状态与转换。它不证明实盘物理通道;启用实盘仍需
`.\rr cert`。

## 压力测试(可选)

`.\rr dev stress` 在模拟账户执行 5Hz 全链路压力测试:行情订阅与回调、5Hz
账户查询、委托/成交回报、撤单、T+0 持仓恢复与故障清理。通过门槛:5Hz 周期
覆盖率 ≥98%、>200ms 周期 ≤1%、查询错误率 ≤0.1%、连续失败 ≤2、无断线、
无时间戳倒退、至少三类 T+0 闭环、无持仓残留、无未决委托。

结果写入 `reports/simulation_interface_stress/`,报告必须人工审查。压力测试
不生成或替代任何证书。

## GC001 早盘盘口信号模拟验证（可选）

`.\rr dev signal [日期]` 在模拟账户上验证 GC001 早盘 eat/wallgone 盘口信号
策略的完整链路：行情订阅（09:28 起）→ 逐帧特征（microprice/吃墙/撤墙）→
触发后挂小额限价卖单（默认 10 万元/模型）→ 未成交到 09:31:30 硬截止撤单。
结果写入 `reports/gc001_signal_simulation/date=YYYYMMDD/`，含逐帧 JSONL 与
summary.json（触发类型、挂单价、成交/撤单状态）。

默认 `-ExecModel all` 同时验证三种执行模型（每模型 10 万元）：
- static：触发时挂单档，不再动；
- trail：触发后随卖一新高撤旧挂新（追高型，验证撤重挂链路）；
- tranche：eat 2,3 / wallgone 6,7 两档分挂（每档 5 万元）。
summary.json 的 `legs` 字段按模型记录各自成交/撤单结果。

先确认模拟 miniQMT 已登录并保持运行，再执行：

```powershell
.\rr dev signal smoke        # 立即检查模拟端行情+交易通道可达
.\rr off                     # 实盘任务需先停用（与 dev cert 同规则）
.\rr dev signal 2026-08-07   # 部署次日模拟验证
.\rr dev signal stat
```

注意：该任务在模拟账户下真实挂单（模拟环境安全），只验证信号与下单链路，
不生成任何证书。模拟端行情源与实盘一致（真实行情，已核对 GC001/600519/
510300 五档与收盘帧），模拟的只是交易撮合；盘中 tick 是否触发信号取决于
当日盘口形态，统计结论仍需多日实盘数据积累。

### 双端并存时的账户绑定保障

实盘端与模拟端可同时运行，`rr dev signal` 的下单通道**只允许连接模拟账户**：
1. `--qmt-path` 必须是含"模拟"的路径，且其 SHA-256 指纹必须等于
   `repo_simulation_account_binding.local.json` 中绑定的
   `qmt_path_fingerprint`；
2. 连接后选出的证券账户，其账户 ID 指纹必须等于绑定中的
   `account_id_fingerprint`；任一不匹配立即中止，绝不进入下单。

该逻辑与 `rr dev cert`/`rr dev stress` 复用的 `select_bound_account` 一致。
行情（xtdata）通道无法指定客户端实例，但两个客户端推送的是同一份真实行情，
不影响信号计算；smoke 输出的 summary 中会记录绑定标签与两项指纹校验结果，
可用 `.\rr dev signal smoke` 随时核对。

## 注意事项

- `rr dev cert`/`rr dev stress` 部署前要求实盘任务为 `Disabled`(先 `.\rr off`);
- 运行中的任务不会被强制终止,避免跳过撤单与模拟持仓恢复;
- 修改模拟验证相关源码会使受保护执行源码哈希变化,现有实盘证书随之失效,
  需重新 `.\rr cert`(与修改正式执行器的后果一致);
- 普通用户不需要也不应该使用本入口;README 主流程不包含模拟步骤。
