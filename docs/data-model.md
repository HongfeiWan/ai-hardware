# 数据模型

硬件诊断 MCP 的基础不是一段自然语言描述，而是可以被程序查询的板级上下文和诊断会话。

## Board Context

`board_context` 描述被测电路：

- `board`：板卡基本信息、版本、设计文件来源。
- `nets`：网标、别名、领域、期望电压或信号范围。
- `components`：器件位号、类型、封装、值、每个引脚连接到哪个网。
- `test_points`：测试点、探针方式、位置描述、可测信号类型。
- `rails`：电源轨、供电来源、上下限、启动顺序、电流限制。
- `constraints`：安全约束，例如某些网标禁止主动驱动、某些动作必须人工确认。

JSON Schema 见 [schemas/board_context.schema.json](../schemas/board_context.schema.json)。

## Diagnostic Session

`diagnostic_session` 描述一次诊断过程：

- `session_id`：一次诊断的唯一标识。
- `board_id`：关联的板级上下文。
- `observed_symptom`：工程师输入的故障现象。
- `instruments`：本次使用的仪器、型号、通道映射。
- `measurements`：每一次测量的目标、设置、结果、特征和原始数据引用。
- `findings`：模型或规则引擎生成的诊断结论。
- `next_actions`：下一步建议，例如测量某个引脚、改变上电条件、停止测试。

JSON Schema 见 [schemas/diagnostic_session.schema.json](../schemas/diagnostic_session.schema.json)。

## 拓扑表达

建议内部用图结构保存：

```text
component:U1.pin:VIN -- net:VBUS
component:U1.pin:SW  -- net:SW_NODE
component:L1.pin:1   -- net:SW_NODE
component:L1.pin:2   -- net:VOUT_3V3
test_point:TP3       -- net:VOUT_3V3
rail:USB_5V          -- net:VBUS
```

常用查询：

- 某网标上有哪些器件引脚和测试点。
- 某电源轨下游有哪些负载。
- 某测试点附近一跳/两跳元件。
- 某 IC 的电源输入、使能脚、开关节点、反馈脚是否都有可测点。
- 从故障网标向上游电源或下游负载追踪路径。

## 从 EDA 文件导入

第一阶段可以手写 YAML/JSON；第二阶段增加导入器：

- KiCad：解析 netlist XML、`.kicad_sch`、`.kicad_pcb` 或 BOM。
- Altium：解析导出的 netlist、BOM、pick-and-place、测试点表。
- CSV：支持从生产测试表导入 `net_name,test_point,expected_voltage,probe_hint`。

导入器只负责转换，不负责诊断。转换后统一落到 `board_context`。

## 信号特征

原始波形通常太大，不适合直接放进模型上下文。推荐先提取：

- DC：均值、最小值、最大值、稳定时间、过冲、欠冲。
- 电源纹波：峰峰值、RMS、主频、低频摆动。
- 启动时序：上电延迟、enable 到 power-good 的时间、rail 顺序。
- 开关电源：开关频率、占空比、跳脉冲、振铃频率。
- 数字信号：逻辑电平、频率、占空比、协议解码摘要。
- 异常事件：电流折返、周期性重启、短路保护、热关断迹象。

模型输入应包含“特征 + 拓扑子图 + 安全约束 + 历史测量”，而不是孤立波形。
