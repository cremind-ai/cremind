"""Read install/_catalog.json so the TUI shares the same prompts as install.sh.

The catalog is produced by ``install/scripts/build_catalog.py`` from
``install/catalog.toml`` and emitted as ``_catalog.sh``, ``_catalog.ps1``,
and ``_catalog.json``. The TUI reads the JSON form so it doesn't have to
parse shell syntax — keeping deployment labels, custom-field prompts, and
mode descriptions consistent across the bash, PowerShell, and Python
front-ends.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CustomField:
    """One advanced field for the ``custom`` deployment.

    ``choices`` is empty for free-text fields and populated for radio
    fields (e.g. wizard_preset). ``key`` matches the shell-side
    ``CUSTOM_<key>`` variable name so the TUI can write back to it.
    """

    key: str
    prompt: str
    hint: str
    default: str
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class Deployment:
    id: str
    label: str
    short: str
    description: str
    requires_host: bool
    advanced_fields: tuple[CustomField, ...] = ()


@dataclass(frozen=True)
class Mode:
    """One install method, and the capabilities it needs to be offered.

    ``requires`` is a visibility gate, not documentation: a front-end offers a
    mode only when every id in it is a capability that front-end probed *and*
    found. See the comment under ``[modes]`` in install/catalog.toml.
    """

    id: str
    label: str
    description: str
    hint: str
    badge: str = ""
    requires: tuple[str, ...] = ()
    order: int = 999


@dataclass(frozen=True)
class DockerDesktop:
    """The Docker desktop-UI sub-question (asked only when mode == docker)."""

    prompt: str = "Install the VNC Desktop UI?"
    hint: str = ""
    default: bool = True


@dataclass(frozen=True)
class VncPasswordPrompt:
    """The VNC password question (asked only when the desktop UI is on)."""

    prompt: str = "Choose a password for the VNC Desktop"
    hint: str = ""


@dataclass(frozen=True)
class KubernetesPrompts:
    """The kubernetes-mode questions (asked only when mode == kubernetes).

    ``advanced_fields`` reuses :class:`CustomField` — same prompt/hint/default/
    choices shape as the ``custom`` deployment's fields, so the TUI walks both
    with one loop.
    """

    context_prompt: str = "Which kubeconfig context should Cremind be installed into?"
    context_hint: str = ""
    namespace_prompt: str = "Which namespace should the Helm release go into?"
    namespace_hint: str = ""
    namespace_default: str = "cremind"
    advanced_prompt: str = "Use the recommended Helm options, or customize them?"
    advanced_hint: str = ""
    advanced_fields: tuple[CustomField, ...] = ()


@dataclass(frozen=True)
class Catalog:
    deployments: tuple[Deployment, ...] = ()
    modes: tuple[Mode, ...] = ()
    docker_desktop: DockerDesktop = DockerDesktop()
    vnc_password: VncPasswordPrompt = VncPasswordPrompt()
    kubernetes: KubernetesPrompts = KubernetesPrompts()

    def deployment(self, deployment_id: str) -> Deployment | None:
        for d in self.deployments:
            if d.id == deployment_id:
                return d
        return None

    def mode(self, mode_id: str) -> Mode | None:
        for m in self.modes:
            if m.id == mode_id:
                return m
        return None

    def available_modes(self, capabilities: frozenset[str]) -> tuple[Mode, ...]:
        """The modes this environment can actually offer, in catalog order.

        The single Python home of the ``requires`` gate: install.sh and
        install.ps1 implement the same rule over ``MODE_REQUIRES_<id>`` /
        ``$script:Modes[id].Requires``. A mode whose requirements name a
        capability the caller did not probe is filtered out, because an
        unprobed capability is an unmet one.
        """
        return tuple(m for m in self.modes if set(m.requires) <= capabilities)


def _load_fields(entries: list[dict] | None) -> tuple[CustomField, ...]:
    """Parse an ``advanced_fields`` array into :class:`CustomField` objects."""
    fields: list[CustomField] = []
    for field_entry in entries or []:
        fields.append(
            CustomField(
                key=field_entry["key"],
                prompt=field_entry.get("prompt", ""),
                hint=field_entry.get("hint", ""),
                default=field_entry.get("default", ""),
                choices=tuple(field_entry.get("choices", []) or []),
            )
        )
    return tuple(fields)


# Missing ``order`` sorts LAST, matching build_catalog._ordered() and
# installCatalogApi.ts. The id tie-break keeps the render deterministic.
def _by_order(kv: tuple[str, dict]) -> tuple[int, str]:
    return (int(kv[1].get("order", 999)), kv[0])


def load(path: str | Path) -> Catalog:
    """Load the catalog from a JSON file written by build_catalog.py."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))

    deployments_raw = data.get("deployments", {})
    deployments = []
    for dep_id, entry in sorted(deployments_raw.items(), key=_by_order):
        deployments.append(
            Deployment(
                id=dep_id,
                label=entry.get("label", dep_id),
                short=entry.get("short", ""),
                description=entry.get("description", ""),
                requires_host=bool(entry.get("requires_host", False)),
                advanced_fields=_load_fields(entry.get("advanced_fields")),
            )
        )

    modes_raw = data.get("modes", {})
    modes = []
    for mode_id, entry in sorted(modes_raw.items(), key=_by_order):
        modes.append(
            Mode(
                id=mode_id,
                label=entry.get("label", mode_id),
                description=entry.get("description", ""),
                hint=entry.get("hint", ""),
                badge=entry.get("badge", ""),
                requires=tuple(entry.get("requires", []) or []),
                order=int(entry.get("order", 999)),
            )
        )

    dd_raw = data.get("docker_desktop", {}) or {}
    docker_desktop = DockerDesktop(
        prompt=dd_raw.get("prompt", DockerDesktop.prompt),
        hint=dd_raw.get("hint", ""),
        default=bool(dd_raw.get("default", True)),
    )

    vp_raw = data.get("vnc_password", {}) or {}
    vnc_password = VncPasswordPrompt(
        prompt=vp_raw.get("prompt", VncPasswordPrompt.prompt),
        hint=vp_raw.get("hint", ""),
    )

    k8s_raw = data.get("kubernetes", {}) or {}
    kubernetes = KubernetesPrompts(
        context_prompt=k8s_raw.get("context_prompt", KubernetesPrompts.context_prompt),
        context_hint=k8s_raw.get("context_hint", ""),
        namespace_prompt=k8s_raw.get(
            "namespace_prompt", KubernetesPrompts.namespace_prompt
        ),
        namespace_hint=k8s_raw.get("namespace_hint", ""),
        namespace_default=k8s_raw.get(
            "namespace_default", KubernetesPrompts.namespace_default
        ),
        advanced_prompt=k8s_raw.get(
            "advanced_prompt", KubernetesPrompts.advanced_prompt
        ),
        advanced_hint=k8s_raw.get("advanced_hint", ""),
        advanced_fields=_load_fields(k8s_raw.get("advanced_fields")),
    )

    return Catalog(
        deployments=tuple(deployments),
        modes=tuple(modes),
        docker_desktop=docker_desktop,
        vnc_password=vnc_password,
        kubernetes=kubernetes,
    )
