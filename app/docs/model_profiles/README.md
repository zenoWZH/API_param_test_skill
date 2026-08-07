# 模型家族 Profile 手册索引

本目录由 `model_capability_profiles.yaml`、`api_reference_specs.yaml` 和 `config.yaml` 自动生成。每个已注册模型家族必须有且只有一份说明文档；每个文字 Reference Source 的可执行 profile、每个图片 case 都必须出现在对应家族文档中。

| 模态 | 模型家族 | 规范模型数 | Route/API Form 组合数 | Profile/Case 数 | 文档 |
|---|---|---:|---:|---:|---|
| text | `deepseek` | 10 | 3 | 32 | [DeepSeek](./deepseek.md) |
| text | `glm` | 8 | 3 | 40 | [GLM](./glm.md) |
| text | `qwen` | 5 | 2 | 25 | [Qwen](./qwen.md) |
| text | `gemini` | 12 | 5 | 61 | [Gemini](./gemini.md) |
| text | `claude` | 9 | 6 | 28 | [Claude](./claude.md) |
| text | `claude_fable` | 2 | 6 | 19 | [Claude Fable](./claude_fable.md) |
| text | `gpt` | 14 | 4 | 38 | [GPT](./gpt.md) |
| text | `kimi` | 4 | 4 | 32 | [Kimi](./kimi.md) |
| text | `minimax` | 3 | 2 | 7 | [MiniMax](./minimax.md) |
| text | `grok` | 5 | 4 | 22 | [Grok](./grok.md) |
| image | `gpt-image-2` | 4 | 2 | 14 | [GPT Image](./gpt_image_2.md) |
| image | `banana` | 4 | 3 | 9 | [Banana / Gemini Image](./banana.md) |
| image | `grok-imagine` | 2 | 2 | 11 | [Grok Imagine](./grok_imagine.md) |

## 更新方法

```bash
python scripts/generate_test_docs.py
python scripts/generate_test_docs.py --check
```

`--check` 不改文件；只要 schema 新增家族、Reference Source、profile 或图片 case 而文档尚未重生成，就会退出 1。
