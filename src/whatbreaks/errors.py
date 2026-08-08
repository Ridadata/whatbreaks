"""Exception taxonomy.

The distinction that matters most here is between *we could not obtain usable
inputs* and *we analysed the inputs and found something*. Conflating them
produces the failure mode ADR 000 R11 warns about: reporting "analysis failed"
when the real problem is that dbt could not build a manifest at all.

`InputError` is the former. Everything analysis-related is modelled as data
(confidence, coverage), not exceptions -- see `lineage.uncertainty`.
"""

from __future__ import annotations


class WhatbreaksError(Exception):
    """Base for every error this tool raises deliberately."""


class InputError(WhatbreaksError):
    """We could not obtain or trust the inputs. Never a finding about the user's SQL.

    Carries `remedy` so the CLI can tell the user what to actually do, rather
    than printing a traceback.
    """

    def __init__(self, message: str, remedy: str | None = None) -> None:
        super().__init__(message)
        self.remedy = remedy


class ManifestNotFoundError(InputError):
    """The manifest path does not exist or is not readable."""


class ManifestParseError(InputError):
    """The file exists but is not valid JSON, or is not a dbt manifest."""


class UnsupportedManifestVersionError(InputError):
    """The manifest schema version is outside the supported range.

    Deliberately fatal. Best-effort parsing of an unknown artifact format is
    how a tool starts producing confidently wrong answers.
    """


class ManifestTooLargeError(InputError):
    """The manifest exceeds a hardening limit (untrusted-input guard)."""


class UnsafePathError(InputError):
    """A path inside the manifest escapes the project root."""
