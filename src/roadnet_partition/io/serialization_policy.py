"""Repository policy on executable serialization of the relation graph.

Remediation batch R5.2. Deserializing a Python pickle executes whatever the
file says to execute, so this repository refuses legacy graph artifacts *by
declaration* — filename, manifest format field, or legacy provenance flag —
and never by inspecting the payload. Refusing on the declaration is what makes
the refusal safe: by the time the bytes are read, the damage is done.

This module holds only name-level predicates and pure declaration walks. It
deliberately imports nothing from the project, so the manifest layer can
enforce the same policy as the graph reader without an import cycle
(``io.safe_graph`` already imports ``io.manifests``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping


#: The only graph interchange format this repository supports.
SUPPORTED_GRAPH_FORMAT = "SafeGraphArtifactV1"

#: Suffixes that make a file executable-on-read. Nothing here is ever loaded.
EXECUTABLE_SERIALIZATION_SUFFIXES = (".gpickle", ".pkl", ".pickle")

#: Sidecar written by the removed R5.1 conversion path. Its presence means a
#: legacy pickle was involved, so it is refused alongside the pickle itself.
LEGACY_PROVENANCE_SUFFIX = ".legacy-provenance.json"

#: Manifest field the removed conversion path used to set.
LEGACY_FLAG_KEY = "legacy_executable_serialization"

#: Single wording for every legacy refusal, so operators get one instruction.
LEGACY_UNSUPPORTED_MESSAGE = (
    "Legacy executable graph serialization is no longer supported. "
    f"Regenerate the graph with Preparation using {SUPPORTED_GRAPH_FORMAT}."
)

_FORMAT_KEYS = ("format", "graph_format", "serialization")
_PATH_SEPARATORS = ("/", "\\")


class ExecutableSerializationRefused(ValueError):
    """A legacy executable serialization artifact or declaration was refused."""


def is_executable_serialization_name(name: str) -> bool:
    """Whether ``name`` is a filename that would be executable on read.

    Name-only: the caller must be able to refuse a legacy artifact without
    opening it.
    """

    return name.endswith(EXECUTABLE_SERIALIZATION_SUFFIXES)


def is_legacy_evidence_name(name: str) -> bool:
    """Whether ``name`` is a pickle or the sidecar the pickle path wrote."""

    return is_executable_serialization_name(name) or name.endswith(LEGACY_PROVENANCE_SUFFIX)


def _basename(value: str) -> str:
    tail = value
    for separator in _PATH_SEPARATORS:
        tail = tail.rsplit(separator, 1)[-1]
    return tail


def declares_executable_serialization(record: Mapping[str, Any]) -> str | None:
    """Why ``record`` declares a legacy graph artifact, or ``None``.

    Checks the declaration only. A record is legacy when it carries the removed
    provenance flag, when it names an executable serialization format, or when
    its declared path is a pickle. The declared path is never opened, so a
    manifest that lies in either direction — a pickle name with a safe format,
    or a safe name with a pickle format — is still refused.
    """

    if record.get(LEGACY_FLAG_KEY):
        return LEGACY_FLAG_KEY
    for key in _FORMAT_KEYS:
        declared = record.get(key)
        if isinstance(declared, str) and declared != SUPPORTED_GRAPH_FORMAT and "pickle" in declared.lower():
            return f"{key}={declared}"
    for key in ("path", "file", "filename"):
        declared = record.get(key)
        if isinstance(declared, str) and is_legacy_evidence_name(_basename(declared)):
            return _basename(declared)
    return None


def legacy_declarations(payload: Any, *, label: str = "") -> list[str]:
    """Every legacy graph declaration inside a decoded manifest.

    Returns ``"<dotted location> (<reason>)"`` strings so an operator is told
    which logical name is at fault. Walks the decoded structure only: no file
    named in the manifest is stat-ed, opened, or deserialized.
    """

    found: list[str] = []
    _walk(payload, label or "manifest", found)
    return sorted(dict.fromkeys(found))


def _walk(payload: Any, location: str, found: list[str]) -> None:
    if isinstance(payload, Mapping):
        reason = declares_executable_serialization(payload)
        if reason is not None:
            # The record is already refused; recursing would only restate the
            # same filename one level deeper.
            found.append(f"{location} ({reason})")
            return
        for key, value in payload.items():
            _walk(value, f"{location}.{key}", found)
        return
    if isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            _walk(value, f"{location}[{index}]", found)
        return
    if isinstance(payload, str) and is_legacy_evidence_name(_basename(payload)):
        found.append(f"{location} ({_basename(payload)})")


def reject_legacy_declarations(payload: Any, *, label: str = "", subject: str = "manifest") -> None:
    """Raise when ``payload`` declares any legacy graph artifact."""

    offenders = legacy_declarations(payload, label=label)
    if offenders:
        raise ExecutableSerializationRefused(
            f"{subject} declares legacy executable graph serialization: {offenders}. {LEGACY_UNSUPPORTED_MESSAGE}"
        )


def _matching_files(root: str | Path, predicate) -> list[str]:
    base = Path(root)
    return sorted(
        path.relative_to(base).as_posix()
        for path in base.rglob("*")
        if path.is_file() and predicate(path.name)
    )


def executable_serialization_files(root: str | Path) -> list[str]:
    """Relative paths under ``root`` that are Python pickles.

    Publication and reproduction bundles must never ship executable
    serialization: a downstream reader that trusts the bundle would be handed
    arbitrary code. Matching is by name only — nothing here is ever opened.
    Callers raise their own error type on a non-empty result.
    """

    return _matching_files(root, is_executable_serialization_name)


def legacy_evidence_files(root: str | Path) -> list[str]:
    """Relative paths under ``root`` that are pickles or legacy sidecars."""

    return _matching_files(root, is_legacy_evidence_name)
