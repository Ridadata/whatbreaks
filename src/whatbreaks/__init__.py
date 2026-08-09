"""whatbreaks - static breaking-change analysis for dbt.

No warehouse, no secrets, no backend. See WHATBREAKS_PROJECT_PLAN.md.
"""

from __future__ import annotations

__version__ = "0.1.0"

# The JSON output schema is a public API and is versioned independently of the
# tool, so consumers can pin to it. See plan section 22.
OUTPUT_SCHEMA_VERSION = 1

__all__ = ["OUTPUT_SCHEMA_VERSION", "__version__"]
