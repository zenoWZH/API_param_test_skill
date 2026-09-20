const appState = {
  config: null,
  workflowPreview: null,
  workflowPreviewKey: "",
  workflowPreviewLoading: false,
  workflowPreviewError: "",
  workflowPreviewRequestId: 0,
  workflowPreviewTimer: null,
  activeTab: "param",
  currentJobId: null,
  currentJob: null,
  paramSpec: null,
  paramHistoryResult: null,
  paramHistoryLoading: false,
  paramHistoryRequestId: 0,
  paramLiveJobId: null,
  imageHistoryResult: null,
  imageHistoryLoading: false,
  imageHistoryRequestId: 0,
  imageLiveJobId: null,
  imagePlanPreviewKey: "",
  imagePlanPreview: null,
  imagePlanPreviewCount: null,
  imagePlanPreviewLoading: false,
  imagePlanPreviewError: "",
  timeoutSec: 300,
  loadResults: [],
  selectedLoadResultId: "",
  selectedLoadResult: null,
  resultsLastRefreshMs: 0,
  formsByTab: {
    param: {
      provider: "",
      model: "",
      routeProfile: "",
      apiForm: "",
      referenceContractId: "",
      workflowBindingId: "",
      workflowCases: "",
      parameterSuite: "",
      contractManual: false,
      toolValidationMode: "auto",
      paramTestRuns: 1,
    },
    image: {
      provider: "",
      model: "",
      routeProfile: "",
      apiForm: "",
      transport: "",
      suite: "full",
      runCount: 1,
      cases: "",
      quality: "low",
      outputFormat: "png",
      include2k: false,
      include4k: false,
      resolutionPreferences: {},
      noNegative: false,
      noCrossControl: false,
      visualForensics: true,
    },
    load: {
      provider: "",
      model: "",
      workload: "throughput",
      users: 10,
      spawnRate: 2,
      duration: "2m",
      targetRpm: 500,
      targetTpm: 0,
      requestMode: "unique",
      staircaseSteps: [10, 20, 40],
      staircaseStepDuration: "5m",
      staircaseSpawnRate: 5,
      staircaseWarmupEnabled: true,
      staircaseWarmupUsers: 10,
      staircaseWarmupDuration: "1m",
      staircaseAutoExtend: false,
      staircaseIncrementUsers: 30,
      staircaseMaxUsers: 200,
      soakUsers: 80,
      soakSpawnRate: 5,
      soakDuration: "1h",
    },
    cache: {
      provider: "",
      model: "",
      routeProfile: "",
      apiForm: "",
      scenario: "progressive_customer_session",
      diagnosticScenario: "",
      sessions: 10,
      roundsPerSession: 4,
      contentProfile: "realistic",
      customContent: false,
      customUserChars: [200, 2000],
      customToolResultChars: [500, 5000],
      toolStage: "3",
      controlMode: "auto",
      positivePairs: 3,
      negativeRequests: 3,
      waitAfterSeed: 5,
      maxTokens: 128,
      seed: 20260715,
      kilocodeSteps: 20,
      kilocodeTrajectoryMode: "scripted",
      diagnosticPositivePairs: 3,
      diagnosticNegativeRequests: 3,
      measuredRequests: 50,
      warmupRequests: 2,
      waitAfterWarmup: 5,
      maxRunSeconds: 1800,
      failureLimit: 3,
      confirmLarge: false,
    },
  },
};

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => (
    {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[char]
  ));
}

function isSafetyParameter(value) {
  return String(value || "").toLowerCase().includes("safety");
}

function fmtPct(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "n/a";
  return `${(Number(value) * 100).toFixed(1)}%`;
}

function fmtNum(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "n/a";
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: 1 });
}

function rangePair(value, fallbackMin, fallbackMax) {
  const source = value || {};
  return [Number(source.min || fallbackMin), Number(source.max || fallbackMax)];
}

function parseRangeInput(id, fallback) {
  const parts = String($(id).value || "").split(",").map((item) => Number(item.trim()));
  if (parts.length !== 2 || parts.some((item) => !Number.isFinite(item) || item <= 0) || parts[1] < parts[0]) {
    return fallback;
  }
  return [Math.trunc(parts[0]), Math.trunc(parts[1])];
}

function fmtDuration(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "n/a";
  const milliseconds = Number(value);
  if (Math.abs(milliseconds) >= 1000) {
    const seconds = milliseconds / 1000;
    return `${seconds.toLocaleString(undefined, { maximumFractionDigits: 2 })}s`;
  }
  return `${milliseconds.toLocaleString(undefined, { maximumFractionDigits: 1 })}ms`;
}

function fmtLatencyPercentiles(summary, prefix) {
  const values = [50, 90, 95, 99].map((percentile) => summary[`${prefix}_p${percentile}_ms`]);
  if (values.every((value) => value === null || value === undefined)) return "n/a";
  return values.map(fmtNum).join(" / ");
}

function fmtTtftCoverage(summary) {
  const samples = Number(summary.ttft_sample_count || 0);
  const successes = Number(summary.business_success_count || 0);
  if (!successes) return "n/a";
  return `${samples}/${successes} · ${fmtPct(summary.ttft_coverage)}`;
}

function fmtCompact(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "n/a";
  const number = Number(value);
  const absolute = Math.abs(number);
  if (absolute >= 1_000_000) {
    return `${(number / 1_000_000).toLocaleString(undefined, { maximumFractionDigits: 1 })}M`;
  }
  if (absolute >= 1_000) {
    return `${(number / 1_000).toLocaleString(undefined, { maximumFractionDigits: 1 })}k`;
  }
  return fmtNum(number);
}

function niceMax(value) {
  const number = Math.max(Number(value) || 0, 1);
  const magnitude = 10 ** Math.floor(Math.log10(number));
  return Math.ceil((number / magnitude) * 2) / 2 * magnitude;
}

function numericValue(value) {
  return value === null || value === undefined || value === "" ? null : Number(value);
}

function renderLoadCharts(job, prefix = "load") {
  const points = Array.isArray(job && job.time_series)
    ? job.time_series.filter((point) => Number.isFinite(Number(point.timestamp)))
    : [];
  const status = $(`${prefix}ChartStatus`);
  if (!points.length) {
    status.textContent = job ? "等待第一批请求完成。" : "No load test data.";
    renderLineChart(`${prefix}RpmChart`, [], [], {});
    renderLineChart(`${prefix}TpmChart`, [], [], {});
    return;
  }

  const latest = points[points.length - 1];
  const summary = job.summary || {};
  const overallTpm = summary.token_usage_record_count ? fmtCompact(summary.total_tpm) : "n/a";
  const context = [
    latest.staircase_step ? `step ${latest.staircase_step}` : "",
    latest.configured_users ? `${latest.configured_users} users` : "",
  ].filter(Boolean).join(" · ");
  status.textContent = [
    new Date(Number(latest.timestamp) * 1000).toLocaleTimeString(),
    context,
    `${fmtNum(latest.business_rpm)} success RPM`,
    `${fmtCompact(latest.total_tpm)} latest observed TPM`,
    `${overallTpm} overall observed TPM`,
    `${fmtPct(latest.success_rate)} success`,
  ].filter(Boolean).join(" · ");

  const targetRpm = Number(job.target_rpm || 0);
  const targetTpm = Number(job.target_tpm || 0);
  const observedRpmMax = Math.max(...points.flatMap((point) => [
    Number(point.attempted_business_rpm || 0),
    Number(point.business_rpm || 0),
  ]), 1);
  const observedTpmMax = Math.max(...points.map((point) => Number(point.total_tpm || 0)), 1);
  const showTargetRpm = targetRpm > 0 && targetRpm <= observedRpmMax * 3;
  const showTargetTpm = targetTpm > 0 && targetTpm <= observedTpmMax * 1.5;
  const offScaleTargets = [
    targetRpm > 0 && !showTargetRpm ? `${fmtCompact(targetRpm)} RPM target off-scale` : "",
    targetTpm > 0 && !showTargetTpm ? `${fmtCompact(targetTpm)} TPM target off-scale` : "",
  ].filter(Boolean);
  if (offScaleTargets.length) status.textContent += ` · ${offScaleTargets.join(" · ")}`;
  const chartPoints = points.map((point) => ({
    ...point,
    target_rpm: targetRpm || null,
    target_tpm: targetTpm || null,
  }));
  const rpmSeries = [
    { key: "success_rate", label: "Success", color: "#067647", axis: "left" },
    { key: "attempted_business_rpm", label: "Attempted RPM", color: "#98a2b3", axis: "right" },
    { key: "business_rpm", label: "Success RPM", color: "#2563eb", axis: "right" },
  ];
  if (showTargetRpm) {
    rpmSeries.push({ key: "target_rpm", label: "RPM cap / goal", color: "#7f56d9", axis: "right", dash: "6 4" });
  }
  renderLineChart(`${prefix}RpmChart`, chartPoints, rpmSeries, {
    leftMax: 1,
    leftFormat: (value) => `${Math.round(value * 100)}%`,
    rightFormat: fmtCompact,
    leftLabel: "success",
    rightLabel: "RPM",
  });

  const tpmSeries = [
    { key: "success_rate", label: "Success", color: "#067647", axis: "left" },
    { key: "total_tpm", label: "Observed TPM", color: "#b54708", axis: "right" },
  ];
  if (showTargetTpm) {
    tpmSeries.push({ key: "target_tpm", label: "TPM cap / goal", color: "#7f56d9", axis: "right", dash: "6 4" });
  }
  renderLineChart(`${prefix}TpmChart`, chartPoints, tpmSeries, {
    leftMax: 1,
    leftFormat: (value) => `${Math.round(value * 100)}%`,
    rightFormat: fmtCompact,
    leftLabel: "success",
    rightLabel: "TPM",
  });
}

function renderLineChart(containerId, points, series, options) {
  const container = $(containerId);
  if (!container) return;
  if (!points.length || !series.length) {
    container.innerHTML = '<div class="muted" style="padding:96px 12px;text-align:center">No samples yet.</div>';
    return;
  }

  const width = 900;
  const height = 250;
  const margin = { top: 38, right: options.rightLabel ? 58 : 20, bottom: 32, left: 58 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const timestamps = points.map((point) => Number(point.timestamp));
  const xMin = Math.min(...timestamps);
  const xMax = Math.max(...timestamps);
  const x = (timestamp, index) => (
    xMax > xMin
      ? margin.left + ((Number(timestamp) - xMin) / (xMax - xMin)) * plotWidth
      : margin.left + (points.length > 1 ? index / (points.length - 1) : 0.5) * plotWidth
  );
  const axisValues = (axis) => series
    .filter((item) => item.axis === axis)
    .flatMap((item) => points.map((point) => numericValue(point[item.key])))
    .filter(Number.isFinite);
  const leftMax = Number(options.leftMax) || niceMax(Math.max(...axisValues("left"), 1));
  const rightValues = axisValues("right");
  const rightMax = rightValues.length ? niceMax(Math.max(...rightValues, 1)) : 1;
  const y = (value, axis) => {
    const max = axis === "right" ? rightMax : leftMax;
    return margin.top + plotHeight - (Math.max(0, Number(value)) / max) * plotHeight;
  };
  const leftFormat = options.leftFormat || fmtCompact;
  const rightFormat = options.rightFormat || fmtCompact;
  const grid = Array.from({ length: 5 }, (_, index) => {
    const ratio = index / 4;
    const yy = margin.top + ratio * plotHeight;
    const leftValue = leftMax * (1 - ratio);
    const rightValue = rightMax * (1 - ratio);
    return [
      `<line x1="${margin.left}" y1="${yy}" x2="${width - margin.right}" y2="${yy}" stroke="#e4e7ec"/>`,
      `<text x="${margin.left - 8}" y="${yy + 4}" text-anchor="end" fill="#667085" font-size="11">${esc(leftFormat(leftValue))}</text>`,
      options.rightLabel
        ? `<text x="${width - margin.right + 8}" y="${yy + 4}" fill="#667085" font-size="11">${esc(rightFormat(rightValue))}</text>`
        : "",
    ].join("");
  }).join("");
  const xTicks = [0, 0.5, 1].map((ratio) => {
    const timestamp = xMin + (xMax - xMin) * ratio;
    const xx = margin.left + plotWidth * ratio;
    return `<text x="${xx}" y="${height - 9}" text-anchor="${ratio === 0 ? "start" : (ratio === 1 ? "end" : "middle")}" fill="#667085" font-size="11">${esc(new Date(timestamp * 1000).toLocaleTimeString())}</text>`;
  }).join("");

  const stepMarkers = [];
  let previousContext = "";
  points.forEach((point, index) => {
    const context = point.staircase_step
      ? `S${point.staircase_step}${point.configured_users ? ` · ${point.configured_users}u` : ""}`
      : (point.configured_users ? `${point.configured_users}u` : "");
    if (context && context !== previousContext) {
      const xx = x(point.timestamp, index);
      stepMarkers.push(`<line x1="${xx}" y1="${margin.top}" x2="${xx}" y2="${height - margin.bottom}" stroke="#d0d5dd" stroke-dasharray="3 4"/>`);
      stepMarkers.push(`<text x="${Math.min(xx + 4, width - margin.right - 45)}" y="${margin.top + 12}" fill="#667085" font-size="10">${esc(context)}</text>`);
    }
    previousContext = context;
  });

  const paths = series.map((item) => {
    const coordinates = points.map((point, index) => {
      const value = numericValue(point[item.key]);
      return Number.isFinite(value) ? `${x(point.timestamp, index)},${y(value, item.axis)}` : null;
    }).filter(Boolean);
    if (!coordinates.length) return "";
    return `<polyline points="${coordinates.join(" ")}" fill="none" stroke="${item.color}" stroke-width="2.25" stroke-linejoin="round" stroke-linecap="round"${item.dash ? ` stroke-dasharray="${item.dash}"` : ""}/>`;
  }).join("");
  const latestIndex = points.length - 1;
  const latest = points[latestIndex];
  const dots = series.map((item) => {
    const value = numericValue(latest[item.key]);
    if (!Number.isFinite(value)) return "";
    return `<circle cx="${x(latest.timestamp, latestIndex)}" cy="${y(value, item.axis)}" r="3.5" fill="${item.color}"><title>${esc(`${item.label}: ${fmtNum(value)}`)}</title></circle>`;
  }).join("");
  const legend = series.map((item, index) => {
    const xx = margin.left + index * 150;
    return `<g><line x1="${xx}" y1="16" x2="${xx + 20}" y2="16" stroke="${item.color}" stroke-width="3"${item.dash ? ` stroke-dasharray="${item.dash}"` : ""}/><text x="${xx + 26}" y="20" fill="#475467" font-size="11">${esc(item.label)}</text></g>`;
  }).join("");

  container.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="load test time series">
    ${grid}
    ${stepMarkers.join("")}
    ${paths}
    ${dots}
    ${legend}
    ${xTicks}
    <text x="12" y="${margin.top - 9}" fill="#667085" font-size="10">${esc(options.leftLabel || "")}</text>
    ${options.rightLabel ? `<text x="${width - margin.right + 8}" y="${margin.top - 9}" fill="#667085" font-size="10">${esc(options.rightLabel)}</text>` : ""}
  </svg>`;
}

function providers() {
  return (appState.config && appState.config.providers) || [];
}

function providerByName(name) {
  return providers().find((item) => item.name === name) || providers()[0] || null;
}

function imageProviders() {
  return (appState.config && appState.config.image_providers) || [];
}

function imageProviderByName(name) {
  return imageProviders().find((item) => item.name === name) || imageProviders()[0] || null;
}

function imageModelsFor(providerName) {
  const provider = imageProviderByName(providerName);
  return provider && Array.isArray(provider.models) ? provider.models : [];
}

function imageModelById(providerName, model) {
  return imageModelsFor(providerName).find((item) => item.id === model) || imageModelsFor(providerName)[0] || null;
}

function selectedImageModel(providerName, currentModel) {
  const provider = imageProviderByName(providerName);
  const models = imageModelsFor(providerName);
  if (currentModel && models.some((item) => item.id === currentModel)) return currentModel;
  return (provider && provider.default_model) || (models[0] && models[0].id) || "";
}

function selectedProviderForTab(tab) {
  if (tab === "image") return imageProviderByName(appState.formsByTab.image.provider);
  return providerByName(appState.formsByTab[tab].provider);
}

function modelsFor(providerName) {
  const provider = providerByName(providerName);
  if (!provider) return [];
  const models = provider.models || {};
  const candidates = Array.isArray(models.candidates) ? models.candidates.slice() : [];
  const selected = models.default || candidates[0] || appState.config.active_model || "";
  if (selected && !candidates.includes(selected)) candidates.unshift(selected);
  return candidates;
}

function selectedModelForProvider(providerName, currentModel) {
  const models = modelsFor(providerName);
  if (currentModel && models.includes(currentModel)) return currentModel;
  const provider = providerByName(providerName);
  return (provider && provider.models && provider.models.default) || models[0] || "";
}

function familyFor(providerName, model) {
  const provider = providerByName(providerName);
  const families = provider && provider.models && provider.models.families;
  if (families && families[model]) return families[model];
  const lowered = String(model || "").toLowerCase();
  if (lowered.startsWith("deepseek")) return "deepseek";
  if (lowered.startsWith("glm")) return "glm";
  if (lowered.startsWith("qwen")) return "qwen";
  if (lowered.startsWith("gemini")) return "gemini";
  if (lowered.includes("fable")) return "claude_fable";
  if (lowered.startsWith("claude")) return "claude";
  if (lowered.startsWith("grok")) return "grok";
  if (lowered.startsWith("kimi") || lowered.startsWith("moonshotai/")) return "kimi";
  if (lowered.startsWith("minimax")) return "minimax";
  if (lowered.startsWith("gpt") || lowered.startsWith("openai/")) return "gpt";
  return "unknown";
}

function referenceContractIdForFamily(family) {
  const contracts = (appState.config && (
    appState.config.reference_contracts || appState.config.reference_sources
  )) || [];
  const match = contracts.find((contract) => (
    contract.default_for_families || []
  ).includes(family));
  return (match && (match.contract_id || match.id))
    || appState.config.default_reference_contract_id
    || appState.config.default_reference_source
    || (contracts[0] && (contracts[0].contract_id || contracts[0].id))
    || "";
}

function modelCapability(providerName, model) {
  const byProvider = (appState.config && appState.config.model_capabilities) || {};
  return (byProvider[providerName] && byProvider[providerName][model]) || null;
}

function routesForModel(providerName, model) {
  const capability = modelCapability(providerName, model) || {};
  return capability.routes || {};
}

function routeProfileForModel(providerName, model, current = "") {
  const capability = modelCapability(providerName, model) || {};
  const routes = routesForModel(providerName, model);
  if (current && routes[current]) return current;
  return capability.workflow_default_route_profile || capability.default_route_profile || Object.keys(routes)[0] || capability.route_profile || "";
}

function routeCapability(providerName, model, routeProfile = "") {
  const capability = modelCapability(providerName, model) || {};
  const selectedRoute = routeProfileForModel(providerName, model, routeProfile);
  return routesForModel(providerName, model)[selectedRoute] || capability;
}

function apiFormsForModel(providerName, model, routeProfile = "") {
  return routeCapability(providerName, model, routeProfile).api_forms || {};
}

function apiFormForModel(providerName, model, routeProfile = "", current = "") {
  const capability = routeCapability(providerName, model, routeProfile);
  const forms = apiFormsForModel(providerName, model, routeProfile);
  if (current && forms[current]) return current;
  return capability.workflow_default_api_form || capability.default_api_form || Object.keys(forms)[0] || capability.api_form || "";
}

function apiFormCapability(providerName, model, routeProfile, apiForm) {
  const capability = routeCapability(providerName, model, routeProfile);
  return apiFormsForModel(providerName, model, routeProfile)[apiForm] || capability;
}

function cacheLeafRunnable(capability) {
  return !!capability
    && capability.profile_status === "registered"
    && capability.pressure_test_runnable === true;
}

function cacheLeafSupportsTools(capability) {
  return cacheLeafRunnable(capability)
    && capability.cache_tool_runnable === true;
}

function selectedCacheLeafCapability() {
  const form = appState.formsByTab.cache;
  return apiFormCapability(
    form.provider, form.model, form.routeProfile, form.apiForm
  ) || {};
}

function cacheScenarioNeedsTools(form) {
  const diagnostic = form.diagnosticScenario || "";
  return diagnostic === "kilocode_agent_session"
    || (!diagnostic && form.toolStage !== "off");
}

function cacheScenarioRunnable(capability, form) {
  return cacheLeafRunnable(capability)
    && (!cacheScenarioNeedsTools(form) || cacheLeafSupportsTools(capability));
}

function normalizeCacheScenarioForCapability(form, capability) {
  if (cacheLeafSupportsTools(capability)) return false;
  let changed = false;
  if (form.toolStage !== "off") {
    form.toolStage = "off";
    changed = true;
  }
  if (form.diagnosticScenario === "kilocode_agent_session") {
    form.diagnosticScenario = "";
    changed = true;
  }
  return changed;
}

function cacheRouteRunnable(capability) {
  const forms = (capability && capability.api_forms) || {};
  const leaves = Object.values(forms);
  return leaves.length
    ? leaves.some((leaf) => cacheLeafRunnable(leaf))
    : cacheLeafRunnable(capability);
}

function cacheModelRunnable(providerName, model) {
  const capability = modelCapability(providerName, model) || {};
  const routeLeaves = Object.values(capability.routes || {});
  return routeLeaves.length
    ? routeLeaves.some((route) => cacheRouteRunnable(route))
    : cacheLeafRunnable(capability);
}

function cacheModelsFor(providerName) {
  const models = modelsFor(providerName);
  const runnable = models.filter((model) => cacheModelRunnable(providerName, model));
  return runnable.length ? runnable : models;
}

function cacheSelectedModelForProvider(providerName, currentModel = "") {
  const models = cacheModelsFor(providerName);
  if (currentModel && models.includes(currentModel)) return currentModel;
  return models[0] || "";
}

function cacheRoutesForModel(providerName, model) {
  const routes = routesForModel(providerName, model);
  const runnable = Object.fromEntries(
    Object.entries(routes).filter(([, capability]) => cacheRouteRunnable(capability))
  );
  return Object.keys(runnable).length ? runnable : routes;
}

function cacheRouteProfileForModel(providerName, model, current = "") {
  const capability = modelCapability(providerName, model) || {};
  const allRoutes = routesForModel(providerName, model);
  const runnableRoutes = Object.entries(allRoutes).filter(
    ([, route]) => cacheRouteRunnable(route)
  );
  const routes = runnableRoutes.length ? Object.fromEntries(runnableRoutes) : allRoutes;
  if (current && routes[current]) return current;
  if (runnableRoutes.length) return runnableRoutes[0][0];
  return capability.default_route_profile || Object.keys(routes)[0] || capability.route_profile || "";
}

function cacheApiFormsForModel(providerName, model, routeProfile = "") {
  const forms = apiFormsForModel(providerName, model, routeProfile);
  const runnable = Object.fromEntries(
    Object.entries(forms).filter(([, capability]) => cacheLeafRunnable(capability))
  );
  return Object.keys(runnable).length ? runnable : forms;
}

function cacheApiFormForModel(providerName, model, routeProfile = "", current = "") {
  const capability = routeCapability(providerName, model, routeProfile);
  const allForms = apiFormsForModel(providerName, model, routeProfile);
  const runnableForms = Object.entries(allForms).filter(
    ([, leaf]) => cacheLeafRunnable(leaf)
  );
  const forms = runnableForms.length ? Object.fromEntries(runnableForms) : allForms;
  if (current && forms[current]) return current;
  if (runnableForms.length) return runnableForms[0][0];
  return capability.default_api_form || Object.keys(forms)[0] || capability.api_form || "";
}

function imageModelCapability(providerName, model) {
  const byProvider = (appState.config && appState.config.image_model_capabilities) || {};
  return (byProvider[providerName] && byProvider[providerName][model]) || null;
}

function imageRouteProfileForModel(providerName, model, current = "") {
  const capability = imageModelCapability(providerName, model) || {};
  const routes = capability.routes || {};
  if (current && routes[current]) return current;
  return capability.default_route_profile || Object.keys(routes)[0] || capability.route_profile || "";
}

function imageRouteCapability(providerName, model, routeProfile = "") {
  const capability = imageModelCapability(providerName, model) || {};
  const selectedRoute = imageRouteProfileForModel(providerName, model, routeProfile);
  return (capability.routes && capability.routes[selectedRoute]) || capability;
}

function imageApiFormForModel(providerName, model, routeProfile = "", current = "") {
  const capability = imageRouteCapability(providerName, model, routeProfile);
  const forms = capability.api_forms || {};
  if (current && forms[current]) return current;
  return capability.default_api_form || Object.keys(forms)[0] || capability.api_form || "";
}

function imageApiFormCapability(providerName, model, routeProfile, apiForm) {
  const capability = imageRouteCapability(providerName, model, routeProfile);
  return (capability.api_forms && capability.api_forms[apiForm]) || capability;
}

function referenceContractIdForModel(providerName, model, routeProfile = "", apiForm = "") {
  const selectedForm = apiFormForModel(providerName, model, routeProfile, apiForm);
  const capability = apiFormCapability(providerName, model, routeProfile, selectedForm);
  const form = appState.formsByTab.param;
  const ordinarySelected = form.workflowBindingId === "__ordinary__"
    && form.provider === providerName && form.model === model && form.apiForm === selectedForm;
  const workflow = !ordinarySelected && capability && capability.workflow_available
    && (capability.test_workflows || []).find((item) => !isOptionalParamWorkflow(item));
  if (workflow) return workflow.reference_contract_id;
  return (capability && (
    capability.default_reference_contract_id || capability.reference_source
  )) || "";
}

function contractById(id) {
  const contracts = (appState.config && (
    appState.config.reference_contracts || appState.config.reference_sources
  )) || [];
  return contracts.find((item) => (item.contract_id || item.id) === id) || null;
}

function isBusy(job = appState.currentJob) {
  return !!job && ["queued", "running", "stopping"].includes(job.status);
}

function tabForJobType(type) {
  if (type === "param_test") return "param";
  if (type === "image_param_test") return "image";
  if (type === "cache_suite") return "cache";
  return "load";
}

function isLoadJob(job) {
  return !!job && !["param_test", "image_param_test", "cache_suite"].includes(job.type);
}

function showError(message) {
  const node = $("actionError");
  node.textContent = message || "";
  node.classList.toggle("active", !!message);
}

async function loadConfig() {
  appState.config = await fetch("/api/config", { cache: "no-store" }).then((resp) => resp.json());
  initialiseForms();
  renderControls();
  renderCacheSummary();
  await Promise.all([loadParamSpecs(), loadLatestImageResult()]);
  await pollJob();
  await refreshLoadResults();
}

function initialiseForms() {
  const defaults = appState.config.defaults || {};
  const provider = appState.config.active_provider || (providers()[0] && providers()[0].name) || "";
  const model = selectedModelForProvider(provider, appState.config.active_model);

  appState.formsByTab.param.provider = provider;
  appState.formsByTab.param.model = model;
  appState.formsByTab.param.routeProfile = routeProfileForModel(provider, model);
  appState.formsByTab.param.apiForm = apiFormForModel(
    provider, model, appState.formsByTab.param.routeProfile
  );
  appState.formsByTab.param.referenceContractId = referenceContractIdForModel(
    provider,
    model,
    appState.formsByTab.param.routeProfile,
    appState.formsByTab.param.apiForm,
  );
  appState.formsByTab.param.toolValidationMode = "auto";
  appState.formsByTab.param.parameterSuite = "";
  appState.formsByTab.param.paramTestRuns = Number(defaults.param_test_runs || 1);

  const imageDefaults = appState.config.image_defaults || {};
  const imageProvider = imageProviders()[0] || null;
  const imageForm = appState.formsByTab.image;
  imageForm.provider = (imageProvider && imageProvider.name) || "";
  imageForm.model = selectedImageModel(imageForm.provider, "");
  const imageModel = imageModelById(imageForm.provider, imageForm.model);
  imageForm.routeProfile = imageRouteProfileForModel(imageForm.provider, imageForm.model);
  imageForm.apiForm = imageApiFormForModel(
    imageForm.provider, imageForm.model, imageForm.routeProfile
  );
  imageForm.transport = (imageModel && imageModel.transport) || "";
  imageForm.suite = imageDefaults.suite || "full";
  imageForm.quality = imageDefaults.quality || "low";
  imageForm.outputFormat = imageDefaults.output_format || "png";
  imageForm.include2k = !!imageDefaults.include_2k;
  imageForm.include4k = !!imageDefaults.include_4k;
  imageForm.noNegative = !!imageDefaults.no_negative;
  imageForm.noCrossControl = !!imageDefaults.no_cross_control;
  imageForm.visualForensics = imageDefaults.visual_forensics !== false;

  appState.formsByTab.load.provider = provider;
  appState.formsByTab.load.model = model;
  appState.formsByTab.load.workload = defaults.workload || "throughput";
  appState.formsByTab.load.users = Number(defaults.users || 10);
  appState.formsByTab.load.spawnRate = Number(defaults.spawn_rate || 2);
  appState.formsByTab.load.duration = defaults.duration || "2m";
  appState.formsByTab.load.targetRpm = Number(defaults.target_rpm || 0);
  appState.formsByTab.load.targetTpm = Number(defaults.target_tpm || 0);
  appState.formsByTab.load.requestMode = "unique";
  const staircase = appState.config.staircase || {};
  const warmup = appState.config.warmup || {};
  const staircaseSteps = staircase.steps || [];
  appState.formsByTab.load.staircaseSteps = staircaseSteps.map((step) => Number(step.users || step));
  appState.formsByTab.load.staircaseStepDuration = staircase.step_duration || "5m";
  appState.formsByTab.load.staircaseSpawnRate = Number(staircase.spawn_rate || 5);
  appState.formsByTab.load.staircaseWarmupEnabled = warmup.enabled !== false;
  appState.formsByTab.load.staircaseWarmupUsers = Number(warmup.users || 10);
  appState.formsByTab.load.staircaseWarmupDuration = warmup.duration || "1m";
  appState.formsByTab.load.staircaseAutoExtend = !!(staircase.auto_extend && staircase.auto_extend.enabled);
  appState.formsByTab.load.staircaseIncrementUsers = Number((staircase.auto_extend && staircase.auto_extend.increment_users) || 30);
  appState.formsByTab.load.staircaseMaxUsers = Number((staircase.auto_extend && staircase.auto_extend.max_users) || 200);
  const soak = appState.config.soak || {};
  appState.formsByTab.load.soakUsers = Number(soak.users || 80);
  appState.formsByTab.load.soakSpawnRate = Number(soak.spawn_rate || 5);
  appState.formsByTab.load.soakDuration = soak.duration || "1h";

  appState.formsByTab.cache.provider = provider;
  appState.formsByTab.cache.model = cacheSelectedModelForProvider(provider, model);
  const cache = appState.config.cache_test || {};
  const diagnosticDefaults = cache.diagnostic_defaults || {};
  const kilocodeDiagnostic = diagnosticDefaults.kilocode_agent_session || {};
  const controls = cache.controls || {};
  const diagnosticControls = kilocodeDiagnostic.controls || {};
  const legacyDiagnostic = diagnosticDefaults.growing_conversation || {};
  const cacheForm = appState.formsByTab.cache;
  cacheForm.routeProfile = cacheRouteProfileForModel(provider, cacheForm.model);
  cacheForm.apiForm = cacheApiFormForModel(
    provider, cacheForm.model, cacheForm.routeProfile
  );
  cacheForm.scenario = "progressive_customer_session";
  cacheForm.diagnosticScenario = cache.scenario && cache.scenario !== "progressive_customer_session"
    ? cache.scenario
    : "";
  cacheForm.sessions = Number(cache.sessions || 10);
  cacheForm.roundsPerSession = Number(cache.rounds_per_session || 4);
  cacheForm.contentProfile = cache.content_profile === "custom" ? "realistic" : (cache.content_profile || "realistic");
  cacheForm.customContent = cache.content_profile === "custom";
  cacheForm.customUserChars = rangePair(
    (cache.content_ranges || {}).user_chars,
    200,
    2000,
  );
  cacheForm.customToolResultChars = rangePair(
    (cache.content_ranges || {}).tool_result_chars,
    500,
    5000,
  );
  const toolStage = cache.tool_stage || {};
  cacheForm.toolStage = toolStage.enabled === false ? "off" : String(toolStage.round || 3);
  cacheForm.controlMode = controls.mode || "auto";
  cacheForm.positivePairs = Number(
    controls.positive_long_prefix_pairs ?? controls.auto_positive_long_prefix_pairs ?? 3
  );
  cacheForm.negativeRequests = Number(
    controls.negative_unique_prefix_requests ?? controls.auto_negative_unique_prefix_requests ?? 3
  );
  cacheForm.waitAfterSeed = Number(cache.wait_after_seed_sec ?? 5);
  cacheForm.maxTokens = Number(cache.max_tokens || 128);
  cacheForm.seed = Number(cache.seed ?? 20260715);
  cacheForm.kilocodeSteps = Number(kilocodeDiagnostic.steps ?? 20);
  cacheForm.kilocodeTrajectoryMode = kilocodeDiagnostic.trajectory_mode || "scripted";
  cacheForm.diagnosticPositivePairs = Number(diagnosticControls.positive_long_prefix_pairs ?? 3);
  cacheForm.diagnosticNegativeRequests = Number(diagnosticControls.negative_unique_prefix_requests ?? 3);
  cacheForm.measuredRequests = Number(legacyDiagnostic.measured_requests ?? 50);
  cacheForm.warmupRequests = Number(legacyDiagnostic.warmup_requests ?? 2);
  cacheForm.waitAfterWarmup = Number(legacyDiagnostic.wait_after_warmup_sec ?? 5);
  appState.formsByTab.cache.maxRunSeconds = Number(cache.max_run_seconds || 1800);
  appState.formsByTab.cache.failureLimit = Number(cache.consecutive_failure_limit || 3);
  appState.timeoutSec = Number(defaults.timeout_sec || 300);
  const timeoutInput = $("timeoutSec");
  if (timeoutInput) timeoutInput.value = String(appState.timeoutSec);
}

function renderControls() {
  renderCapabilityCoverage();
  renderProviderSelect("param");
  renderProviderSelect("load");
  renderProviderSelect("cache");
  renderImageControls();
  renderParamRouteProfiles();
  renderParamApiForms();
  renderCacheRouteProfiles();
  renderCacheApiForms();
  renderReferenceContracts();
  renderToolValidationMode();
  renderParamRunHint();
  renderBusyState();
  renderAdaptiveSizingHint();
  renderCacheFormState();
}

function renderCapabilityCoverage() {
  const node = $("capabilityCoverage");
  if (!node) return;
  const summary = (appState.config && appState.config.capability_summary) || {};
  const text = summary.text || {};
  const image = summary.image || {};
  const textComplete = text.complete === true;
  const imageComplete = image.complete === true;
  node.textContent = [
    `model profiles: text ${Number(text.registered_models || 0)}/${Number(text.configured_models || 0)}`,
    `image ${Number(image.registered_models || 0)}/${Number(image.configured_models || 0)}`,
  ].join(" · ");
  node.className = `pill ${textComplete && imageComplete ? "ok" : "warn"}`;
}

function renderAdaptiveSizingHint() {
  const node = $("adaptiveSizingHint");
  if (!node) return;
  const form = appState.formsByTab.load;
  const rpm = Number($("targetRpm") ? $("targetRpm").value : form.targetRpm) || 0;
  const tpm = Number($("targetTpm") ? $("targetTpm").value : form.targetTpm) || 0;
  if (rpm > 0 && tpm > 0) {
    const target = tpm / rpm;
    const supported = String(form.workload || "").startsWith("throughput")
      && form.workload !== "throughput_streaming";
    node.textContent = supported
      ? `adaptive sizing: ${fmtNum(target)} total tokens/request`
      : "adaptive sizing: unavailable for fixed-length streaming";
    node.className = `pill ${supported ? "ok" : "bad"}`;
  } else {
    node.textContent = "adaptive sizing: set both RPM and TPM";
    node.className = "pill";
  }
}

function renderProviderSelect(tab) {
  const form = appState.formsByTab[tab];
  const providerSelect = $(`${tab}Provider`);
  providerSelect.innerHTML = providers().map((provider) => (
    `<option value="${esc(provider.name)}">${esc(provider.label || provider.name)}</option>`
  )).join("");
  providerSelect.value = form.provider;

  renderModelSelect(tab);
  renderProviderStatus(tab);
}

function renderModelSelect(tab) {
  const form = appState.formsByTab[tab];
  const modelSelect = $(`${tab}Model`);
  const models = tab === "cache" ? cacheModelsFor(form.provider) : modelsFor(form.provider);
  form.model = tab === "cache"
    ? cacheSelectedModelForProvider(form.provider, form.model)
    : selectedModelForProvider(form.provider, form.model);
  modelSelect.innerHTML = models.map((model) => `<option value="${esc(model)}">${esc(model)}</option>`).join("");
  modelSelect.value = form.model;
  renderProviderStatus(tab);
}

function renderProviderStatus(tab) {
  const form = appState.formsByTab[tab];
  const provider = providerByName(form.provider);
  const family = familyFor(form.provider, form.model);
  const keyStatus = $(`${tab}KeyStatus`);
  const familyStatus = $(`${tab}FamilyStatus`);
  const profileStatus = $(`${tab}ProfileStatus`);
  if (keyStatus) {
    keyStatus.textContent = provider && provider.has_key ? "key: configured" : "key: missing";
    keyStatus.className = `pill ${provider && provider.has_key ? "ok" : "bad"}`;
  }
  if (familyStatus) familyStatus.textContent = `family: ${family}`;
  if (profileStatus) {
    const capability = ["param", "cache"].includes(tab)
      ? apiFormCapability(
        form.provider, form.model, form.routeProfile, form.apiForm
      )
      : (modelCapability(form.provider, form.model) || {});
    const status = capability.profile_status || "unknown";
    const scope = capability.certification_scope || "raw_route_contract";
    profileStatus.textContent = `model profile: ${status}${capability.profile_id ? ` · ${capability.profile_id}` : ""} · scope: ${scope}`;
    profileStatus.className = `pill ${status === "registered" && scope !== "adapter_only" ? "ok" : "warn"}`;
  }
}

function renderParamRouteProfiles() {
  const form = appState.formsByTab.param;
  const select = $("paramRouteProfile");
  if (!select) return;
  const routes = routesForModel(form.provider, form.model);
  form.routeProfile = routeProfileForModel(
    form.provider, form.model, form.routeProfile
  );
  select.innerHTML = Object.keys(routes).map((route) => (
    `<option value="${esc(route)}">${esc(route)}</option>`
  )).join("");
  select.value = form.routeProfile;
}

function renderParamApiForms() {
  const form = appState.formsByTab.param;
  const select = $("paramApiForm");
  if (!select) return;
  const rows = apiFormsForModel(form.provider, form.model, form.routeProfile);
  form.apiForm = apiFormForModel(
    form.provider, form.model, form.routeProfile, form.apiForm
  );
  select.innerHTML = Object.keys(rows).map((apiForm) => {
    return `<option value="${esc(apiForm)}">${esc(apiForm)}</option>`;
  }).join("");
  select.value = form.apiForm;
}

function renderCacheRouteProfiles() {
  const form = appState.formsByTab.cache;
  const select = $("cacheRouteProfile");
  if (!select) return;
  const routes = cacheRoutesForModel(form.provider, form.model);
  form.routeProfile = cacheRouteProfileForModel(
    form.provider, form.model, form.routeProfile
  );
  select.innerHTML = Object.keys(routes).map((route) => (
    `<option value="${esc(route)}">${esc(route)}</option>`
  )).join("");
  select.value = form.routeProfile;
}

function renderCacheApiForms() {
  const form = appState.formsByTab.cache;
  const select = $("cacheApiForm");
  if (!select) return;
  const rows = cacheApiFormsForModel(form.provider, form.model, form.routeProfile);
  form.apiForm = cacheApiFormForModel(
    form.provider, form.model, form.routeProfile, form.apiForm
  );
  select.innerHTML = Object.keys(rows).map((apiForm) => (
    `<option value="${esc(apiForm)}">${esc(apiForm)}</option>`
  )).join("");
  select.value = form.apiForm;
}

function imageSelectionKey() {
  const form = appState.formsByTab.image;
  return `${form.provider || ""}\u0000${form.model || ""}\u0000${form.routeProfile || ""}\u0000${form.apiForm || ""}`;
}

function matchesImageSelection(job) {
  const form = appState.formsByTab.image;
  const capability = imageApiFormCapability(
    form.provider, form.model, form.routeProfile, form.apiForm
  );
  return !!job
    && job.type === "image_param_test"
    && job.provider === form.provider
    && job.model === form.model
    && (job.route_profile || "") === (form.routeProfile || "")
    && (job.api_form || "") === (form.apiForm || "")
    && (job.model_profile_id || "") === (capability.profile_id || "");
}

function imageRunCountValue() {
  const raw = Number(appState.formsByTab.image.runCount || 1);
  return Number.isFinite(raw) ? Math.max(1, Math.min(Math.trunc(raw), 1000)) : 1;
}

function imageRequestPayload() {
  const form = appState.formsByTab.image;
  const cases = String(form.cases || "").split(/[,，\n]+/).map((value) => value.trim()).filter(Boolean);
  return {
    type: "image_param_test", provider: form.provider, model: form.model,
    timeout_sec: timeoutSecValue(), run_count: imageRunCountValue(),
    image_plan: {
      route_profile: form.routeProfile, api_form: form.apiForm, suite: form.suite,
      include_2k: !!form.include2k, include_4k: !!form.include4k, quality: form.quality,
      output_format: form.outputFormat, no_negative: !!form.noNegative,
      no_cross_control: !!form.noCrossControl, visual_forensics: !!form.visualForensics,
      ...(cases.length ? {cases} : {}),
    },
  };
}

function imagePreviewCurrent() {
  return !!appState.imagePlanPreview && typeof appState.imagePlanPreview.plan_digest === "string"
    && !appState.imagePlanPreviewLoading && appState.imagePlanPreviewKey === JSON.stringify(imageRequestPayload());
}

function renderImageCaseHint() {
  const count = appState.imagePlanPreviewCount;
  $("imageCaseHint").textContent = appState.imagePlanPreviewLoading
    ? "counting cases…"
    : count === null
      ? "case count unavailable"
      : `${count} case${count === 1 ? "" : "s"}`;
  if (imagePreviewCurrent()) {
    const preview = appState.imagePlanPreview;
    $("imageCaseHint").textContent = `${preview.estimated_case_count} 个用例 × ${preview.plan.run_count} 轮 · 请求上限 ${preview.request_cap} · 清理上限 ${preview.cleanup_request_cap}`;
  }
  $("imageCaseHint").title = appState.imagePlanPreviewError;
}

async function refreshImagePlanPreview() {
  const payload = imageRequestPayload();
  const key = JSON.stringify(payload);
  if (imagePreviewCurrent() && !appState.imagePlanPreviewError) return appState.imagePlanPreview;
  appState.imagePlanPreviewKey = key;
  appState.imagePlanPreviewCount = null;
  appState.imagePlanPreview = null;
  appState.imagePlanPreviewError = "";
  appState.imagePlanPreviewLoading = !!payload.provider && !!payload.model;
  renderImageCaseHint();
  renderBusyState();
  if (!appState.imagePlanPreviewLoading) return;
  try {
    const response = await fetch("/api/image-plan/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const preview = await response.json();
    if (key !== appState.imagePlanPreviewKey) return;
    if (!response.ok) throw new Error(preview.error || "failed to count image cases");
    if (!Number.isInteger(preview.estimated_case_count) || preview.estimated_case_count < 1) {
      throw new Error("invalid image case count");
    }
    appState.imagePlanPreviewCount = preview.estimated_case_count;
    appState.imagePlanPreview = preview;
    return preview;
  } catch (error) {
    if (key !== appState.imagePlanPreviewKey) return;
    appState.imagePlanPreviewError = error.message || String(error);
  } finally {
    if (key === appState.imagePlanPreviewKey) {
      appState.imagePlanPreviewLoading = false;
      renderImageCaseHint();
      renderBusyState();
    }
  }
}

function applyImageResolutionDefaults(form) {
  const capability = imageApiFormCapability(form.provider, form.model, form.routeProfile, form.apiForm) || {};
  const suite = capability.suite;
  if (!suite || form.suite === "smoke") return;
  const explicit = (form.resolutionPreferences || {})[suite];
  if (explicit) {
    form.include2k = explicit.include2k;
    form.include4k = explicit.include4k;
  } else if (form.suite === "full") {
    form.include2k = suite === "grok_imagine";
    form.include4k = suite !== "grok_imagine";
  }
}

function renderImageControls() {
  const form = appState.formsByTab.image;
  const configured = imageProviders();
  const empty = configured.length === 0;
  $("imageEmptyState").hidden = !empty;
  $("imageProvider").innerHTML = configured.map((provider) => (
    `<option value="${esc(provider.name)}">${esc(provider.label || provider.name)}</option>`
  )).join("");
  if (!configured.some((provider) => provider.name === form.provider)) {
    form.provider = configured[0] ? configured[0].name : "";
  }
  $("imageProvider").value = form.provider;

  const models = imageModelsFor(form.provider);
  form.model = selectedImageModel(form.provider, form.model);
  $("imageModel").innerHTML = models.map((model) => (
    `<option value="${esc(model.id)}">${esc(model.id)}</option>`
  )).join("");
  $("imageModel").value = form.model;
  const model = imageModelById(form.provider, form.model);
  const routes = (model && model.routes) || {};
  form.routeProfile = imageRouteProfileForModel(
    form.provider, form.model, form.routeProfile
  );
  $("imageRouteProfile").innerHTML = Object.keys(routes).map((route) => (
    `<option value="${esc(route)}">${esc(route)}</option>`
  )).join("");
  $("imageRouteProfile").value = form.routeProfile;
  const selectedRoute = routes[form.routeProfile] || {};
  const formRows = selectedRoute.api_forms || {};
  const allowed = Object.keys(formRows);
  form.apiForm = imageApiFormForModel(
    form.provider, form.model, form.routeProfile, form.apiForm
  );
  form.transport = (formRows[form.apiForm] || {}).transport || "";
  $("imageApiForm").innerHTML = allowed.map((apiForm) => (
    `<option value="${esc(apiForm)}">${esc(apiForm)}</option>`
  )).join("");
  $("imageApiForm").value = form.apiForm;
  $("imageApiForm").disabled = allowed.length <= 1;

  applyImageResolutionDefaults(form);
  const grokImagine = (imageApiFormCapability(form.provider, form.model, form.routeProfile, form.apiForm) || {}).suite === "grok_imagine";
  const fixedImage25Matrix = !!model && /^gpt-image-2\.5-(sunburst|flare)(-2026-09-08)?$/.test(model.id);
  if (fixedImage25Matrix) {
    form.quality = "low";
    form.outputFormat = "png";
  }
  if (form.transport === "gemini-interactions") form.outputFormat = "jpeg";
  if (grokImagine) form.include4k = false;
  else form.include2k = false;
  if (form.suite === "smoke") {
    form.include2k = false;
    form.include4k = false;
  }
  $("imageSuite").value = form.suite;
  $("imageRunCount").value = imageRunCountValue();
  $("imageCases").value = form.cases || "";
  $("imageQuality").value = form.quality;
  $("imageOutputFormat").value = form.outputFormat;
  $("imageInclude2k").checked = form.include2k;
  $("imageInclude2k").disabled = form.suite === "smoke";
  $("imageInclude4k").checked = form.include4k;
  $("imageInclude4k").disabled = form.suite === "smoke";
  $("imageInclude2kWrap").hidden = !grokImagine;
  $("imageInclude4kWrap").hidden = grokImagine;
  $("imageNoNegative").checked = form.noNegative;
  $("imageNoCrossControl").checked = form.noCrossControl;
  $("imageNoCrossControl").disabled = false;
  $("imageVisualForensics").checked = form.visualForensics;

  const familyManagedQuality = ["chat-completions", "gemini-interactions"].includes(form.transport)
    || grokImagine || fixedImage25Matrix;
  const familyManagedFormat = ["chat-completions", "gemini-interactions"].includes(form.transport)
    || grokImagine || fixedImage25Matrix;
  $("imageQualityWrap").hidden = familyManagedQuality;
  $("imageOutputFormatWrap").hidden = familyManagedFormat;
  $("imageQuality").disabled = familyManagedQuality;
  $("imageOutputFormat").disabled = familyManagedFormat;
  $("imageNoNegativeWrap").hidden = !model;
  $("imageNoCrossControlWrap").hidden = !model || model.family !== "banana";
  const provider = imageProviderByName(form.provider);
  $("imageKeyStatus").textContent = provider && provider.has_key ? "key: configured" : "key: missing";
  $("imageKeyStatus").className = `pill ${provider && provider.has_key ? "ok" : "bad"}`;
  $("imageFamilyStatus").textContent = `family: ${(model && model.family) || "unknown"}`;
  const capability = imageApiFormCapability(
    form.provider, form.model, form.routeProfile, form.apiForm
  );
  const profileStatus = capability.profile_status || "unknown";
  const certificationScope = capability.certification_scope || "raw_route_contract";
  $("imageProfileStatus").textContent = `model profile: ${profileStatus}${capability.profile_id ? ` · ${capability.profile_id}` : ""} · scope: ${certificationScope}`;
  $("imageProfileStatus").className = `pill ${profileStatus === "registered" && certificationScope !== "adapter_only" ? "ok" : "warn"}`;
  refreshImagePlanPreview();
  const hasBillableNegative = !!model && !form.noNegative && form.suite !== "smoke";
  const costly = form.include2k || form.include4k || hasBillableNegative;
  $("imageCostHint").textContent = costly
    ? "billing: 2K/4K/negative cases require review"
    : "billing: standard image requests";
  $("imageCostHint").className = `pill ${costly ? "warn" : ""}`;
  renderBusyState();
}

async function loadLatestImageResult() {
  if (!appState.config) return;
  const key = imageSelectionKey();
  const requestId = ++appState.imageHistoryRequestId;
  appState.imageHistoryResult = null;
  if (!appState.formsByTab.image.provider || !appState.formsByTab.image.model) {
    appState.imageHistoryLoading = false;
    renderImageResults(appState.currentJob);
    return;
  }
  appState.imageHistoryLoading = true;
  renderImageResults(appState.currentJob);
  const query = new URLSearchParams({
    provider: appState.formsByTab.image.provider,
    model: appState.formsByTab.image.model,
    route_profile: appState.formsByTab.image.routeProfile,
    api_form: appState.formsByTab.image.apiForm,
  });
  try {
    const response = await fetch(`/api/image-results/latest?${query.toString()}`, { cache: "no-store" });
    const payload = await response.json();
    if (requestId !== appState.imageHistoryRequestId || key !== imageSelectionKey()) return;
    if (!response.ok) throw new Error(payload.error || "failed to load image history");
    appState.imageHistoryResult = payload.result || null;
  } catch (error) {
    if (requestId === appState.imageHistoryRequestId) showError(`image history failed: ${error.message || error}`);
  } finally {
    if (requestId === appState.imageHistoryRequestId) {
      appState.imageHistoryLoading = false;
      renderImageResults(appState.currentJob);
    }
  }
}

function isOptionalParamWorkflow(workflow) {
  return workflow.optional === true || workflow.factory_id === "media_input";
}

function paramWorkflowChoices() {
  const form = appState.formsByTab.param;
  const leaf = apiFormCapability(form.provider, form.model, form.routeProfile, form.apiForm) || {};
  return leaf.workflow_available === true ? (leaf.test_workflows || []) : [];
}

function selectedParamWorkflow() {
  const form = appState.formsByTab.param;
  if (form.workflowBindingId === "__ordinary__") return null;
  const choices = paramWorkflowChoices();
  return choices.find((item) => item.workflow_binding_id === form.workflowBindingId)
    || choices.find((item) => !isOptionalParamWorkflow(item)) || null;
}

function workflowCaseGroupLabel(item) {
  const group = item.group || item.phase || "";
  return ({image: "图片输入", audio: "音频输入", video: "视频输入", mixed: "图音混合"})[group] || group;
}

function renderParamWorkflowSelector(workflow) {
  const select = $("paramWorkflow");
  if (!select) return;
  const form = appState.formsByTab.param;
  const leaf = apiFormCapability(form.provider, form.model, form.routeProfile, form.apiForm) || {};
  const hasDefaultWorkflow = paramWorkflowChoices().some((item) => !isOptionalParamWorkflow(item));
  const ordinaryEnabled = !hasDefaultWorkflow && leaf.profile_status === "registered" && leaf.parameter_test_enabled !== false;
  select.innerHTML = `<option value="__ordinary__"${ordinaryEnabled ? "" : " disabled"}>普通参数测试${ordinaryEnabled ? "" : "（不可用）"}</option>`
    + paramWorkflowChoices().map((item) => {
      const label = item.label || (item.factory_id === "media_input" ? "图片/视频/音频输入" : item.workflow_id || "功能套件");
      return `<option value="${esc(item.workflow_binding_id)}">${esc(label)} · ${Number(item.case_count || (item.cases || []).length)} 个用例</option>`;
    }).join("");
  select.value = workflow ? workflow.workflow_binding_id : "__ordinary__";
}

async function changeParamWorkflow(bindingId) {
  const form = appState.formsByTab.param;
  form.workflowBindingId = bindingId || "__ordinary__";
  form.workflowCases = "";
  form.referenceContractId = "";
  if ("referenceManual" in form) form.referenceManual = false;
  if ("contractManual" in form) form.contractManual = false;
  if ("parameterSuite" in form) form.parameterSuite = "";
  appState.workflowPreviewRequestId += 1;
  appState.workflowPreview = null;
  appState.workflowPreviewKey = "";
  appState.workflowPreviewError = "";
  appState.workflowPreviewLoading = false;
  clearTimeout(appState.workflowPreviewTimer);
  appState.paramHistoryResult = null;
  renderReferenceContracts();
  renderProviderStatus("param");
  await loadParamSpecs();
  renderBusyState();
}

function syncParamWorkflow() {
  const form = appState.formsByTab.param;
  const workflow = selectedParamWorkflow();
  const binding = workflow ? workflow.workflow_binding_id : (form.workflowBindingId === "__ordinary__" ? "__ordinary__" : "");
  if (form.workflowBindingId !== binding) {
    form.workflowCases = "";
    appState.workflowPreview = null;
    appState.workflowPreviewKey = "";
    appState.workflowPreviewError = "";
  }
  form.workflowBindingId = binding;
  renderParamWorkflowSelector(workflow);
  if (workflow) {
    form.referenceContractId = workflow.reference_contract_id;
    form.toolValidationMode = "auto";
    if ("referenceManual" in form) form.referenceManual = false;
    if ("contractManual" in form) form.contractManual = false;
    if ("parameterSuite" in form) form.parameterSuite = "";
  }
  const wrap = $("paramWorkflowCasesWrap");
  if (wrap) wrap.hidden = !workflowRequestPayload() || !!form.parameterSuite || (!workflow && form.apiForm === "deepseek_beta_chat_prefix");
  const evidence = $("paramWorkflowEvidence");
  if (evidence) evidence.hidden = !workflow;
  const input = $("paramWorkflowCases");
  if (input) input.value = form.workflowCases || "";
  const choices = $("paramWorkflowCaseOptions");
  if (choices) choices.innerHTML = workflow ? workflow.cases.map((item) => (
    `<option value="${esc(item.id)}">${esc(workflowCaseGroupLabel(item))} · ${esc(item.name || item.id)}</option>`
  )).join("") : "";
  const contract = $("referenceContractId");
  if (contract) contract.disabled = !!workflow;
  const reset = $("resetContract");
  if (reset) reset.disabled = !!workflow;
  const mode = $("toolValidationMode");
  if (mode) mode.disabled = !!workflow;
  const suite = $("parameterSuite");
  if (suite && suite.parentElement) suite.parentElement.hidden = !!workflow;
  if (suite && workflow) { suite.value = ""; suite.disabled = true; }
  renderWorkflowPreview();
  return workflow;
}

function workflowRequestPayload() {
  const form = appState.formsByTab.param;
  const workflow = selectedParamWorkflow();
  if (!workflow && (!form.provider || !form.model)) return null;
  const cases = String(form.workflowCases || "").split(/[,，\n]+/).map((value) => value.trim()).filter(Boolean);
  if (!workflow) return {
    type: "param_test", provider: form.provider, model: form.model,
    route_profile: form.routeProfile, api_form: form.apiForm,
    reference_contract_id: form.referenceContractId,
    ...(form.parameterSuite ? {parameter_suite: form.parameterSuite} : {}),
    tool_validation_mode: form.toolValidationMode || "auto",
    param_test_runs: paramTestRunsValue(), timeout_sec: timeoutSecValue(),
    ...(cases.length ? {cases} : {suite: "full"}),
  };
  return {
    type: "param_test", provider: form.provider, model: form.model,
    route_profile: form.routeProfile, api_form: form.apiForm,
    workflow_binding_id: workflow.workflow_binding_id,
    reference_contract_id: workflow.reference_contract_id,
    source_id: workflow.source_id, profile_id: workflow.profile_id, interface_id: workflow.interface_id,
    tool_validation_mode: "auto", param_test_runs: paramTestRunsValue(), timeout_sec: timeoutSecValue(),
    ...(cases.length ? {cases} : {suite: "full"}),
  };
}

function workflowSelectionKey() {
  return JSON.stringify(workflowRequestPayload());
}

function workflowPreviewCurrent() {
  return !!appState.workflowPreview && !appState.workflowPreviewLoading
    && appState.workflowPreviewKey === workflowSelectionKey();
}

function renderWorkflowPreview() {
  const node = $("paramWorkflowPreview");
  if (!node) return;
  const workflow = selectedParamWorkflow();
  const available = !!workflowRequestPayload();
  node.hidden = !available;
  node.className = available ? "notice active" : "notice";
  if (!available) { node.textContent = ""; return; }
  if (appState.workflowPreviewLoading) { node.textContent = "正在核对用例、请求上限与清理计划…"; return; }
  if (appState.workflowPreviewError) { node.textContent = appState.workflowPreviewError; return; }
  if (!workflowPreviewCurrent()) { node.textContent = "等待测试计划预览。"; return; }
  const preview = appState.workflowPreview;
  const names = new Map(preview.plan.definition.cases.map((item) => [item.id, item.name || item.id]));
  const conditions = (preview.preconditions || []).map((item) => (
    `${names.get(item.case_id) || item.case_id}: ${(item.requirements || []).map((value) => typeof value === "string" ? value : JSON.stringify(value)).join("; ")}`
  ));
  const selection = String(appState.formsByTab.param.workflowCases || "").trim() ? "选定用例及必要前置步骤" : "完整套件";
  node.innerHTML = `<b>${esc(selection)}</b> · ${preview.selected_cases.length} 个用例 · ${preview.plan.run_count} 轮 · `
    + `请求上限 ${preview.request_cap} · 清理上限 ${preview.cleanup_request_cap}`
    + (conditions.length ? `<details><summary>${conditions.length} 项执行前提</summary><ul>${conditions.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></details>` : "");
}

async function refreshWorkflowPreview(force = false) {
  const payload = workflowRequestPayload();
  if (!payload) return null;
  const key = JSON.stringify(payload);
  if (!force && workflowPreviewCurrent()) return appState.workflowPreview;
  const requestId = ++appState.workflowPreviewRequestId;
  appState.workflowPreviewKey = key;
  appState.workflowPreview = null;
  appState.workflowPreviewError = "";
  appState.workflowPreviewLoading = true;
  renderWorkflowPreview();
  renderBusyState();
  try {
    const response = await fetch("/api/test-plan/preview", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload),
    });
    const preview = await response.json();
    if (requestId !== appState.workflowPreviewRequestId || key !== workflowSelectionKey()) return null;
    if (!response.ok) throw new Error(preview.error || "无法生成测试计划。");
    appState.workflowPreview = preview;
    if (!selectedParamWorkflow() && $("paramWorkflowCaseOptions")) {
      $("paramWorkflowCaseOptions").innerHTML = preview.plan.definition.cases
        .filter((item) => item.id !== "identity_probe")
        .map((item) => `<option value="${esc(item.id)}">${esc(item.name || item.id)}</option>`).join("");
    }
    return preview;
  } catch (error) {
    if (requestId === appState.workflowPreviewRequestId && key === workflowSelectionKey()) {
      appState.workflowPreviewError = error.message || String(error);
    }
    return null;
  } finally {
    if (requestId === appState.workflowPreviewRequestId) {
      appState.workflowPreviewLoading = false;
      renderWorkflowPreview();
      renderParamRunHint();
      renderBusyState();
    }
  }
}

function queueWorkflowPreview() {
  if (!workflowRequestPayload()) return;
  appState.workflowPreview = null;
  appState.workflowPreviewError = "";
  clearTimeout(appState.workflowPreviewTimer);
  renderWorkflowPreview();
  renderBusyState();
  appState.workflowPreviewTimer = setTimeout(() => selectedParamWorkflow() ? loadWorkflowParamSpecs() : refreshWorkflowPreview(), 200);
}

function renderParamSpecMode(workflow) {
  const title = $("paramSpecsTitle");
  if (title) title.textContent = workflow ? "套件用例" : "Interface Contract Parameter Specs";
  const head = $("paramSpecsHead");
  if (head) head.innerHTML = workflow
    ? "<tr><th>用例</th><th>官方来源</th><th>分组</th><th>用例 ID</th></tr>"
    : "<tr><th>Parameter</th><th>Official / Contract</th><th>Model profile expectation</th><th>Coverage</th></tr>";
}

async function loadWorkflowParamSpecs() {
  const workflow = syncParamWorkflow();
  if (!workflow) return;
  renderParamSpecMode(true);
  const requestId = ++appState.paramHistoryRequestId;
  appState.paramSpec = {workflow: true, comparison: []};
  appState.paramHistoryResult = null;
  appState.paramHistoryLoading = true;
  $("paramProfileStatus").textContent = `功能套件可用 · 来源 ${workflow.source_id}`;
  $("paramProfileStatus").className = "pill ok";
  const preview = await refreshWorkflowPreview();
  if (requestId !== appState.paramHistoryRequestId) return;
  const selected = new Set(preview ? preview.selected_cases : []);
  $("paramSpecs").innerHTML = (workflow.cases || []).filter((item) => !preview || selected.has(item.id)).map((item) => (
    `<tr><td>${esc(item.name || item.id)}</td><td>${esc(workflow.source_id)}</td><td>${esc(workflowCaseGroupLabel(item))}</td><td>${esc(item.id)}</td></tr>`
  )).join("");
  if (preview) {
    try {
      const response = await fetch("/api/jobs", {cache: "no-store"});
      const history = await response.json();
      if (!response.ok) throw new Error(history.error || "无法读取历史结果。");
      const match = (history.jobs || []).find((job) => matchesParamSelection(job) && !isBusy(job));
      if (match) {
        const detail = await fetch(`/api/jobs/${encodeURIComponent(match.id)}`, {cache: "no-store"});
        const result = await detail.json();
        if (detail.ok && requestId === appState.paramHistoryRequestId) appState.paramHistoryResult = result;
      }
    } catch (error) {
      if (requestId === appState.paramHistoryRequestId) showError(error.message || String(error));
    }
  }
  if (requestId === appState.paramHistoryRequestId) {
    appState.paramHistoryLoading = false;
    renderParamResults(appState.currentJob);
  }
}

function workflowStateLabel(state) {
  return ({passed: "通过", failed: "失败", blocked: "依赖阻塞", cancelled: "已取消", skipped: "跳过",
    inconclusive: "未确认", unknown: "未确认", running: "执行中", stopping: "停止并清理中", stopped: "已停止",
    pending: "等待", incomplete: "未完成", invalid: "证据无效"})[state] || state || "等待";
}

function renderWorkflowParamResult(job) {
  const progress = (job && job.progress) || {};
  const plan = (job && job.job_spec && job.job_spec.execution_plan)
    || (appState.workflowPreview && appState.workflowPreview.plan);
  const runs = progress.runs || [];
  const verifiedPass = progress.pass === true && (!job || !job.result_validation || job.result_validation.pass === true);
  const outcome = verifiedPass ? "passed" : progress.status === "passed" ? "incomplete" : progress.status || "pending";
  renderMetrics("param", [
    ["用例校验与清理", workflowStateLabel(outcome)],
    ["用例", `${progress.completed_cases || 0}/${progress.total_cases || (plan ? plan.selected_cases.length * plan.run_count : 0)}`],
    ["步骤", `${progress.completed_steps || 0}/${progress.total_steps || (plan ? plan.ordered_steps.length * plan.run_count : 0)}`],
    ["请求尝试", progress.attempt_count || 0], ["依赖阻塞", progress.blocked_steps || 0],
    ["清理", workflowStateLabel(progress.cleanup_status || "pending")],
    ["资源已清理", `${progress.deleted_resource_count || 0}/${progress.resource_count || 0}`],
  ]);
  $("paramResultsHead").innerHTML = "<tr><th>测试用例</th><th>每轮结果与步骤</th></tr>";
  const definitions = new Map(plan ? plan.definition.cases.map((item) => [item.id, item]) : []);
  $("paramResults").innerHTML = (plan ? plan.selected_cases : []).map((caseId) => {
    const definition = definitions.get(caseId) || {};
    const cells = Array.from({length: plan.run_count}, (_, index) => {
      const run = runs.find((item) => item.run_index === index + 1);
      const result = run && (run.cases || []).find((item) => item.id === caseId);
      const status = result ? result.status : "pending";
      const style = status === "passed" ? "pass" : status === "failed" ? "fail" : status === "blocked" ? "incompatible" : "pending";
      const details = run ? (run.steps || []).filter((item) => item.case_id === caseId).map((item) => (
        `${item.id}: ${workflowStateLabel(item.status)}${(item.blocked_dependencies || []).length ? ` · 前置 ${item.blocked_dependencies.join(", ")}` : ""}`
      )).join("\n") : "";
      return `<span class="run-chip status-${style}" title="${esc(details)}">R${index + 1}: ${esc(workflowStateLabel(status))}</span>`;
    }).join("");
    const policy = (plan.case_policies || {})[caseId] || definition.success_policy || "all";
    const aggregate = (progress.case_outcomes || {})[caseId];
    const policyLabel = policy === "any" ? "任一轮（有条件）" : "每轮";
    const aggregateLabel = aggregate ? ` · 汇总：${workflowStateLabel(aggregate.status)}` : "";
    return `<tr><td><b>${esc(definition.name || caseId)}</b><div class="muted">${esc(definition.phase || "")}</div>`
      + `<div class="muted" title="保留每轮原始结果。仅合并用例允许的非致命失败；缺失轮次、执行异常或清理失败仍不通过。">规则：${esc(policyLabel)}${esc(aggregateLabel)}</div></td><td>${cells}</td></tr>`;
  }).join("") || '<tr><td colspan="2" class="muted">等待测试计划。</td></tr>';
  renderFiles("param", job);
  renderTokenAudit(null);
  renderModelIdentity(null);
  $("paramFailedCaseLog").textContent = (progress.evidence_errors || []).join("\n")
    || runs.flatMap((run) => (run.steps || []).filter((step) => ["failed", "blocked", "cancelled", "inconclusive"].includes(step.status)).map((step) => (
      `R${run.run_index} ${step.id}: ${workflowStateLabel(step.status)}`
    ))).join("\n") || "暂无失败或阻塞用例。";
  $("paramLogTail").textContent = job ? job.log_tail || "" : "";
  const evidence = $("paramWorkflowEvidence");
  if (evidence) evidence.hidden = false;
  const attempts = $("paramWorkflowAttempts");
  if (attempts) attempts.innerHTML = runs.flatMap((run) => (run.attempts || []).map((attempt) => {
    const step = (run.steps || []).find((item) => item.id === attempt.step_id);
    const definition = definitions.get(step && step.case_id) || {};
    return `<tr><td>R${run.run_index}</td><td>${esc(definition.name || (step && step.case_id) || attempt.step_id)}</td>`
      + `<td>${esc(attempt.phase === "cleanup" ? "清理" : "用例")}</td><td>${esc(({dispatching: "等待响应", received: "已收到响应", exception: "请求异常"})[attempt.status] || attempt.status)}</td>`
      + `<td>${esc(attempt.http_status || "—")}</td></tr>`;
  })).join("") || '<tr><td colspan="5" class="muted">尚无请求记录。</td></tr>';
  const cleanup = $("paramWorkflowCleanup");
  if (cleanup) cleanup.textContent = runs.map((run) => (
    `R${run.run_index}: 清理${workflowStateLabel(run.cleanup_status)} · ${run.deleted_resource_count}/${run.resource_count} 个资源已删除 · ${run.unknown_creation_count} 个创建结果未确认`
  )).join("\n") || "尚无清理记录。";
}

function renderReferenceContracts() {
  const form = appState.formsByTab.param;
  const workflow = syncParamWorkflow();
  if (workflow) {
    const select = $("referenceContractId");
    select.innerHTML = `<option value="${esc(workflow.reference_contract_id)}">${esc(workflow.reference_contract_id)}</option>`;
    select.value = workflow.reference_contract_id;
    renderContractMode();
    renderToolValidationMode();
    return;
  }
  const select = $("referenceContractId");
  const allContracts = appState.config.reference_contracts
    || appState.config.reference_sources
    || [];
  const capability = apiFormCapability(
    form.provider, form.model, form.routeProfile, form.apiForm
  );
  const allowed = new Set(
    capability.reference_contract_ids || capability.reference_sources || []
  );
  const contracts = allContracts.filter((contract) => (
    allowed.has(contract.contract_id || contract.id)
  ));
  if (!form.referenceContractId) {
    form.referenceContractId = referenceContractIdForModel(
      form.provider, form.model, form.routeProfile, form.apiForm
    );
  }
  if (!contracts.some((contract) => (
    (contract.contract_id || contract.id) === form.referenceContractId
  ))) {
    form.referenceContractId = referenceContractIdForModel(
      form.provider, form.model, form.routeProfile, form.apiForm
    ) || (contracts[0] && (contracts[0].contract_id || contracts[0].id)) || "";
    form.contractManual = false;
  }
  select.innerHTML = contracts.map((contract) => (
    `<option value="${esc(contract.contract_id || contract.id)}">${esc(contract.label || contract.contract_id || contract.id)}</option>`
  )).join("");
  select.value = form.referenceContractId;
  renderContractMode();
  renderToolValidationMode();
}

function renderContractMode() {
  const form = appState.formsByTab.param;
  const contract = contractById(form.referenceContractId);
  const contractId = contract
    ? (contract.contract_id || contract.id)
    : form.referenceContractId;
  $("contractMode").textContent = `contract: ${form.contractManual ? "manual" : "default"} ${contractId || ""}`;
  $("contractMode").className = `pill ${form.contractManual ? "warn" : "ok"}`;
}

function renderToolValidationMode() {
  const form = appState.formsByTab.param;
  const select = $("toolValidationMode");
  if (!select) return;
  select.value = form.toolValidationMode || "auto";
  const contractId = String(form.referenceContractId || "");
  let automatic = "OpenAI-compatible tool_calls";
  if (contractId === "gemini_native_generate_content" || contractId === "gemini_vertex_generate_content") {
    automatic = "Gemini Native functionCall";
  }
  if (contractId === "claude_native_messages" || contractId === "claude_fable_native_messages") {
    automatic = "Claude Native tool_use";
  }
  const workflow = selectedParamWorkflow();
  if (workflow && workflow.factory_id === "media_input") automatic = "图片/语音理解（无工具调用）";
  else if (workflow && form.apiForm === "anthropic_messages") automatic = "Anthropic Messages tool_use";
  const effective = form.toolValidationMode === "auto"
    ? `auto → ${automatic}`
    : form.toolValidationMode;
  $("toolValidationHint").textContent = `tool validation: ${effective}`;
  $("toolValidationHint").className = `pill ${form.toolValidationMode === "auto" ? "ok" : "warn"}`;
}

async function loadParamSpecs() {
  if (!appState.config) return;
  if (selectedParamWorkflow()) return loadWorkflowParamSpecs();
  renderParamSpecMode(false);
  const form = appState.formsByTab.param;
  const contractId = form.referenceContractId || referenceContractIdForModel(
    form.provider, form.model, form.routeProfile, form.apiForm
  );
  const selectionKey = paramSelectionKey(
    form.provider,
    form.model,
    form.routeProfile,
    form.apiForm,
    contractId,
    form.toolValidationMode,
    form.parameterSuite,
  );
  const requestId = ++appState.paramHistoryRequestId;
  appState.paramSpec = null;
  appState.paramHistoryResult = null;
  appState.paramHistoryLoading = true;
  renderParameterSuites();
  renderBusyState();
  renderParamResults(appState.currentJob);
  const historyQuery = new URLSearchParams({
    provider: form.provider,
    model: form.model,
    route_profile: form.routeProfile,
    api_form: form.apiForm,
    contract_id: contractId,
    tool_validation_mode: form.toolValidationMode,
  });
  if (form.parameterSuite) historyQuery.set("parameter_suite", form.parameterSuite);
  const specQuery = new URLSearchParams({
    provider: form.provider, model: form.model, route_profile: form.routeProfile,
    api_form: form.apiForm, contract_id: contractId,
  });
  if (form.parameterSuite) specQuery.set("parameter_suite", form.parameterSuite);
  const [payload, historyPayload] = await Promise.all([
    fetch(`/api/param-specs?${specQuery.toString()}`, { cache: "no-store" }).then((resp) => resp.json()),
    fetch(`/api/param-results/latest?${historyQuery.toString()}`, { cache: "no-store" }).then((resp) => resp.json()),
  ]);
  const currentForm = appState.formsByTab.param;
  if (
    requestId !== appState.paramHistoryRequestId
    || selectionKey !== paramSelectionKey(
      currentForm.provider,
      currentForm.model,
      currentForm.routeProfile,
      currentForm.apiForm,
      currentForm.referenceContractId,
      currentForm.toolValidationMode,
      currentForm.parameterSuite,
    )
  ) return;
  appState.paramSpec = payload;
  appState.paramHistoryResult = historyPayload.result || null;
  appState.paramHistoryLoading = false;
  renderParameterSuites();
  renderBusyState();
  if (payload.error) showError(payload.error);
  const capability = payload.model_capability_profile || {};
  const profileStatus = capability.profile_status || "unknown";
  const certificationScope = capability.certification_scope || "raw_route_contract";
  $("paramProfileStatus").textContent = `model profile: ${profileStatus}${capability.profile_id ? ` · ${capability.profile_id}` : ""} · scope: ${certificationScope}`;
  $("paramProfileStatus").className = `pill ${profileStatus === "registered" && certificationScope !== "adapter_only" ? "ok" : "warn"}`;
  $("paramSpecs").innerHTML = (payload.comparison || []).map((row) => {
    const rowClass = isSafetyParameter(row.parameter) ? "safety-param-row" : "";
    return `<tr class="${rowClass}"><td>${esc(row.parameter)}</td><td>${esc(row.official)}</td><td>${esc(row.local)}</td><td>${esc(row.coverage)}</td></tr>`;
  }).join("") || '<tr><td colspan="4" class="muted">No reference params configured.</td></tr>';
  renderParamRunHint();
  const currentJob = appState.currentJob && appState.currentJob.type === "param_test" ? appState.currentJob : null;
  renderParamResults(currentJob);
  await refreshWorkflowPreview();
}

function paramSelectionKey(provider, model, routeProfile, apiForm, referenceContractId, toolValidationMode, parameterSuite) {
  return `${provider || ""}\u0000${model || ""}\u0000${routeProfile || ""}\u0000${apiForm || ""}\u0000${referenceContractId || ""}\u0000${toolValidationMode || "auto"}\u0000${parameterSuite || ""}`;
}

function matchesParamSelection(job) {
  if (!job || job.type !== "param_test") return false;
  const form = appState.formsByTab.param;
  const workflow = selectedParamWorkflow();
  if (workflow) {
    const spec = job.job_spec || {};
    return job.provider === form.provider && job.model === form.model
      && job.route_profile === form.routeProfile && job.api_form === form.apiForm
      && spec.test_binding_id === workflow.workflow_binding_id
      && spec.reference_contract_id === workflow.reference_contract_id
      && workflowPreviewCurrent() && (spec.execution_plan || {}).plan_digest === appState.workflowPreview.plan_digest;
  }
  if (((job.job_spec || {}).execution_plan || {}).definition?.factory?.factory_id === "media_input") return false;
  const capability = apiFormCapability(
    form.provider, form.model, form.routeProfile, form.apiForm
  );
  return job.provider === form.provider
    && job.model === form.model
    && (job.route_profile || "") === (form.routeProfile || "")
    && (job.api_form || "") === (form.apiForm || "")
    && (job.model_profile_id || "") === (capability.profile_id || "")
    && (job.reference_contract_id || job.reference_source) === form.referenceContractId
    && (job.tool_validation_mode || "auto") === form.toolValidationMode
    && (job.parameter_suite || (job.job_spec && job.job_spec.parameter_suite) || "") === (form.parameterSuite || "");
}

function fixedParamRequestCount() {
  const form = appState.formsByTab.param;
  const prefillModels = ["claude-haiku-4-5-20251001", "claude-opus-4-5-20251101", "claude-sonnet-4-5-20250929"];
  const cacheModels = ["claude-haiku-4-5-20251001", "claude-opus-4-5-20251101", "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5", "claude-sonnet-4-5-20250929", "claude-sonnet-4-6", "claude-sonnet-5"];
  if (form.provider === "anthropic_official" && cacheModels.includes(form.model)
      && form.routeProfile === "vendor_direct" && form.apiForm === "anthropic_messages"
      && form.referenceContractId === "claude_native_messages"
      && form.parameterSuite === `anthropic_cache_${form.model.replaceAll("-", "_")}_20260908`) return 5;
  if (form.provider === "anthropic_official" && prefillModels.includes(form.model)
      && form.routeProfile === "vendor_direct" && form.apiForm === "anthropic_messages"
      && form.referenceContractId === "claude_native_messages"
      && form.parameterSuite === `anthropic_prefill_${form.model.replaceAll("-", "_")}_20260908`) return 3;
  if (form.provider !== "deepseek_official" || form.model !== "deepseek-v4-pro" || form.routeProfile !== "vendor_direct") return 0;
  if (form.apiForm === "openai_fim_completions_beta"
      && form.referenceContractId === "deepseek_v4_pro_0813_fim_beta"
      && form.parameterSuite === "deepseek_fim_causal_20260908") return 8;
  if (form.apiForm === "deepseek_beta_chat_prefix"
      && form.referenceContractId === "deepseek_v4_pro_0813_chat_prefix_beta"
      && !form.parameterSuite) return 5;
  return 0;
}

function renderParameterSuites() {
  const form = appState.formsByTab.param;
  const selected = form.parameterSuite || "";
  const suites = (appState.paramSpec && appState.paramSpec.available_parameter_suites) || [];
  const options = suites.filter(item => item && typeof item.id === "string");
  const available = options.some(item => item.id === selected);
  const select = $("parameterSuite");
  select.innerHTML = `<option value="">${!form.parameterSuite && fixedParamRequestCount() === 5 ? "固定前缀验证" : "通用参数矩阵"}</option>`
    + options.map(item => `<option value="${esc(item.id)}">${esc(item.label || item.id)} · ${Number(item.request_count)} 次</option>`).join("")
    + (selected && !available ? `<option value="${esc(selected)}" disabled>所选套件当前不可用</option>` : "");
  select.value = selected;
  select.disabled = options.length === 0 && !selected;
}

function paramTestRunsValue() {
  if (!selectedParamWorkflow() && fixedParamRequestCount()) return 1;
  const defaults = (appState.config && appState.config.defaults) || {};
  const maxRuns = Number(defaults.param_test_runs_max || 1000);
  const raw = Number(appState.formsByTab.param.paramTestRuns || $("paramTestRuns").value || 1);
  if (!Number.isFinite(raw)) return 1;
  return Math.max(1, Math.min(Math.trunc(raw), maxRuns));
}

function renderParamRunHint() {
  if (!appState.config) return;
  if (selectedParamWorkflow()) {
    const runs = paramTestRunsValue();
    $("paramTestRuns").value = runs;
    $("paramTestRuns").disabled = false;
    const preview = workflowPreviewCurrent() ? appState.workflowPreview : null;
    $("paramRunHint").textContent = preview
      ? `${preview.selected_cases.length} 个用例 × ${runs} 轮 · ${preview.ordered_steps.length} 步骤/轮`
      : `完整套件默认执行 ${runs} 轮`;
    return;
  }
  const form = appState.formsByTab.param;
  const runs = paramTestRunsValue();
  const contract = contractById(form.referenceContractId);
  const profiles = Number((contract && contract.test_profile_count) || 0);
  const testedParams = Number((contract && contract.tested_param_count) || 0);
  const totalParams = Number((contract && contract.param_count) || 0);
  $("paramTestRuns").value = runs;
  const fixedCount = fixedParamRequestCount();
  $("paramTestRuns").disabled = fixedCount > 0;
  if (fixedCount) {
    form.paramTestRuns = 1;
    $("paramRunHint").textContent = fixedCount === 5 && form.provider === "anthropic_official"
      ? "2 次官方输入估算 + 3 次缓存生成对照 · 输入合格后继续 · 报告保留一个月"
      : `${fixedCount} 项固定对照 · 顺序执行一次`;
    return;
  }
  $("paramRunHint").textContent = `${profiles * runs} cells · ${testedParams}/${totalParams} params · ${profiles} profiles x ${runs} runs`;
}

function cacheControlCounts() {
  const form = appState.formsByTab.cache;
  if (form.controlMode === "off") return { positive: 0, negative: 0 };
  if (form.controlMode === "custom") {
    return {
      positive: Math.max(0, Math.trunc(Number(form.positivePairs) || 0)),
      negative: Math.max(0, Math.trunc(Number(form.negativeRequests) || 0)),
    };
  }
  return { positive: 3, negative: 3 };
}

function cacheContentRanges() {
  const form = appState.formsByTab.cache;
  if (form.customContent) {
    return { user: form.customUserChars, tool: form.customToolResultChars };
  }
  const profiles = ((appState.config && appState.config.cache_test) || {}).content_profiles || {};
  const selected = profiles[form.contentProfile] || {};
  return {
    user: rangePair(selected.user_chars, 200, 2000),
    tool: rangePair(selected.tool_result_chars, 500, 5000),
  };
}

function cacheRequestEstimate() {
  const form = appState.formsByTab.cache;
  const diagnostic = form.diagnosticScenario || "";
  if (diagnostic === "kilocode_agent_session") {
    const control = Math.max(0, Number(form.diagnosticPositivePairs) || 0) * 2
      + Math.max(0, Number(form.diagnosticNegativeRequests) || 0);
    const customer = 1 + Math.max(2, Math.trunc(Number(form.kilocodeSteps) || 2));
    return { scenario: diagnostic, customer, structure: 0, control, total: customer + control };
  }
  if (diagnostic) {
    const total = Math.max(1, Number(form.measuredRequests) || 1)
      + Math.max(0, Number(form.warmupRequests) || 0);
    return { scenario: diagnostic, customer: total, structure: 0, control: 0, total };
  }
  const sessions = Math.max(1, Math.trunc(Number(form.sessions) || 1));
  const rounds = Math.max(2, Math.trunc(Number(form.roundsPerSession) || 2));
  const toolEnabled = form.toolStage !== "off";
  const controls = cacheControlCounts();
  const customer = sessions * (rounds + (toolEnabled ? 1 : 0));
  const structure = 1;
  const control = controls.positive * 2 + controls.negative;
  return {
    scenario: "progressive_customer_session",
    customer,
    structure,
    control,
    total: customer + structure + control,
  };
}

function renderCacheToolStageOptions() {
  const form = appState.formsByTab.cache;
  const toolsRunnable = cacheLeafSupportsTools(selectedCacheLeafCapability());
  const rounds = Math.max(2, Math.trunc(Number(form.roundsPerSession) || 2));
  form.roundsPerSession = rounds;
  const current = form.toolStage === "off" ? "off" : String(
    Math.min(Math.max(2, Number(form.toolStage) || 3), rounds)
  );
  $("cacheToolStage").innerHTML = [
    '<option value="off">关闭</option>',
    ...Array.from({ length: rounds - 1 }, (_item, index) => {
      const round = index + 2;
      return `<option value="${round}"${toolsRunnable ? "" : " disabled"}>第 ${round} 轮 · 真实工具调用</option>`;
    }),
  ].join("");
  form.toolStage = current;
  $("cacheToolStage").value = current;
  $("cacheToolStage").disabled = !toolsRunnable;
}

function renderCacheFormState() {
  const form = appState.formsByTab.cache;
  const capability = selectedCacheLeafCapability();
  const toolsRunnable = cacheLeafSupportsTools(capability);
  const adjustedForCapability = normalizeCacheScenarioForCapability(
    form, capability
  );
  renderCacheToolStageOptions();
  const diagnostic = form.diagnosticScenario || "";
  const diagnosticSelect = $("cacheDiagnosticScenario");
  const kilocodeOption = diagnosticSelect.querySelector(
    'option[value="kilocode_agent_session"]'
  );
  if (kilocodeOption) kilocodeOption.disabled = !toolsRunnable;
  diagnosticSelect.value = diagnostic;
  const progressive = !diagnostic;
  $("cacheProgressiveFields").hidden = !progressive;
  $("cacheAdvanced").hidden = !progressive;
  $("cacheProgressiveAdvanced").hidden = !progressive;
  $("cacheKilocodeDiagnosticFields").hidden = diagnostic !== "kilocode_agent_session";
  $("cacheLegacyDiagnosticFields").hidden = !["growing_conversation", "shared_prefix"].includes(diagnostic);
  $("cacheCustomUserWrap").hidden = !progressive || !form.customContent;
  $("cacheCustomToolWrap").hidden = !progressive || !form.customContent;
  const customControls = progressive && form.controlMode === "custom";
  $("cachePositiveWrap").hidden = !customControls;
  $("cacheNegativeWrap").hidden = !customControls;

  const estimate = cacheRequestEstimate();
  const requiresConfirmation = estimate.total > 100;
  $("cacheLargeRunConfirmWrap").classList.toggle("active", requiresConfirmation);
  if (!requiresConfirmation) {
    form.confirmLarge = false;
    $("cacheConfirmLarge").checked = false;
  }
  const flow = progressive
    ? `短固定 system → 批量 seed → 等待 ${esc(form.waitAfterSeed)}s → 逐轮增长${form.toolStage === "off" ? "" : ` → 第 ${esc(form.toolStage)} 轮真实 tool call + follow-up`} → 独立结构探针。`
    : `诊断场景 <strong>${esc(diagnostic)}</strong> 使用历史口径，结果不会与 v10 混画。`;
  const toolGate = toolsRunnable
    ? `MPDB tool: ${esc(capability.cache_tool_profile || "approved")}`
    : `MPDB tool: unavailable${adjustedForCapability ? " · 已自动关闭工具场景" : ""}`;
  const limitTone = estimate.total > 1000 ? "bad" : estimate.total > 100 ? "warn" : "ok";
  $("cacheFlowPreview").innerHTML = `${flow} <strong>客户 ${estimate.customer}</strong> + 结构探针 ${estimate.structure} + 控制 ${estimate.control} = <strong>${estimate.total} requests</strong> <span class="pill ${limitTone}">${estimate.total > 1000 ? "超过硬上限" : estimate.total > 100 ? "需确认" : "规模安全"}</span> <span class="pill ${toolsRunnable ? "ok" : "warn"}">${toolGate}</span>`;
  renderCacheSummary();
  renderBusyState();
}

function renderCacheSummary() {
  const form = appState.formsByTab.cache;
  const estimate = cacheRequestEstimate();
  const controls = cacheControlCounts();
  const ranges = cacheContentRanges();
  const diagnostic = form.diagnosticScenario || "";
  const rows = [
    ["Result semantics", diagnostic ? (diagnostic === "kilocode_agent_session" ? "v11 agent session" : "legacy diagnostic") : "v10 structural efficiency"],
    ["Scenario", estimate.scenario],
    ["Customer requests", estimate.customer],
    ["Structure probe", estimate.structure],
    ["Control requests", estimate.control],
    ["Total requests", estimate.total],
    ["Sessions / rounds", diagnostic ? "diagnostic-specific" : `${form.sessions} / ${form.roundsPerSession}`],
    ["Content", diagnostic ? "diagnostic-specific" : (form.customContent ? "custom" : form.contentProfile)],
    ["User chars / turn", diagnostic ? "diagnostic-specific" : ranges.user.join("–")],
    ["Tool result chars", diagnostic ? "diagnostic-specific" : ranges.tool.join("–")],
    ["Tool stage", diagnostic ? "diagnostic-specific" : (form.toolStage === "off" ? "off" : `round ${form.toolStage}`)],
    ["Controls", diagnostic ? "diagnostic-specific" : `${form.controlMode} · ${controls.positive}+${controls.negative}`],
    ["Evidence", "official_usage"],
  ];
  $("cacheSummary").innerHTML = rows.map(([label, value]) => (
    `<div class="summary-item"><div>${esc(label)}</div><div>${esc(value)}</div></div>`
  )).join("");
}

function setActiveTab(tab) {
  appState.activeTab = tab;
  document.querySelectorAll(".test-tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === tab);
  });
  document.querySelectorAll(".test-view").forEach((view) => {
    view.classList.toggle("active", view.id === `${tab}View`);
  });
}

async function createJob(type) {
  if (isBusy()) {
    showError(`已有 job 正在运行：${appState.currentJob.id} (${appState.currentJob.status})`);
    return;
  }
  showError("");
  const tab = tabForJobType(type);
  const provider = selectedProviderForTab(tab);
  if (!provider || !provider.has_key) {
    showError(`Provider ${provider ? (provider.label || provider.name) : ""} has no API key configured.`);
    return;
  }
  let payload = jobPayload(type);
  if (type === "param_test" && workflowRequestPayload()) {
    const preview = await refreshWorkflowPreview();
    if (!preview || !workflowPreviewCurrent()) {
      showError(appState.workflowPreviewError || "测试选择已改变，请等待新预览。");
      return;
    }
    payload = {...workflowRequestPayload(), plan_digest: preview.plan_digest, ...(preview.plan_seed ? {plan_seed: preview.plan_seed} : {})};
  }
  if (type === "image_param_test") {
    const preview = await refreshImagePlanPreview();
    if (!preview || !imagePreviewCurrent()) {
      showError(appState.imagePlanPreviewError || "图片测试选择已改变，请等待新预览。");
      return;
    }
    payload = {...imageRequestPayload(), plan_digest: preview.plan_digest};
  }
  if (
    (type === "quick_load" || type === "staircase")
    && payload.target_rpm > 0
    && payload.target_tpm > 0
    && (
      !String(payload.workload || "").startsWith("throughput")
      || payload.workload === "throughput_streaming"
    )
  ) {
    showError("throughput_streaming 使用固定长度请求，不能同时设置 RPM 和 TPM；请清空其中一个目标。");
    return;
  }
  const resp = await fetch("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await resp.json();
  if (!resp.ok) {
    showError(data.error || "failed to create job");
    if (type === "param_test" && workflowRequestPayload() && String(data.error || "").includes("changed since preview")) {
      await refreshWorkflowPreview(true);
      showError(`${data.error} · 预览已重新读取，请核对后重试。`);
    }
    if (type === "image_param_test" && String(data.error || "").includes("changed since preview")) {
      appState.imagePlanPreview = null;
      await refreshImagePlanPreview();
      showError(`${data.error} · 预览已重新读取，请核对后重试。`);
    }
    await pollJob();
    return;
  }
  appState.currentJobId = data.id;
  if (data.type === "param_test") appState.paramLiveJobId = data.id;
  if (data.type === "image_param_test") appState.imageLiveJobId = data.id;
  setActiveTab(tabForJobType(data.type));
  renderJob(data);
}

function timeoutSecValue() {
  const input = $("timeoutSec");
  const raw = input ? Number(input.value) : appState.timeoutSec;
  const value = Number.isFinite(raw) && raw > 0 ? Math.floor(raw) : appState.timeoutSec;
  appState.timeoutSec = value;
  if (input) input.value = String(value);
  return value;
}

function targetRpmValue() {
  const form = appState.formsByTab.load;
  const input = $("targetRpm");
  const raw = input ? Number(input.value) : form.targetRpm;
  const value = Number.isFinite(raw) && raw > 0 ? Math.floor(raw) : 0;
  form.targetRpm = value;
  if (input) input.value = value ? String(value) : "";
  return value;
}

function targetTpmValue() {
  const form = appState.formsByTab.load;
  const input = $("targetTpm");
  const raw = input ? Number(input.value) : form.targetTpm;
  const value = Number.isFinite(raw) && raw > 0 ? Math.floor(raw) : 0;
  form.targetTpm = value;
  if (input) input.value = value ? String(value) : "";
  return value;
}

function jobPayload(type) {
  const timeout_sec = timeoutSecValue();
  if (type === "param_test" && workflowRequestPayload()) {
    return {...workflowRequestPayload(), ...(workflowPreviewCurrent() ? {
      plan_digest: appState.workflowPreview.plan_digest,
      ...(appState.workflowPreview.plan_seed ? {plan_seed: appState.workflowPreview.plan_seed} : {}),
    } : {})};
  }
  if (type === "param_test") {
    const form = appState.formsByTab.param;
    const runs = paramTestRunsValue();
    form.paramTestRuns = runs;
    return {
      type,
      provider: form.provider,
      model: form.model,
      route_profile: form.routeProfile,
      api_form: form.apiForm,
      reference_contract_id: form.referenceContractId,
      parameter_suite: form.parameterSuite || null,
      tool_validation_mode: form.toolValidationMode,
      param_test_runs: runs,
      timeout_sec,
    };
  }
  if (type === "image_param_test") {
    return {...imageRequestPayload(), ...(imagePreviewCurrent() ? {plan_digest: appState.imagePlanPreview.plan_digest} : {})};
  }
  if (type === "cache_suite") {
    const form = appState.formsByTab.cache;
    const diagnostic = form.diagnosticScenario || "";
    let cache_plan;
    if (!diagnostic) {
      form.customUserChars = parseRangeInput("cacheCustomUserChars", form.customUserChars);
      form.customToolResultChars = parseRangeInput(
        "cacheCustomToolResultChars",
        form.customToolResultChars,
      );
      const controls = { mode: form.controlMode };
      if (form.controlMode === "custom") {
        controls.positive_long_prefix_pairs = Number(form.positivePairs);
        controls.negative_unique_prefix_requests = Number(form.negativeRequests);
      }
      cache_plan = {
        scenario: "progressive_customer_session",
        sessions: Number(form.sessions),
        rounds_per_session: Number(form.roundsPerSession),
        content_profile: form.customContent ? "custom" : form.contentProfile,
        tool_stage: {
          enabled: form.toolStage !== "off",
          round: form.toolStage === "off" ? 3 : Number(form.toolStage),
        },
        controls,
        wait_after_seed_sec: Number(form.waitAfterSeed),
        max_tokens: Number(form.maxTokens),
        max_run_seconds: Number(form.maxRunSeconds),
        consecutive_failure_limit: Number(form.failureLimit),
        seed: Number(form.seed),
        evidence_mode: "official_usage",
      };
      if (form.customContent) {
        cache_plan.content_ranges = {
          user_chars: { min: form.customUserChars[0], max: form.customUserChars[1] },
          tool_result_chars: {
            min: form.customToolResultChars[0],
            max: form.customToolResultChars[1],
          },
        };
      }
    } else if (diagnostic === "kilocode_agent_session") {
      cache_plan = {
        scenario: diagnostic,
        steps: Number(form.kilocodeSteps),
        trajectory_mode: form.kilocodeTrajectoryMode,
        warmup_requests: 1,
        controls: {
          positive_long_prefix_pairs: Number(form.diagnosticPositivePairs),
          negative_unique_prefix_requests: Number(form.diagnosticNegativeRequests),
        },
        wait_after_seed_sec: Number(form.waitAfterSeed),
        max_tokens: Number(form.maxTokens),
        max_run_seconds: Number(form.maxRunSeconds),
        consecutive_failure_limit: Number(form.failureLimit),
        seed: Number(form.seed),
        evidence_mode: "official_usage",
      };
    } else {
      cache_plan = {
        scenario: diagnostic,
        measured_requests: Number(form.measuredRequests),
        warmup_requests: Number(form.warmupRequests),
        wait_after_warmup_sec: Number(form.waitAfterWarmup),
        max_tokens: Number(form.maxTokens),
        evidence_mode: "official_usage",
      };
    }
    const estimate = cacheRequestEstimate();
    return {
      type,
      provider: form.provider,
      model: form.model,
      route_profile: form.routeProfile,
      api_form: form.apiForm,
      workload: "cache_suite",
      cache_plan,
      confirm_large_run: estimate.total > 100 && !!form.confirmLarge,
      timeout_sec,
    };
  }
  const form = appState.formsByTab.load;
  if (type === "staircase") {
    return {
      type,
      provider: form.provider,
      model: form.model,
      workload: form.workload,
      request_mode: form.requestMode,
      timeout_sec,
      target_rpm: targetRpmValue(),
      target_tpm: targetTpmValue(),
      staircase_plan: {
        steps: form.staircaseSteps,
        step_duration: form.staircaseStepDuration,
        spawn_rate: Number(form.staircaseSpawnRate),
        warmup: {
          enabled: !!form.staircaseWarmupEnabled,
          users: Number(form.staircaseWarmupUsers),
          duration: form.staircaseWarmupDuration,
          workload: form.workload,
          per_step: false,
        },
        auto_extend: {
          enabled: !!form.staircaseAutoExtend,
          increment_users: Number(form.staircaseIncrementUsers),
          max_users: Number(form.staircaseMaxUsers),
        },
      },
    };
  }
  if (type === "soak") {
    return {
      type,
      provider: form.provider,
      model: form.model,
      workload: form.workload,
      request_mode: form.requestMode,
      timeout_sec,
      soak_plan: {
        users: Number(form.soakUsers),
        spawn_rate: Number(form.soakSpawnRate),
        duration: form.soakDuration,
        workload: form.workload,
      },
    };
  }
  return {
    type,
    provider: form.provider,
    model: form.model,
    workload: form.workload,
    users: Number(form.users || 10),
    spawn_rate: Number(form.spawnRate || 2),
    duration: form.duration || "2m",
    timeout_sec,
    target_rpm: targetRpmValue(),
    target_tpm: targetTpmValue(),
    request_mode: form.requestMode,
  };
}

async function stopActiveJob() {
  if (!appState.currentJobId || !isBusy()) return;
  const resp = await fetch(`/api/jobs/${appState.currentJobId}/stop`, { method: "POST" });
  const data = await resp.json();
  if (!resp.ok) {
    showError(data.error || "failed to stop job");
    return;
  }
  renderJob(data);
}

async function pollJob() {
  try {
    const payload = await fetch("/api/jobs/current", { cache: "no-store" }).then((resp) => resp.json());
    const activeJob = payload.active || null;
    const newestJob = payload.newest || null;
    if (activeJob) {
      appState.currentJobId = activeJob.id;
    } else if (
      newestJob
      && (
        !appState.currentJobId
        || !appState.currentJob
        || Number(newestJob.created_at || 0) > Number(appState.currentJob.created_at || 0)
      )
    ) {
      appState.currentJobId = newestJob.id;
    }
    if (!appState.currentJobId) {
      renderGlobalJob(null);
      renderAllResults(null);
      renderBusyState();
      return;
    }
    const data = await fetch(`/api/jobs/${appState.currentJobId}`, { cache: "no-store" }).then((resp) => resp.json());
    if (!data.error) renderJob(data);
  } catch (error) {
    showError(`poll failed: ${error.message || error}`);
  }
}

function renderJob(job) {
  const previous = appState.currentJob;
  appState.currentJob = job;
  appState.currentJobId = job.id;
  if (
    job.type === "param_test"
    && matchesParamSelection(job)
    && ["completed", "failed"].includes(job.status)
  ) {
    appState.paramHistoryResult = job;
  }
  if (
    job.type === "image_param_test"
    && matchesImageSelection(job)
    && ["completed", "failed"].includes(job.status)
  ) {
    appState.imageHistoryResult = job;
  }
  renderGlobalJob(job);
  renderAllResults(job);
  renderBusyState();
  if (isLoadJob(job) && !isBusy(job) && (!previous || previous.id !== job.id || isBusy(previous))) {
    refreshLoadResults(true);
  }
}

function renderGlobalJob(job) {
  if (!job) {
    $("globalJobTitle").textContent = "No job yet.";
    $("globalJobMeta").textContent = "选择测试类型并启动一个 job。";
    $("globalStop").disabled = true;
    return;
  }
  $("globalJobTitle").textContent = `${job.id} · ${job.type} · ${job.status}`;
  const timeoutLabel = job.timeout_sec ? ` · timeout ${job.timeout_sec}s` : "";
  let planLabel = `request ${job.request_mode || "fixed"}`;
  if (job.effective_staircase_plan) {
    planLabel = `steps ${(job.effective_staircase_plan.steps || []).join(",")} · ${job.effective_staircase_plan.step_duration}`;
  } else if (job.effective_cache_plan) {
    planLabel = `${job.effective_cache_plan.scenario} · ${job.effective_cache_plan.estimated_request_count || "?"} requests`;
  } else if (job.effective_soak_plan) {
    planLabel = `${job.effective_soak_plan.users} users · ${job.effective_soak_plan.duration}`;
  } else if (job.effective_image_plan) {
    planLabel = `${job.effective_image_plan.suite} · ${job.effective_image_plan.estimated_case_count || "?"} image cases`;
  }
  $("globalJobMeta").textContent = `${job.provider_label || job.provider} / ${job.model}${timeoutLabel} · ${planLabel} · ${job.report_dir || ""}`;
  $("globalStop").disabled = !isBusy(job);
}

function renderBusyState() {
  const busy = isBusy();
  ["startParam", "startImage", "startQuickLoad", "startStaircase", "startSoak", "startCache"].forEach((id) => {
    $(id).disabled = busy;
  });
  const imageProvider = imageProviderByName(appState.formsByTab.image.provider);
  const imageModel = imageModelById(
    appState.formsByTab.image.provider,
    appState.formsByTab.image.model,
  );
  const imageFullMissingHighResolution = appState.formsByTab.image.suite === "full"
    && ((imageApiFormCapability(appState.formsByTab.image.provider, appState.formsByTab.image.model, appState.formsByTab.image.routeProfile, appState.formsByTab.image.apiForm) || {}).suite === "grok_imagine"
      ? !appState.formsByTab.image.include2k
      : !appState.formsByTab.image.include4k);
  $("startImage").disabled = busy
    || !imagePreviewCurrent()
    || appState.imagePlanPreviewLoading
    || appState.imagePlanPreviewCount === null
    || !imageProvider
    || !imageProvider.has_key
    || (imageApiFormCapability(
      appState.formsByTab.image.provider,
      appState.formsByTab.image.model,
      appState.formsByTab.image.routeProfile,
      appState.formsByTab.image.apiForm,
    ) || {}).profile_status !== "registered"
    || imageFullMissingHighResolution;
  const paramForm = appState.formsByTab.param;
  const paramCapability = apiFormCapability(
    paramForm.provider,
    paramForm.model,
    paramForm.routeProfile,
    paramForm.apiForm,
  ) || {};
  const workflow = selectedParamWorkflow();
  $("startParam").disabled = workflow
    ? busy || !workflowPreviewCurrent() || !!appState.workflowPreviewError
    : busy|| paramCapability.profile_status !== "registered"
    || paramCapability.parameter_test_enabled === false
    || (Boolean(paramForm.parameterSuite) && (!appState.paramSpec || appState.paramSpec.error
        || appState.paramSpec.parameter_suite !== paramForm.parameterSuite));
  const loadForm = appState.formsByTab.load;
  const loadCapability = modelCapability(loadForm.provider, loadForm.model) || {};
  ["startQuickLoad", "startStaircase", "startSoak"].forEach((id) => {
    $(id).disabled = busy
      || loadCapability.profile_status !== "registered"
      || loadCapability.pressure_test_runnable !== true;
  });
  const cacheForm = appState.formsByTab.cache;
  const cacheCapability = apiFormCapability(
    cacheForm.provider,
    cacheForm.model,
    cacheForm.routeProfile,
    cacheForm.apiForm,
  ) || {};
  const cacheEstimate = cacheRequestEstimate();
  $("startCache").disabled = busy
    || cacheCapability.profile_status !== "registered"
    || cacheCapability.pressure_test_runnable !== true
    || !cacheScenarioRunnable(cacheCapability, cacheForm)
    || cacheEstimate.total > 1000
    || (cacheEstimate.total > 100 && !appState.formsByTab.cache.confirmLarge);
  ["globalStop", "stopParam", "stopImage", "stopLoad", "stopCache"].forEach((id) => {
    $(id).disabled = !busy;
  });
  const hint = busy ? `job running: ${appState.currentJob.type}` : "ready";
  ["paramBusyHint", "imageBusyHint", "loadBusyHint", "cacheBusyHint"].forEach((id) => {
    $(id).textContent = hint;
    $(id).className = `pill ${busy ? "warn" : "ok"}`;
  });
}

function renderAllResults(job) {
  renderParamResults(job && job.type === "param_test" ? job : null);
  renderImageResults(job && job.type === "image_param_test" ? job : null);
  renderLoadResults(isLoadJob(job) ? job : null);
  renderCacheResults(job && job.type === "cache_suite" ? job : null);
}

function renderProgress(prefix, job, emptyText) {
  if (!job) {
    $(`${prefix}ProgressBar`).style.width = "0%";
    $(`${prefix}ProgressLabel`).textContent = emptyText;
    $(`${prefix}ProgressDetail`).textContent = "";
    return;
  }
  const progress = job.progress || {};
  const percent = Math.max(0, Math.min(100, Number(progress.percent || 0)));
  $(`${prefix}ProgressBar`).style.width = `${percent}%`;
  $(`${prefix}ProgressLabel`).textContent = `${percent}% · ${progress.label || job.status || "waiting"}`;
  $(`${prefix}ProgressDetail`).textContent = progress.detail || "";
}

function renderMetrics(prefix, metrics) {
  $(`${prefix}Metrics`).innerHTML = metrics.map(([label, value]) => (
    `<div class="metric"><div>${esc(label)}</div><div>${esc(value)}</div></div>`
  )).join("");
}

function renderLoadMetricSections(prefix, sections) {
  $(`${prefix}Metrics`).innerHTML = sections.map((section) => (
    `<section class="metric-section">
      <div class="metric-section-head">
        <h3>${esc(section.title)}</h3>
        ${section.description ? `<span>${esc(section.description)}</span>` : ""}
      </div>
      <div class="metric-section-grid">
        ${(section.metrics || []).map((metric) => (
          `<div class="metric ${esc(metric.tone || "")}">
            <div>${esc(metric.label)}</div>
            <div>${esc(metric.value)}</div>
            ${metric.hint ? `<div class="metric-hint">${esc(metric.hint)}</div>` : ""}
          </div>`
        )).join("")}
      </div>
    </section>`
  )).join("");
}

function renderFiles(prefix, job) {
  const files = job ? (job.report_files || []) : [];
  $(`${prefix}Files`).innerHTML = files.map((file) => (
    `<a href="${esc(file.url)}" target="_blank" rel="noreferrer">${esc(file.name)}</a>`
  )).join("") || '<span class="muted">No report files yet.</span>';
}

function observedUpstreamSignals(job) {
  if (!job) return [];
  let audits = modelIdentityRows(job);
  if (job.type === "image_param_test") {
    audits = (job.image_results || []).flatMap((result) => (
      result.model_identity_audit && Array.isArray(result.model_identity_audit.exchanges)
        ? result.model_identity_audit.exchanges
        : []
    ));
  }
  return audits.flatMap((audit) => (audit.evidence || []).map((item) => {
    if (item.kind === "system_fingerprint") return `system=${item.value}`;
    if (item.kind === "response_header") return `${item.name}=${item.value}`;
    if (item.kind === "protocol_fingerprint") return `protocol=${item.status}`;
    return "";
  })).filter(Boolean).filter((item, index, values) => values.indexOf(item) === index);
}

function renderRouteEvidence(prefix, configuredRoute, job) {
  const routeNode = $(`${prefix}ConfiguredRoute`);
  const fingerprintNode = $(`${prefix}ObservedFingerprint`);
  if (routeNode) routeNode.textContent = `configured route: ${configuredRoute || "unknown"}`;
  if (fingerprintNode) {
    const signals = observedUpstreamSignals(job);
    fingerprintNode.textContent = `observed upstream: ${signals.slice(0, 3).join(" · ") || "n/a"}`;
    fingerprintNode.title = "Observed evidence is diagnostic and never rewrites the configured route.";
  }
}

function renderParamResults(job) {
  const currentResult = matchesParamSelection(job)
    && (isBusy(job) || job.id === appState.paramLiveJobId)
    ? job
    : null;
  const historicalResult = matchesParamSelection(appState.paramHistoryResult)
    ? appState.paramHistoryResult
    : null;
  const visibleJob = currentResult || historicalResult;
  renderRouteEvidence("param", appState.formsByTab.param.routeProfile, visibleJob);
  const resultSource = $("paramResultSource");
  if (currentResult) {
    resultSource.textContent = `live/current: ${currentResult.status}`;
    resultSource.className = `pill ${isBusy(currentResult) ? "warn" : "ok"}`;
  } else if (historicalResult) {
    const timestamp = historicalResult.finished_at || historicalResult.created_at;
    const timeLabel = timestamp ? new Date(Number(timestamp) * 1000).toLocaleString() : "unknown time";
    resultSource.textContent = `last result: ${timeLabel}`;
    resultSource.className = "pill ok";
  } else if (appState.paramHistoryLoading) {
    resultSource.textContent = "result: loading";
    resultSource.className = "pill warn";
  } else {
    resultSource.textContent = "result: none";
    resultSource.className = "pill";
  }
  const emptyText = appState.paramHistoryLoading
    ? "Loading the latest matching parameter test result."
    : "No previous result for this provider / model / Interface Contract.";
  renderProgress("param", visibleJob, emptyText);
  if (selectedParamWorkflow() || (visibleJob && visibleJob.job_spec && visibleJob.job_spec.test_workflow_snapshot)) {
    if (visibleJob) {
      const progress = visibleJob.progress || {};
      const verifiedPass = progress.pass === true && (!visibleJob.result_validation || visibleJob.result_validation.pass === true);
      resultSource.className = `pill ${verifiedPass ? "ok" : ["invalid", "incomplete", "failed"].includes(progress.status)
        || progress.cleanup_status === "incomplete" ? "bad" : "warn"}`;
    }
    renderWorkflowParamResult(visibleJob);
    return;
  }
  if (!visibleJob) {
    renderMetrics("param", paramEmptyMetrics());
    renderFiles("param", null);
    renderParamMatrix(null);
    renderTokenAudit(null);
    renderModelIdentity(null);
    $("paramFailedCaseLog").textContent = "No failed or incompatible parameter test cases.";
    $("paramLogTail").textContent = "";
    return;
  }

  renderMetrics("param", paramTestMetrics(visibleJob));
  renderFiles("param", visibleJob);
  $("paramLogTail").textContent = visibleJob.log_tail || "";
  renderParamMatrix(visibleJob);
  renderTokenAudit(visibleJob);
  renderModelIdentity(visibleJob);
}

function caseOverallPass(row) {
  if (!row) return undefined;
  if ((row.request_input_integrity || {}).status === "fail") return false;
  if (row.overall_pass === false || row.token_validation_pass === false || row.pass === false) return false;
  const exchanges = ((row.token_audit || {}).exchanges) || [];
  if (!exchanges.length || exchanges.some((exchange) => tokenExchangeValidationPass(exchange) === null)) return undefined;
  if ((row.token_audit || {}).validation_pass !== true) return false;
  if (exchanges.some((exchange) => tokenExchangeValidationPass(exchange) !== true)) return false;
  return row.overall_pass === true || row.pass === true;
}

function imageTestMetrics(job) {
  const progress = (job && job.progress) || {};
  const summary = (job && job.image_summary) || {};
  const audit = summary.token_audit_summary || null;
  const results = (job && job.image_results) || [];
  const passed = results.length
    ? results.filter((row) => caseOverallPass(row) === true).length
    : summary.pass_count ?? progress.pass_count ?? 0;
  const failed = results.length
    ? results.filter((row) => caseOverallPass(row) === false).length
    : summary.failure_count ?? progress.failure_count ?? 0;
  if (isResponsesImageAudit(audit)) {
    return [
      ["Completed", `${progress.completed_cases ?? summary.case_count ?? 0}/${progress.total_cases ?? summary.case_count ?? 0}`],
      ["Passed", passed], ["Failed", failed],
      ["Current case", progress.current_case || (job ? job.status : "n/a")],
      ["Return code", job && job.returncode != null ? job.returncode : "running"],
      ["Usage and image-estimate gate", tokenValidationGateLabel(job, audit)],
      ["Validated / required generations", tokenExchangeCountLabel(audit)],
      ["Expected rejections", audit.expected_rejection_count ?? "n/a"],
      ["Exact media token counts", "Not verified"],
    ];
  }
  return [
    ["Completed", `${progress.completed_cases ?? summary.case_count ?? 0}/${progress.total_cases ?? summary.case_count ?? 0}`],
    ["Passed", passed],
    ["Failed", failed],
    ["Current case", progress.current_case || (job ? job.status : "n/a")],
    ["Last latency", progress.last_latency_ms == null ? "n/a" : fmtDuration(progress.last_latency_ms)],
    ["Return code", job && job.returncode != null ? job.returncode : "running"],
    ["Token validation gate", tokenValidationGateLabel(job, audit)],
    ["Validation status", auditStatusLabel(audit && audit.validation_status)],
    ["Validated / required exchanges", tokenExchangeCountLabel(audit)],
    ["Missing usage", audit ? audit.missing_usage_count ?? "n/a" : "n/a"],
    ["Gross failures / partial", tokenGrossCountLabel(audit)],
    ["Exact coverage", fmtPct(audit && audit.coverage)],
    ["Exact accuracy", tokenExactAccuracyLabel(audit)],
  ];
}

function imageRequestedLabel(result) {
  const requested = result.requested || {};
  if (requested.aspect_ratio || requested.resolution) {
    return `${requested.resolution || "?"} / ${requested.aspect_ratio || "?"} · n=${requested.n || 1} · ${requested.response_format || "url"}`;
  }
  if (requested.size) return requested.size;
  const google = requested.extra_body && requested.extra_body.google;
  const imageConfig = google && google.image_config;
  return (imageConfig && `${imageConfig.image_size || "?"} / ${imageConfig.aspect_ratio || "?"}`) || "n/a";
}

function imageExpectedLabel(result) {
  if ((result.tags || []).some((tag) => String(tag).includes("negative"))) return "HTTP 400/422";
  const metadata = result.metadata || {};
  if (metadata.expected_size) return `${metadata.expected_size[0]}×${metadata.expected_size[1]}`;
  if (metadata.expected_aspect_ratio) return `ratio ${metadata.expected_aspect_ratio} · ${metadata.resolution || "tier n/a"}`;
  if (metadata.requested_resolution) return metadata.requested_resolution;
  return result.verification_level || "image constraint";
}

function renderImageSummary(job) {
  const node = $("imageSummary");
  if (!job || !job.image_summary) {
    node.innerHTML = '<div class="image-empty">Summary will appear after the suite finishes.</div>';
    return;
  }
  const summary = job.image_summary || {};
  const resolution = summary.resolution_correspondence || {};
  const postprocess = summary.postprocess_inference || {};
  const modelCheck = summary.model_check || job.image_model_check || {};
  const tokenAudit = summary.token_audit_summary || {};
  const tokenGate = tokenValidationGateInfo(job, tokenAudit);
  const tokenDetail = isResponsesImageAudit(tokenAudit)
    ? `${tokenExchangeCountLabel(tokenAudit)} validated / required generations · ${tokenAudit.expected_rejection_count ?? 0} expected rejections · separate mainline and image-tool usage · official image-output estimates; exact media counts unverified`
    : `${tokenExchangeCountLabel(tokenAudit)} required exchanges · missing usage ${tokenAudit.missing_usage_count ?? "n/a"} · gross ${tokenAudit.gross_failure_count ?? "n/a"} fail / ${tokenAudit.gross_partial_count ?? "n/a"} partial · exact coverage ${fmtPct(tokenAudit.coverage)} · exact accuracy ${tokenExactAccuracyLabel(tokenAudit)}`;
  const suitePass = summary.pass === true && tokenGate.pass === true;
  const suiteFail = summary.pass === false || tokenGate.pass === false;
  const identityAudit = summary.model_identity_summary || {};
  const missing = modelCheck.missing_requested_models || [];
  const postEvidence = postprocess.evidence || [];
  const visualMetrics = ((job && job.image_results) || []).flatMap((result) => (
    (result.actual_images || [])
      .map((actual) => actual.visual_metrics)
      .filter(Boolean)
  ));
  const visualReasons = Array.from(new Set(
    visualMetrics
      .filter((metrics) => metrics.available === false)
      .map((metrics) => metrics.reason || "unavailable"),
  ));
  const visualStatus = !visualMetrics.length
    ? "not collected"
    : (visualReasons.length ? "unavailable / partial" : "available");
  const visualDetail = !visualMetrics.length
    ? "No per-image forensic metrics were collected."
    : (visualReasons.length ? visualReasons.join(", ") : "Per-image forensic metrics were collected.");
  node.innerHTML = `
    <div class="image-summary-card ${suitePass ? "pass" : suiteFail ? "fail" : "warn"}">
      <div class="eyebrow">Suite verdict</div>
      <strong>${suitePass ? "PASS" : suiteFail ? "FAIL" : "UNVERIFIED"}</strong>
      <span>${esc(`${summary.pass_count ?? 0} passed / ${summary.failure_count ?? 0} failed`)}</span>
    </div>
    <div class="image-summary-card">
      <div class="eyebrow">Resolution correspondence</div>
      <strong>${esc(resolution.verdict || "unknown")}</strong>
      <span>${esc(resolution.interpretation || "No crossed-control conclusion.")}</span>
    </div>
    <div class="image-summary-card">
      <div class="eyebrow">Postprocess inference</div>
      <strong>${esc(`${postprocess.verdict || "unknown"} · score ${postprocess.score ?? 0}`)}</strong>
      <span>${esc(postEvidence.join(", ") || "No sufficient multi-resolution evidence.")}</span>
    </div>
    <div class="image-summary-card ${missing.length ? "warn" : ""}">
      <div class="eyebrow">Model check</div>
      <strong>${esc(modelCheck.status_code == null ? "unavailable" : `HTTP ${modelCheck.status_code}`)}</strong>
      <span>${esc(missing.length ? `Missing: ${missing.join(", ")}` : `${modelCheck.model_count ?? 0} models listed`)}</span>
    </div>
    <div class="image-summary-card ${tokenGate.pass === false ? "fail" : (tokenGate.pass === true ? "pass" : "warn")}">
      <div class="eyebrow">Token validation</div>
      <strong>${esc(`${tokenValidationGateLabel(job, tokenAudit)} · ${auditStatusLabel(tokenAudit.validation_status)}`)}</strong>
      <span>${esc(tokenDetail)}</span>
    </div>
    <div class="image-summary-card ${identityAudit.status === "mismatch" ? "fail" : (identityAudit.status === "match" ? "pass" : "warn")}">
      <div class="eyebrow">Execution identity</div>
      <strong>${esc(identityAudit.status || "unverifiable")}</strong>
      <span>${esc((identityAudit.returned_models || []).join(", ") || "No verifiable response model signal")}</span>
    </div>
    <div class="image-summary-card ${!visualMetrics.length || visualReasons.length ? "warn" : ""}">
      <div class="eyebrow">Visual analysis</div>
      <strong>${visualStatus}</strong>
      <span>${esc(visualDetail)}</span>
    </div>`;
}

function renderImageCaseRows(job) {
  const results = (job && job.image_results) || [];
  $("imageResults").innerHTML = results.map((result) => {
    const overallPass = caseOverallPass(result);
    const overallStatus = overallPass === undefined ? "token_unverified"
      : result.overall_status || result.status || "unknown";
    const actual = (result.actual_images || [])[0] || {};
    const actualSize = actual.width && actual.height ? `${actual.width}×${actual.height}` : "n/a";
    const previews = (result.artifact_urls || []).map((url, index) => (
      `<button class="image-thumb-button" type="button" data-image-url="${esc(url)}" data-image-caption="${esc(`${result.case || "case"} #${index + 1}`)}"><img class="image-thumb ${overallPass ? "pass" : "fail"}" src="${esc(url)}" loading="lazy" alt="${esc(result.case || "image result")}"></button>`
    )).join("");
    const failures = (result.overall_failures || result.failures || []).join(", ");
    const identityStatus = result.model_identity_audit && result.model_identity_audit.status || "unverifiable";
    return `<tr>
      <td>${esc(result.case || "")}</td>
      <td>${esc(imageRequestedLabel(result))}</td>
      <td>${esc(imageExpectedLabel(result))}</td>
      <td>${esc(result.status_code ?? "n/a")}</td>
      <td>${result.latency_ms == null ? "n/a" : esc(fmtDuration(result.latency_ms))}</td>
      <td>${esc(actualSize)}</td>
      <td>${esc(actual.format || "n/a")}</td>
      <td>${imageTokenAuditDetail(result)}<div class="muted">identity ${esc(identityStatus)}</div></td>
      <td class="${overallPass ? "status-pass" : "status-fail"}" title="${esc(failures)}">${esc(overallStatus)}</td>
      <td><div class="image-thumb-list">${previews || '<span class="muted">No artifact</span>'}</div></td>
    </tr>`;
  }).join("") || '<tr><td colspan="10" class="muted">No image test results yet.</td></tr>';
}

function renderImageResults(job) {
  const currentResult = matchesImageSelection(job)
    && (isBusy(job) || job.id === appState.imageLiveJobId)
    ? job
    : null;
  const historicalResult = matchesImageSelection(appState.imageHistoryResult)
    ? appState.imageHistoryResult
    : null;
  const visibleJob = currentResult || historicalResult;
  renderRouteEvidence("image", appState.formsByTab.image.routeProfile, visibleJob);
  const source = $("imageResultSource");
  if (currentResult) {
    source.textContent = `live/current: ${currentResult.status}`;
    source.className = `pill ${isBusy(currentResult) ? "warn" : "ok"}`;
  } else if (historicalResult) {
    const timestamp = historicalResult.finished_at || historicalResult.created_at;
    source.textContent = `last result: ${timestamp ? new Date(Number(timestamp) * 1000).toLocaleString() : "unknown"}`;
    source.className = "pill ok";
  } else if (appState.imageHistoryLoading) {
    source.textContent = "result: loading";
    source.className = "pill warn";
  } else {
    source.textContent = "result: none";
    source.className = "pill";
  }
  renderProgress("image", visibleJob, appState.imageHistoryLoading ? "Loading image history." : "No matching image result.");
  renderMetrics("image", imageTestMetrics(visibleJob));
  renderImageSummary(visibleJob);
  renderImageCaseRows(visibleJob);
  renderFiles("image", visibleJob);
  $("imageLogTail").textContent = (visibleJob && visibleJob.log_tail) || "";
}

function openImageLightbox(url, caption) {
  if (!String(url || "").startsWith("/reports/")) return;
  $("imageLightboxImage").src = url;
  $("imageLightboxCaption").textContent = caption || "";
  $("imageLightbox").hidden = false;
  $("imageLightboxClose").focus();
}

function closeImageLightbox() {
  $("imageLightbox").hidden = true;
  $("imageLightboxImage").removeAttribute("src");
}

function renderLoadResults(job) {
  renderProgress("load", job, "No load test job selected.");
  if (!job) {
    renderLoadMetricSections("load", loadEmptyMetricSections());
    renderLoadCharts(null, "load");
    renderFiles("load", null);
    $("loadLogTail").textContent = "";
    renderAdaptiveNotice("load", null, null);
    return;
  }
  renderLoadMetricSections("load", loadMetricSections(job));
  renderAdaptiveNotice("load", job.summary || {}, job);
  renderLoadCharts(job, "load");
  renderFiles("load", job);
  $("loadLogTail").textContent = job.log_tail || "";
}

async function refreshLoadResults(force = false) {
  const now = Date.now();
  if (!force && now - appState.resultsLastRefreshMs < 5000) return;
  appState.resultsLastRefreshMs = now;
  try {
    const payload = await fetch("/api/results", { cache: "no-store" }).then((resp) => resp.json());
    appState.loadResults = payload.results || [];
    if (!appState.selectedLoadResultId && appState.loadResults.length) {
      appState.selectedLoadResultId = appState.loadResults[0].id;
    }
    renderLoadResultSelect();
    if (appState.selectedLoadResultId) await loadSavedResult(appState.selectedLoadResultId);
    else renderSavedLoadResult(null);
  } catch (error) {
    showError(`load results failed: ${error.message || error}`);
  }
}

function renderLoadResultSelect() {
  const select = $("loadResultSelect");
  const results = appState.loadResults || [];
  select.innerHTML = results.map((result) => (
    `<option value="${esc(result.id)}">${esc(resultLabel(result))}</option>`
  )).join("");
  if (appState.selectedLoadResultId && results.some((result) => result.id === appState.selectedLoadResultId)) {
    select.value = appState.selectedLoadResultId;
  } else if (results.length) {
    appState.selectedLoadResultId = results[0].id;
    select.value = appState.selectedLoadResultId;
  } else {
    appState.selectedLoadResultId = "";
  }
}

function resultLabel(result) {
  const s = result.summary || {};
  const rpm = s.business_rpm === undefined || s.business_rpm === null ? "n/a" : Number(s.business_rpm).toFixed(1);
  const success = s.success_rate === undefined || s.success_rate === null ? "n/a" : `${(Number(s.success_rate) * 100).toFixed(1)}%`;
  return `${result.title || result.id} · ${rpm} success RPM · ${success}`;
}

async function loadSavedResult(resultId) {
  if (!resultId) {
    renderSavedLoadResult(null);
    return;
  }
  const payload = await fetch(`/api/result?id=${encodeURIComponent(resultId)}`, { cache: "no-store" }).then((resp) => resp.json());
  if (payload.error) {
    showError(payload.error);
    return;
  }
  appState.selectedLoadResult = payload;
  renderSavedLoadResult(payload);
}

function renderSavedLoadResult(result) {
  if (!result) {
    $("loadResultMeta").textContent = "No saved load result selected.";
    renderLoadMetricSections("loadResult", loadEmptyMetricSections());
    renderLoadCharts(null, "loadResult");
    $("loadResultStats").innerHTML = '<tr><td colspan="11" class="muted">No saved load results yet.</td></tr>';
    $("loadResultFiles").innerHTML = '<span class="muted">No report files yet.</span>';
    renderAdaptiveNotice("loadResult", null, null);
    return;
  }
  const summary = result.summary || {};
  $("loadResultMeta").textContent = `${result.provider_label || result.provider} / ${result.model} · ${result.type || "load"} · ${result.report_dir || ""}`;
  renderLoadMetricSections("loadResult", loadMetricSections(result));
  renderAdaptiveNotice("loadResult", summary, result);
  renderLoadCharts(result, "loadResult");
  renderSavedStatsTable(result.profile_stats || []);
  renderSavedFiles(result.report_files || []);
}

function renderSavedStatsTable(rows) {
  $("loadResultStats").innerHTML = rows.map((row) => (
    `<tr>
      <td>${esc(row.name)}</td>
      <td>${esc(row.request_count ?? "n/a")}</td>
      <td>${esc(row.failure_count ?? "n/a")}</td>
      <td>${esc(fmtPct(row.success_rate))}</td>
      <td>${esc(fmtNum(row.rpm))}</td>
      <td>${esc(fmtNum(row.median_ms))}</td>
      <td>${esc(fmtNum(row.avg_ms))}</td>
      <td>${esc(fmtNum(row.p95_ms))}</td>
      <td>${esc(fmtNum(row.p99_ms))}</td>
      <td>${esc(fmtNum(row.max_ms))}</td>
      <td>${esc(formatCounts(row.failure_classification_counts))}</td>
    </tr>`
  )).join("") || '<tr><td colspan="11" class="muted">No stats available.</td></tr>';
}

function formatCounts(value) {
  if (!value || typeof value !== "object" || !Object.keys(value).length) return "";
  return Object.entries(value).map(([key, count]) => `${key}: ${count}`).join(", ");
}

function renderSavedFiles(files) {
  $("loadResultFiles").innerHTML = files.map((file) => (
    `<a href="${esc(file.url)}" target="_blank" rel="noreferrer">${esc(file.name)}</a>`
  )).join("") || '<span class="muted">No report files yet.</span>';
}

function renderCacheResults(job) {
  renderProgress("cache", job, "No cache test job selected.");
  if (!job) {
    renderMetrics("cache", cacheEmptyMetrics());
    $("cacheStageWrap").hidden = true;
    $("cacheTrustWrap").hidden = true;
    $("cacheEffectivePlanWrap").hidden = true;
    $("cacheStageRows").innerHTML = "";
    $("cacheTrustMetrics").innerHTML = "";
    $("cacheEffectivePlan").textContent = "";
    renderFiles("cache", null);
    $("cacheLogTail").textContent = "";
    return;
  }
  renderMetrics("cache", cacheMetrics(job));
  renderCacheStageMetrics(job);
  renderCacheTrustMetrics(job);
  $("cacheEffectivePlanWrap").hidden = !job.effective_cache_plan;
  $("cacheEffectivePlan").textContent = job.effective_cache_plan
    ? JSON.stringify(job.effective_cache_plan, null, 2)
    : "";
  renderFiles("cache", job);
  $("cacheLogTail").textContent = job.log_tail || "";
}

function loadMetricSections(job) {
  const s = job.summary || {};
  const requestCount = s.business_record_count ?? s.record_count;
  const successCount = s.business_success_count;
  const failureCount = s.business_failure_count;
  const tokenUsageAvailable = Number(s.token_usage_record_count || 0) > 0;
  const adaptiveEnabled = Number(
    job.target_tokens_per_request || s.target_tokens_per_request || 0
  ) > 0;
  const streamingEnabled = job.workload === "throughput_streaming"
    || Number(s.ttft_sample_count || 0) > 0;

  const sections = [
    {
      title: "Throughput",
      description: "Success RPM counts only successful business requests; Attempted RPM includes failures.",
      metrics: [
        {
          label: "Success RPM",
          value: fmtCompact(s.business_rpm),
          hint: "Successful chat-completion requests per minute.",
          tone: "primary",
        },
        {
          label: "Attempted RPM",
          value: fmtCompact(s.attempted_business_rpm),
          hint: "All attempted business requests per minute.",
        },
        {
          label: "Success Rate",
          value: fmtPct(s.success_rate),
          hint: "Successful requests divided by attempted requests.",
          tone: Number(s.success_rate) < 0.99 ? "warning" : "",
        },
        {
          label: "HTTP 2xx Rate",
          value: fmtPct(s.http_2xx_rate),
          hint: "Transport-level 2xx among sent HTTP requests; a refusal can still be a business failure.",
          tone: Number(s.http_2xx_rate) < 0.99 ? "warning" : "",
        },
        {
          label: "Successful Requests",
          value: fmtCompact(successCount),
        },
        {
          label: "Failed Requests",
          value: fmtCompact(failureCount),
          tone: Number(failureCount || 0) > 0 ? "danger" : "",
        },
        {
          label: "HTTP 429 Rate",
          value: fmtPct(s.error_429_ratio),
          hint: "Rate-limited requests.",
          tone: Number(s.error_429_ratio || 0) > 0 ? "warning" : "",
        },
        {
          label: "HTTP 5xx Rate",
          value: fmtPct(s.error_5xx_ratio),
          tone: Number(s.error_5xx_ratio || 0) > 0 ? "danger" : "",
        },
        {
          label: "Attempted Requests",
          value: fmtCompact(requestCount),
        },
      ],
    },
    {
      title: "Latency",
      description: "Successful E2E excludes failed requests; All-request P95 includes both.",
      metrics: [
        {
          label: "Successful E2E P50",
          value: fmtDuration(s.e2e_latency_p50_ms),
        },
        {
          label: "Successful E2E P90",
          value: fmtDuration(s.e2e_latency_p90_ms),
        },
        {
          label: "Successful E2E P95",
          value: fmtDuration(s.e2e_latency_p95_ms),
          tone: "primary",
        },
        {
          label: "Successful E2E P99",
          value: fmtDuration(s.e2e_latency_p99_ms),
        },
        {
          label: "All-request P95",
          value: fmtDuration(s.p95_latency_ms),
          hint: "Includes fast failures such as 413 and 429.",
        },
      ],
    },
    {
      title: "Tokens",
      description: "Token metrics use provider usage fields; TPM is input plus output tokens per minute.",
      metrics: [
        {
          label: "Observed TPM",
          value: tokenUsageAvailable ? fmtCompact(s.total_tpm) : "n/a",
          hint: "Input + output tokens reported by usage.",
          tone: "primary",
        },
        {
          label: "Avg Total Tokens / Request",
          value: fmtCompact(s.avg_tokens_per_request),
        },
        {
          label: "P50 Total Tokens / Request",
          value: fmtCompact(s.p50_tokens_per_request),
        },
        {
          label: "P95 Total Tokens / Request",
          value: fmtCompact(s.p95_tokens_per_request),
        },
        {
          label: "Usage Coverage",
          value: fmtPct(s.token_usage_coverage),
          hint: "Requests with usable token accounting.",
        },
      ],
    },
  ];

  if (adaptiveEnabled) {
    sections[2].metrics.push(
      {
        label: "Target Total Tokens / Request",
        value: fmtCompact(job.target_tokens_per_request || s.target_tokens_per_request),
      },
      {
        label: "Token Target Deviation",
        value: fmtPct(s.tokens_per_request_deviation_ratio),
      },
      {
        label: "Adaptive State",
        value: s.adaptive_controller_status || "learning",
      },
    );
  }

  if (streamingEnabled) {
    sections.splice(2, 0, {
      title: "Streaming TTFT",
      description: "Time to first token, measured only for successful streaming requests.",
      metrics: [
        { label: "TTFT P50", value: fmtDuration(s.ttft_p50_ms) },
        { label: "TTFT P90", value: fmtDuration(s.ttft_p90_ms) },
        { label: "TTFT P95", value: fmtDuration(s.ttft_p95_ms), tone: "primary" },
        { label: "TTFT P99", value: fmtDuration(s.ttft_p99_ms) },
        {
          label: "TTFT Coverage",
          value: fmtTtftCoverage(s),
          hint: "TTFT samples / successful requests.",
        },
      ],
    });
  }

  sections.push({
    title: "Run",
    metrics: [
      { label: "Job Status", value: job.status || "n/a" },
      { label: "Exit Code", value: job.returncode ?? "running" },
      { label: "Model Family", value: job.model_family || "n/a" },
      { label: "Workload", value: job.workload || "n/a" },
      {
        label: "RPM Cap / Goal",
        value: Number(job.target_rpm || 0) > 0 ? fmtCompact(job.target_rpm) : "unlimited",
      },
      {
        label: "TPM Cap / Goal",
        value: Number(job.target_tpm || 0) > 0 ? fmtCompact(job.target_tpm) : "unlimited",
      },
    ],
  });
  return sections;
}

function renderAdaptiveNotice(prefix, summary, job) {
  const node = $(`${prefix}AdaptiveNotice`);
  if (!node) return;
  if (!job || !Number(job.target_tokens_per_request || (summary && summary.target_tokens_per_request) || 0)) {
    node.textContent = "";
    node.className = "notice";
    return;
  }
  const messages = [];
  const state = summary && summary.adaptive_controller_status;
  if (state === "learning") messages.push("自适应控制器正在学习 provider usage，至少需要 20 个有效样本。");
  if (state === "usage_degraded") messages.push("usage 覆盖率低于 90%，长度校准可信度下降。");
  if (state === "off_target") messages.push("平均 tokens/request 偏离目标超过 10%。");
  if (summary && summary.adaptive_context_clamped_count) {
    messages.push(`${summary.adaptive_context_clamped_count} 个请求因上下文窗口安全上限被截断。`);
  }
  (summary && summary.adaptive_warnings || []).forEach((message) => messages.push(message));
  if (!messages.length) {
    messages.push(`自适应长度控制正常；上下文窗口 ${fmtCompact(job.context_window_tokens || summary.adaptive_context_window_tokens)}。`);
  }
  node.textContent = messages.join(" ");
  node.className = "notice active";
}

function paramTestMetrics(job) {
  const results = referenceParamResults(job.param_results);
  const passed = results.filter((row) => caseOverallPass(row) === true).length;
  const failed = results.filter((row) => caseOverallPass(row) === false).length;
  const incompatible = results.filter((row) => row.status === "incompatible").length;
  const total = (job.verdict && job.verdict.total) || (job.progress && job.progress.total_cells) || results.length;
  const successRate = total ? passed / Number(total) : null;
  const audit = tokenAuditSummary(job);
  const identity = job && job.verdict && job.verdict.model_identity_summary;
  return [
    ["Cells complete", results.length],
    ["Overall success rate", fmtPct(successRate)],
    ["Pass cells", passed],
    ["Incompatible cells", incompatible],
    ["Fail cells", failed],
    ["Total", total || "waiting"],
    ["Return code", job.returncode ?? "running"],
    ["Contract", job.reference_label || job.reference_contract_id || job.reference_source || "waiting"],
    ["Tool validation", job.tool_validation_mode || "auto"],
    ["Token validation gate", tokenValidationGateLabel(job, audit)],
    ["Validation status", auditStatusLabel(audit && audit.validation_status)],
    ["Validated / required exchanges", tokenExchangeCountLabel(audit)],
    ["Missing usage", audit ? audit.missing_usage_count ?? "n/a" : "n/a"],
    ["Gross failures / partial", tokenGrossCountLabel(audit)],
    ["Exact coverage", fmtPct(audit && audit.coverage)],
    ["Exact accuracy", tokenExactAccuracyLabel(audit)],
    ["Exact mismatches", audit ? audit.exact_mismatch_count ?? audit.failed_dimensions ?? "n/a" : "n/a"],
    ["Model identity", identity ? identity.status : "n/a"],
    ["Identity gate", job && job.verdict && job.verdict.model_identity_pass === false ? "FAIL" : (job && job.verdict && job.verdict.model_identity_pass === true ? "PASS" : "n/a")],
    ["Thinking tokens", audit && audit.thinking_tokens !== null ? fmtNum(audit.thinking_tokens) : "n/a"],
    ["Thinking advisory", audit && audit.advisory_thinking_tokens !== null ? fmtNum(audit.advisory_thinking_tokens) : "n/a"],
    ["Thinking share", fmtPct(audit && audit.thinking_share)],
  ];
}

function cacheMetrics(job) {
  const s = job.summary || {};
  const cp = job.cache_progress || {};
  if (s.cache_evaluation_policy === "scenario_expectations_v1") {
    const controls = s.cache_control_metrics || {};
    return [
      ["缓存 token 命中率", fmtPct(s.cached_input_token_ratio)],
      ["命中请求比例", fmtPct(s.cache_hit_request_ratio)],
      ["测量覆盖率", fmtPct(s.cache_measurement_coverage)],
      ["缓存读取 token", fmtNum(s.cached_input_tokens)],
      ["总输入 token", fmtNum(s.customer_input_tokens)],
      ["正例命中请求比例", fmtPct((controls.positive_long_prefix || {}).cache_hit_request_ratio)],
      ["负例意外命中比例", fmtPct((controls.negative_unique_prefix || {}).cache_hit_request_ratio)],
      ["前缀复用效率（诊断）", fmtPct(s.prefix_cache_hit_rate ?? s.cache_efficiency)],
      ["缺失读数", fmtNum(s.cache_missing_measurement_count)],
      ["异常读数", fmtNum(s.cache_invalid_measurement_count)],
      ["控制判定", s.cache_expectation_status || s.cache_usage_accuracy_status || "n/a"],
      ["Phase", cp.phase || "waiting"],
      ["Family", job.model_family],
    ];
  }
  const scenario = (job.effective_cache_plan && job.effective_cache_plan.scenario)
    || job.cache_result_scenario
    || "legacy";
  const schema = Number(job.cache_result_schema_version || 0);
  const progressivePlan = job.effective_cache_plan || {};
  const semantics = scenario === "kilocode_agent_session" || schema >= 11
    ? "v11 agent session"
    : schema >= 10 || (
      scenario === "progressive_customer_session"
      && progressivePlan.structure_probe
      && progressivePlan.structure_probe.enabled
    )
      ? "v10 structural efficiency"
      : schema >= 9 || scenario === "progressive_customer_session"
        ? "v9 progressive"
        : schema === 8 || scenario === "customer_tool_flow" ? "v8 diagnostic" : "legacy";
  if (semantics === "v11 agent session") {
    return [
      ["Result semantics", semantics],
      ["Cached input token ratio", fmtPct(s.cached_input_token_ratio ?? s.cache_hit_rate)],
      ["Hit request ratio", fmtPct(s.cache_hit_request_ratio)],
      ["Measurement coverage", fmtPct(s.cache_measurement_coverage)],
      ["Steps", `${s.kilocode_step_success_count ?? "?"}/${s.kilocode_step_count ?? "?"}`],
      ["Positive control", fmtPct((s.cache_control_metrics && s.cache_control_metrics.positive_long_prefix || {}).cached_input_token_ratio)],
      ["Negative control", fmtPct((s.cache_control_metrics && s.cache_control_metrics.negative_unique_prefix || {}).cached_input_token_ratio)],
      ["Planned / actual", `${(job.effective_cache_plan && job.effective_cache_plan.estimated_request_count) || "?"} / ${job.cache_actual_request_count ?? s.record_count ?? "running"}`],
      ["Phase", cp.phase || (job.progress && job.progress.detail) || "waiting"],
      ["Return code", job.returncode ?? "running"],
      ["Family", job.model_family],
    ];
  }
  if (semantics === "v10 structural efficiency" || semantics === "v9 progressive") {
    return [
      ["Result semantics", semantics],
      ["结构上限", fmtPct(s.structural_hit_rate_ceiling)],
      ["实际命中率", fmtPct(s.actual_cache_hit_rate ?? s.cached_input_token_ratio)],
      ["缓存效率", fmtPct(s.cache_efficiency)],
      ["效率状态", s.cache_efficiency_status || "n/a"],
      ["Hit request ratio", fmtPct(s.cache_hit_request_ratio)],
      ["Progressive prefix reuse", fmtPct(s.progressive_prefix_reuse_rate)],
      ["Measurement coverage", fmtPct(s.cache_measurement_coverage)],
      ["Session completion", fmtPct(s.session_completion_ratio)],
      ["Tool flow supported", fmtPct(s.tool_flow_supported_session_ratio)],
      ["Customer success", s.cache_customer_request_count ? fmtPct(s.cache_customer_success_count / s.cache_customer_request_count) : "n/a"],
      ["Planned / actual", `${(job.effective_cache_plan && job.effective_cache_plan.estimated_request_count) || "?"} / ${job.cache_actual_request_count ?? s.record_count ?? "running"}`],
      ["Phase", cp.phase || (job.progress && job.progress.detail) || "waiting"],
      ["Return code", job.returncode ?? "running"],
      ["Family", job.model_family],
    ];
  }

  const cases = s.cache_case_metrics || {};
  const hitLabel = s.cache_hit_rate_semantics
    ? "Cached input token ratio"
    : "Legacy reusable-prefix hit rate";
  return [
    ["Cache progress", cp.label || (job.progress && job.progress.label) || job.status],
    ["Phase", cp.phase || (job.progress && job.progress.detail) || "waiting"],
    ["Result semantics", semantics],
    ["Scenario", scenario],
    ["Planned requests", (job.effective_cache_plan && job.effective_cache_plan.estimated_request_count) || job.cache_measured_requests || "n/a"],
    [hitLabel, fmtPct(s.cached_input_token_ratio ?? s.cache_hit_rate)],
    ["Tool follow-up reuse", fmtPct(s.tool_followup_reuse_rate)],
    ["Direct cached input", fmtPct((cases.direct_varying_user || {}).cached_input_token_ratio)],
    ["Tool initial cached input", fmtPct((cases.tool_initial || {}).cached_input_token_ratio)],
    ["Tool follow-up cached input", fmtPct((cases.tool_followup || {}).cached_input_token_ratio)],
    ["Hit request ratio", fmtPct(s.cache_hit_request_ratio)],
    ["Measurement coverage", fmtPct(s.cache_measurement_coverage)],
    ["Customer success rate", s.cache_customer_request_count ? fmtPct(s.cache_customer_success_count / s.cache_customer_request_count) : fmtPct(s.success_rate)],
    ["Records", s.record_count ?? "waiting"],
    ["Return code", job.returncode ?? "running"],
    ["Family", job.model_family],
  ];
}

function renderCacheStageMetrics(job) {
  const stages = (job.summary && job.summary.cache_stage_metrics) || {};
  const order = ["seed", "direct_growth", "tool_initial", "tool_followup", "final_growth"];
  const labels = {
    seed: "Seed",
    direct_growth: "Direct growth",
    tool_initial: "Tool initial",
    tool_followup: "Tool follow-up",
    final_growth: "Final growth",
  };
  const available = order.some((stage) => stages[stage]);
  $("cacheStageWrap").hidden = !available;
  $("cacheStageRows").innerHTML = available
    ? order.map((stage) => {
      const row = stages[stage] || {};
      return `<tr>
        <td>${esc(labels[stage])}</td>
        <td>${esc(row.request_count ?? 0)}</td>
        <td>${esc(row.success_count ?? 0)}</td>
        <td>${esc(fmtPct(row.measurement_coverage))}</td>
        <td>${esc(fmtNum(row.input_tokens))}</td>
        <td>${esc(fmtNum(row.structural_cacheable_prefix_tokens))}</td>
        <td>${esc(fmtPct(row.structural_hit_rate_ceiling))}</td>
        <td>${esc(fmtNum(row.cached_input_tokens))}</td>
        <td>${esc(fmtPct(row.cached_input_token_ratio))}</td>
        <td>${esc(fmtPct(row.cache_efficiency))}</td>
        <td>${esc(fmtPct(row.cache_hit_request_ratio))}</td>
      </tr>`;
    }).join("")
    : "";
}

function renderCacheTrustMetrics(job) {
  const s = job.summary || {};
  const controls = s.cache_control_metrics || {};
  const positive = controls.positive_long_prefix || {};
  const negative = controls.negative_unique_prefix || {};
  const hasControls = Object.keys(controls).length > 0;
  const isProgressive = (job.effective_cache_plan && job.effective_cache_plan.scenario) === "progressive_customer_session";
  $("cacheTrustWrap").hidden = !hasControls && !isProgressive;
  $("cacheTrustMetrics").innerHTML = [
    ["cached_tokens accuracy", s.cache_usage_accuracy_status || "n/a"],
    ["Accuracy coverage", fmtPct(s.cache_usage_accuracy_coverage)],
    ["Control group coverage", fmtPct(s.cache_control_group_coverage)],
    ["Control usage coverage", fmtPct(s.cache_control_usage_coverage)],
    ["Over-reported tokens", fmtNum(s.cache_usage_accuracy_excess_tokens)],
    ["Accuracy failures", s.cache_usage_accuracy_failure_count ?? "n/a"],
    ["Failure reasons", (s.cache_usage_accuracy_failures || []).join("; ") || "none"],
    ["Official usage coverage", fmtPct(s.cache_measurement_coverage)],
    ["Structure ceiling coverage", fmtPct(s.structure_ceiling_measurement_coverage)],
    ["Structure probe tokens", fmtNum(s.structure_probe_input_tokens)],
    ["Positive control", fmtPct(positive.cached_input_token_ratio)],
    ["Negative control", fmtPct(negative.cached_input_token_ratio)],
    ["Positive pairs", positive.pair_count ?? "n/a"],
    ["Tool unsupported sessions", s.tool_flow_unsupported_session_count ?? "n/a"],
    ["Latency evidence only", fmtPct(job.verdict && job.verdict.latency_speedup_ratio)],
  ].map(([label, value]) => (
    `<div class="metric"><div>${esc(label)}</div><div>${esc(value)}</div></div>`
  )).join("");
}

function paramEmptyMetrics() {
  return [
    ["Cells complete", "n/a"],
    ["Overall success rate", "n/a"],
    ["Pass cells", "n/a"],
    ["Incompatible cells", "n/a"],
    ["Fail cells", "n/a"],
    ["Token validation gate", "n/a"],
    ["Validation status", "n/a"],
    ["Validated / required exchanges", "n/a"],
    ["Missing usage", "n/a"],
    ["Gross failures / partial", "n/a"],
    ["Exact coverage", "n/a"],
    ["Exact accuracy", "n/a"],
    ["Exact mismatches", "n/a"],
    ["Model identity", "n/a"],
    ["Identity gate", "n/a"],
    ["Thinking tokens", "n/a"],
    ["Thinking advisory", "n/a"],
    ["Thinking share", "n/a"],
  ];
}

function loadEmptyMetricSections() {
  return [
    {
      title: "Throughput",
      metrics: [
        { label: "Success RPM", value: "n/a", tone: "primary" },
        { label: "Attempted RPM", value: "n/a" },
        { label: "Success Rate", value: "n/a" },
      ],
    },
    {
      title: "Latency",
      metrics: [
        { label: "Successful E2E P50", value: "n/a" },
        { label: "Successful E2E P95", value: "n/a" },
        { label: "Successful E2E P99", value: "n/a" },
      ],
    },
    {
      title: "Tokens",
      metrics: [
        { label: "Observed TPM", value: "n/a", tone: "primary" },
        { label: "Avg Total Tokens / Request", value: "n/a" },
        { label: "Usage Coverage", value: "n/a" },
      ],
    },
  ];
}

function cacheEmptyMetrics() {
  return [
    ["Cache hit rate", "n/a"],
    ["Success rate", "n/a"],
    ["Records", "n/a"],
    ["Phase", "n/a"],
  ];
}

function renderParamMatrix(job) {
  const spec = appState.paramSpec || {};
  const specRows = spec.params || spec.comparison || [];
  const results = referenceParamResults(job && job.param_results);
  $("paramResultsHead").innerHTML = "<tr><th>Contract Parameter</th><th>Runs</th></tr>";
  if (!specRows.length) {
    $("paramResults").innerHTML = '<tr><td colspan="2" class="muted">Loading reference parameters.</td></tr>';
    renderFailedCaseLog(job, results);
    return;
  }

  const runs = job ? paramRunCount(job, results) : paramTestRunsValue();
  $("paramResults").innerHTML = specRows.map((row) => {
    const profiles = Array.isArray(row.test_profiles) ? row.test_profiles : [];
    const chips = Array.from({ length: runs }, (_, index) => {
      const runIndex = index + 1;
      const matching = results.filter((item) => {
        if (Number(item.run_index || 1) !== runIndex) return false;
        if (row.coverage_mode === "all_profiles" || row.coverage_mode === "selection") return true;
        return profiles.includes(item.profile);
      });
      const summary = parameterRunSummary(row, matching, spec.test_profiles || []);
      const status = summary.status;
      const detail = matching.map((item) => `${item.profile}:${item.status}`).join(", ");
      const title = detail || row.coverage || summary.title || status;
      return `<span class="run-chip status-${esc(status)}" title="${esc(title)}">R${runIndex}:${esc(summary.label)}</span>`;
    }).join("");
    const profileLabel = parameterCoverageLabel(row, profiles, spec.test_profiles || []);
    const rowClass = isSafetyParameter(row.parameter) ? "safety-param-row" : "";
    return `<tr class="${rowClass}"><td><b>${esc(row.parameter)}</b><div class="muted">${esc(profileLabel || "not mapped")}</div></td><td><div class="run-list">${chips}</div></td></tr>`;
  }).join("");
  renderFailedCaseLog(job, results);
}

function renderTokenAudit(job) {
  const rows = tokenAuditRows(job);
  const body = $("paramTokenAudit");
  if (!body) return;
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="6" class="muted">No token audit data</td></tr>';
    return;
  }
  body.innerHTML = rows.map((row) => {
    const input = row.input || {};
    const output = row.output || {};
    const accounting = row.usage_accounting || {};
    const presence = row.usage_presence || {};
    const gross = row.gross_plausibility || {};
    const inputAccuracy = row.input_accuracy || {
      status: "not_available",
      reported_tokens: input.compared_tokens ?? input.reported_tokens,
      independent_tokens: input.estimated_tokens,
      delta: null,
      evidence_level: "legacy_estimate",
      note: "legacy estimate is display-only",
    };
    const outputAccuracy = row.output_accuracy || {
      status: "not_available",
      reported_tokens: output.reported_total_tokens,
      independent_tokens: output.estimated_visible_output_tokens,
      delta: null,
      evidence_level: "legacy_estimate",
      note: "legacy estimate is display-only",
    };
    const arithmetic = row.usage_arithmetic || {
      status: accounting.errors && accounting.errors.length ? "fail" : "not_available",
      errors: accounting.errors || [],
    };
    const reported = row.reported || accounting;
    const inputGross = gross.input || {
      status: input.status || "not_available",
      reported_tokens: input.compared_tokens ?? input.reported_tokens,
      estimated_tokens: input.estimated_tokens,
      minimum_plausible_tokens: input.expected_min,
      maximum_plausible_tokens: input.expected_max,
      note: input.note,
    };
    const outputGross = gross.output || {
      status: output.status || "not_available",
      reported_tokens: output.reported_total_tokens,
      estimated_tokens: output.estimated_visible_output_tokens,
      minimum_plausible_tokens: output.expected_total_min,
      maximum_plausible_tokens: output.expected_total_max,
      note: output.note,
    };
    const validationStatus = row.validation_status
      || (row.validation_pass === false ? "fail" : "not_available");
    const currentPass = tokenExchangeValidationPass(row);
    const validationGate = currentPass === true
      ? "PASS"
      : currentPass === false
        ? "FAIL"
        : "UNVERIFIED (legacy)";
    const validationFailures = (row.validation_failures || []).join("; ");
    const sources = [input.source, output.source, output.thinking_source].filter(Boolean).join(" · ");
    return `<tr>
      <td><b>${esc(row.profile || "unknown")}</b><div class="muted">R${esc(row.run_index || 1)} · ${esc(row.exchange || "initial")}</div></td>
      <td class="status-${esc(auditStatusClass(inputGross.status))}">${auditTokenDimension("Input", inputGross, inputAccuracy)}</td>
      <td class="status-${esc(auditStatusClass(outputGross.status))}">${auditTokenDimension("Output", outputGross, outputAccuracy)}${auditOutputCompletion(row)}</td>
      <td class="status-${esc(auditStatusClass(presence.status))}">${auditUsagePresence(row, presence)}<div class="muted">answer ${esc(fmtNum(reported.answer_tokens))} · thinking ${esc(fmtNum(reported.thinking_tokens))} · image ${esc(fmtNum(reported.image_tokens))} · cached ${esc(fmtNum(reported.cached_tokens ?? reported.cache_tokens))} · total ${esc(fmtNum(reported.total_tokens))}</div></td>
      <td class="status-${esc(auditStatusClass(arithmetic.status))}"><b>${esc(auditStatusLabel(arithmetic.status))}</b><div class="muted">${esc((arithmetic.errors || []).join("; ") || "input + output = total; components are inclusive")}</div></td>
      <td class="status-${esc(auditStatusClass(validationStatus))}"><b>validation ${esc(auditStatusLabel(validationStatus))} · gate ${esc(validationGate)}</b><div class="muted">evidence ${esc(auditStatusLabel(row.status))} · ${esc(row.evidence_level || "unavailable")}</div><div class="muted" title="${esc(sources)}">${esc(validationFailures || sources || "No validation failures")}</div></td>
    </tr>`;
  }).join("");
}

function auditTokenDimension(label, gross, accuracy) {
  const reported = gross.reported_tokens == null ? "n/a" : fmtNum(gross.reported_tokens);
  const estimate = gross.estimated_tokens == null ? "n/a" : fmtNum(gross.estimated_tokens);
  const minimum = gross.minimum_plausible_tokens;
  const maximum = gross.maximum_plausible_tokens;
  const range = minimum == null || maximum == null
    ? "n/a"
    : `${fmtNum(minimum)}–${fmtNum(maximum)}`;
  const note = gross.note || "within the gross plausibility envelope";
  return `<b>${esc(label)} gross ${esc(auditStatusLabel(gross.status))}</b><div class="muted">reported ${esc(reported)} · estimate ${esc(estimate)} · range ${esc(range)}</div><div class="muted">${esc(note)}</div>${auditAccuracyDimension(label, accuracy)}`;
}

function auditAccuracyDimension(label, accuracy) {
  const reported = accuracy.reported_tokens == null ? "n/a" : fmtNum(accuracy.reported_tokens);
  const independent = accuracy.independent_tokens == null ? "n/a" : fmtNum(accuracy.independent_tokens);
  const delta = accuracy.delta == null ? "n/a" : fmtNum(accuracy.delta);
  return `<div class="muted">exact ${esc(label.toLowerCase())} ${esc(auditStatusLabel(accuracy.status))} · reported ${esc(reported)} · independent ${esc(independent)} · delta ${esc(delta)} · ${esc(accuracy.evidence_level || "unavailable")}</div>`;
}

function auditUsagePresence(row, presence) {
  const required = presence.required === true || row.usage_required === true;
  const requirement = required ? "REQUIRED" : "NOT REQUIRED";
  const input = presence.input_present === true
    ? "present"
    : presence.input_present === false
      ? "missing"
      : "unknown";
  const output = presence.output_present === true
    ? "present"
    : presence.output_present === false
      ? "missing"
      : "unknown";
  const missing = (presence.missing_fields || []).join(", ");
  const note = presence.note || (missing ? `missing ${missing}` : "input/output usage fields observed");
  return `<b>usage ${esc(requirement)} · ${esc(auditStatusLabel(presence.status))}</b><div class="muted">input ${esc(input)} · output ${esc(output)}${missing ? ` · missing ${esc(missing)}` : ""}</div><div class="muted">${esc(note)}</div>`;
}

function tokenAuditSources(job) {
  if (!job) return [];
  const results = referenceParamResults(job.param_results);
  const probe = job.verdict && job.verdict.identity_probe;
  return probe ? [{ ...probe, profile: "identity_probe", run_index: 0 }, ...results] : results;
}

function tokenAuditRows(job) {
  return tokenAuditSources(job).flatMap((result) => {
    const exchanges = result && result.token_audit && Array.isArray(result.token_audit.exchanges)
      ? result.token_audit.exchanges
      : [];
    return exchanges.map((exchange) => ({
      profile: result.profile,
      run_index: result.run_index,
      ...exchange,
    }));
  });
}

function modelIdentityRows(job) {
  if (!job) return [];
  const results = referenceParamResults(job.param_results);
  const probe = job.verdict && job.verdict.identity_probe;
  const sources = probe ? [{ ...probe, profile: "identity_probe", run_index: 0 }, ...results] : results;
  return sources.flatMap((result) => {
    const exchanges = result && result.model_identity_audit && Array.isArray(result.model_identity_audit.exchanges)
      ? result.model_identity_audit.exchanges
      : [];
    return exchanges.map((exchange) => ({
      profile: result.profile,
      run_index: result.run_index,
      ...exchange,
    }));
  });
}

function renderModelIdentity(job) {
  const body = $("paramModelIdentity");
  if (!body) return;
  const rows = modelIdentityRows(job);
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="6" class="muted">No model identity data</td></tr>';
    return;
  }
  body.innerHTML = rows.map((row) => {
    const signals = (row.evidence || []).map((item) => {
      if (item.kind === "response_model") return `${item.source}=${item.value}`;
      if (item.kind === "system_fingerprint") return `fingerprint=${item.value}`;
      if (item.kind === "response_header") return `${item.name}=${item.value}`;
      if (item.kind === "protocol_fingerprint") return `protocol=${item.status}`;
      return "";
    }).filter(Boolean);
    const conflicts = row.conflicts || [];
    return `<tr>
      <td><b>${esc(row.profile || "unknown")}</b><div class="muted">R${esc(row.run_index ?? 1)} · ${esc(row.exchange || "initial")}</div></td>
      <td><b>${esc(row.requested_model || "n/a")}</b><div class="muted">allowed: ${esc((row.allowed_identities || []).join(", ") || "exact only")}</div></td>
      <td>${esc(row.returned_model || "N/A")}</td>
      <td>${esc(row.transport || "n/a")}<div class="muted">${esc(row.backend || "unknown")}</div></td>
      <td class="status-${esc(auditStatusClass(row.status))}"><b>${esc(String(row.status || "unverifiable").toUpperCase())}</b><div class="muted">confidence ${esc(row.confidence || "low")}</div></td>
      <td><div>${esc(signals.join(" · ") || "No independent response identity signal")}</div><div class="muted">${esc(conflicts.join("; ") || "No conflicts")}</div></td>
    </tr>`;
  }).join("");
}

function isResponsesImageAudit(audit) {
  return !!audit && audit.schema_version === 1
    && audit.policy === "openai_responses_image_usage_and_estimate_v1";
}

function responsesImageExchangePass(row) {
  if (!isResponsesImageAudit(row)) return null;
  if (row.validation_pass === false || !Array.isArray(row.validation_failures)
      || row.validation_failures.length) return false;
  const integrity = row.request_integrity || {};
  const hashes = [integrity.expected_sha256, integrity.actual_sha256, integrity.wire_sha256];
  if (row.validation_pass !== true || integrity.status !== "pass"
      || hashes.some((value) => typeof value !== "string" || !/^[0-9a-f]{64}$/.test(value))
      || new Set(hashes).size !== 1) return null;
  const checks = row.checks || {};
  if (row.usage_required === false) {
    return row.validation_status === "not_applicable"
      && (checks.rejection || {}).parameter_rejection_proven === true;
  }
  if (row.usage_required !== true || row.validation_status !== "pass") return false;
  const aggregate = checks.aggregate_image_usage;
  const individual = checks.individual_image_usage || [];
  const imageCheck = (item) => item && item.pass === true
    && item.image_output_token_accuracy_pass === true && item.exact_output_count_verified === false;
  return (checks.mainline_usage || {}).pass === true
    && (aggregate ? imageCheck(aggregate) : individual.length > 0 && individual.every(imageCheck));
}

function responsesImageGateInfo(job, audit) {
  const summary = (job && job.image_summary) || {};
  const validation = summary.result_validation || {};
  if (audit.pass === false || summary.token_validation_pass === false
      || audit.validation_failure_count > 0) return { pass: false, source: "image_policy" };
  const numbers = [audit.exchange_count, audit.required_exchange_count,
    audit.validated_exchange_count, audit.expected_rejection_count, audit.validation_failure_count];
  const caseAudits = audit.case_audits || [];
  const complete = numbers.every((value) => Number.isInteger(value) && value >= 0)
    && audit.exchange_count > 0 && audit.required_exchange_count <= audit.exchange_count
    && audit.validated_exchange_count === audit.required_exchange_count
    && audit.expected_rejection_count === audit.exchange_count - audit.required_exchange_count
    && audit.validation_status === (audit.required_exchange_count > 0 ? "pass" : "not_applicable")
    && audit.validation_failure_count === 0 && Array.isArray(caseAudits)
    && caseAudits.length === audit.exchange_count
    && caseAudits.every((item) => isResponsesImageAudit(item) && item.validation_pass === true
      && Array.isArray(item.exchanges) && item.exchanges.length === 1
      && responsesImageExchangePass(item.exchanges[0]) === true);
  if (!complete || !job || job.type !== "image_param_test"
      || validation.validation_policy !== audit.policy || validation.current !== true
      || validation.identity_match !== true || validation.token_audit_summary_current !== true
      || validation.token_validation_pass !== true || summary.token_validation_pass !== true) {
    return { pass: null, source: "incomplete" };
  }
  return { pass: audit.pass === true, source: audit.required_exchange_count === 0 ? "image_rejection" : "image_policy" };
}

function tokenExchangeValidationPass(row) {
  if (isResponsesImageAudit(row)) return responsesImageExchangePass(row);
  if (row.validation_pass === false || row.count_request_integrity === "fail"
      || (row.validation_failures || []).length) return false;
  if (Number(row.schema_version || 0) !== 4) return null;
  if (row.validation_pass !== true) return false;
  if (row.usage_required === false) return row.validation_status === "not_applicable";
  const gross = row.gross_plausibility || {};
  return row.usage_required === true
    && row.validation_status === "pass"
    && (row.usage_presence || {}).status === "pass"
    && (row.usage_arithmetic || {}).status === "pass"
    && gross.status === "pass"
    && (gross.input || {}).status === "pass"
    && (gross.output || {}).status === "pass"
    && (row.output_completion || {}).status === "pass"
    && (row.input_accuracy || {}).status !== "fail"
    && (row.output_accuracy || {}).status !== "fail";
}

function tokenAuditSummary(job) {
  const verdictSummary = job && job.verdict && job.verdict.token_audit_summary;
  const hasValidationSummary = verdictSummary && (
    Number(verdictSummary.schema_version || 0) === 4
    || verdictSummary.validation_status !== undefined
    || verdictSummary.required_exchange_count !== undefined
  );
  if (verdictSummary && (hasValidationSummary || Number(verdictSummary.exchange_count || 0) > 0)) {
    return verdictSummary;
  }
  const rows = tokenAuditRows(job);
  if (!rows.length) return null;
  const missingAuditResults = tokenAuditSources(job).filter((result) => (
    !Array.isArray((result.token_audit || {}).exchanges)
    || !result.token_audit.exchanges.length
  ));
  const invalidAuditResults = tokenAuditSources(job).filter((result) => (
    Array.isArray((result.token_audit || {}).exchanges)
    && result.token_audit.exchanges.length
    && result.token_audit.validation_pass !== true
  ));
  const dimensions = rows.flatMap((row) => (
    row.input_accuracy || row.output_accuracy
      ? [row.input_accuracy || {}, row.output_accuracy || {}]
      : [row.input || {}, row.output || {}]
  ));
  const exactDimensions = dimensions.filter((item) => (
    item.evidence_level === "exact" && ["pass", "fail"].includes(item.status)
  ));
  const exactPassed = exactDimensions.filter((item) => item.status === "pass").length;
  const exactFailed = exactDimensions.filter((item) => item.status === "fail").length;
  const requiredRows = rows.filter((row) => (
    row.usage_required === true || (row.usage_presence || {}).required === true
  ));
  const validatedRows = requiredRows.filter((row) => tokenExchangeValidationPass(row) === true);
  const validationFailures = rows.filter((row) => tokenExchangeValidationPass(row) !== true);
  const missingUsage = requiredRows.filter((row) => (
    (row.usage_presence || {}).status !== "pass"
  ));
  const grossFailures = requiredRows.filter((row) => (
    (row.gross_plausibility || {}).status === "fail"
  ));
  const grossPartial = requiredRows.filter((row) => (
    (row.gross_plausibility || {}).status === "partial"
  ));
  const thinkingRows = rows.filter((row) => {
    const value = (row.usage_accounting || {}).thinking_tokens;
    return value !== null && value !== undefined;
  });
  const thinkingTokens = thinkingRows.reduce((sum, row) => sum + Number((row.usage_accounting || {}).thinking_tokens || 0), 0);
  const thinkingOutputTokens = thinkingRows.reduce((sum, row) => sum + Number((row.usage_accounting || {}).output_tokens || 0), 0);
  const advisoryThinking = rows
    .filter((row) => {
      const accounting = row.usage_accounting || {};
      return (accounting.thinking_tokens === null || accounting.thinking_tokens === undefined)
        && accounting.details_advisory
        && accounting.details_advisory.reasoning_tokens !== null
        && accounting.details_advisory.reasoning_tokens !== undefined;
    })
    .reduce((sum, row) => sum + Number((row.usage_accounting.details_advisory || {}).reasoning_tokens || 0), 0);
  return {
    schema_version: Math.max(...rows.map((row) => Number(row.schema_version || 0))),
    validation_status: validationFailures.length || missingAuditResults.length || invalidAuditResults.length
      ? "fail"
      : grossPartial.length
        ? "partial"
        : requiredRows.length
          ? "pass"
          : "not_applicable",
    pass: validationFailures.length === 0 && missingAuditResults.length === 0 && invalidAuditResults.length === 0,
    exchange_count: rows.length,
    required_exchange_count: requiredRows.length,
    validated_exchange_count: validatedRows.length,
    validation_failure_count: validationFailures.length,
    missing_usage_count: missingUsage.length,
    gross_check_count: requiredRows.filter((row) => row.gross_plausibility).length,
    gross_failure_count: grossFailures.length,
    gross_partial_count: grossPartial.length,
    arithmetic_check_count: rows.filter((row) => row.usage_arithmetic).length,
    arithmetic_failure_count: rows.filter((row) => (row.usage_arithmetic || {}).status === "fail").length,
    completion_check_count: requiredRows.filter((row) => row.output_completion).length,
    completion_failure_count: requiredRows.filter((row) => (row.output_completion || {}).status === "fail").length,
    completion_unverified_count: requiredRows.filter((row) => !["pass", "fail"].includes((row.output_completion || {}).status)).length,
    missing_audit_result_count: missingAuditResults.length,
    invalid_audit_result_count: invalidAuditResults.length,
    exact_dimension_count: exactDimensions.length,
    coverage: dimensions.length ? exactDimensions.length / dimensions.length : 0,
    pass_rate: exactDimensions.length ? exactPassed / exactDimensions.length : null,
    exact_mismatch_count: exactFailed,
    mismatch_count: validationFailures.length,
    thinking_tokens: thinkingRows.length ? thinkingTokens : null,
    advisory_thinking_tokens: advisoryThinking || null,
    thinking_share: thinkingOutputTokens > 0 ? thinkingTokens / thinkingOutputTokens : null,
  };
}

function tokenValidationGateInfo(job, audit) {
  if (isResponsesImageAudit(audit)) return responsesImageGateInfo(job, audit);
  const verdict = (job && job.verdict) || {};
  const imageSummary = (job && job.image_summary) || {};
  if (verdict.token_validation_pass === false || imageSummary.token_validation_pass === false) {
    return { pass: false, source: "verdict" };
  }
  const visibleResults = [...tokenAuditSources(job), ...((job && job.image_results) || [])];
  let unverifiedVisibleExchange = false;
  for (const result of visibleResults) {
    const detail = result.token_audit || {};
    const exchanges = Array.isArray(detail.exchanges) ? detail.exchanges : [];
    if (result.token_validation_pass === false || detail.validation_pass === false
        || exchanges.some((row) => tokenExchangeValidationPass(row) === false)) {
      return { pass: false, source: "exchange" };
    }
    if (!exchanges.length || detail.validation_pass !== true
        || exchanges.some((row) => tokenExchangeValidationPass(row) !== true)) {
      unverifiedVisibleExchange = true;
    }
  }
  if (unverifiedVisibleExchange) return { pass: null, source: "incomplete" };
  if (audit && Number(audit.schema_version || 0) === 4) {
    const required = audit.required_exchange_count;
    const checked = ["validation_failure_count", "missing_usage_count", "gross_failure_count",
      "gross_partial_count", "missing_audit_result_count", "invalid_audit_result_count",
      "completion_failure_count", "completion_unverified_count", "arithmetic_failure_count"];
    const numeric = [audit.exchange_count, required, audit.validated_exchange_count,
      audit.gross_check_count, audit.completion_check_count, audit.arithmetic_check_count,
      ...checked.map((key) => audit[key])];
    if (numeric.some((value) => !Number.isInteger(value) || value < 0)) {
      return { pass: audit.pass === false ? false : null, source: "incomplete" };
    }
    const complete = audit.exchange_count > 0 && required <= audit.exchange_count
      && audit.validated_exchange_count === required
      && audit.gross_check_count === required
      && audit.completion_check_count === required
      && audit.arithmetic_check_count === audit.exchange_count
      && checked.every((key) => audit[key] === 0)
      && audit.validation_status === (required > 0 ? "pass" : "not_applicable");
    return {
      pass: audit.pass === true && complete,
      source: typeof verdict.token_validation_pass === "boolean" || typeof imageSummary.token_validation_pass === "boolean"
        ? "verdict" : "schema_v4_summary",
    };
  }
  if (verdict.token_validation_pass === true || imageSummary.token_validation_pass === true
      || (audit && audit.pass === true)) return { pass: null, source: "legacy" };
  const legacy = typeof verdict.token_accuracy_pass === "boolean"
    ? verdict.token_accuracy_pass
    : typeof imageSummary.token_accuracy_pass === "boolean"
      ? imageSummary.token_accuracy_pass
      : null;
  if (legacy !== null) {
    return { pass: legacy === false ? false : null, source: "legacy" };
  }
  return { pass: null, source: "none" };
}

function tokenValidationGateLabel(job, audit) {
  const gate = tokenValidationGateInfo(job, audit);
  if (gate.source === "image_rejection" && gate.pass === true) return "N/A (expected rejection)";
  if (gate.source === "image_policy") return gate.pass === true ? "PASS (image policy)" : "FAIL (image policy)";
  if (gate.source === "legacy") return gate.pass === false ? "FAIL (legacy)" : "UNVERIFIED (legacy)";
  if (gate.source === "incomplete" && gate.pass === null) return "UNVERIFIED (incomplete)";
  if (gate.pass === true) return gate.source === "schema_v4_summary" ? "PASS (current)" : "PASS";
  if (gate.pass === false) return gate.source === "schema_v4_summary" ? "FAIL (current)" : "FAIL";
  return "n/a";
}

function tokenExchangeCountLabel(audit) {
  if (!audit || (audit.validated_exchange_count == null && audit.required_exchange_count == null)) {
    return "n/a";
  }
  return `${audit.validated_exchange_count ?? 0}/${audit.required_exchange_count ?? 0}`;
}

function tokenGrossCountLabel(audit) {
  if (!audit || (audit.gross_failure_count == null && audit.gross_partial_count == null)) {
    return "n/a";
  }
  return `${audit.gross_failure_count ?? 0} / ${audit.gross_partial_count ?? 0}`;
}

function tokenExactAccuracyLabel(audit) {
  if (!audit) return "n/a";
  const coverage = Number(audit.coverage || 0);
  const exactDimensions = Number(audit.exact_dimension_count || 0);
  if (coverage <= 0 || exactDimensions <= 0) return "N/A (no exact evidence)";
  const mismatches = Number(audit.exact_mismatch_count ?? audit.failed_dimensions ?? 0);
  return mismatches > 0 ? "FAIL" : "PASS";
}

function imageTokenAuditDetail(result) {
  const audit = result.token_audit || {};
  const exchanges = Array.isArray(audit.exchanges) ? audit.exchanges : [];
  if (isResponsesImageAudit(audit)) {
    const exchange = exchanges[0] || {};
    const passed = responsesImageExchangePass(exchange);
    const label = passed === true ? (exchange.usage_required ? "PASS (image policy)" : "N/A (expected rejection)")
      : passed === false ? "FAIL" : "UNVERIFIED";
    const checks = exchange.checks || {};
    const mainline = (checks.mainline_usage || {}).reported_usage || {};
    const imageChecks = checks.aggregate_image_usage ? [checks.aggregate_image_usage] : (checks.individual_image_usage || []);
    const imageDetails = imageChecks.map((check) => auditDimension(
      check.scope === "aggregate_of_all_image_generation_calls" ? "Image tool output (aggregate estimate)" : "Image tool output (per-call estimate)",
      (check.reported_usage || {}).output_tokens, check.comparison_min, check.comparison_max, check.status,
    )).join("");
    return `<b>${esc(label)}</b><div class="muted">${esc((exchange.validation_failures || []).join("; "))}</div>`
      + (exchange.usage_required === false ? '<div class="muted">No generation usage required for this attributed parameter rejection.</div>'
        : `<div>Main model: input ${esc(fmtNum(mainline.input_tokens))} · output ${esc(fmtNum(mainline.output_tokens))} · total ${esc(fmtNum(mainline.total_tokens))}</div>${imageDetails}<div class="muted">Mainline and image-tool usage are separate. Exact media-input and image-output counts remain unverified.</div>`);
  }
  if (exchanges.length > 1) {
    return exchanges.map((exchange) => `<div><b>${esc(exchange.exchange || "exchange")}</b>${imageTokenAuditDetail({
      token_audit: { ...audit, exchanges: [exchange] },
      token_validation_status: exchange.validation_status,
      token_validation_pass: tokenExchangeValidationPass(exchange),
      token_validation_failures: exchange.validation_failures,
    })}</div>`).join("");
  }
  const exchange = Array.isArray(audit.exchanges) ? audit.exchanges[0] || {} : {};
  const presence = exchange.usage_presence || {};
  const gross = exchange.gross_plausibility || {};
  const input = exchange.input || {};
  const output = exchange.output || {};
  const inputAccuracy = exchange.input_accuracy || {};
  const outputAccuracy = exchange.output_accuracy || {};
  const inputGross = gross.input || {
    status: input.status || "not_available",
    reported_tokens: input.compared_tokens ?? input.reported_tokens,
    estimated_tokens: input.estimated_tokens,
    minimum_plausible_tokens: input.expected_min,
    maximum_plausible_tokens: input.expected_max,
    note: input.note,
  };
  const outputGross = gross.output || {
    status: output.status || "not_available",
    reported_tokens: output.reported_total_tokens,
    estimated_tokens: output.estimated_visible_output_tokens,
    minimum_plausible_tokens: output.expected_total_min,
    maximum_plausible_tokens: output.expected_total_max,
    note: output.note,
  };
  const validationStatus = result.token_validation_status
    || audit.validation_status
    || exchange.validation_status
    || "not_available";
  const validationPass = typeof result.token_validation_pass === "boolean"
    ? result.token_validation_pass
    : typeof audit.validation_pass === "boolean"
      ? audit.validation_pass
      : exchange.validation_pass;
  const currentPass = tokenExchangeValidationPass(exchange);
  const gate = validationPass === false || currentPass === false ? "FAIL"
    : validationPass === true && currentPass === true ? "PASS" : "UNVERIFIED";
  const failures = result.token_validation_failures
    || audit.validation_failures
    || exchange.validation_failures
    || [];
  if (!Object.keys(exchange).length) {
    return `<b>validation ${esc(auditStatusLabel(validationStatus))} · gate ${esc(gate)}</b><div class="muted">No per-exchange token audit details</div>`;
  }
  return `<b>validation ${esc(auditStatusLabel(validationStatus))} · gate ${esc(gate)}</b><div class="muted">${esc(failures.join("; ") || "No validation failures")}</div>${auditUsagePresence(exchange, presence)}<div>${auditTokenDimension("Input", inputGross, inputAccuracy)}</div><div>${auditTokenDimension("Output", outputGross, outputAccuracy)}${auditOutputCompletion(exchange)}</div>`;
}

function auditOutputCompletion(exchange) {
  const completion = exchange.output_completion || {};
  return `<div class="muted">Output completion ${esc(auditStatusLabel(completion.status))}${completion.note ? ": " + esc(completion.note) : ""}</div>`;
}

function auditDimension(label, value, expectedMin, expectedMax, status) {
  const reported = value === null || value === undefined ? "n/a" : fmtNum(value);
  const expected = expectedMin === null || expectedMin === undefined || expectedMax === null || expectedMax === undefined
    ? "n/a"
    : `${fmtNum(expectedMin)}–${fmtNum(expectedMax)}`;
  return `<b>${esc(label)}: ${esc(reported)}</b><div class="muted">expected ${esc(expected)} · ${esc(auditStatusLabel(status))}</div>`;
}

function auditBreakdown(label, value, estimate, expectedMin, expectedMax, status, qualifier) {
  const reported = value === null || value === undefined ? "n/a" : fmtNum(value);
  const estimateText = estimate === null || estimate === undefined ? "n/a" : fmtNum(estimate);
  const expected = expectedMin === null || expectedMin === undefined || expectedMax === null || expectedMax === undefined
    ? "n/a"
    : `${fmtNum(expectedMin)}–${fmtNum(expectedMax)}`;
  const suffix = qualifier && qualifier !== "none" ? ` · ${qualifier}` : "";
  return `<b>${esc(label)}: ${esc(reported)}</b><div class="muted">visible est ${esc(estimateText)} · expected ${esc(expected)} · ${esc(auditStatusLabel(status))}${esc(suffix)}</div>`;
}

function auditStatusLabel(status) {
  if (!status || status === "not_available") return "N/A";
  return String(status).toUpperCase();
}

function auditStatusClass(status) {
  return String(status || "not_available").replaceAll("_", "-");
}

function parameterCoverageLabel(row, profiles, allProfiles) {
  if (row.coverage_mode === "all_profiles") return `all profiles (${allProfiles.length})`;
  if (row.coverage_mode === "selection") return row.coverage || "provider/model selection";
  if (!profiles.length) return row.coverage;
  if (profiles.length <= 3) return profiles.join(", ");
  return `${profiles.slice(0, 3).join(", ")} +${profiles.length - 3} more`;
}

function parameterRunSummary(row, matching, allProfiles) {
  if (row.coverage_mode === "not_tested") return { status: "not-tested", label: "n/t" };
  if (!matching.length) return { status: "waiting", label: "-" };
  const tokenFailures = matching.filter((item) => (
    item.token_validation_pass === false
    || item.overall_status === "token_validation_failed"
    || (((item.token_audit || {}).exchanges) || []).some((exchange) => tokenExchangeValidationPass(exchange) === false)
  ));
  if (tokenFailures.length) {
    return {
      status: "fail",
      label: "token fail",
      title: `${tokenFailures.length} request(s) failed required token validation`,
    };
  }
  const unverifiedTokens = matching.some((item) => {
    const exchanges = ((item.token_audit || {}).exchanges) || [];
    return !exchanges.length || item.token_audit.validation_pass !== true
      || exchanges.some((exchange) => tokenExchangeValidationPass(exchange) !== true);
  });
  if (unverifiedTokens) return {
    status: "partial", label: "token unverified",
    title: "Current input/output quantity and completion evidence is missing",
  };
  if (row.coverage_mode === "all_profiles") {
    const expected = allProfiles.length;
    const rejected = matching.filter((item) => !httpAccepted(item));
    if (rejected.length) {
      return {
        status: "fail",
        label: "fail",
        title: `${rejected.length} request(s) were not accepted by the API`,
      };
    }
    if (matching.length >= expected) return { status: "pass", label: "ok" };
    return {
      status: "partial-pass",
      label: `ok ${matching.length}/${expected}`,
      title: "completed requests were accepted; waiting for remaining mapped profiles",
    };
  }
  if (row.coverage_mode === "selection") {
    const passed = matching.some(httpAccepted);
    return { status: passed ? "pass" : "fail", label: passed ? "ok" : "fail" };
  }
  if (matching.some((item) => item.status === "fail")) return { status: "fail", label: "fail" };
  if (matching.some((item) => item.status === "incompatible")) return { status: "incompatible", label: "inc" };
  const expected = row.coverage_mode === "all_profiles"
    ? allProfiles.length
    : Math.max((row.test_profiles || []).length, 1);
  if (matching.length >= expected && matching.every((item) => item.status === "pass")) {
    return { status: "pass", label: "ok" };
  }
  if (matching.every((item) => item.status === "pass")) {
    return {
      status: "partial-pass",
      label: `ok ${matching.length}/${expected}`,
      title: "completed checks are passing; waiting for remaining mapped profiles",
    };
  }
  return {
    status: "partial",
    label: `${matching.length}/${expected}`,
    title: "partial results; waiting for remaining mapped profiles",
  };
}

function httpAccepted(item) {
  const statusCode = Number(item && item.status_code);
  if (Number.isFinite(statusCode)) return statusCode >= 200 && statusCode < 300;
  return item && item.pass === true;
}

function renderFailedCaseLog(job, results) {
  if (job && job.param_failed_cases_log) {
    $("paramFailedCaseLog").textContent = job.param_failed_cases_log;
    return;
  }
  const failed = (Array.isArray(results) ? results : []).filter((row) => (
    caseOverallPass(row) === false
    || row.status === "incompatible"
    || row.status === "fail"
    || row.status === "unexpected_acceptance"
  ));
  if (!failed.length) {
    $("paramFailedCaseLog").textContent = "No failed or incompatible parameter test cases.";
    return;
  }
  $("paramFailedCaseLog").textContent = failed.map((item, index) => [
    `===== Case ${index + 1}: ${item.overall_status || item.status} =====`,
    `profile: ${item.profile || ""}`,
    `parameter: ${item.parameter || ""}`,
    `run_index: ${item.run_index || ""}`,
    `provider/model: ${item.provider || ""} / ${item.model || ""}`,
    `contract: ${item.reference_contract_id || item.reference_source || ""} (${item.reference_family || ""})`,
    `input_sample: ${item.input_sample || ""}`,
    `status_code: ${item.status_code ?? ""}`,
    `latency_ms: ${item.latency_ms ?? ""}`,
    `failure_classification: ${item.failure_classification || ""}`,
    `failure_reason: ${item.failure_reason || item.reason || ""}`,
    `token_validation_status: ${item.token_validation_status || ""}`,
    `token_validation_pass: ${item.token_validation_pass ?? ""}`,
    `token_validation_failures: ${JSON.stringify(item.token_validation_failures || [])}`,
    `failed_check: ${item.failed_check || (item.failure_detail && item.failure_detail.failed_check) || ""}`,
    `failed_item: ${item.failed_item || (item.failure_detail && item.failure_detail.failed_item) || ""}`,
    "expected:",
    prettyJson(item.expected || (item.failure_detail && item.failure_detail.expected) || ""),
    "actual:",
    prettyJson(item.actual !== undefined ? item.actual : ((item.failure_detail && item.failure_detail.actual) || "")),
    `warnings: ${JSON.stringify(item.warnings || [])}`,
    "",
    "input:",
    prettyJson(item.input || { sample_id: item.input_sample || "" }),
    "",
    "failed_request_body:",
    prettyJson(item.failed_request_body || item.request_body || null),
    "",
    "failed_response_status_code:",
    String(item.status_code ?? ""),
    "",
    "failed_response_headers:",
    prettyJson(item.failed_response_headers || item.response_headers || {}),
    "",
    "failed_response_json:",
    prettyJson(item.failed_response_json || item.response_json || {}),
    "",
    "failed_response_raw:",
    item.failed_response_raw || item.response_raw || item.message || "",
  ].join("\n")).join("\n\n");
}

function prettyJson(value) {
  if (value === null || value === undefined) return "null";
  try {
    return JSON.stringify(value, null, 2);
  } catch (_error) {
    return String(value);
  }
}

function referenceParamResults(results) {
  if (!Array.isArray(results)) return [];
  return results.filter((row) => row.status !== "expected_unsupported");
}

function paramRunCount(job, results) {
  const fromVerdict = job.verdict && job.verdict.param_test_runs;
  const fromJob = job.param_test_runs;
  const fromRows = results.length ? Math.max(...results.map((row) => Number(row.run_index || 1))) : 1;
  return Math.max(Number(fromVerdict || fromJob || fromRows || 1), 1);
}

function syncProvider(tab) {
  const form = appState.formsByTab[tab];
  form.provider = $(`${tab}Provider`).value;
  form.model = tab === "cache"
    ? cacheSelectedModelForProvider(form.provider)
    : selectedModelForProvider(form.provider, "");
  renderModelSelect(tab);
  if (tab === "param") {
    form.workflowBindingId = "";
    form.workflowCases = "";
    form.parameterSuite = "";
    form.routeProfile = routeProfileForModel(form.provider, form.model);
    renderParamRouteProfiles();
    form.apiForm = apiFormForModel(
      form.provider, form.model, form.routeProfile
    );
    renderParamApiForms();
    form.contractManual = false;
    form.referenceContractId = referenceContractIdForModel(
      form.provider, form.model, form.routeProfile, form.apiForm
    );
    renderReferenceContracts();
    loadParamSpecs();
  }
  if (tab === "cache") {
    form.routeProfile = cacheRouteProfileForModel(form.provider, form.model);
    form.apiForm = cacheApiFormForModel(
      form.provider, form.model, form.routeProfile
    );
    renderCacheRouteProfiles();
    renderCacheApiForms();
    renderProviderStatus(tab);
    renderCacheFormState();
  }
  renderBusyState();
}

function syncModel(tab) {
  const form = appState.formsByTab[tab];
  form.model = $(`${tab}Model`).value;
  renderProviderStatus(tab);
  if (tab === "param") {
    form.workflowBindingId = "";
    form.workflowCases = "";
    form.parameterSuite = "";
    form.routeProfile = routeProfileForModel(form.provider, form.model);
    renderParamRouteProfiles();
    form.apiForm = apiFormForModel(
      form.provider, form.model, form.routeProfile
    );
    renderParamApiForms();
    form.contractManual = false;
    form.referenceContractId = referenceContractIdForModel(
      form.provider, form.model, form.routeProfile, form.apiForm
    );
    renderReferenceContracts();
    loadParamSpecs();
  }
  if (tab === "cache") {
    form.routeProfile = cacheRouteProfileForModel(form.provider, form.model);
    form.apiForm = cacheApiFormForModel(
      form.provider, form.model, form.routeProfile
    );
    renderCacheRouteProfiles();
    renderCacheApiForms();
    renderProviderStatus(tab);
    renderCacheFormState();
  }
  renderBusyState();
}

function syncImageProvider() {
  const form = appState.formsByTab.image;
  form.cases = "";
  form.provider = $("imageProvider").value;
  form.model = selectedImageModel(form.provider, "");
  const model = imageModelById(form.provider, form.model);
  form.routeProfile = imageRouteProfileForModel(form.provider, form.model);
  form.apiForm = imageApiFormForModel(form.provider, form.model, form.routeProfile);
  form.transport = (model && model.transport) || "";
  renderImageControls();
  loadLatestImageResult();
}

function syncImageModel() {
  const form = appState.formsByTab.image;
  form.cases = "";
  form.model = $("imageModel").value;
  const model = imageModelById(form.provider, form.model);
  form.routeProfile = imageRouteProfileForModel(form.provider, form.model);
  form.apiForm = imageApiFormForModel(form.provider, form.model, form.routeProfile);
  form.transport = (model && model.transport) || "";
  renderImageControls();
  loadLatestImageResult();
}

function bindEvents() {
  document.querySelectorAll(".test-tab").forEach((button) => {
    button.addEventListener("click", () => setActiveTab(button.dataset.tab));
  });

  ["param", "load", "cache"].forEach((tab) => {
    $(`${tab}Provider`).addEventListener("change", () => syncProvider(tab));
    $(`${tab}Model`).addEventListener("change", () => syncModel(tab));
  });
  $("cacheRouteProfile").addEventListener("change", () => {
    const form = appState.formsByTab.cache;
    form.routeProfile = $("cacheRouteProfile").value;
    form.apiForm = cacheApiFormForModel(form.provider, form.model, form.routeProfile);
    renderCacheApiForms();
    renderProviderStatus("cache");
    renderCacheFormState();
    renderBusyState();
  });
  $("cacheApiForm").addEventListener("change", () => {
    appState.formsByTab.cache.apiForm = $("cacheApiForm").value;
    renderProviderStatus("cache");
    renderCacheFormState();
    renderBusyState();
  });
  $("imageProvider").addEventListener("change", syncImageProvider);
  $("imageModel").addEventListener("change", syncImageModel);
  $("imageRouteProfile").addEventListener("change", () => {
    const form = appState.formsByTab.image;
    form.cases = "";
    form.routeProfile = $("imageRouteProfile").value;
    form.apiForm = "";
    form.transport = "";
    appState.imageHistoryResult = null;
    renderImageControls();
    loadLatestImageResult();
  });
  $("imageApiForm").addEventListener("change", () => {
    appState.formsByTab.image.cases = "";
    appState.formsByTab.image.apiForm = $("imageApiForm").value;
    appState.formsByTab.image.transport = "";
    renderImageControls();
    loadLatestImageResult();
  });
  $("imageRunCount").addEventListener("input", () => {
    appState.formsByTab.image.runCount = $("imageRunCount").value;
    refreshImagePlanPreview();
  });
  $("imageCases").addEventListener("input", () => {
    appState.formsByTab.image.cases = $("imageCases").value;
    refreshImagePlanPreview();
  });
  $("imageSuite").addEventListener("change", () => {
    appState.formsByTab.image.suite = $("imageSuite").value;
    renderImageControls();
  });
  $("imageQuality").addEventListener("change", () => {
    appState.formsByTab.image.quality = $("imageQuality").value;
    renderImageControls();
  });
  $("imageOutputFormat").addEventListener("change", () => {
    appState.formsByTab.image.outputFormat = $("imageOutputFormat").value;
    renderImageControls();
  });
  const imageChecks = {
    imageInclude2k: "include2k",
    imageInclude4k: "include4k",
    imageNoNegative: "noNegative",
    imageNoCrossControl: "noCrossControl",
    imageVisualForensics: "visualForensics",
  };
  Object.entries(imageChecks).forEach(([id, key]) => {
    $(id).addEventListener("change", () => {
      const form = appState.formsByTab.image;
      form[key] = $(id).checked;
      if (key === "include2k" || key === "include4k") {
        const suite = (imageApiFormCapability(form.provider, form.model, form.routeProfile, form.apiForm) || {}).suite;
        if (suite) (form.resolutionPreferences ||= {})[suite] = {include2k: form.include2k, include4k: form.include4k};
      }
      renderImageControls();
    });
  });

  $("referenceContractId").addEventListener("change", () => {
    const form = appState.formsByTab.param;
    form.referenceContractId = $("referenceContractId").value;
    form.parameterSuite = "";
    form.contractManual = true;
    renderContractMode();
    renderToolValidationMode();
    loadParamSpecs();
  });
  $("paramRouteProfile").addEventListener("change", () => {
    const form = appState.formsByTab.param;
    form.workflowBindingId = "";
    form.workflowCases = "";
    form.routeProfile = $("paramRouteProfile").value;
    form.parameterSuite = "";
    form.apiForm = "";
    form.referenceContractId = "";
    form.contractManual = false;
    appState.paramHistoryResult = null;
    renderParamApiForms();
    form.referenceContractId = referenceContractIdForModel(
      form.provider, form.model, form.routeProfile, form.apiForm
    );
    renderReferenceContracts();
    renderProviderStatus("param");
    loadParamSpecs();
  });
  $("paramApiForm").addEventListener("change", () => {
    const form = appState.formsByTab.param;
    form.workflowBindingId = "";
    form.workflowCases = "";
    form.apiForm = $("paramApiForm").value;
    form.parameterSuite = "";
    form.contractManual = false;
    form.referenceContractId = referenceContractIdForModel(
      form.provider, form.model, form.routeProfile, form.apiForm
    );
    renderReferenceContracts();
    renderProviderStatus("param");
    loadParamSpecs();
  });
  $("toolValidationMode").addEventListener("change", () => {
    const form = appState.formsByTab.param;
    form.toolValidationMode = $("toolValidationMode").value;
    renderToolValidationMode();
    loadParamSpecs();
  });
  $("resetContract").addEventListener("click", () => {
    const form = appState.formsByTab.param;
    form.contractManual = false;
    form.parameterSuite = "";
    form.referenceContractId = referenceContractIdForModel(
      form.provider, form.model, form.routeProfile, form.apiForm
    );
    renderReferenceContracts();
    renderToolValidationMode();
    loadParamSpecs();
  });
  $("paramTestRuns").addEventListener("input", () => {
    appState.formsByTab.param.paramTestRuns = $("paramTestRuns").value;
    renderParamRunHint();
    queueWorkflowPreview();
  });
  $("parameterSuite").addEventListener("change", () => {
    appState.formsByTab.param.parameterSuite = $("parameterSuite").value;
    appState.paramHistoryResult = null;
    renderParamRunHint();
    queueWorkflowPreview();
    loadParamSpecs();
  });
  $("paramTestRuns").addEventListener("change", () => {
    appState.formsByTab.param.paramTestRuns = paramTestRunsValue();
    renderParamRunHint();
    queueWorkflowPreview();
  });

  $("paramWorkflow").addEventListener("change", () => {
    changeParamWorkflow($("paramWorkflow").value);
  });
  $("paramWorkflowCases").addEventListener("input", () => {
    appState.formsByTab.param.workflowCases = $("paramWorkflowCases").value;
    queueWorkflowPreview();
  });

  $("workload").addEventListener("change", () => {
    appState.formsByTab.load.workload = $("workload").value;
    renderAdaptiveSizingHint();
  });
  $("users").addEventListener("input", () => { appState.formsByTab.load.users = $("users").value; });
  $("spawnRate").addEventListener("input", () => { appState.formsByTab.load.spawnRate = $("spawnRate").value; });
  $("duration").addEventListener("input", () => { appState.formsByTab.load.duration = $("duration").value; });
  $("requestMode").addEventListener("change", () => { appState.formsByTab.load.requestMode = $("requestMode").value; });
  $("staircaseSteps").addEventListener("input", () => {
    appState.formsByTab.load.staircaseSteps = $("staircaseSteps").value.split(",").map((item) => Number(item.trim())).filter((item) => Number.isFinite(item) && item > 0);
  });
  const loadBindings = {
    staircaseStepDuration: "staircaseStepDuration",
    staircaseSpawnRate: "staircaseSpawnRate",
    staircaseWarmupUsers: "staircaseWarmupUsers",
    staircaseWarmupDuration: "staircaseWarmupDuration",
    staircaseIncrementUsers: "staircaseIncrementUsers",
    staircaseMaxUsers: "staircaseMaxUsers",
    soakUsers: "soakUsers",
    soakSpawnRate: "soakSpawnRate",
    soakDuration: "soakDuration",
  };
  Object.entries(loadBindings).forEach(([field, id]) => {
    $(id).addEventListener("input", () => { appState.formsByTab.load[field] = $(id).value; });
  });
  $("staircaseWarmupEnabled").addEventListener("change", () => { appState.formsByTab.load.staircaseWarmupEnabled = $("staircaseWarmupEnabled").value === "true"; });
  $("staircaseAutoExtend").addEventListener("change", () => { appState.formsByTab.load.staircaseAutoExtend = $("staircaseAutoExtend").value === "true"; });
  $("targetRpm").addEventListener("input", () => {
    appState.formsByTab.load.targetRpm = $("targetRpm").value;
    renderAdaptiveSizingHint();
  });
  $("targetTpm").addEventListener("input", () => {
    appState.formsByTab.load.targetTpm = $("targetTpm").value;
    renderAdaptiveSizingHint();
  });
  const cacheBindings = {
    sessions: "cacheSessions",
    roundsPerSession: "cacheRounds",
    positivePairs: "cachePositivePairs",
    negativeRequests: "cacheNegativeRequests",
    waitAfterSeed: "cacheWaitAfterSeed",
    maxTokens: "cacheMaxTokens",
    seed: "cacheSeed",
    kilocodeSteps: "cacheKilocodeSteps",
    diagnosticPositivePairs: "cacheDiagnosticPositivePairs",
    diagnosticNegativeRequests: "cacheDiagnosticNegativeRequests",
    measuredRequests: "cacheMeasuredRequests",
    warmupRequests: "cacheWarmupRequests",
    waitAfterWarmup: "cacheWaitAfterWarmup",
    maxRunSeconds: "cacheMaxRunSeconds",
    failureLimit: "cacheFailureLimit",
  };
  Object.entries(cacheBindings).forEach(([field, id]) => {
    $(id).addEventListener("input", () => {
      appState.formsByTab.cache[field] = $(id).value;
      renderCacheFormState();
    });
  });
  $("cacheContentProfile").addEventListener("change", () => {
    appState.formsByTab.cache.contentProfile = $("cacheContentProfile").value;
    renderCacheFormState();
  });
  $("cacheToolStage").addEventListener("change", () => {
    appState.formsByTab.cache.toolStage = $("cacheToolStage").value;
    renderCacheFormState();
  });
  $("cacheCustomContent").addEventListener("change", () => {
    appState.formsByTab.cache.customContent = $("cacheCustomContent").checked;
    renderCacheFormState();
  });
  $("cacheCustomUserChars").addEventListener("change", () => {
    const form = appState.formsByTab.cache;
    form.customUserChars = parseRangeInput("cacheCustomUserChars", form.customUserChars);
    $("cacheCustomUserChars").value = form.customUserChars.join(",");
    renderCacheFormState();
  });
  $("cacheCustomToolResultChars").addEventListener("change", () => {
    const form = appState.formsByTab.cache;
    form.customToolResultChars = parseRangeInput(
      "cacheCustomToolResultChars",
      form.customToolResultChars,
    );
    $("cacheCustomToolResultChars").value = form.customToolResultChars.join(",");
    renderCacheFormState();
  });
  $("cacheControlMode").addEventListener("change", () => {
    appState.formsByTab.cache.controlMode = $("cacheControlMode").value;
    renderCacheFormState();
  });
  $("cacheDiagnosticScenario").addEventListener("change", () => {
    appState.formsByTab.cache.diagnosticScenario = $("cacheDiagnosticScenario").value;
    renderCacheFormState();
  });
  $("cacheKilocodeTrajectoryMode").addEventListener("change", () => {
    appState.formsByTab.cache.kilocodeTrajectoryMode = $("cacheKilocodeTrajectoryMode").value;
    renderCacheFormState();
  });
  $("cacheConfirmLarge").addEventListener("change", () => {
    appState.formsByTab.cache.confirmLarge = $("cacheConfirmLarge").checked;
    renderBusyState();
  });
  $("refreshLoadResults").addEventListener("click", () => refreshLoadResults(true));
  $("loadResultSelect").addEventListener("change", () => {
    appState.selectedLoadResultId = $("loadResultSelect").value;
    loadSavedResult(appState.selectedLoadResultId);
  });

  $("startParam").addEventListener("click", () => createJob("param_test"));
  $("startImage").addEventListener("click", () => createJob("image_param_test"));
  $("startQuickLoad").addEventListener("click", () => createJob("quick_load"));
  $("startStaircase").addEventListener("click", () => createJob("staircase"));
  $("startSoak").addEventListener("click", () => createJob("soak"));
  $("startCache").addEventListener("click", () => createJob("cache_suite"));
  ["globalStop", "stopParam", "stopImage", "stopLoad", "stopCache"].forEach((id) => {
    $(id).addEventListener("click", stopActiveJob);
  });
  $("imageResults").addEventListener("click", (event) => {
    const button = event.target.closest(".image-thumb-button");
    if (button) openImageLightbox(button.dataset.imageUrl, button.dataset.imageCaption);
  });
  $("imageLightboxClose").addEventListener("click", closeImageLightbox);
  $("imageLightbox").addEventListener("click", (event) => {
    if (event.target === $("imageLightbox")) closeImageLightbox();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("imageLightbox").hidden) closeImageLightbox();
  });
}

function pushInitialFormValues() {
  renderImageControls();
  const load = appState.formsByTab.load;
  $("workload").value = load.workload;
  $("users").value = load.users;
  $("spawnRate").value = load.spawnRate;
  $("duration").value = load.duration;
  $("targetRpm").value = Number(load.targetRpm || 0) ? load.targetRpm : "";
  $("targetTpm").value = Number(load.targetTpm || 0) ? load.targetTpm : "";
  $("requestMode").value = load.requestMode;
  $("staircaseSteps").value = load.staircaseSteps.join(",");
  $("staircaseStepDuration").value = load.staircaseStepDuration;
  $("staircaseSpawnRate").value = load.staircaseSpawnRate;
  $("staircaseWarmupEnabled").value = String(load.staircaseWarmupEnabled);
  $("staircaseWarmupUsers").value = load.staircaseWarmupUsers;
  $("staircaseWarmupDuration").value = load.staircaseWarmupDuration;
  $("staircaseAutoExtend").value = String(load.staircaseAutoExtend);
  $("staircaseIncrementUsers").value = load.staircaseIncrementUsers;
  $("staircaseMaxUsers").value = load.staircaseMaxUsers;
  $("soakUsers").value = load.soakUsers;
  $("soakSpawnRate").value = load.soakSpawnRate;
  $("soakDuration").value = load.soakDuration;
  const cache = appState.formsByTab.cache;
  $("cacheSessions").value = cache.sessions;
  $("cacheRounds").value = cache.roundsPerSession;
  $("cacheContentProfile").value = cache.contentProfile;
  $("cacheCustomContent").checked = cache.customContent;
  $("cacheCustomUserChars").value = cache.customUserChars.join(",");
  $("cacheCustomToolResultChars").value = cache.customToolResultChars.join(",");
  $("cacheControlMode").value = cache.controlMode;
  $("cachePositivePairs").value = cache.positivePairs;
  $("cacheNegativeRequests").value = cache.negativeRequests;
  $("cacheWaitAfterSeed").value = cache.waitAfterSeed;
  $("cacheMaxTokens").value = cache.maxTokens;
  $("cacheMaxRunSeconds").value = cache.maxRunSeconds;
  $("cacheFailureLimit").value = cache.failureLimit;
  $("cacheSeed").value = cache.seed;
  $("cacheDiagnosticScenario").value = cache.diagnosticScenario;
  $("cacheKilocodeSteps").value = cache.kilocodeSteps;
  $("cacheKilocodeTrajectoryMode").value = cache.kilocodeTrajectoryMode;
  $("cacheDiagnosticPositivePairs").value = cache.diagnosticPositivePairs;
  $("cacheDiagnosticNegativeRequests").value = cache.diagnosticNegativeRequests;
  $("cacheMeasuredRequests").value = cache.measuredRequests;
  $("cacheWarmupRequests").value = cache.warmupRequests;
  $("cacheWaitAfterWarmup").value = cache.waitAfterWarmup;
  $("cacheConfirmLarge").checked = cache.confirmLarge;
  $("cacheDiagnostics").open = !!cache.diagnosticScenario;
  renderAdaptiveSizingHint();
  renderCacheFormState();
}

bindEvents();
loadConfig().then(() => {
  pushInitialFormValues();
  renderBusyState();
  pollJob();
});
setInterval(pollJob, 3000);
