# 鲜切花保鲜承诺服务

面向玉溪鲜切花产销双方的保鲜承诺与责任结算服务。它把“从采切到索赔”的每一个决定建立在可溯源、可回放、按版本冻结的事实之上：

- **每束花有不可变来源**：建档时固化种苗批次、种植棚（含种植户）、采切班次、采切成熟度与采后处理（预冷等待、保鲜剂）。
- **物流全程留痕**：拼箱、拆箱、装车、换冷链车都是追加事件，任意时刻都能回答“这束花在哪、跟谁同箱、在哪辆车上”。
- **接单即承诺**：以接单当时有效的品质规则版本和每束花的剩余冷量（Q10 温度积算模型）算出可承诺到货窗口与瓶插天数；规则全文快照进订单，**事后放宽的标准不能解释旧订单**。
- **采集幂等**：温度记录仪断连补传按序号合并、承运方回调按事件号去重、收货方可分批签收，同一订单行只结算一次。
- **异常自动圈定**：超温、断连、延误按订单冻结规则扫描，自动圈出受影响花束，事故进入“等待责任方补证”，补证与裁定完成后才允许结算。
- **三方各有视图**：客服看到可说明的承诺与赔付依据，种植户核对逐笔品质扣款，管理层回放一张订单从采切到索赔的完整决定过程。

## 运行

```bash
python3 service.py --check            # 配置与领域装配自检
python3 service.py --port 8000        # 启动服务，curl http://localhost:8000/health
npm test                              # 运行全部契约与端到端测试（36 项）
```

仅依赖 Python 3 标准库，无第三方依赖；内存存储，便于本地联调与教学演示。

## 领域模块

| 模块 | 职责 |
| --- | --- |
| `freshkeep/models.py` | 种苗批次、种植棚、采切班次、采后记录、花束、冷链载具 |
| `freshkeep/lineage.py` | 来源建档与拼箱/拆箱/装车/换车事件，任意时刻归属查询 |
| `freshkeep/rules.py` | 品质规则版本化（生效时间、品种覆盖、参数深拷贝快照） |
| `freshkeep/coldlife.py` | Q10 剩余冷量/瓶插寿命纯函数模型 |
| `freshkeep/ordering.py` | 接单承诺：规则快照、来源冻结、按最差束给到货窗口 |
| `freshkeep/monitoring.py` | 温度读数幂等、断连登记、补传合并与完整时间线 |
| `freshkeep/fulfillment.py` | 回调/签收幂等、异常圈定、补证裁定、实测复盘、一次结算 |
| `freshkeep/views.py` | 客服视图、种植户视图、管理层决定过程回放 |
| `freshkeep/httpapi.py` | `/v1/...` JSON HTTP 接口 |

## 承诺模型

瓶插寿命采用采后生理学常用的 Q10 温度积算：

```
衰老速率 r(T) = Q10 ^ ((T - 参考温度) / 10)
剩余瓶插天数 = (寿命预算当量小时 − 各段 r(T)×时长之和) / 24
```

- 寿命预算由**品种 + 采切成熟度**决定（规则参数 `base_vase_days_by_maturity`）；
- 采切后未及时预冷的等待段按环境温度从严积算（计入种植端责任），在库与在途按实测/理想冷链温度积算；
- 接单时承诺到货窗口末端的瓶插天数，并向下取整到 0.5 天，只保守不冒进；
- 一行多束时按剩余冷量最差的那束给整行承诺。

## HTTP 接口一览

```
POST /v1/rules/versions                         发布新版本（effective_from 生效）
GET  /v1/rules/versions                         列出全部版本
POST /v1/lineage/{seed-batches,greenhouses,shifts,post-harvest,bouquets,containers}
POST /v1/lineage/events                         pack/unpack/load/unload/transfer（event_id 幂等）
POST /v1/lineage/loggers                        温度记录仪绑定载具
GET  /v1/bouquets/{id}/provenance               花束完整来源档案
POST /v1/temperature/readings                   上报读数（seq/event_id 去重）
POST /v1/temperature/gaps                       登记断连
POST /v1/temperature/gaps/backfill              断连补传（整批幂等，合并进时间线）
POST /v1/orders                                 接单（返回逐行承诺与规则版本）
POST /v1/orders/{id}/carrier-events             承运回调（event_id 幂等）
POST /v1/orders/{id}/receipts                   分批签收（receipt_id 幂等，每束只签一次）
POST /v1/orders/{id}/scan                       重扫异常（确定性、可重入）
POST /v1/incidents/{id}/evidence                责任方补证
POST /v1/incidents/{id}/adjudicate              管理层裁定责任方
POST /v1/orders/{id}/lines/{lid}/settle         一次结算（未全签/事故未裁定将被拒绝）
GET  /v1/views/customer-service/{id}            客服：承诺与赔付依据
GET  /v1/views/grower/{growerId}                种植户：品质扣款明细
GET  /v1/views/replay/{id}                      管理层：决定过程完整回放
```

## 典型时序

1. 产地建档种苗批次 → 种植棚 → 采切班次（成熟度）→ 采后处理（预冷等待/保鲜剂）→ 花束；
2. 花束拼箱、装车，记录仪绑定到箱，途中换冷链车（箱与来源随箱保留）；
3. 销售接单：系统快照当时规则版本、冻结花束来源、按剩余冷量返回可承诺窗口与瓶插天数；
4. 在途中持续上报温度；断连登记缺口、恢复后补传，重复回调全部安全合并；
5. 收货方分批发货现场/花店分批签收；系统扫描超温与延误，自动圈出受影响花束并挂起理赔；
6. 责任方补证、管理层裁定；按实测温度复盘每束花瓶插天数；
7. 全部事故裁定后一次性结算：客户退款按责任方（承运/种植/平台）拆分，预冷等待超时形成种植端品质扣款；
8. 客服、种植户、管理层分别在自己的视图中看到同一套事实与依据，管理层可按时间回放全过程，包括“接单后发布的新版本未被用于旧订单”的明确声明。
