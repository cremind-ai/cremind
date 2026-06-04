#!/usr/bin/env python3
"""Render bash / PowerShell / JSON includes from install/catalog.toml.

The master at install/catalog.toml is hand-edited; everything else under
install/ that needs to read the catalog reads it through a generated
include:

  install/_catalog.sh    sourced by install.sh
  install/_catalog.ps1   dot-sourced by install.ps1
  install/_catalog.json  raw JSON copy (handy for inspection; the UI
                         goes through the backend API instead)

Usage:

  python install/scripts/build_catalog.py          # regenerate
  python install/scripts/build_catalog.py --check  # exit 1 if stale

The script is stdlib-only so CI can run it before any pip install. Both
generated files include a SHA-256 of catalog.toml in their header so a
glance at the first line is enough to confirm they match the master.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
CATALOG = ROOT / "catalog.toml"
OUT_SH = ROOT / "_catalog.sh"
OUT_PS1 = ROOT / "_catalog.ps1"
OUT_JSON = ROOT / "_catalog.json"
# Copy shipped with the backend so installed packages (which don't carry
# install/catalog.toml) can still load the catalog at runtime. CI checks
# this file in lock-step with the install/* artifacts.
OUT_PKG_TOML = REPO_ROOT / "app" / "config" / "install_catalog.toml"
# Copy bundled into the SPA at build time so the Electron installer
# stage — which runs before any backend exists — can render the
# deployment radios and the ``custom`` advanced-field form from the
# same data the install scripts use.
OUT_UI_JSON = REPO_ROOT / "ui" / "src" / "services" / "installCatalogData.json"


# ── helpers ─────────────────────────────────────────────────────────────

def _ordered(items: dict[str, dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Return (id, body) pairs sorted by ``order`` then by id.

    The install scripts render numbered prompts from this order, so
    making it deterministic is critical — re-running the generator
    must produce byte-identical output for the CI check to be useful.
    """
    return sorted(
        items.items(),
        key=lambda kv: (kv[1].get("order", 999), kv[0]),
    )


def _bash_quote(value: Any) -> str:
    """Quote a Python value for safe use inside a bash double-quoted string.

    Escapes the four characters that have meaning inside ``"..."`` —
    backslash, double-quote, backtick, and dollar-sign — so the
    generated includes never embed shell metacharacters by accident.
    """
    s = str(value)
    if "\n" in s or "\r" in s:
        raise ValueError(f"catalog values must not contain newlines: {s!r}")
    s = s.replace("\\", "\\\\")
    s = s.replace('"', '\\"')
    s = s.replace("`", "\\`")
    s = s.replace("$", "\\$")
    return f'"{s}"'


def _bash_var_name(*parts: str) -> str:
    """Build a bash variable name like DEPLOYMENT_LABEL_local from parts."""
    return "_".join(str(p) for p in parts)


def _ps_quote(value: Any) -> str:
    """Quote a value for a PowerShell single-quoted string.

    Inside ``'...'`` only a literal single quote needs escaping (as
    ``''``). Everything else passes through literally, including ``$``
    — which is exactly what we want for descriptions that mention
    placeholder variables.
    """
    s = str(value)
    if "\n" in s or "\r" in s:
        raise ValueError(f"catalog values must not contain newlines: {s!r}")
    return "'" + s.replace("'", "''") + "'"


# ── bash include ────────────────────────────────────────────────────────

def render_bash(catalog: dict[str, Any], digest: str) -> str:
    lines: list[str] = []
    add = lines.append

    add("# AUTO-GENERATED from install/catalog.toml. Do not edit by hand.")
    add(f"# Regenerate with: python install/scripts/build_catalog.py")
    add(f"# Source SHA-256:  {digest}")
    add("")
    add(f"CATALOG_SCHEMA={catalog.get('schema_version', 1)}")
    add("")

    # Deployments
    deployments = _ordered(catalog.get("deployments", {}))
    add("# ── Deployments ──")
    add("DEPLOYMENT_IDS=" + _bash_quote(" ".join(d_id for d_id, _ in deployments)))
    for d_id, body in deployments:
        add(_bash_var_name("DEPLOYMENT_LABEL", d_id) + "=" + _bash_quote(body.get("label", d_id)))
        add(_bash_var_name("DEPLOYMENT_SHORT", d_id) + "=" + _bash_quote(body.get("short", "")))
        add(_bash_var_name("DEPLOYMENT_DESC", d_id) + "=" + _bash_quote(body.get("description", "")))
        add(_bash_var_name("DEPLOYMENT_ENV_VALUE", d_id) + "=" + _bash_quote(body.get("env_value", d_id)))
        add(_bash_var_name("DEPLOYMENT_HOST", d_id) + "=" + _bash_quote(body.get("host", "")))
        add(_bash_var_name("DEPLOYMENT_REQUIRES_HOST", d_id) + "=" + ("1" if body.get("requires_host") else "0"))
    add("")

    # Custom advanced fields (read from the `custom` deployment, if present).
    custom = catalog.get("deployments", {}).get("custom", {})
    fields = custom.get("advanced_fields", []) or []
    add("# ── Custom-deployment advanced fields ──")
    add("CUSTOM_FIELD_IDS=" + _bash_quote(" ".join(f["key"] for f in fields)))
    for field in fields:
        key = field["key"]
        add(_bash_var_name("CUSTOM_FIELD_PROMPT", key) + "=" + _bash_quote(field.get("prompt", "")))
        add(_bash_var_name("CUSTOM_FIELD_HINT", key) + "=" + _bash_quote(field.get("hint", "")))
        add(_bash_var_name("CUSTOM_FIELD_DEFAULT", key) + "=" + _bash_quote(field.get("default", "")))
        choices = field.get("choices") or []
        add(_bash_var_name("CUSTOM_FIELD_CHOICES", key) + "=" + _bash_quote(" ".join(choices)))
    add("")

    # Install modes
    modes = _ordered(catalog.get("modes", {}))
    add("# ── Install modes ──")
    add("MODE_IDS=" + _bash_quote(" ".join(m_id for m_id, _ in modes)))
    for m_id, body in modes:
        add(_bash_var_name("MODE_LABEL", m_id) + "=" + _bash_quote(body.get("label", m_id)))
        add(_bash_var_name("MODE_DESC", m_id) + "=" + _bash_quote(body.get("description", "")))
        add(_bash_var_name("MODE_HINT", m_id) + "=" + _bash_quote(body.get("hint", "")))
        add(_bash_var_name("MODE_BADGE", m_id) + "=" + _bash_quote(body.get("badge", "")))
        requires = body.get("requires") or []
        add(_bash_var_name("MODE_REQUIRES", m_id) + "=" + _bash_quote(" ".join(requires)))
    add("")

    # Mode rules
    rules = catalog.get("mode_rules", {})
    add("# ── Mode rules ──")
    for rule_id, body in rules.items():
        allowed = body.get("allowed_service_modes") or []
        add(_bash_var_name("MODE_RULE_ALLOWED", rule_id) + "=" + _bash_quote(" ".join(allowed)))
        add(_bash_var_name("MODE_RULE_DEFAULT", rule_id) + "=" + _bash_quote(body.get("default_service_mode", "")))
    add("")

    # Service modes
    service_modes = catalog.get("service_modes", {})
    add("# ── Service modes ──")
    add("SERVICE_MODE_IDS=" + _bash_quote(" ".join(service_modes.keys())))
    for sm_id, body in service_modes.items():
        add(_bash_var_name("SERVICE_MODE_LABEL", sm_id) + "=" + _bash_quote(body.get("label", sm_id)))
        add(_bash_var_name("SERVICE_MODE_DESC_TMPL", sm_id) + "=" + _bash_quote(body.get("description_template", "")))
    add("")

    # Helpers — small bash functions that consume the variables above.
    # The eval-based indirection lets callers do `catalog_get DEPLOYMENT_LABEL local`
    # without forcing every consumer to know the naming convention.
    add("# ── Helpers ──")
    add("catalog_get() {")
    add('    # Usage: catalog_get PREFIX KEY  →  prints the value of $PREFIX_KEY.')
    add('    local _name="${1}_${2}"')
    add('    eval "printf %s \\"\\${${_name}-}\\""')
    add("}")
    add("")

    return "\n".join(lines) + "\n"


# ── PowerShell include ──────────────────────────────────────────────────

def render_powershell(catalog: dict[str, Any], digest: str) -> str:
    lines: list[str] = []
    add = lines.append

    add("# AUTO-GENERATED from install/catalog.toml. Do not edit by hand.")
    add("# Regenerate with: python install/scripts/build_catalog.py")
    add(f"# Source SHA-256:  {digest}")
    add("")
    add(f"$script:CatalogSchema = {catalog.get('schema_version', 1)}")
    add("")

    # Deployments
    deployments = _ordered(catalog.get("deployments", {}))
    add("# ── Deployments ──")
    add("$script:DeploymentIds = @(" + ", ".join(_ps_quote(d_id) for d_id, _ in deployments) + ")")
    add("$script:Deployments = [ordered]@{")
    for d_id, body in deployments:
        add(f"    {_ps_quote(d_id)} = [ordered]@{{")
        add(f"        Label        = {_ps_quote(body.get('label', d_id))}")
        add(f"        Short        = {_ps_quote(body.get('short', ''))}")
        add(f"        Description  = {_ps_quote(body.get('description', ''))}")
        add(f"        EnvValue     = {_ps_quote(body.get('env_value', d_id))}")
        add(f"        DeployHost   = {_ps_quote(body.get('host', ''))}")
        add(f"        RequiresHost = ${'true' if body.get('requires_host') else 'false'}")
        add(f"        Order        = {int(body.get('order', 999))}")
        add("    }")
    add("}")
    add("")

    # Custom advanced fields
    custom = catalog.get("deployments", {}).get("custom", {})
    fields = custom.get("advanced_fields", []) or []
    add("# ── Custom-deployment advanced fields ──")
    add("$script:CustomFieldIds = @(" + ", ".join(_ps_quote(f["key"]) for f in fields) + ")")
    add("$script:CustomFields = [ordered]@{")
    for field in fields:
        key = field["key"]
        choices = field.get("choices") or []
        add(f"    {_ps_quote(key)} = [ordered]@{{")
        add(f"        Key     = {_ps_quote(key)}")
        add(f"        Prompt  = {_ps_quote(field.get('prompt', ''))}")
        add(f"        Hint    = {_ps_quote(field.get('hint', ''))}")
        add(f"        Default = {_ps_quote(field.get('default', ''))}")
        if choices:
            add(f"        Choices = @({', '.join(_ps_quote(c) for c in choices)})")
        else:
            add("        Choices = @()")
        add("    }")
    add("}")
    add("")

    # Install modes
    modes = _ordered(catalog.get("modes", {}))
    add("# ── Install modes ──")
    add("$script:ModeIds = @(" + ", ".join(_ps_quote(m_id) for m_id, _ in modes) + ")")
    add("$script:Modes = [ordered]@{")
    for m_id, body in modes:
        requires = body.get("requires") or []
        add(f"    {_ps_quote(m_id)} = [ordered]@{{")
        add(f"        Label       = {_ps_quote(body.get('label', m_id))}")
        add(f"        Description = {_ps_quote(body.get('description', ''))}")
        add(f"        Hint        = {_ps_quote(body.get('hint', ''))}")
        add(f"        Badge       = {_ps_quote(body.get('badge', ''))}")
        if requires:
            add(f"        Requires    = @({', '.join(_ps_quote(r) for r in requires)})")
        else:
            add("        Requires    = @()")
        add(f"        Order       = {int(body.get('order', 999))}")
        add("    }")
    add("}")
    add("")

    # Mode rules
    rules = catalog.get("mode_rules", {})
    add("# ── Mode rules ──")
    add("$script:ModeRules = [ordered]@{")
    for rule_id, body in rules.items():
        allowed = body.get("allowed_service_modes") or []
        add(f"    {_ps_quote(rule_id)} = [ordered]@{{")
        if allowed:
            add(f"        AllowedServiceModes = @({', '.join(_ps_quote(a) for a in allowed)})")
        else:
            add("        AllowedServiceModes = @()")
        add(f"        DefaultServiceMode  = {_ps_quote(body.get('default_service_mode', ''))}")
        add("    }")
    add("}")
    add("")

    # Service modes
    service_modes = catalog.get("service_modes", {})
    add("# ── Service modes ──")
    add("$script:ServiceModeIds = @(" + ", ".join(_ps_quote(s) for s in service_modes) + ")")
    add("$script:ServiceModes = [ordered]@{")
    for sm_id, body in service_modes.items():
        add(f"    {_ps_quote(sm_id)} = [ordered]@{{")
        add(f"        Label               = {_ps_quote(body.get('label', sm_id))}")
        add(f"        DescriptionTemplate = {_ps_quote(body.get('description_template', ''))}")
        add("    }")
    add("}")
    add("")

    return "\n".join(lines) + "\n"


# ── JSON include ────────────────────────────────────────────────────────

def render_json(catalog: dict[str, Any], digest: str) -> str:
    """Dump the catalog as JSON, prefixed by a marker the loader can skip.

    JSON doesn't support comments, so the digest goes into a ``_meta``
    key at the top level. Consumers that only want the data ignore the
    key; consumers that want the integrity check (CI) read it.
    """
    payload = dict(catalog)
    payload["_meta"] = {"sha256": digest, "schema_version": catalog.get("schema_version", 1)}
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def render_ui_json(catalog: dict[str, Any], digest: str) -> str:
    """Same shape as ``render_json``, written into the UI source tree.

    Imported by ``ui/src/services/installCatalogApi.ts`` at build time so
    the Electron installer stage (which runs before the backend exists)
    has the catalog data ready. Vite inlines the JSON into the SPA
    bundle.
    """
    return render_json(catalog, digest)


# ── driver ──────────────────────────────────────────────────────────────

def render_package_toml(catalog: dict[str, Any], digest: str, master_text: str) -> str:
    """Return the master TOML prefixed with a digest header.

    The backend loader reads this file when the package is installed
    via pip (where install/catalog.toml isn't on disk). Keeping it
    byte-identical to the master + a header keeps CI's drift check
    trivial: same digest → same content.
    """
    header = (
        "# AUTO-GENERATED copy of install/catalog.toml shipped with the\n"
        "# backend. Do not edit by hand — re-run\n"
        "# `python install/scripts/build_catalog.py`.\n"
        f"# Source SHA-256: {digest}\n\n"
    )
    return header + master_text


def build() -> tuple[str, str, str, str, str, str]:
    raw = CATALOG.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8")
    catalog = tomllib.loads(text)
    return (
        digest,
        render_bash(catalog, digest),
        render_powershell(catalog, digest),
        render_json(catalog, digest),
        render_package_toml(catalog, digest, text),
        render_ui_json(catalog, digest),
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero when the generated files don't match the master",
    )
    args = parser.parse_args(argv)

    if not CATALOG.exists():
        sys.stderr.write(f"catalog.toml not found at {CATALOG}\n")
        return 1

    digest, bash_text, ps_text, json_text, pkg_toml_text, ui_json_text = build()

    pairs = [
        (OUT_SH, bash_text),
        (OUT_PS1, ps_text),
        (OUT_JSON, json_text),
        (OUT_PKG_TOML, pkg_toml_text),
        (OUT_UI_JSON, ui_json_text),
    ]

    if args.check:
        stale: list[Path] = []
        for path, expected in pairs:
            actual = path.read_text(encoding="utf-8") if path.exists() else ""
            if actual != expected:
                stale.append(path)
        if stale:
            sys.stderr.write(
                "catalog includes are stale (re-run: python install/scripts/build_catalog.py):\n"
            )
            for path in stale:
                sys.stderr.write(f"  {path}\n")
            return 1
        sys.stdout.write(f"catalog includes up to date (sha256={digest[:12]})\n")
        return 0

    for path, text in pairs:
        path.write_text(text, encoding="utf-8")
    sys.stdout.write(f"wrote {len(pairs)} catalog include(s) (sha256={digest[:12]})\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
