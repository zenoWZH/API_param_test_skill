# Yibu Model Profile DB

`yibu-model-profile-db` is the reusable, versioned model catalog extracted from
the load-test console. Its public hierarchy is:

```text
modality -> official reference source -> model family -> concrete model -> interfaces
```

`text`, `image`, and the reserved `video` category are explicit database rows.
A source is either the origin vendor or an approved cloud (`aws_bedrock`,
`azure_openai`, `azure_foundry`, `google_vertex`, or `aliyun_maas`). Third-party suppliers,
aggregators, account routes, and API compatibility labels are runtime concerns
and can never become Profile sources. Model family and API form are separate
fields.

The package contains no provider credentials, private/account endpoints, or
load-test thresholds. Core model/interface contracts live in `catalog.yaml`;
project-specific probe and pressure behavior lives in the optional
`test_extensions.yaml`. This keeps the core usable by unrelated projects.

Image and video are non-pressure modalities. Newly compiled modality rows and
every `model_test_policy` binding explicitly serialize
`pressure_test_enabled=false`; legacy schema-v2/v3 rows that omit the field are
read and recompiled as false. Media bindings cannot contain populated pressure
maps or overrides. The compiler and JSON Schema reject an explicit true value;
sequential media parameter matrices remain independent of this invariant.

## Stable identifiers

```text
profile_id   = <modality>/<source_id>/<family_id>/<model_slug>
interface_id = <profile_id>#<api-form-and-route-slug>
```

`request_model_ids` holds source-scoped, officially documented request model
identifiers. An interface can narrow that set with its own `request_model_ids`
when one official source uses different IDs across API surfaces (for example,
Bedrock Runtime and Bedrock Mantle). API form and routing mode live on the interface, so
OpenAI-compatible transport never changes a Gemini, Claude, or DeepSeek model
into a GPT-family model.

Every canonical model has `verification_status=exact_official_model_id`.
Every Profile has `verification_status=exact_official_source_model_id` and an
explicit `request_model_id_semantics`. Azure OpenAI and Microsoft Foundry use
`azure_deployment_model_identity`; all other current sources use
`exact_source_model_id`. The compiler rejects weaker or missing evidence
states instead of publishing them as reusable identities.

Schema v3 catalog version 0.4.0 adds a deterministic field-provenance ledger.
The logical schema version remains frozen by the migration approval while the
extension and SQLite physical artifact versions evolve independently. Every retained Source,
canonical model, Profile, Interface, and Contract has exactly one
`provenance_record_id`; every optional Test Binding has an independent record
in the test-extension ledger. A record fixes the owning `source_id`, authority
and official-domain allowlist, approved official URLs, retrieval date, target
object and covered fields, exact identity, API form, parameter names, and a
section/field summary. The initial 2026-09-02 approval snapshot and later
source-local additions do not retain official page bodies or publisher revision
identifiers, so their records transparently use
`unversioned_retrieval_snapshot` with the actual retrieval date and
`page_content_sha256_available=false`; no page hash is synthesized.

Contracts expose one scalar `source_id`. The single-item `source_ids` list is
temporarily retained as a compatibility alias and must equal `[source_id]`.
Every Test Binding likewise carries one explicit scalar `source_id`. Missing,
wrong-source, cross-source, stale-field, or orphan provenance fails closed in
the compiler, including disabled, identity-only, retired, and not-certified
objects.

Canonical identity and test-suite compatibility are separate. A
`model_test_policy` records both `canonical_family_id` (the Profile identity)
and `suite_family_id` (the parameter/pressure policy), plus the suite's
reference contracts. One canonical interface can therefore expose a legacy
suite policy without publishing a duplicate model identity.
The Interface `contract_ids` remain the canonical callable contracts; once a
test binding is selected, test consumers must use that binding's
`reference_contract_ids` and `default_reference_contract_id` instead of
crossing the canonical contract with another suite's policy.

## Python API

```python
from model_profile_db import load_catalog

catalog = load_catalog()
info = catalog.database_info()
modalities = catalog.list_modalities()
sources = catalog.list_sources("text")
families = catalog.list_families(modality="text", source="openai")
models = catalog.list_models(modality="text", source="openai", family="gpt")
profiles = catalog.list_profiles(
    modality="text",
    source="openai",
    family="gpt",
    model="gpt/gpt-5.3-codex",
)
profile = catalog.get_profile(profiles[0]["profile_id"])
interface = catalog.resolve_request_model(
    "openai", "gpt-5.3-codex", "openai_responses"
)
templates = catalog.list_route_templates(
    modality="image", family="banana", routing_mode="provider_compat"
)
interfaces = catalog.list_interfaces(
    modality="text",
    source="openai",
    family="gpt",
    model="gpt-5.3-codex",
    api_form="openai_responses",
    routing_mode="vendor_direct",
    enabled=True,
)
contracts = catalog.list_contracts(family="gpt", api_form="openai_responses")
test_bindings = catalog.list_test_bindings(
    interface_id=interface["interface_id"],
    extension_type="model_test_policy",
)
parameter_config = catalog.resolve_parameter_config(
    source_id="deepseek",
    modality="text",
    family_id="deepseek",
    model_slug="deepseek-v4-flash-0731",
    interface_id=(
        "text/deepseek/deepseek/deepseek-v4-flash-0731"
        "#openai-chat-default"
    ),
    api_form="openai_chat_completions",
)
```

The `model` filter on Profiles and Interfaces accepts a model slug, a
source-scoped request model ID, or the canonical `<family>/<model_slug>` ID.

Projects that need only the reusable model/interface database can omit all
load-test extensions at read time:

```python
core_catalog = load_catalog(include_test_extensions=False)
assert core_catalog.list_test_bindings() == []
```

`data/catalog.json` is a physically core-only compiled artifact: it contains no
Test Bindings. `load_catalog()` composes the independently published
`test_extensions.yaml` at read time by default; `include_test_extensions=False`
loads only the core. `database_info()` reports whether extensions are loaded
and exposes the independent core and extension digests.

The compiled core carries `catalog_version`, `catalog_digest`, and
`mpdb_schema_version`; the extension artifact has its own
`test_extension_digest`. Job specs embed those values plus a complete immutable
profile/interface snapshot, so old reports are not reinterpreted using current
provider routing.

## CLI and artifacts

Build or install the package without importing the load-test repository:

```bash
python -m pip wheel --no-deps ./packages/model-profile-db --wheel-dir /tmp/mpdb-wheel
python -m pip install /tmp/mpdb-wheel/yibu_model_profile_db-0.3.0-py3-none-any.whl
```

For repository development, install this directory in editable mode or add it
to `PYTHONPATH`, then:

```bash
mpdb validate
mpdb build
mpdb verify-artifacts
mpdb info --core-only
mpdb query --entity sources --modality text
mpdb query --entity models --modality text --source openai --family gpt
mpdb query --entity interfaces --modality text --source openai --family gpt \
  --model gpt/gpt-5.3-codex --api-form openai_responses \
  --routing-mode vendor_direct --enabled true
mpdb query --entity route-templates --modality image --family banana
mpdb query --entity contracts --family gpt --api-form openai_responses
mpdb query --entity test-bindings --interface-id \
  'text/openai/gpt/gpt-5.3-codex#openai-responses-default'
mpdb query --modality text --source openai --family gpt --model gpt-5.3-codex
mpdb resolve openai gpt-5.3-codex --modality text \
  --api-form openai_responses --routing-mode vendor_direct --core-only
mpdb parameter-configs --source deepseek --view
mpdb resolve-parameter-config --source deepseek --modality text \
  --family deepseek --model deepseek-v4-flash-0731 \
  --interface-id \
  'text/deepseek/deepseek/deepseek-v4-flash-0731#openai-chat-default' \
  --api-form openai_chat_completions
mpdb diff old-catalog.json new-catalog.json
```

`mpdb diff` reports release/digest/identity metadata changes as well as added,
removed, and changed rows in every registry. `mpdb verify-artifacts` validates
the manifest schema, every artifact's SHA256/size/schema identity and format
version, source-to-core recompilation, extension digest, and SQLite integrity
and relational parity.

Published data files are:

- `data/catalog.yaml`: editable core source of truth.
- `data/test_extensions.yaml`: independently versioned optional test-runner
  bindings and their provenance ledger (test-extension format v2); it is not
  embedded in either compiled core artifact.
- `data/catalog.json`: deterministic, physically core-only runtime artifact.
- `data/catalog.sqlite3`: core-only relational/query artifact (SQLite artifact
  format v3) with foreign-key relationships, normalized core
  `provenance_records`, and no Test Binding table.
- `data/manifest.json`: format versions, core/extension digests, counts, schema
  identities, and SHA256/size records for every published artifact.
- `schemas/*.schema.json`: separate source-catalog, compiled-core,
  test-extension, and artifact-manifest schemas.

## SQLite consumers

SQLite contains normalized lookup columns plus a canonical `payload_json` for
every core row. The entity tables are `modalities`, `sources`, `families`,
`canonical_models`, `profiles`, `interfaces`, `route_templates`, `contracts`,
`legacy_aliases`, `provenance_records`, and `metadata`; relationship tables bind
contracts to their sources, Interfaces, and route templates. Test Bindings and
their provenance stay exclusively in the extension artifact. Installed
applications can locate the database without knowing a repository path:

```python
import sqlite3
from importlib.resources import files

database = files("model_profile_db").joinpath("data/catalog.sqlite3")
with sqlite3.connect(str(database)) as connection:
    profiles = connection.execute(
        """
        SELECT profile_id, model_slug
        FROM profiles
        WHERE modality = ? AND source_id = ? AND family_id = ?
        ORDER BY model_slug
        """,
        ("text", "openai", "gpt"),
    ).fetchall()
```

The `metadata.identity_contract` JSON value makes the hierarchy and ID formats
self-describing for non-Python consumers.

## Compatibility contract

- `mpdb_schema_version` changes only when the database shape or ID semantics
  require a breaking consumer migration.
- `catalog_version` versions data releases; consumers may pin it when they
  require a reviewed model set.
- `catalog_digest` identifies the exact compiled core. The independent
  `test_extension_digest` identifies optional Test Bindings; store both with
  test results for reproducibility.
- Loading a compiled catalog recomputes the core digest and verifies the
  derived `modality_source_family_model` view. Manifest verification additionally
  checks every artifact hash, schema identity, format version, extension digest,
  and SQLite parity, so stale or partially replaced artifacts fail closed.
- Existing `profile_id` and `interface_id` values are never reassigned to a
  different source, family, model, or API form. Lifecycle changes are expressed
  in row metadata; aliases map old identifiers forward.
- API form is always an interface field. A compatible transport never changes
  the source or model family identity.
- Test-suite reuse never creates another canonical Profile. Compatibility
  bindings must identify both suite and canonical families and reference
  contracts with the same suite family and API form.
- Credentials, account endpoints, and private provider configuration are not
  part of the database contract.

`scripts/migrate_model_profile_database.py` at the repository root is a
read-only replay verifier. Its migration write phase is closed: it reads the
byte-pinned public legacy snapshot plus `official_model_profile_additions.yaml`,
never current provider configuration or `providers.local.yaml`. It compares
the resulting source, extensions, JSON, SQLite, and manifest without rewriting
artifacts. Later source-local additions must remain replayable alongside their
actual retrieval dates, retained provenance summaries, exact public Interface
aliases, and model-specific case selection; inheritance must not silently add
another model's unsupported parameter cases.

The 2026-09-06 review reconciled the existing GLM-4.5 / GLM-5.3 /
GLM-5.3-Flash additions with this replay without changing the reviewed catalog,
test-extension digests, or pressure gates. GPT-6 Astra retains independent
Chat and Responses Contracts. Artifact and replay checks establish offline
consistency, not new live certification or application parameter-runner parity.
Downstream projects should consume the package artifacts or Python API instead
of importing the load-test repository.
