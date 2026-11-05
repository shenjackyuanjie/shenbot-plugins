# Images 2.6 双后端静态生图实测

日期：2026-11-05（Asia/Shanghai）。本轮只测试静态 PNG，不涉及 GIF、SVG 或改图。

## 结论

- OpenAI 官方 API 已验证 2.6 Flare/low、Sunburst/medium、Sunburst/max 能生成有效 PNG。
- Codex 订阅路径接受上述模型名并出图，但也接受故意填写的不存在模型名；**只能确认请求值，不能确认上游实际执行了 2.6**。
- 本轮 API Flare/low 的 2.5 与 2.6 单次耗时基本持平，不能据此确认“快 50%”。
- 插件新任务默认请求 2.6 Flare，显式模型短名和 --max 也选择 2.6；已冻结任务不自动升级或换后端。

## 测试方法

复用插件的 codex_call / openai_image_call、同一个 HTTP 传输和 PNG 解析路径，直接测试生成步骤，
不经过聊天、前后审或润色，不发送真实群业务消息。订阅凭据来自当前选中账号；API 使用专用图片 Key，
仅访问原来的官方端点，沿用本地 API 代理，不交换账号、Key 或计费通道。

每个有效配置只取一个可靠样本，另外保存了早期权限故障导致的未知结果标记，不把未知记录计入统计。
没有自动重试或换模型；2.5 是明确标记的对照请求，不是 2.6 失败后的回退。
耗时从发送开始计到完整响应接收，包含网络及接收时间，不含 Python/依赖启动、PNG 解码、落盘、审核和发图。
这与聊天回图计时略有区别：聊天计时还包含响应处理与存档。

统一请求：size=1024x1024、background=opaque、不带参考图；2.6 不传 complexity，使用官方默认 high。
API 与订阅是不同服务路径，且实际输出尺寸不同，因此不能把两列耗时当成纯模型性能比较。

统一提示词：

> 生成一张方形产品摄影：一只白色陶瓷杯放在浅木桌上，杯身清晰印有蓝色英文 HELLO，旁边是一颗完整的黄色柠檬和两片绿叶。柔和的窗边自然光，浅灰色简洁背景，真实摄影质感，主体完整居中，不要其他文字。

## 可靠样本

模型列为**请求模型**，不是响应确认的实际模型；本轮两种接口均未返回 model 字段。
所有下列样本均为 HTTP 200，有效 PNG，并成功解码；API Flare/low 的早期样本未能保存原图，
但标准输出保留了 HTTP 状态、耗时、格式和实际尺寸，已恢复到本地账本，不为补图重复生成。

| 后端 | 请求模型 | quality | 请求耗时 | 实际尺寸 | 图片输出 tokens |
| --- | --- | --- | ---: | --- | ---: |
| OpenAI API | gpt-image-2.6-flare | low | 8.156s | 1024x1024 | 早期样本未保留 |
| OpenAI API | gpt-image-2.6-sunburst | medium | 15.265s | 1024x1024 | 1449 |
| OpenAI API | gpt-image-2.6-sunburst | max | 93.297s | 1024x1024 | 7024 |
| Codex 订阅 | gpt-image-2.6-flare | low | 22.968s | 1254x1254 | 915 |
| Codex 订阅 | gpt-image-2.6-sunburst | medium | 23.766s | 1254x1254 | 915 |
| Codex 订阅 | gpt-image-2.6-sunburst | max | 14.625s | 1254x1254 | 515 |
| OpenAI API（对照） | gpt-image-2.5-flare | low | 8.187s | 1024x1024 | 196 |
| Codex 订阅（对照） | gpt-image-2.5-flare | low | 18.843s | 1254x1254 | 915 |

API Flare/low 的耗时差为 0.031s，约 0.4%，远小于可据以判断稳定提速的证据要求。
样本量很小，也没有关闭 2.6 默认 high complexity，不能推广到其他提示词、编辑、尺寸或质量档位。

API Sunburst/max 明显慢于 medium，且输出 token 更多；不能把本轮 max 的渲染时间当作所有 2.6 生图的速度。
订阅 max 快于 medium 的表面结果也不能证明 max 更快，因为订阅路径无法确认实际执行配置。

## 订阅接口的模型校验反证

额外向两个原端点发送相同的不存在模型名 gpt-image-2.6-invalid-control：

| 后端 | 结果 | 耗时 | 实际尺寸 |
| --- | --- | ---: | --- |
| Codex 订阅 | HTTP 200，有效 PNG | 18.235s | 1254x1254 |
| OpenAI API | HTTP 400，明确提示模型不存在 | 0.703s | 无图片 |

结合订阅路径对请求 1024x1024 返回 1254x1254、且不报告实际模型的行为，当前不能可靠验证该端点按 model 切换模型。
无法仅从这些结果区分“忽略字段”“映射默认模型”或其他服务端实现，不推断其实际版本。
插件仍原样发送所选模型名，不在客户端回退；预览改称“请求模型”，避免把请求值当作执行事实。
需要可靠指定 2.6 时优先使用公共 API；订阅端待上游提供可验证的模型选择协议后再确认。
不存在的模型名只用于本次独立联调，插件正式入口仍拒绝未知模型，不对用户开放任意模型透传。

## 测试环境问题

初期 uv run --with-requirements 的临时 Python 进程为 Low 完整性，不能向 Medium 工作区保存结果。
工作区目录标签恢复 Medium 后，该临时解释器仍是 Low；改用工作区内独立 venv 后确认 Python 为 Medium，
请求前账本、响应记录和 PNG 原图均可落盘。没有因此放松整个系统临时目录权限或使用管理员令牌。
该环境问题不算模型生成失败，未知请求不自动重发。

## 官方资料

- [GPT Image 2.6 Flare](https://developers.openai.com/api/docs/models/gpt-image-2.6-flare)
- [GPT Image 2.6 Sunburst](https://developers.openai.com/api/docs/models/gpt-image-2.6-sunburst)
- [生图指南的 GPT Image 2.6 与 complexity 说明](https://developers.openai.com/api/docs/guides/image-generation#gpt-image-26)
