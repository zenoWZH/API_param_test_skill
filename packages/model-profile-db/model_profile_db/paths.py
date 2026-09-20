from __future__ import annotations

from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_ROOT / "data"
SCHEMA_DIR = PACKAGE_ROOT / "schemas"
SOURCE_CATALOG_PATH = DATA_DIR / "catalog.yaml"
TEST_EXTENSIONS_PATH = DATA_DIR / "test_extensions.yaml"
COMPILED_CATALOG_PATH = DATA_DIR / "catalog.json"
SQLITE_CATALOG_PATH = DATA_DIR / "catalog.sqlite3"
MANIFEST_PATH = DATA_DIR / "manifest.json"
# ``catalog.schema.json`` is the author/source-catalog schema kept at its
# established package path.  Compiled core and artifact manifests have their
# own schemas because their topologies are intentionally different.
CATALOG_SCHEMA_PATH = SCHEMA_DIR / "catalog.schema.json"
SOURCE_CATALOG_SCHEMA_PATH = CATALOG_SCHEMA_PATH
COMPILED_CORE_SCHEMA_PATH = SCHEMA_DIR / "compiled_core.schema.json"
TEST_EXTENSIONS_SCHEMA_PATH = SCHEMA_DIR / "test_extensions.schema.json"
ARTIFACT_MANIFEST_SCHEMA_PATH = SCHEMA_DIR / "artifact_manifest.schema.json"
