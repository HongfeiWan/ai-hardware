# MCP 框架调研

调研日期：2026-06-14。

本项目里的 MCP 指 Model Context Protocol。它适合把“硬件测试站能做什么”和“模型需要什么上下文”标准化：工具用于执行测量和控制，资源用于提供网表、拓扑、测量记录，提示词用于固定诊断工作流。

## 需求约束

- Python 侧需要调用可编程电源、示波器和模型 API，优先考虑 Python SDK 生态。
- ESP32 侧资源有限，不能把完整诊断、波形分析和模型调用都压到 MCU 上。
- 诊断过程涉及真实硬件，工具层必须有电压/电流/时间/通道 allowlist。
- 电路上下文不是普通文本，需要以图结构表达网标、元件、引脚、测试点和拓扑邻接关系。
- 波形数据可能很大，MCP 工具返回应优先返回结构化特征和数据引用，避免把原始大波形直接塞进模型上下文。

## 候选框架

| 方案 | 适用位置 | 优点 | 风险/限制 | 本项目建议 |
| --- | --- | --- | --- | --- |
| 官方 MCP Python SDK | Python 测试站、客户端、集成测试 | 官方维护；支持 tools/resources/prompts、stdio、SSE、Streamable HTTP；示例使用 `FastMCP` | SDK 和协议仍在演进，生产原型应锁版本 | 第一阶段主选 |
| standalone FastMCP | Python 测试站、快速原型、聚合网关 | Pythonic，函数装饰器上手快；文档强调 schema、validation、transport lifecycle；适合把仪器驱动包装为工具 | 文档跟随 main，部分功能可能尚未发布；要显式 pin 版本 | 第一阶段可直接用，注意版本锁定 |
| 官方 TypeScript SDK | Web 控制台、远端网关、团队门户 | 官方维护；适合 Node/Bun/Deno，支持 server/client、Streamable HTTP、stdio、auth helpers | 仪器控制和信号处理生态不如 Python；引入时同样需要锁定 SDK 版本 | 第二阶段再引入 |
| Espressif `mcp-c-sdk` | ESP32 固件 | 面向 ESP-IDF；组件库最新 `2.0.1`；支持 MCP 2025-11-25 默认目标、JSON-RPC 2.0、HTTP transport、server/client、tools/resources/prompts/completions、实验性 tasks | MCU 不适合承载复杂诊断和大波形；OAuth resource-server 仍不完整 | ESP32 侧主选 |
| EMQX `esp-mcp-over-mqtt` | 多夹具、多设备发现、跨网络 IoT | MQTT 5.0 更适合 IoT；内置发现、QoS、Broker 访问控制；有 ESP32 示例 | 项目较轻，协议生态比官方 HTTP 路线更早期；增加 broker 运维 | 多设备实验室再评估 |
| 直接自写 JSON-RPC | 极小 MCU 或特殊链路 | 完全可控，体积小 | 容易偏离 MCP 规范；客户端兼容性和安全治理成本高 | 仅作为最小后备方案 |

## 推荐架构决策

第一阶段采用双层 MCP：

1. `Python Bench MCP Server`：使用 Python SDK/FastMCP 暴露仪器控制、拓扑查询、信号特征提取、模型诊断工具。它是诊断大脑，也是所有危险动作的安全闸门。
2. `ESP32 Fixture MCP`：使用 Espressif `mcp-c-sdk` 暴露板载夹具能力，例如 `set_mux_channel`、`reset_dut`、`read_fixture_adc`、`set_load_switch`。这些工具必须小而明确，参数范围写死。

不要在第一阶段让 ESP32 直接调用模型或直接控制所有仪器。原因是：

- 模型调用需要网络、密钥、上下文窗口和日志治理，放在 Python 站更好管理。
- 示波器波形和 FFT/协议解析需要更强算力和成熟 Python 库。
- ESP32 侧应该尽量做可验证的动作和局部采样，减少真实硬件被错误调用的风险。

## Transport 建议

- 本地开发：Python MCP 用 Streamable HTTP，便于 MCP Inspector、浏览器控制台和测试脚本连接。
- CI/mock：stdio 也可用，但不适合真实仪器长连接。
- ESP32 单设备：优先 HTTP JSON-RPC，测试简单，抓包调试方便。
- ESP32 多设备：引入 MQTT broker，使用 MCP over MQTT 进行发现和访问控制。

## 模型调用边界

模型不直接接触原始仪器驱动。推荐把模型调用封装成 `ModelAdapter`：

- 输入：板级上下文、候选拓扑子图、测量特征、已知约束、历史诊断状态。
- 输出：结构化 JSON，包括 `diagnosis`、`confidence`、`evidence`、`next_measurements`、`risk_level`。
- 工具层只执行经过 schema 校验和安全策略检查的下一步测量。

## 版本建议

- Python：先锁官方 `mcp` v1.x 或稳定 FastMCP 发行版；如果使用 v2 开发文档中的能力，需要单独标注实验性。
- ESP32：使用 Espressif Component Registry 的 `espressif/mcp-c-sdk^2.0.1` 起步。
- MQTT：只有当设备发现、多 ESP32、多实验室网络成为明确需求时再引入。

## 参考

- Model Context Protocol 官方介绍：https://modelcontextprotocol.io/docs/getting-started/intro
- 官方 SDK 列表：https://modelcontextprotocol.io/docs/sdk
- MCP Python SDK：https://py.sdk.modelcontextprotocol.io/
- FastMCP：https://gofastmcp.com/getting-started/welcome
- FastMCP Tools：https://gofastmcp.com/servers/tools
- FastMCP Resources：https://gofastmcp.com/servers/resources
- Espressif mcp-c-sdk：https://components.espressif.com/components/espressif/mcp-c-sdk
- EMQX esp-mcp-over-mqtt：https://github.com/emqx/esp-mcp-over-mqtt
- EMQX MCP over MQTT Python SDK：https://docs.emqx.com/en/emqx/latest/emqx-ai/sdks/mcp-sdk-python.html
