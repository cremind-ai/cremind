"""What kind of install this process is — answered once, for everyone.

"Are we in Docker?", "is there a VNC desktop?", "which release channel is
this?" used to be re-derived from the same handful of environment variables in
every place that needed them: the tray-capabilities endpoint, the Codex
sign-in flow, the server's shutdown path, the Setup Wizard's config export.
Each copy had its own heuristics, so they could disagree about the very same
machine. This module states those facts once, so the Developer page, the
``cremind server environment`` command and the agent's own system prompt all
describe the same install.

The *install* facts are fixed for the life of the process — the installer
writes those environment variables and nothing rewrites them at runtime — so
they are cached. That is not (only) about speed: they are rendered into the
agent's system prompt, which is prompt-cached upstream, and a line that
wobbled between turns would cost a cache miss on every single turn. The
handful of facts that this process really can rewrite (the public URL, the
System Directory) are read live on every call instead; see
:func:`describe_runtime_environment`.

Import discipline: stdlib at module level, everything from ``app`` imported
inside the functions. The CLI, the installer and ``app/server.py``'s boot path
all want these answers, and none of them can afford to drag the settings or
storage stack in as a side effect of asking what install mode they are running
under.
"""

from __future__ import annotations

import copy
import os
import platform
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit


# Last-resort "are we in a container?" signal, used only when INSTALL_MODE is
# absent (an older Docker ``.env`` predating the key). Module-level so tests can
# point it somewhere that doesn't exist instead of patching Path.exists
# globally — CI itself may well run inside a container.
_CONTAINER_MARKER = Path("/.dockerenv")

# The one fact a pod gets for free: the default service-account token is
# mounted here and its ``namespace`` file names the namespace, with no downward
# API and no RBAC-granted API call. Module-level for the same reason as
# ``_CONTAINER_MARKER`` - tests point it at a path that cannot exist, because a
# suite running inside a real cluster would otherwise inherit that cluster's
# namespace and every "nothing known" row would fail there and nowhere else.
_SA_NAMESPACE_FILE = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")

# ``<workload>-<replicaset hash>-<pod suffix>`` - the name a Deployment gives
# its pods, and the only way a pod started by an older chart can guess its own
# Deployment name. A StatefulSet pod (``<name>-0``) or a bare pod does not
# match and is left unknown rather than guessed wrong: a wrong workload name
# renders a ``kubectl`` line that fails for the operator, which is worse than a
# placeholder that visibly needs filling in.
_POD_NAME_RE = re.compile(r"(.+)-[a-z0-9]{5,10}-[a-z0-9]{5}")

# RFC 1123 label: what Kubernetes accepts as a namespace, Deployment or Service
# name. Every name that reaches a rendered command is checked against it,
# because these values arrive from ``cremind.extraEnv`` where an operator can
# put anything at all - a value carrying a newline would make
# ``tls_steps.command()`` raise and turn ``/api/tls/status`` into a 500.
_DNS_NAME_RE = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")

#: The local port every Cremind runbook forwards to: the UI port, so the
#: tunnelled URL reads exactly like a local install's.
PORT_FORWARD_LOCAL_PORT = 1515

# The chart's ``service.port`` default. Used when the chart is too old to state
# it, so a rendered port-forward line is still runnable on a stock install.
_DEFAULT_SERVICE_PORT = 80

# websockify's port in every image we ship (compose publishes it as
# ``NOVNC_PORT``; the Helm noVNC Service uses it on both sides).
_DEFAULT_NOVNC_PORT = 6080

# Where noVNC answers under each access shape. Behind the nginx sidecar it is a
# route on the app origin; everywhere else websockify owns the whole port.
_NOVNC_DIRECT_PATH = "/vnc.html"
_NOVNC_PROXY_PATH = "/vnc/vnc.html"

# Runs of slashes in a URL path. The chart composes CREMIND_NOVNC_URL by
# concatenating ``cremind.appUrl`` with the noVNC route, and appUrl is a value
# an operator types: ``https://cremind.example/`` renders
# ``https://cremind.example//vnc/vnc.html``. nginx merges those slashes before
# it matches a location, so the URL works in a browser and only our own
# classifier saw the difference.
_SLASH_RUN_RE = re.compile(r"/{2,}")

# The same placeholders ``app.config.tls_steps`` prints when the pod cannot
# name itself, so one install never shows two vocabularies for the same unknown
# (the HTTPS runbook says ``<release>`` for the workload because that is what
# the operator types into ``helm upgrade``). Spelled out here rather than
# imported: this module answers boot-path and installer questions and must not
# grow a dependency on the runbook builder; ``tests/config/test_tls_steps.py``
# pins the two spellings equal.
_NAMESPACE_PLACEHOLDER = "<namespace>"
_WORKLOAD_PLACEHOLDER = "<release>"

# Why the desktop is reachable the way it is, in the user's terms. Each one
# answers the question the button raises: "Cremind is on HTTPS, why is this
# link plain http?" / "why do I need a second tunnel?"
_SCHEME_NOTES = {
    "direct": (
        "noVNC listens on its own port over plain http; Cremind's own HTTPS "
        "does not cover it."
    ),
    "same_origin": (
        "The desktop is served by the nginx sidecar on the app origin, so it "
        "uses the same scheme you reach Cremind with."
    ),
    "port_forward": (
        "With in-pod TLS the sidecar is a plain TCP relay, so noVNC answers on "
        "its own Service port over plain http; tunnel that port first."
    ),
}

# ``ENV`` as the installers write it (install/templates/*.env) → the deployment
# vocabulary the Setup Wizard and the config export speak. The names differ on
# purpose: ``ENV`` is the app's runtime profile, ``deployment`` is the choice
# the user made in the installer ("only this machine" vs "reachable from other
# devices" vs "I'll configure it myself").
_ENV_DEPLOYMENTS = {
    "local": "local",
    "production": "server",
    "custom": "custom",
}

# ``SETUP_WIZARD_ENV`` as the installers write it → the same deployment
# vocabulary. This is the *only* signal a container has: the compose file pins
# ``ENV: production`` in its ``environment:`` block for every container
# (install/templates/docker-compose.yml.tmpl), so the wizard preset is where
# the operator's actual pick survives. ``docker`` is a preset, not a deployment
# — a Docker install is still reached on localhost unless the operator said
# otherwise.
_WIZARD_DEPLOYMENTS = {
    "local": "local",
    "docker": "local",
    "server": "server",
    "kubernetes": "kubernetes",
}


def detect_install_mode(container_marker: Path | None = None) -> str:
    """Return ``native`` | ``docker`` | ``kubernetes`` for the running install.

    ``INSTALL_MODE`` is authoritative (compose writes ``docker``, the Helm
    chart writes ``kubernetes``). Only when it is absent — an older Docker
    ``.env`` predating the key — do we fall back to the container marker or the
    ``VNC_PASSWORD`` the desktop image always carries.

    ``container_marker`` overrides :data:`_CONTAINER_MARKER` for callers that
    keep their own patchable copy of it (see
    :mod:`app.api.llm_codex_flow`); it exists for tests, not for production
    callers.
    """
    try:
        from app.config.install_catalog import get_active_install_mode

        mode = (get_active_install_mode() or "").strip().lower()
    except Exception as exc:  # noqa: BLE001
        from app.utils.logger import logger

        logger.debug(f"[runtime-env] install-mode lookup failed: {exc}")
        mode = ""
    if mode in ("docker", "kubernetes"):
        return mode
    marker = container_marker if container_marker is not None else _CONTAINER_MARKER
    if not mode and (os.environ.get("VNC_PASSWORD") or marker.exists()):
        return "docker"
    return "native"


def is_container(install_mode: str | None = None) -> bool:
    """Is this process running inside one of the images we ship?

    The two signals miss in opposite directions, which is why both are here:
    ``/.dockerenv`` is absent on a Kubernetes pod (containerd never writes it),
    while ``INSTALL_MODE`` is absent on an older Docker ``.env`` that predates
    the key. Anything deciding *where a file belongs* asks this -
    ``app.config.coding_cli_homes`` keeps the Claude/Codex CLI logins under the
    System Directory in a container, because ``~`` there is an image layer that
    a ``docker compose down``, an image upgrade or a pod replacement throws
    away along with the OAuth tokens in it.

    ``_describe_cached`` calls this instead of repeating the expression, so a
    test that points :data:`_CONTAINER_MARKER` somewhere harmless moves both
    answers at once.
    """
    mode = detect_install_mode() if install_mode is None else install_mode
    return _CONTAINER_MARKER.exists() or mode in ("docker", "kubernetes")


def get_image_flavor() -> str | None:
    """The Docker image flavor this container was built as, or ``None``.

    Read from the ``CREMIND_IMAGE_FLAVOR`` env var baked into each image
    (``desktop`` for cremind/cremind-desktop, ``basic`` for cremind/cremind).
    Returns ``None`` for native installs and for pre-flavor images that
    predate the var — the Electron client treats ``None`` as "desktop" for
    Docker installs, which is correct because every pre-flavor image is a
    desktop image.

    Deliberately *not* cached: ``app.api.features`` re-exports it and its tests
    set the env var and call straight through.
    """
    raw = os.environ.get("CREMIND_IMAGE_FLAVOR", "").strip().lower()
    return raw if raw in ("desktop", "basic") else None


def supervised(install_mode: str | None = None) -> bool:
    """Is something out there that will restart us if we exit?

    Docker (compose sets ``restart: unless-stopped``), Kubernetes (the kubelet
    restarts the pod), Electron (which respawns us over IPC) and the boot
    service ``cremind boot enable`` registers on a native install (which sets
    ``CREMIND_SUPERVISED``) all qualify. A bare ``cremind serve`` in a terminal
    does not — telling a user their backend will come back when nothing is
    watching it is the failure this answers.

    ``install_mode`` is the mode :func:`detect_install_mode` resolved, not the
    raw ``INSTALL_MODE`` variable this used to read. An install whose ``.env``
    predates that key is still the Docker install whose restart policy brings
    it back, so reading the variable made this the one field in the description
    that could contradict the ``install_mode`` and ``vnc_enabled`` beside it —
    the agent's prompt line called one machine a "Docker install (VNC desktop
    enabled) ... no supervisor". Both consumers that *act* on this had already
    settled it the same way: the Developer page's restart dialog and ``cremind
    server restart`` pick their copy from ``install_mode`` first and only fall
    through to this field on a native install. The default resolves the mode
    for callers that have not; ``_describe_cached`` passes the one it holds.

    ``app/server.py::_supervised_env`` keeps its own copy of this logic on
    purpose: it decides how the shutdown path behaves and must not import this
    module (or anything else optional) while it is booting or dying.
    """
    from app.config.tls_mode import env_supervised

    mode = detect_install_mode() if install_mode is None else install_mode
    return (
        mode in ("docker", "kubernetes")
        or os.environ.get("CREMIND_ELECTRON_PARENT") is not None
        or env_supervised()
    )


def _detect_deployment(install_mode: str) -> str:
    """``local`` | ``server`` | ``custom`` | ``kubernetes`` — how it is reached.

    Read from ``os.environ`` rather than ``BaseConfig.ENV`` because that
    attribute defaults to ``production`` when the var is unset, which would
    make every native dev box report itself as a server deployment. Only a
    value that was actually written by an installer counts here.

    On a container ``ENV`` says nothing at all: the compose file hard-codes
    ``ENV: production`` in the service's ``environment:`` block, so reading it
    first told every Docker user — including the ones who picked "only this
    machine" — that they were on a server deployment. ``SETUP_WIZARD_ENV`` is
    the value the installer actually derived from that answer, so on Docker it
    wins outright. A preset the table doesn't know can only have come from the
    installer's ``--wizard-preset`` flag, which is offered on a *custom*
    deployment and is not validated against the catalog's choice list — so an
    unrecognised value means ``custom``, not ``local``.
    """
    if install_mode == "kubernetes":
        return "kubernetes"
    wizard = (os.environ.get("SETUP_WIZARD_ENV") or "").strip().lower()
    if install_mode == "docker" and wizard:
        return _WIZARD_DEPLOYMENTS.get(wizard, "custom")
    mapped = _ENV_DEPLOYMENTS.get((os.environ.get("ENV") or "").strip().lower())
    if mapped:
        return mapped
    return _WIZARD_DEPLOYMENTS.get(wizard, "local")


# --- Kubernetes: who this pod is ---------------------------------------


def _dns_name(raw: str | None) -> str | None:
    """``raw`` if it is a usable Kubernetes object name, else ``None``.

    The gate is not pedantry: these strings are interpolated into ``kubectl``
    and ``helm`` lines that a user copies into a shell, and they come from
    container environment variables an operator can set to anything. A name
    with a space, a newline or a shell metacharacter in it is not a name we
    can print, so it counts as "the chart didn't say" and the placeholder is
    shown instead.
    """
    value = (raw or "").strip()
    if not value or len(value) > 63:
        return None
    return value if _DNS_NAME_RE.fullmatch(value) else None


def _service_account_namespace() -> str | None:
    """The namespace from the mounted service-account token, if it is there.

    Present in every pod that keeps the default ``automountServiceAccountToken``
    and readable without any API call, which is what makes it the fallback for
    a pod whose chart is too old to state its namespace.
    """
    try:
        return _dns_name(_SA_NAMESPACE_FILE.read_text(encoding="utf-8"))
    except OSError:
        return None


def _workload_from_pod_name() -> str | None:
    """The Deployment name guessed from ``HOSTNAME``, when we are truly in a pod.

    Gated on ``KUBERNETES_SERVICE_HOST`` - the variable the kubelet injects
    into every pod - because ``HOSTNAME`` exists everywhere and a developer's
    laptop called ``lee-cremind-5b8d7c9f8d-x2k9p`` is not a hint about a
    cluster. ``INSTALL_MODE=kubernetes`` alone is not enough: it can be forced
    by hand in a local ``.env``.
    """
    if not (os.environ.get("KUBERNETES_SERVICE_HOST") or "").strip():
        return None
    match = _POD_NAME_RE.fullmatch((os.environ.get("HOSTNAME") or "").strip())
    return _dns_name(match.group(1)) if match else None


def kubernetes_port_forward(
    namespace: str, service: str, local_port: int, remote_port: int
) -> str:
    """The ``kubectl port-forward`` line, in one place.

    The chart's NOTES, the HTTPS runbook, the Codex sign-in hint and the VNC
    card all print this command, and they printed four slightly different
    spellings of it. One function means a user who follows two of our screens
    types the same thing twice. The result is a bare single line by contract -
    ``tls_steps.command()`` rejects anything else, and the tests assert it
    passes.
    """
    return (
        f"kubectl --namespace {namespace} port-forward "
        f"svc/{service} {local_port}:{remote_port}"
    )


def kubernetes_identity(install_mode: str | None = None) -> dict | None:
    """Namespace, Helm release and Deployment/Service of this pod, or ``None``.

    A pod knows nothing about itself by default: Kubernetes injects no release
    name, no Deployment name and not even its namespace into the container
    environment, and Helm's NOTES are printed once on the operator's terminal
    and never reach the app. That is why Settings -> HTTPS & Certificate used to
    print ``helm upgrade <release> ... --namespace <namespace>`` and open with
    ``helm list --all-namespaces`` - the server had no way to fill the blanks.

    The chart now states the three names only it knows
    (``CREMIND_K8S_NAMESPACE`` / ``_RELEASE`` / ``_WORKLOAD``, plus
    ``_SERVICE_PORT``). Everything here still has to degrade, because a cluster
    upgrades the venv PVC in place: the pod running this code may well have
    been started by a chart that sets none of them. Each name therefore has a
    fallback that needs no API call and no RBAC:

    - namespace -> the mounted service-account namespace file;
    - workload -> ``HOSTNAME`` with the ReplicaSet hash and pod suffix stripped;
    - release -> nothing at all. It is Helm's own name for the install and the
      pod carries no trace of it, so an inferred identity keeps ``helm list``
      as the runbook's first command.

    ``service`` is the workload: the chart names the Deployment and the Service
    alike (both are ``cremind.fullname``). ``source`` is ``chart`` only when all
    three env vars arrived intact, so no consumer has to re-derive how much of
    this is a guess; anything short of that is ``inferred``, and ``None`` means
    the pod could not name itself at all.

    Uncached, like :func:`get_image_flavor`: ``_describe_cached`` holds the one
    copy that matters (these names are fixed for the life of a pod), while
    ``app/api/tls.py`` and the Codex sign-in hint call straight through and
    their tests set the environment and expect to see it.
    """
    mode = detect_install_mode() if install_mode is None else install_mode
    if mode != "kubernetes":
        return None

    stated_namespace = _dns_name(os.environ.get("CREMIND_K8S_NAMESPACE"))
    stated_release = _dns_name(os.environ.get("CREMIND_K8S_RELEASE"))
    stated_workload = _dns_name(os.environ.get("CREMIND_K8S_WORKLOAD"))

    namespace = stated_namespace or _service_account_namespace()
    workload = stated_workload or _workload_from_pod_name()
    release = stated_release

    if stated_namespace and stated_release and stated_workload:
        source = "chart"
    elif namespace or release or workload:
        source = "inferred"
    else:
        source = None

    service_port = _DEFAULT_SERVICE_PORT
    try:
        parsed_port = int((os.environ.get("CREMIND_K8S_SERVICE_PORT") or "").strip())
    except ValueError:
        parsed_port = 0
    if 0 < parsed_port < 65536:
        service_port = parsed_port

    return {
        "namespace": namespace,
        "release": release,
        "workload": workload,
        "service": workload,
        "service_port": service_port,
        "source": source,
        # Only when both halves are real: a command with a placeholder in it
        # belongs to the runbook, which explains what to substitute. This field
        # is copied to a clipboard and run.
        "port_forward": (
            kubernetes_port_forward(
                namespace, workload, PORT_FORWARD_LOCAL_PORT, service_port
            )
            if namespace and workload
            else None
        ),
    }


# --- the VNC desktop: where it answers, and what it takes to get there ---


def _novnc_port() -> int:
    """The port noVNC is published on for a container install.

    ``NOVNC_PORT`` lives in the Compose *project* file, not in the container's
    environment - ``docker-compose.yml.tmpl`` uses it in ``ports:`` only - so
    inside the container the env var is normally absent and the host's
    ``docker/.env`` is the only place the operator's choice survives. The same
    two-source walk as ``/api/config/install-secrets``: the bind-mounted
    ``CREMIND_COMPOSE_ENV_FILE`` first, then the installer's own docker folder
    for a host install with no container in front of the backend.
    """
    from app.config.credentials_file import parse_docker_env
    from app.config.settings import BaseConfig

    raw = (os.environ.get("NOVNC_PORT") or "").strip()
    if not raw:
        compose_env = (os.environ.get("CREMIND_COMPOSE_ENV_FILE") or "").strip()
        candidates = [Path(compose_env)] if compose_env else []
        candidates.append(Path(BaseConfig.CREMIND_INSTALL_DIR) / "docker" / ".env")
        for candidate in candidates:
            try:
                parsed = parse_docker_env(candidate)
            except OSError:
                continue
            if parsed:
                raw = (parsed.get("NOVNC_PORT") or "").strip()
                break
    try:
        port = int(raw)
    except ValueError:
        return _DEFAULT_NOVNC_PORT
    return port if 0 < port < 65536 else _DEFAULT_NOVNC_PORT


def _ssl_mode_configured() -> bool:
    """Is in-pod TLS asked for in this process's environment?

    Read from ``os.environ`` rather than ``BaseConfig.SSL_MODE`` because that
    attribute is bound at class-definition time: a test (and the chart's own
    render checks) set the variable and expect the answer to follow. The
    false-y spellings mirror ``tls_mode.effective_ssl_mode``.
    """
    mode = (os.environ.get("CREMIND_SSL") or "").strip().lower()
    return mode not in ("", "false", "0", "no", "none")


def _edge_tls_termination() -> bool:
    """Does something in front of the pod terminate TLS (a Helm Ingress)?

    A one-line wrapper so this module's Kubernetes branch reads in one
    vocabulary; ``app.config.tls_mode`` owns the definition.
    """
    from app.config.tls_mode import edge_tls_termination

    return edge_tls_termination()


def _stated_novnc_url() -> tuple[str, str, int | None]:
    """``CREMIND_NOVNC_URL`` as (url, path, port) - the chart speaking.

    The chart is the only party that knows which of the two Kubernetes shapes
    is deployed (see helm/cremind/templates/configmap.yaml): with the nginx
    sidecar noVNC is a route on the app origin, without it websockify owns its
    own Service port. From the browser the two are indistinguishable, hence
    this variable.

    The path comes back with its slash runs collapsed. The chart composes this
    value onto ``cremind.appUrl``, so a trailing slash there produced
    ``//vnc/vnc.html``; the chart no longer emits that, but an install that
    already rendered it keeps it in its ConfigMap. Normalising once here means
    the shape test below, the published ``novnc_path`` and the ``open_url``
    built from it all read the same spelling.
    """
    stated = (os.environ.get("CREMIND_NOVNC_URL") or "").strip()
    if not stated:
        return "", "", None
    try:
        parts = urlsplit(stated)
        return stated, _SLASH_RUN_RE.sub("/", parts.path), parts.port
    except ValueError:
        # A malformed port makes urlsplit raise. Treat the whole value as
        # unusable rather than half-trusting it.
        return "", "", None


def _in_pod_novnc_route(path: str) -> str | None:
    """The sidecar's own ``/vnc/...`` route inside ``path``, or ``None``.

    Two jobs, because they are the same question. It says *whether* a stated
    URL is the proxy-sidecar shape, and it says *what a tunnel to that sidecar
    exposes* - which is not the public path when an Ingress serves Cremind
    under a sub-path.

    The sidecar declares exactly one noVNC location, ``/vnc/`` (plus a ``= /vnc``
    redirect into it); see helm/cremind/templates/proxy-configmap.yaml. The
    chart hangs that route off ``cremind.appUrl``, so everything *before* the
    ``/vnc/`` segment is prefix the edge adds and the sidecar never sees:
    ``https://host/cremind/vnc/vnc.html`` is served in-pod as ``/vnc/vnc.html``,
    and handing the public path to a port-forwarded localhost falls through to
    the SPA route and 404s.

    Matched by segment, not by page name: noVNC ships several pages
    (``vnc_lite.html``), the directory form ``/cremind/vnc/`` is a route too,
    and a stored value may carry a trailing slash - all of which a
    ``endswith("/vnc/vnc.html")`` test called the in-pod-TLS relay instead. The
    *last* ``vnc`` segment wins because the chart appends the route, so an
    appUrl whose own path ends in ``vnc`` still yields the appended one.
    ``/vnc.html`` - the relay literal, where websockify owns the whole port -
    has no ``vnc`` segment at all and correctly matches nothing.
    """
    segments = path.split("/")
    for index in range(len(segments) - 1, -1, -1):
        if segments[index] == "vnc":
            return "/" + "/".join(segments[index:])
    return None


def _describe_vnc(
    install_mode: str, vnc_enabled: bool, kubernetes: dict | None
) -> dict:
    """How to reach the VNC desktop from a browser, in one shape for everyone.

    "Open the desktop" hides three different deployments, and the client cannot
    tell them apart on its own - which is how the Electron shell ended up
    guessing ``localhost:6080`` for Kubernetes pods where nothing listens on
    that port until a tunnel exists:

    - ``direct`` - Docker publishes websockify on its own host port. Plain
      http even when Cremind itself serves HTTPS, and reached on whatever host
      the browser already uses, so the URL is composed client-side.
    - ``same_origin`` - the Kubernetes nginx sidecar fronts the SPA, the API
      and noVNC on one port, so the desktop is just a path on the origin the
      user is already on. Behind an Ingress that is the whole story; reached
      through a port-forward, the tunnel that carries Cremind carries the
      desktop too.
    - ``port_forward`` - with in-pod TLS the sidecar degrades to an L4 relay
      and noVNC gets its own Service port, which no existing tunnel forwards.
      Nothing in the browser can reach it until the user runs one of the
      commands, so they ship with the descriptor.

    ``port_forward_commands`` entries are ``{label, command, open_url}``:
    prose, one bare shell line (the ``tls_steps.command()`` contract - the copy
    button must yield something runnable), and where to go once it runs.
    """
    if not vnc_enabled:
        return {
            "enabled": False,
            "access": None,
            "novnc_path": None,
            "novnc_port": None,
            "novnc_url": None,
            "port_forward_commands": [],
            "scheme_note": None,
        }

    if install_mode != "kubernetes":
        return {
            "enabled": True,
            "access": "direct",
            "novnc_path": _NOVNC_DIRECT_PATH,
            "novnc_port": _novnc_port(),
            # The host belongs to the browser, not to us: a Docker install is
            # reached on whatever address the operator typed, and websockify is
            # published on that same address. Naming localhost here would be
            # wrong for every tab that is not on the Docker host, so the client
            # composes the URL from its own location.
            "novnc_url": None,
            "port_forward_commands": [],
            "scheme_note": _SCHEME_NOTES["direct"],
        }

    identity = kubernetes or {}
    namespace = identity.get("namespace") or _NAMESPACE_PLACEHOLDER
    service = identity.get("service") or _WORKLOAD_PLACEHOLDER
    service_port = identity.get("service_port") or _DEFAULT_SERVICE_PORT

    stated, stated_path, stated_port = _stated_novnc_url()
    in_pod_route = _in_pod_novnc_route(stated_path)
    if stated:
        # The proxy shape is "there is a /vnc/ segment in the path", not "the
        # path starts with /vnc/" and not "it ends in the noVNC page we happen
        # to name". The chart hangs that route off ``cremind.appUrl``, which is
        # whatever the operator typed: a sub-path origin (https://host/cremind)
        # renders /cremind/vnc/vnc.html and a trailing slash used to render
        # //vnc/vnc.html - both are the sidecar route, and both failed a prefix
        # test. Reported as the relay, the card then said "Needs a tunnel" and
        # printed two kubectl port-forward lines for Service port 6080, which
        # only the relay shape publishes: they fail with "Service does not have
        # a service port 6080".
        same_origin = in_pod_route is not None
    else:
        # An older chart states nothing. In-pod TLS is the only reason the
        # sidecar stops proxying noVNC, so CREMIND_SSL answers the same
        # question the variable would have.
        same_origin = not _ssl_mode_configured()

    if same_origin:
        path = stated_path or _NOVNC_PROXY_PATH
        commands = []
        if not _edge_tls_termination():
            # No Ingress: the user reaches Cremind through a port-forward, and
            # the same tunnel already carries the desktop. Say so rather than
            # leaving them to wonder whether a second one is needed.
            #
            # The link is the *in-pod* route, not ``path``: a tunnel skips the
            # edge and speaks to the sidecar directly, so an Ingress sub-path
            # prefix (https://host/cremind/vnc/vnc.html) is not there to be
            # asked for - that URL on a forwarded localhost hits the SPA
            # fallback and 404s. ``path`` stays public because the SPA composes
            # it against the origin the browser really used.
            tunnel_path = in_pod_route or _NOVNC_PROXY_PATH
            commands = [
                {
                    "label": (
                        "Reaching Cremind through kubectl port-forward? The "
                        "same tunnel carries the desktop:"
                    ),
                    "command": kubernetes_port_forward(
                        namespace, service, PORT_FORWARD_LOCAL_PORT, service_port
                    ),
                    "open_url": (
                        f"http://localhost:{PORT_FORWARD_LOCAL_PORT}{tunnel_path}"
                    ),
                }
            ]
        return {
            "enabled": True,
            "access": "same_origin",
            "novnc_path": path,
            # No port of its own: it is a path on whichever origin the browser
            # already reached Cremind on.
            "novnc_port": None,
            "novnc_url": stated or None,
            "port_forward_commands": commands,
            "scheme_note": _SCHEME_NOTES["same_origin"],
        }

    port = stated_port or _DEFAULT_NOVNC_PORT
    path = stated_path or _NOVNC_DIRECT_PATH
    # Both ends of the tunnel are the Service port: the chart's own URL says
    # localhost:<port>, so forwarding it to a different local port would break
    # the link we hand out.
    open_url = f"http://localhost:{port}{path}"
    return {
        "enabled": True,
        "access": "port_forward",
        "novnc_path": path,
        "novnc_port": port,
        "novnc_url": stated or open_url,
        "port_forward_commands": [
            {
                "label": "Tunnel the desktop port on its own:",
                "command": kubernetes_port_forward(namespace, service, port, port),
                "open_url": open_url,
            },
            {
                "label": "Or carry Cremind and the desktop in one tunnel:",
                "command": (
                    kubernetes_port_forward(
                        namespace, service, PORT_FORWARD_LOCAL_PORT, service_port
                    )
                    + f" {port}:{port}"
                ),
                "open_url": open_url,
            },
        ],
        "scheme_note": _SCHEME_NOTES["port_forward"],
    }


def public_vnc_descriptor(vnc: dict) -> dict:
    """The part of the VNC descriptor an unauthenticated caller may see.

    ``/api/features/tray-capabilities`` answers before anyone has logged in -
    it exists so the desktop shell can grey out menu items - and namespaces,
    Service names and ready-to-run ``kubectl`` lines are cluster topology.
    Those stay behind the admin-only endpoints; what leaks here is only enough
    to decide whether a "VNC desktop" entry belongs in a menu and what URL it
    would open.
    """
    return {
        "enabled": bool(vnc.get("enabled")),
        "access": vnc.get("access"),
        "novnc_path": vnc.get("novnc_path"),
        "novnc_port": vnc.get("novnc_port"),
    }


@lru_cache(maxsize=1)
def _describe_cached() -> dict:
    """The facts that cannot change while this process runs.

    Everything :func:`runtime_environment_prompt_line` reads lives in here, so
    the agent's line stays byte-identical for the life of the process. The
    mutable fields are added by :func:`describe_runtime_environment`.
    """
    from app.__version__ import __version__
    from app.config.settings import BaseConfig
    from app.upgrade.channel import get_channel

    install_mode = detect_install_mode()
    container = is_container(install_mode)
    image_flavor = get_image_flavor()
    # A pre-flavor Docker image sets no CREMIND_IMAGE_FLAVOR and *is* a desktop
    # image (the basic flavor only exists because the split created it), so an
    # unknown flavor on a container install means the VNC desktop is there.
    # The flavor itself stays None — we know what the container can do, not
    # which image built it. Mirrors get_image_flavor's docstring and the
    # Electron client's own fallback.
    vnc_enabled = image_flavor == "desktop" or (
        image_flavor is None and install_mode in ("docker", "kubernetes")
    )
    # Identity first: the VNC descriptor renders the pod's own namespace and
    # Service into the port-forward commands it publishes.
    kubernetes = kubernetes_identity(install_mode)

    return {
        "release_channel": get_channel(),
        "install_mode": install_mode,
        "deployment": _detect_deployment(install_mode),
        "container": container,
        "image_flavor": image_flavor,
        "vnc_enabled": vnc_enabled,
        # ``None`` off Kubernetes; see kubernetes_identity and _describe_vnc.
        # Both are nested blocks, which is why describe_runtime_environment
        # deep-copies rather than shallow-copying what it gets from here.
        "kubernetes": kubernetes,
        "vnc": _describe_vnc(install_mode, vnc_enabled, kubernetes),
        "supervised": supervised(install_mode),
        "electron": os.environ.get("CREMIND_ELECTRON_PARENT") is not None,
        "os": platform.system(),
        "os_release": platform.release(),
        "python_version": platform.python_version(),
        "backend_version": __version__,
        # Fixed at class-definition time in app.config.settings and never
        # reassigned — unlike the System Directory, which the Setup Wizard can
        # relocate on first setup and which describe_runtime_environment
        # therefore reads live.
        "install_dir": BaseConfig.CREMIND_INSTALL_DIR,
        # The ``CREMIND_TIMEZONE`` boot default, and *only* that: not the zone
        # anything actually schedules in. That answer is per-profile and lives
        # in app.config.timezone; ``/api/system/environment`` resolves it there
        # as ``effective_timezone``.
        "boot_timezone": os.environ.get("CREMIND_TIMEZONE", ""),
    }


def describe_runtime_environment() -> dict:
    """Everything this process knows about the install it is running in.

    Returns a fresh copy of the cached description, so a caller that adds
    fields to it (the ``/api/system/environment`` handler does) cannot poison
    the copy the next caller - and the agent's prompt line - reads. The copy is
    deep because two of those fields (``kubernetes``, ``vnc``) are nested
    blocks: a shallow copy hands every caller the *same* dict and list, so the
    tray endpoint trimming the VNC descriptor down to its public subset, or a
    handler stamping a rendered URL into it, would rewrite what the next
    request - and the config export - sees.

    Three fields are read live on top of that copy because this process really
    does rewrite them: ``app.config.tls_transition`` reassigns
    ``BaseConfig.APP_URL`` when an HTTPS switch is armed (and again if it rolls
    back), with the follow-up restart optional and able to fail;
    ``relocate_system_directory`` reassigns ``BaseConfig.CREMIND_SYSTEM_DIR``
    straight from the Setup Wizard's submit handler, its only caller and a
    first-setup-only one. Cached copies of those went stale inside a single
    response — the same body's
    ``deployment_custom_fields.public_url`` reads ``APP_URL`` live, so it
    contradicted the frozen ``app_url`` two keys above it. ``host`` joins them
    because it is the sibling of that same live-read field, not because
    anything rewrites it. None of the three is in the agent's prompt line, so
    reading them live costs no prompt-cache stability.

    Tests that mutate the environment must call
    ``describe_runtime_environment.cache_clear()`` first.
    """
    from app.config.settings import BaseConfig

    described = copy.deepcopy(_describe_cached())
    described["app_url"] = BaseConfig.APP_URL
    described["system_dir"] = BaseConfig.CREMIND_SYSTEM_DIR
    described["host"] = BaseConfig.HOST
    return described


# The cache lives on the private worker, but callers (and tests) only ever see
# the public name, so hang the standard lru_cache handle off it too.
describe_runtime_environment.cache_clear = _describe_cached.cache_clear  # type: ignore[attr-defined]
describe_runtime_environment.cache_info = _describe_cached.cache_info  # type: ignore[attr-defined]


def runtime_environment_prompt_line() -> str:
    """One line describing this install, for the agent's system prompt.

    The agent gets asked "am I running in Docker?", "is VNC on?", "is this the
    dev channel?" often enough that answering from the prompt beats sending it
    to the CLI for facts that never change. One line, no trailing newline, and
    byte-identical for the life of the process — it sits inside a prompt-cached
    system message.
    """
    env = describe_runtime_environment()

    install_label = {
        "docker": "Docker install",
        "kubernetes": "Kubernetes install",
    }.get(env["install_mode"], "native install")

    details: list[str] = []
    if env["image_flavor"]:
        details.append(f"{env['image_flavor']} image")
    if env["container"]:
        details.append(
            "VNC desktop enabled" if env["vnc_enabled"] else "no VNC desktop"
        )
    if details:
        install_label += f" ({', '.join(details)})"

    parts = [
        install_label,
        f"{env['deployment']} deployment",
        f"{env['release_channel']} release channel",
        "restarts supervised" if env["supervised"] else "no supervisor",
    ]
    if env["electron"]:
        parts.append("running under the Electron desktop app")
    return f"Runtime environment: {', '.join(parts)}."
