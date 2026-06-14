# 路线图

## Milestone 0：仓库基线

- 完成 README、架构、框架调研、数据模型和诊断流程文档。
- 固化 `board_context` 和 `diagnostic_session` schema。
- 添加一个小型电源链路示例。

## Milestone 1：Python Bench MCP 原型

- 使用 MCP Python SDK/FastMCP 实现最小服务器。
- 先用 mock 仪器实现 `set_power_rail`、`capture_waveform`、`extract_signal_features`。
- 接入 MCP Inspector，验证 tools/resources/prompts 可发现可调用。
- 所有危险工具增加安全策略和 dry-run。

## Milestone 2：真实仪器驱动

- 建立 `InstrumentDriver` 抽象。
- 支持 SCPI/PyVISA 电源。
- 支持至少一个示波器型号，能采集 CSV/二进制波形和截图。
- 保存原始 artifact，并只把特征摘要传给模型。

## Milestone 3：网表和拓扑导入

- 支持手写 YAML/JSON。
- 支持 KiCad netlist 或导出 CSV。
- 建立拓扑查询工具：邻居、上游电源、下游负载、测试点查找。

## Milestone 4：ESP32 Fixture MCP

- 基于 ESP-IDF 新建固件工程。
- 引入 `espressif/mcp-c-sdk^2.0.1`。
- 暴露 `set_mux_channel`、`reset_dut`、`read_fixture_adc`、`set_load_switch`。
- 增加互锁：同一时刻只能闭合安全组合。
- 先走 HTTP；多夹具后评估 MQTT。

## Milestone 5：模型诊断闭环

- 实现 `ModelAdapter`，隔离模型供应商。
- 固定诊断 prompt 和 JSON 输出 schema。
- 加入规则引擎兜底，例如绝对最大值、短路、过流、rail 顺序。
- 建立回归样板，比较不同模型的诊断稳定性。

## Milestone 6：安全和可观测性

- 为所有工具增加审计日志。
- 每次诊断生成 session artifact。
- 工具调用支持 dry-run 和人工确认。
- 为高风险动作引入硬件互锁或物理急停。

## Milestone 7：UI 和团队使用

- 增加 Web 控制台或 notebook 工作流。
- 支持导入板卡、选择仪器、查看拓扑图、回放波形。
- 根据需要引入 TypeScript SDK 做团队网关。
