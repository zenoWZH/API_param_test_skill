---
name: llm-api-test
description: "Use for auditing or testing an LLM provider/model: API parameter compatibility, returned-model identity, token/cache telemetry, image parameters and safety, bounded smoke tests, fixed-rate load tests, multi-model sweeps, job results, or supplier onboarding evidence. Do not use for ordinary application development that merely calls an LLM."
license: MIT
metadata: {"author":"wangzhouhao","version":"2.1.0","openclaw":{"emoji":"🧪","os":["linux"],"requires":{"bins":["bash","python3","uv"]}}}
---

# LLM API Test

Use the bundled CLI and model-profile database; the Web console is optional. Resolve `<skill-root>` to the directory containing this file. Never assume the current working directory or write runtime state into the skill directory.

## Safety and evidence rules

1. Start with the offline doctor. Read-only inspection and planning do not authorize real traffic.
2. Use the user's already-authorized provider, requested model, route, API form, test type, bounds and cost scope. Resolve any missing material bounds before live requests; do not ask again for existing authorization. Keep credentials in the private data directory or process environment; never echo them into chat, argv, logs, reports, or Git.
3. Discover with `/v1/models` or the provider's equivalent when appropriate, then send only a minimal reachability request. A listing, metadata row, HTTP 202, or accepted async job is not proof of working capability; require a completed response and applicable identity, usage/cache, or media evidence.
4. Treat model family, requested model, source, route profile, API form, transport, Profile, Interface, Contract, and Test Binding as separate facts. Never infer one from another.
5. Model facts come from bundled MPDB snapshots. `providers.local.yaml` describes private execution routes and cannot invent capabilities. Distinguish executable parameter bindings, registered functional workflows, and dedicated research matrices. If no matching execution path exists, fail closed and produce a review proposal; a research declaration never enables generic parameter or pressure dispatch.
6. Pressure-test only an explicitly pressure-enabled text binding. First enumerate actual candidates and verify each route/API form. A client run that did not drive the configured target rate is not capacity evidence.
7. Stop immediately when the user says stop or exit. Report completed, failed, skipped, and still-unverified work separately.

## Bootstrap and discovery

Run these through the stable launcher:

```bash
bash <skill-root>/bin/llm-api-test setup
bash <skill-root>/bin/llm-api-test doctor --json
bash <skill-root>/bin/llm-api-test providers
```

`setup` writes only to:

- `${LLM_API_TEST_DATA_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/llm-api-test}` for secrets, provider overlays, reports, and workflow state.
- `${LLM_API_TEST_RUNTIME_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/llm-api-test}` for the disposable Python runtime and dependency cache.

If the skill is mounted read-only or runs in a sandbox, explicitly bind both destinations as writable. See [tool compatibility](references/tool-compatibility.md).

Inspect database identity and resolve one exact source/model leaf before testing:

```bash
bash <skill-root>/bin/llm-api-test mpdb info
bash <skill-root>/bin/llm-api-test mpdb resolve <source-id> <model> \
  --modality text --api-form <api-form> --routing-mode <routing-mode>
```

## Stable commands

| Task | Command |
| --- | --- |
| Offline health and MPDB integrity | `bin/llm-api-test doctor --json` |
| Provider/model discovery | `bin/llm-api-test providers` |
| One bounded test | `bin/llm-api-test run --type <type> --provider <P> --model <M> ...` |
| Current parameter matrices | `bin/llm-api-test matrix list --source <S> --model <M>` |
| Freeze a functional plan offline | `bin/llm-api-test matrix preview --provider <P> --model <M> ...` |
| Execute the reviewed frozen plan | `bin/llm-api-test matrix run --job-spec <path> --yes` |
| Dedicated DeepSeek V4.1 research | `bin/llm-api-test deepseek-v41 --suite smoke --output-dir <external-dir>` |
| Fixed-rate multi-model plan/run | `bin/llm-api-test sweep --provider <P> [--models ...]` |
| Jobs and cancellation | `bin/llm-api-test jobs [--running|--id ID|--stop ID]` |
| Condensed evidence | `bin/llm-api-test result --id <job-id> [--full]` |
| Supplier workflow | `bin/llm-api-test workflow <subcommand> ...` |
| MPDB query/verification | `bin/llm-api-test mpdb <subcommand> ...` |
| Optional local console | `bin/llm-api-test console <start|status|stop|...>` |

Available one-job types are `param_test`, `cache_suite`, `image_param_test`, `quick_load`, `staircase`, `soak`, and `trace_test`. Use `--background` for a managed asynchronous job. Read [testing guide](references/testing-guide.md) before interpreting its evidence.

For current parameter matrices, read [parameter matrix and frozen plans](references/parameter-matrix.md). Functional jobs use v6 plans with dependency closure, request and cleanup budgets, and per-run resource ownership. Default to the complete enabled suite and one run unless the user selects otherwise. Keep a dedicated research result's proof scope in the report. Cleanup recovery uses the original job and run ID and never replays business requests.

Image/video/audio input tests of text-output APIs use an explicitly selected `media-input/...` workflow under `param_test`. They are not image-generation jobs and must not silently replace ordinary parameter or Fable defaults. External video fixtures have counted before/after byte-hash checks. Recognition, identity, usage arithmetic and independent media token proof remain separate evidence; inconclusive media counts are not a full certification or an unsupported-model verdict.

### Fixed-rate model sweep

Planning is offline and does not send traffic:

```bash
bash <skill-root>/bin/llm-api-test sweep \
  --provider <P> --models <M1>,<M2>
```

Inspect the returned family, route, API form, source, Profile/Interface, and `eligible` flag for every candidate. Execution requires every bound to be explicit plus `--execute --yes`:

```bash
bash <skill-root>/bin/llm-api-test sweep \
  --provider <P> --models <M1>,<M2> \
  --target-rpm 100 --duration 10m --users 60 --spawn-rate 30 \
  --execute --yes
```

Only use `--yes` after the user has approved the exact plan. The bundled sweep currently runs candidates sequentially and enforces a constant-throughput plan per model; do not describe it as parallel execution.

## Supplier onboarding

Start or resume the state machine:

```bash
bash <skill-root>/bin/llm-api-test workflow start --provider <P> --model <M>
bash <skill-root>/bin/llm-api-test workflow status --provider <P> --model <M>
bash <skill-root>/bin/llm-api-test workflow next --provider <P> --model <M>
```

At the onboarding node, `onboard-propose` emits JSON evidence for independent MPDB review. It never changes the database. After the approved MPDB source snapshot and consumer have been migrated and the exact binding resolves, close the workflow with:

```bash
bash <skill-root>/bin/llm-api-test workflow onboard-apply \
  --provider <P> --model <M> --review-ref <approval-or-commit> --yes
```

Despite the compatibility name, `onboard-apply` only verifies the installed binding and records completion; it does not create a local model overlay or require an independent package-registry release. See [supplier onboarding](references/supplier-onboarding-workflow.md) and [MPDB guide](references/model-profile-database.md).

## Reporting

Summarize the exact job result and include:

- provider/model/route/API form and returned-model identity status;
- test bounds and whether the client achieved the intended request rate;
- success/error counts, latency windows, token/cache telemetry, or decoded media evidence as applicable;
- evidence gaps, skipped checks, and whether the result is offline/mock or live-provider evidence.

Do not claim provider support merely because a catalog/profile exists. Do not reuse older report counts as current evidence.

## References

- [Current matrix review](MATRIX_REVIEW_20260919.md)
- [Parameter matrix and frozen functional plans](references/parameter-matrix.md)
- [Historical migration plan](MIGRATION_PLAN.md)
- [Model-profile database](references/model-profile-database.md)
- [Multi-tool and OpenClaw compatibility](references/tool-compatibility.md)
- [Supplier onboarding workflow](references/supplier-onboarding-workflow.md)
- [Test interpretation](references/testing-guide.md)
- [Console access and tunnel boundaries](references/console-access.md)
- [Vendored engine details](app/README.md)
