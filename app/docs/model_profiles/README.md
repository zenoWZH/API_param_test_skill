# 模型家族 Profile 手册索引

本目录由 Model Profile Database（含测试扩展）的普通运行投影和 `config.yaml` 自动生成。`identity_only`、禁用或研究绑定可能被过滤，不代表完整 catalog 的全部身份。Claude、Claude Fable、DeepSeek、GPT 属于暂缓 App 同步的家族，现有手册仅保留，正文不参与本生成器的重写与一致性检查。

| 模态 | 模型家族 | 规范模型数 | Route/API Form 组合数 | Profile/Case 数 | 文档 |
|---|---|---:|---:|---:|---|
| image | `banana` | 4 | 2 | 151 | [Banana / Gemini Image](./banana.md) |
| image | `gpt-image-2` | 6 | 3 | 169 | [GPT Image](./gpt_image_2.md) |
| image | `grok-imagine` | 3 | 1 | 11 | [Grok Imagine](./grok_imagine.md) |
| text | `deepseek` | 4 | 6 | 119 | [DeepSeek](./deepseek.md) |
| text | `glm` | 8 | 2 | 75 | [GLM](./glm.md) |
| text | `kimi` | 4 | 2 | 32 | [Kimi](./kimi.md) |
| text | `qwen` | 5 | 1 | 25 | [Qwen](./qwen.md) |
| text | `claude` | 9 | 3 | 44 | [Claude](./claude.md) |
| text | `claude_fable` | 1 | 2 | 37 | [Claude Fable](./claude_fable.md) |
| text | `gemini` | 10 | 2 | 53 | [Gemini](./gemini.md) |
| text | `minimax` | 3 | 1 | 7 | [MiniMax](./minimax.md) |
| text | `gpt` | 18 | 2 | 86 | [GPT](./gpt.md) |
| text | `grok` | 5 | 2 | 22 | [Grok](./grok.md) |

## 更新方法

```bash
python scripts/generate_test_docs.py
python scripts/generate_test_docs.py --check
```

`--check` 不改文件；本次实际生成的索引与非暂缓家族文档不一致时退出 1。它不验证暂缓家族正文、被过滤的身份/研究矩阵、手写指南或历史实网证据。

## 专用研究与历史证据

- [源码仓库的 Fable 5 / 5.1 专项审计](https://github.com/zenoWZH/api_pressure/blob/main/docs/fable_5_5_1_param_audit_20260912.md)：5.1 已登记，专用研究 runner 与普通参数入口分开，普通执行尚未开放。
