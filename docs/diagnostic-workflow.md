# 诊断工作流

一次诊断会话应该像实验记录一样可复现。模型可以参与判断，但工具层要保证每一步测量可审计、可回放、可拒绝危险动作。

## 标准流程

1. 加载 `board_context`，校验 schema。
2. 输入故障现象，例如“3V3 不启动”“USB 枚举失败”“某路电源纹波过大”。
3. 根据网标和拓扑找到相关电源轨、IC 引脚、测试点和上下游器件。
4. 生成低风险首轮测量计划，例如不上电电阻、限流上电、关键 rail DC 电压。
5. Python 工具执行仪器动作，保存原始数据引用和结构化特征。
6. 模型读取板级上下文、测量特征和历史记录，输出诊断或下一步测量。
7. 工具层校验下一步动作是否在安全边界内。
8. 重复测量，直到得到结论、需要人工检查，或触发停止条件。

## 首轮测量建议

对未知故障板，默认不要直接满功率上电。建议顺序：

- 断电状态下检查关键电源轨对地阻抗。
- 可编程电源设置低电流限制，缓慢上电。
- 采集输入电流曲线和关键 rail 的启动曲线。
- 观察 power-good、enable、reset、clock 等启动链路。
- 对异常 rail 追踪上游供电和下游负载。

## 模型输出格式

模型诊断输出必须结构化。建议字段：

```json
{
  "diagnosis": "3V3 rail is likely current-limited during startup",
  "confidence": 0.72,
  "evidence": [
    "VOUT_3V3 ramps to 1.1 V then collapses every 42 ms",
    "Input current reaches the configured 180 mA limit",
    "EN_3V3 remains high during the collapse"
  ],
  "next_measurements": [
    {
      "type": "measure_net",
      "net": "SW_NODE",
      "instrument": "oscilloscope",
      "reason": "Check whether the buck converter is switching before shutdown",
      "risk_level": "medium"
    }
  ],
  "stop_reason": null
}
```

## 停止条件

工具层必须能在以下情况停止：

- 输入电流超过上限。
- 某个电源轨超过绝对最大电压。
- 温升或短路迹象明显。
- 模型建议的动作没有匹配 allowlist。
- 需要移动探针到高风险节点，例如高压、RF、强开关节点。
- 同一测量重复多次但没有新增信息。

## 可回归诊断任务

建议后续建立样板任务：

- 电源轨短路。
- LDO 输入正常但输出异常。
- Buck 开关节点异常。
- Enable 脚未拉高。
- Reset 释放时序错误。
- 晶振不起振。
- I2C/SPI 总线被拉低。

每个任务应有正常板、故障板、板级上下文、期望测量序列和最终结论，用于验证模型提示词和工具实现。
