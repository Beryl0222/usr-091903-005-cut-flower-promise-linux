# 鲜切花保鲜承诺

连接鲜切花**采后处理—冷链运输—销售承诺—责任结算**的领域服务。
产地知道品种、成熟度、预冷等待和途中温度会改变品质；本服务让销售端的
每一句瓶插期承诺都有可追溯、可重放、可结算的依据。

## 解决的问题

- **来源继承**：每束花采切时即继承种苗批次、种植棚、采切班次与采后处理记录；
  拼箱、拆箱、换冷链车后来源链不断。
- **时点承诺**：接单时固化**当时有效**的品质规则版本和每束花的剩余冷量快照，
  据此给到货窗口与瓶插期；后来放宽或收紧的规则不能解释旧订单。
- **幂等接入**：温度记录器断连补传、承运方重复回调、收货方分批签收，
  都靠自然键/幂等键只生效一次。
- **自动圈定**：超温、断连、延误、到货品相异常自动圈出受影响花束，
  开放等待责任方补证；补传读数后自动重评，异常洗白即解除。
- **一次结算**：客赔、承运罚金、种植户品质扣款各归其责；订单只结算一次。
- **三种视角**：客服看到可说明的承诺与赔付依据，种植户逐笔核对扣款，
  管理层回放一张订单从采切到索赔的完整决定过程。

## 架构

```
service.py                 运行入口（标准库 HTTP，/health 与 /api/*）
freshness/
  clock.py                 可替换时钟、分钟级时间工具
  errors.py                带稳定错误码的业务异常
  rules.py                 版本化品质规则（生效窗口、冷量/赔付档位）
  store.py                 仅追加事件流（JSONL，可落盘）
  projection.py            事件 -> 当前状态的折叠（随时可重建）
  coldchain.py             谱系位置解析、温度暴露与剩余冷量计算
  app.py                   命令处理、幂等、异常圈定/重评、结算、回放
  views.py                 客服 / 种植户 / 管理层只读视图
  api.py                   JSON HTTP 适配与统一错误转换
test_domain.py             领域端到端测试（17 例）
test_api.py                HTTP 端到端测试（2 例）
service_contract.py        基础服务契约（/health）
demo.py                    三渠道完整业务演示
```

**事件溯源**：所有变化只追加事件（约 23 种），当前状态不持久化、随时由事件流
重放得到。`--data events.jsonl` 可把事件落盘，重启自动回放；管理层的订单回放
就是同一条事件流的过滤视图。

### 冷量口径（客服与种植户看到的是同一套数字）

1. 采切时瓶插期预算 = 品种基准小时 × 成熟度系数 − 预冷等待分钟 × 每分钟惩罚；
2. 装箱后每一分钟按花束**当时所在箱/车的有效温度记录器**读数折减，
   温度越高消耗越快（规则 `temp_budget`）；箱记录器优先于车厢记录器；
3. 读数空档超过 30 分钟、或该位置没有记录器，按规则的**断连保守温度**计费；
   断连补传只是把读数插回时间线，重算结果自动改变；
4. 接单承诺按剩余冷量给到货窗口（下限不足直接拒绝承诺）；
5. 结算时按订单**固化的规则快照**重算实际瓶插期，缩水比例取赔付档；
   到货品相异常按定额档；同一束花两档取高，不重复计赔。

### 责任口径

| 异常 | 触发 | 责任 |
|---|---|---|
| `temperature_excursion` 在途超温 | 真实读数落入超温档 | 承运方（终裁） |
| `logger_disconnect` 记录器断连 | 读数空档/在途无覆盖 | 承运方补传洗白，否则终裁 |
| `late_delivery` 延误 | 到达/签收晚于承诺窗口 | 承运方补证后终裁 |
| `arrival_quality` 品相异常 | 收货记录 wilted/damaged | 按证据终裁产地或承运 |

无承运责任且基线（成熟度/预冷等待）低于品种标准 10% 以上的赔付，计种植户扣款；
凡裁定承运责任的，赔付转为承运罚金，不扣种植户。

## 运行

```bash
python3 service.py --check                 # 配置自检
python3 service.py --port 8000             # 纯内存
python3 service.py --port 8000 --data ./data/events.jsonl   # 事件落盘
curl http://127.0.0.1:8000/health
```

## 测试与演示

```bash
npm test                 # = python3 -m unittest service_contract test_domain test_api
python3 demo.py           # 打印三渠道（批发/婚礼/花店）从采切到索赔的全过程
```

## HTTP API（均在 /api 下，POST 体为 JSON）

### 产地建档
- `POST /seed-batches` `{seed_batch_id, cultivar, supplier?, planted_at?, at?}`
- `POST /greenhouses` `{greenhouse_id, name?, at?}`
- `POST /cut-shifts` `{shift_id, greenhouse_id, seed_batch_id, shift_code?, at?}`
- `POST /bouquets/harvest` `{shift_id, bouquet_ids[], maturity, stem_count?, at?}`
- `POST /postharvest` `{bouquet_ids[], precool_wait_min, pulse_solution?, grading?, note?, at?}`

### 拼箱/拆箱/运输（来源持续保留）
- `POST /boxes/pack` `{box_id, bouquet_ids[], logger_id?, at?}`
- `POST /boxes/split` `{from_box_id, splits:[{box_id, bouquet_ids[]}], at?}`
- `POST /loggers/bind` `{logger_id, target_type: box|vehicle, target_id, at?}`
- `POST /shipments` `{shipment_id, carrier, box_ids[], vehicle_id, vehicle_logger_id?, route?, at?}`
- `POST /shipments/change-vehicle` `{shipment_id, to_vehicle_id, to_vehicle_logger_id?, at?}`
- `POST /shipments/arrive` `{shipment_id, arrived_at?, at?}`

### 接单（规则时点固化）与温度
- `POST /orders/quote` `{bouquet_ids[], at?}` → 可承诺窗口，不出单
- `POST /orders` `{order_id, bouquet_ids[], amount, channel, customer, destination, idempotency_key?, at?}`
- `POST /temperatures` `{logger_id, at, temp_c, received_at?, recorded_at?}`
  自然键 `(logger_id, at)` 去重
- `POST /temperatures/batch` `{readings:[{logger_id, at, temp_c}], at?}` 断连补传

### 回调与分批签收
- `POST /carrier-callbacks` `{shipment_id, status, occurred_at, idempotency_key, note?, at?}`
- `POST /receipts` `{order_id, bouquet_ids[], received_at, condition: normal|wilted|damaged, shipment_id?, note?, at?}`

### 异常补证/终裁
- `GET /flags?order_id=&status=`
- `POST /flags/manual` `{order_id, reason, bouquet_ids[], detail?, at?}`
- `POST /flags/{flag_id}/evidence` `{party, kind?, note?, at?}`
- `POST /flags/{flag_id}/rule` `{liable_party, resolution, reason?, at?}`

### 结算与查询
- `POST /orders/{id}/settle` → 唯一一次结算（有开放异常或未全部签收会被拒绝）
- `GET /orders/{id}` · `GET /orders/{id}/settlement` · `GET /orders/{id}/replay`
- `GET /bouquets/{id}` · `GET /rules` · `POST /rules/publish`
- `GET /views/customer-service/orders/{id}`
- `GET /views/grower?greenhouse_id=&seed_batch_id=`
- `GET /views/management`

所有写命令支持显式 `at`（事件发生时间），便于回放与补录；
错误响应统一为 `{error, message, context?}`，状态码 404/409/422/400。
