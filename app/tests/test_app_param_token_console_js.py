from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


class ParamTokenConsoleJavascriptTest(unittest.TestCase):
    SCRIPT_ROOT = Path(__file__).resolve().parents[1]

    @unittest.skipUnless(shutil.which("node"), "node is required for console JS tests")
    def test_schema_v4_validation_and_exact_evidence_are_rendered_separately(
        self,
    ) -> None:
        source = (
            self.SCRIPT_ROOT
            / "scripts"
            / "static"
            / "web_console.js"
        ).read_text(encoding="utf-8")
        source = source.rsplit("\nbindEvents();", 1)[0]
        probe = r"""
const nodes = {};
globalThis.document = {
  getElementById(id) {
    if (!nodes[id]) nodes[id] = { innerHTML: "", textContent: "", className: "" };
    return nodes[id];
  },
};

const exchange = {
  schema_version: 4,
  exchange: "initial",
  validation_status: "fail",
  validation_pass: false,
  validation_failures: ["reported input <unsafe> is implausible"],
  usage_required: true,
  usage_presence: {
    required: true,
    status: "fail",
    input_present: false,
    output_present: true,
    missing_fields: ["input_tokens"],
    note: "successful <exchange> is missing authoritative input_tokens",
  },
  gross_plausibility: {
    status: "fail",
    input: {
      status: "fail",
      reported_tokens: 999999,
      estimated_tokens: 4,
      minimum_plausible_tokens: 1,
      maximum_plausible_tokens: 128,
      note: "reported input <unsafe> exceeds the gross envelope",
    },
    output: {
      status: "partial",
      reported_tokens: 500,
      estimated_tokens: 2,
      minimum_plausible_tokens: 1,
      maximum_plausible_tokens: 128,
      note: "hidden reasoning prevents a conclusive mismatch",
    },
  },
  input: {},
  output: {},
  input_accuracy: {
    status: "not_available",
    reported_tokens: null,
    independent_tokens: null,
    delta: null,
    evidence_level: "unavailable",
  },
  output_accuracy: {
    status: "not_available",
    reported_tokens: 500,
    independent_tokens: null,
    delta: null,
    evidence_level: "unavailable",
  },
  usage_arithmetic: { status: "pass", errors: [] },
  output_completion: { status: "fail", note: "output token limit truncated the image" },
  reported: {
    input_tokens: null,
    output_tokens: 500,
    answer_tokens: 500,
    thinking_tokens: null,
    image_tokens: null,
    cached_tokens: 0,
    total_tokens: null,
  },
  usage_accounting: { input_tokens: null, output_tokens: 500 },
  status: "partial",
  evidence_level: "unavailable",
};

const tokenSummary = {
  schema_version: 4,
  validation_status: "fail",
  pass: false,
  exchange_count: 3,
  required_exchange_count: 3,
  validated_exchange_count: 2,
  validation_failure_count: 1,
  missing_usage_count: 1,
  gross_failure_count: 1,
  gross_partial_count: 1,
  gross_check_count: 3,
  arithmetic_check_count: 3,
  arithmetic_failure_count: 0,
  completion_check_count: 3,
  completion_failure_count: 1,
  completion_unverified_count: 0,
  missing_audit_result_count: 0,
  invalid_audit_result_count: 0,
  coverage: 0,
  exact_dimension_count: 0,
  exact_mismatch_count: 0,
};
const currentCompleteExchange = {
  ...exchange, validation_pass: true, validation_status: "pass", validation_failures: [],
  usage_presence: {status: "pass"}, usage_arithmetic: {status: "pass"},
  output_completion: {status: "pass"},
  gross_plausibility: {status: "pass", input: {status: "pass"}, output: {status: "pass"}},
};
const contradictoryExchanges = [
  {count_request_integrity: "fail"}, {input_accuracy: {status: "fail"}},
  {output_accuracy: {status: "fail"}}, {validation_failures: ["count mismatch"]},
  {validation_status: "partial"},
].map((changes) => tokenExchangeValidationPass({...currentCompleteExchange, ...changes}));
const tokenAudit = {
  validation_status: "fail",
  validation_pass: false,
  validation_failures: exchange.validation_failures,
  exchanges: [exchange],
};
const result = {
  profile: "basic_stream",
  run_index: 1,
  status: "pass",
  pass: true,
  compatibility_status: "pass",
  compatibility_pass: true,
  overall_status: "token_validation_failed",
  overall_pass: false,
  token_validation_status: "fail",
  token_validation_pass: false,
  token_validation_failures: exchange.validation_failures,
  token_audit: tokenAudit,
};
const failingJob = {
  status: "failed",
  returncode: 1,
  verdict: {
    total: 1,
    token_validation_pass: false,
    token_accuracy_pass: true,
    token_audit_summary: tokenSummary,
  },
  param_results: [result],
};

const passingNoExactSummary = {
  ...tokenSummary,
  validation_status: "pass",
  pass: true,
  validated_exchange_count: 3,
  validation_failure_count: 0,
  missing_usage_count: 0,
  gross_failure_count: 0,
  gross_partial_count: 0,
  completion_failure_count: 0,
};
const passingNoExactJob = {
  verdict: {
    token_validation_pass: true,
    token_accuracy_pass: true,
    token_audit_summary: passingNoExactSummary,
  },
  param_results: [],
};
const zeroExchangeFailure = {
  verdict: {
    token_validation_pass: false,
    token_audit_summary: {
      ...tokenSummary,
      exchange_count: 0,
      required_exchange_count: 0,
      validated_exchange_count: 0,
      missing_audit_result_count: 1,
    },
  },
  param_results: [],
};
const legacyJob = {
  verdict: {
    token_accuracy_pass: true,
    token_audit_summary: {
      schema_version: 2,
      exchange_count: 1,
      coverage: 0,
      exact_dimension_count: 0,
    },
  },
  param_results: [],
};

const failingMetrics = Object.fromEntries(paramTestMetrics(failingJob));
const passingNoExactMetrics = Object.fromEntries(paramTestMetrics(passingNoExactJob));
const zeroExchangeMetrics = Object.fromEntries(paramTestMetrics(zeroExchangeFailure));
const legacyMetrics = Object.fromEntries(paramTestMetrics(legacyJob));
const stalePositive = {
  verdict: { token_validation_pass: true, token_audit_summary: { ...passingNoExactSummary, schema_version: 3 } },
};
const incompletePositive = {
  verdict: { token_validation_pass: true, token_audit_summary: { ...passingNoExactSummary, completion_check_count: undefined } },
};
renderTokenAudit(failingJob);
renderFailedCaseLog(null, [{ ...result, overall_pass: undefined }]);

const imageResult = {
  ...result,
  case: "image_case",
  overall_failures: ["token_validation_failed"],
  actual_images: [],
  artifact_urls: [],
  model_identity_audit: { status: "match" },
};
const imageJob = {
  status: "failed",
  returncode: 1,
  progress: { completed_cases: 1, total_cases: 1, pass_count: 1, failure_count: 0 },
  image_summary: {
    pass: false,
    pass_count: 0,
    failure_count: 1,
    case_count: 1,
    token_validation_pass: false,
    token_audit_summary: tokenSummary,
  },
  image_results: [imageResult],
};
const imageMetrics = Object.fromEntries(imageTestMetrics(imageJob));
renderImageSummary(imageJob);
renderImageCaseRows(imageJob);
const imageSummaryHtml = nodes.imageSummary.innerHTML;
renderImageSummary({image_summary: {pass: true, token_validation_pass: true,
  token_audit_summary: {...passingNoExactSummary, schema_version: 3}}});
const staleImageSummaryHtml = nodes.imageSummary.innerHTML;

const runSummary = parameterRunSummary(
  { coverage_mode: "profiles", test_profiles: ["basic_stream"] },
  [result],
  ["basic_stream"],
);

process.stdout.write(JSON.stringify({
  failingMetrics,
  passingNoExactMetrics,
  zeroExchangeMetrics,
  legacyMetrics,
  contradictoryExchanges,
  staleGate: tokenValidationGateLabel(stalePositive, stalePositive.verdict.token_audit_summary),
  incompleteGate: tokenValidationGateLabel(incompletePositive, incompletePositive.verdict.token_audit_summary),
  notApplicableWithRequired: tokenValidationGateInfo(passingNoExactJob, {
    ...passingNoExactSummary, validation_status: "not_applicable",
    exchange_count: 1, required_exchange_count: 1, validated_exchange_count: 1,
    gross_check_count: 1, completion_check_count: 1, arithmetic_check_count: 1,
  }),
  staleMatrix: parameterRunSummary({coverage_mode: "profiles", test_profiles: ["basic"]},
    [{profile: "basic", status: "pass", pass: true, overall_pass: true, token_validation_pass: true}], ["basic"]),
  tokenHtml: nodes.paramTokenAudit.innerHTML,
  failedLog: nodes.paramFailedCaseLog.textContent,
  imageMetrics,
  imageSummaryHtml,
  staleImageSummaryHtml,
  contradictorySummaryGate: tokenValidationGateInfo({
    ...passingNoExactJob, param_results: [{profile: "old", pass: true}, {...result, token_validation_pass: true,
      token_audit: {...tokenAudit, validation_pass: true,
        exchanges: [{...currentCompleteExchange, count_request_integrity: "fail"}]}}],
  }, passingNoExactSummary),
  imageRowsHtml: nodes.imageResults.innerHTML,
  runSummary,
}));
"""
        completed = subprocess.run(
            [shutil.which("node") or "node", "-"],
            input=f"{source}\n{probe}",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)

        failing = result["failingMetrics"]
        self.assertEqual(failing["Token validation gate"], "FAIL")
        self.assertEqual(failing["Validation status"], "FAIL")
        self.assertEqual(failing["Validated / required exchanges"], "2/3")
        self.assertEqual(failing["Missing usage"], 1)
        self.assertEqual(failing["Gross failures / partial"], "1 / 1")
        self.assertEqual(failing["Exact coverage"], "0.0%")
        self.assertEqual(failing["Exact accuracy"], "N/A (no exact evidence)")
        self.assertEqual(failing["Overall success rate"], "0.0%")
        self.assertEqual(failing["Pass cells"], 0)
        self.assertEqual(failing["Fail cells"], 1)

        no_exact = result["passingNoExactMetrics"]
        self.assertEqual(no_exact["Token validation gate"], "PASS")
        self.assertEqual(no_exact["Exact accuracy"], "N/A (no exact evidence)")
        self.assertEqual(result["zeroExchangeMetrics"]["Validation status"], "FAIL")
        self.assertEqual(result["legacyMetrics"]["Token validation gate"], "UNVERIFIED (legacy)")
        self.assertEqual(result["staleGate"], "UNVERIFIED (legacy)")
        self.assertEqual(result["incompleteGate"], "UNVERIFIED (incomplete)")
        self.assertIs(result["notApplicableWithRequired"]["pass"], False)
        self.assertEqual(result["staleMatrix"]["label"], "token unverified")
        self.assertEqual(result["contradictoryExchanges"], [False] * 5)
        self.assertIs(result["contradictorySummaryGate"]["pass"], False)
        self.assertIn("<strong>UNVERIFIED</strong>", result["staleImageSummaryHtml"])
        self.assertNotIn("<strong>PASS</strong>", result["staleImageSummaryHtml"])

        token_html = result["tokenHtml"]
        for text in (
            "usage REQUIRED · FAIL",
            "input missing · output present · missing input_tokens",
            "Input gross FAIL",
            "Output gross PARTIAL",
            "reported",
            "estimate",
            "range",
            "hidden reasoning prevents a conclusive mismatch",
            "exact input N/A",
            "validation FAIL · gate FAIL",
        ):
            self.assertIn(text, token_html)
        self.assertIn("&lt;unsafe&gt;", token_html)
        self.assertIn("Output completion FAIL", token_html)
        self.assertNotIn("<unsafe>", token_html)

        self.assertEqual(result["runSummary"]["status"], "fail")
        self.assertEqual(result["runSummary"]["label"], "token fail")
        self.assertIn("token_validation_failed", result["failedLog"])
        self.assertIn("token_validation_status: fail", result["failedLog"])

        image_metrics = result["imageMetrics"]
        self.assertEqual(image_metrics["Passed"], 0)
        self.assertEqual(image_metrics["Failed"], 1)
        self.assertEqual(image_metrics["Token validation gate"], "FAIL")
        self.assertEqual(image_metrics["Exact accuracy"], "N/A (no exact evidence)")
        for rendered in (result["imageSummaryHtml"], result["imageRowsHtml"]):
            self.assertIn("validation", rendered.lower())
            self.assertIn("FAIL", rendered)
        self.assertIn("Input gross FAIL", result["imageRowsHtml"])
        self.assertIn("Output gross PARTIAL", result["imageRowsHtml"])
        self.assertIn("token_validation_failed", result["imageRowsHtml"])


if __name__ == "__main__":
    unittest.main()
