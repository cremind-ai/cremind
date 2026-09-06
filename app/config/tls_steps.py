"""The deployment runbook the HTTPS switch publishes, as typed steps.

Enabling HTTPS on a deployment Cremind does not own (Docker, Kubernetes, a
reverse proxy) ends in the operator running commands somewhere else. What the
server can do is say exactly *what* to change, *which* commands to run, and in
*what order* — which is what this module builds.

Two kinds of step, and the distinction is the whole point:

- ``note`` — prose. What to edit before running anything, what to expect
  afterwards. Never a command, never backticks: the UI renders it as an
  ordinary paragraph and the CLI prints it flush-left.
- ``command`` — one bare shell line, ready to paste. No ``Run `` prefix, no
  trailing period, no backticks, no newline. The UI gives *only* these a
  copy-to-clipboard button, and the CLI indents them.

``command()`` enforces that shape rather than trusting the author, because a
sentence that slipped into a command step is exactly the bug this replaces: a
copy button that yields prose. The check runs per request, so the unit-test
matrix in ``tests/config/test_tls_steps.py`` is what actually catches it.

Pure by design — no ``Request``, no ``BaseConfig``, no I/O — so every mode can
be asserted directly. ``app/api/tls.py`` supplies the facts and derives the
legacy flat ``instructions`` list from ``flatten()``.

The CLI must **not** import this module: ``app/cli/`` stays free of server
dependencies (see ``app/cli/main.py``). It renders whatever the wire payload
carries.
"""

from __future__ import annotations

import re


#: Where the chart is published. ``helm push`` in ``release-rc.yml`` and
#: ``release-prod.yml`` targets ``oci://registry-1.docker.io/cremind``, and the
#: chart is named ``cremind``. Fixed for every published build, so the command
#: carries it rather than making the operator look it up.
CHART_REFERENCE = "oci://registry-1.docker.io/cremind/cremind"

#: Restart through a supervisor that will bring the process back.
RESTART_COMMAND = "cremind server restart --yes"
#: Start an unsupervised install again by hand.
SERVE_COMMAND = "cremind serve"
#: Replace a Compose container so it re-reads its environment.
DOCKER_RECREATE_COMMAND = "docker compose up -d --force-recreate cremind"

_ATLASSIAN_CONSOLE = "Atlassian developer console"


def note(text: str) -> dict:
    """Prose guidance. Rendered as plain text; never copyable."""
    value = (text or "").strip()
    if not value:
        raise ValueError("A note needs text.")
    if "`" in value:
        raise ValueError(
            f"A note is rendered as plain text, so backticks would show up "
            f"literally: {value!r}"
        )
    return {"kind": "note", "text": value}


def command(text: str) -> dict:
    """One bare shell line, ready to paste.

    The constraints are what make the copy button trustworthy: whatever is in
    ``text`` is exactly what lands on the clipboard, so a sentence, a trailing
    period or a ``Run `` prefix would each be pasted into a terminal verbatim.
    """
    value = (text or "").strip()
    if not value:
        raise ValueError("A command needs text.")
    if "\n" in value:
        raise ValueError(f"A command must be a single line: {value!r}")
    if value.endswith("."):
        raise ValueError(f"A command must not end with a sentence period: {value!r}")
    if value.startswith("Run "):
        raise ValueError(
            f"A command must be bare — move the instruction into a note: {value!r}"
        )
    if "`" in value:
        raise ValueError(f"A command must not be quoted in backticks: {value!r}")
    return {"kind": "command", "text": value}


def flatten(steps: list[dict]) -> list[str]:
    """The legacy flat ``instructions`` list older clients still read."""
    return [step["text"] for step in steps]


_PEP440_RC = re.compile(r"^(\d+\.\d+\.\d+(?:\.\d+)?)rc(\d+)(?:\.dev(\d+))?$")


def pep440_to_semver(version: str) -> str:
    """This build's version as the chart's SemVer2 spelling.

    ``0.0.17rc9.dev4`` → ``0.0.17-rc.9.dev.4``; a stable version is already
    SemVer2 and passes through. Helm rejects the PEP 440 form outright, so the
    two spellings of one release cannot be used interchangeably.

    Deliberately duplicated from ``scripts/sync_ui_version.py``, which the
    release workflow uses to stamp the chart: ``scripts/`` is not packaged into
    the wheel (``pyproject.toml`` ships ``packages = ["app"]``), so a running
    server cannot import it. ``tests/config/test_tls_steps.py`` pins the two
    implementations equal.
    """
    match = _PEP440_RC.match(version)
    if not match:
        return version
    base, rc, dev = match.group(1), match.group(2), match.group(3)
    return f"{base}-rc.{rc}.dev.{dev}" if dev is not None else f"{base}-rc.{rc}"


def running_chart_version() -> str:
    """The chart version that shipped the build answering this request."""
    from app.__version__ import __version__

    return pep440_to_semver(__version__)


# ── the runbook, per deployment mode ─────────────────────────────────────


def _chart_note(chart_version: str) -> dict:
    """Why the chart reference and version are already filled in.

    Both are knowable from here — the registry is fixed and the version is this
    build's own — so the operator only has to supply what is genuinely local to
    their install. The escape hatches matter though: a chart installed from a
    checkout has a different source, and an install whose image tag was pinned
    by hand can be running a build its chart never shipped.
    """
    return note(
        "The chart reference and version above are already filled in for this "
        f"build ({chart_version}). Keep the --version pin: without it Helm "
        "resolves whatever the registry calls latest — skipping pre-release "
        "charts entirely — instead of the version you are running. Use a local "
        "path in place of the registry reference if you installed from a "
        "checkout, and the chart version helm list reports for this release if "
        "it differs from the one above."
    )


def _ingress_steps(https_url: str, chart_version: str) -> list[dict]:
    """Kubernetes with an Ingress terminating TLS.

    No port-forward anywhere: the public hostname *is* the HTTPS address here,
    and the certificate belongs to the Ingress, not to Cremind.
    """
    return [
        note(
            "Cremind keeps its in-pod TLS disabled here: the Ingress terminates "
            "HTTPS with its own certificate, so no Cremind CA is involved. Obtain "
            "a certificate that covers the public hostname (from cert-manager or "
            "your issuer) before continuing."
        ),
        note(
            "Edit your Helm values first: set ingress.enabled=true, ingress.host, "
            "and ingress.tls with the HTTPS host and its certificate Secret; set "
            "cremind.appUrl to the public HTTPS origin; set "
            "ingress.trustedProxyCidrs to the Ingress controller's source range "
            "(it becomes FORWARDED_ALLOW_IPS, so only that proxy may assert the "
            "HTTPS scheme); leave cremind.ssl empty and remove any CREMIND_SSL "
            "entry from cremind.extraEnv."
        ),
        note(
            "Do not enable an HTTP-to-HTTPS redirect or HSTS on the controller: "
            "old HTTP links must still reach Cremind's recovery page, and Cremind "
            "refuses plaintext API calls on its own after activation."
        ),
        note(
            "Allow the public HTTPS /api/oauth/callback URI in the "
            f"{_ATLASSIAN_CONSOLE} before linking Jira or Confluence; set "
            "cremind.atlassianRedirectUri only when the callback differs from the "
            "one derived from cremind.appUrl."
        ),
        note(
            "Then run these commands in order from a machine with helm and kubectl "
            "access, replacing <release> and <namespace> with the NAME and "
            "NAMESPACE the first command prints, plus the certificate paths and "
            "your values file. Skip the create-secret command when cert-manager or "
            "another issuer already owns the Secret. One Helm upgrade moves the "
            "proxy, Service, probes and public URL together; changing only the "
            "container environment breaks routing."
        ),
        command("helm list --all-namespaces"),
        command(
            "kubectl --namespace <namespace> create secret tls cremind-tls "
            "--cert=<path-to-fullchain.pem> --key=<path-to-privkey.pem>"
        ),
        command(
            f"helm upgrade <release> {CHART_REFERENCE} --version {chart_version} "
            "--namespace <namespace> --reuse-values -f <your-values.yaml>"
        ),
        _chart_note(chart_version),
        command(
            "kubectl --namespace <namespace> rollout status "
            "deployment/<release> --timeout=5m"
        ),
        note(
            "Verify the rollout: the Ingress should list the host and TLS Secret, "
            "and the status endpoint should answer over HTTPS."
        ),
        command("kubectl --namespace <namespace> get ingress <release>"),
        command(f"curl --fail {https_url}/api/tls/status"),
        note(
            f"Cremind then answers at {https_url}; tabs that joined this switch "
            "move there on their own, and devices need only the certificate "
            "issuer's normal trust chain. An old HTTP link shows the recovery page "
            "and an HTTP API request returns status 426; if the controller "
            "redirects instead, disable its redirect or HSTS setting."
        ),
    ]


def _kubernetes_steps(https_url: str, chart_version: str) -> list[dict]:
    """Kubernetes terminating TLS inside the pod (``cremind.ssl=auto``)."""
    return [
        note(
            "Edit your Helm values first: set cremind.ssl=auto, remove any "
            "CREMIND_SSL entry from cremind.extraEnv, and change cremind.appUrl to "
            "the HTTPS origin if it is set. Add a LAN hostname or IP to "
            "cremind.sslAutoHosts if you reach Cremind that way."
        ),
        note(
            "If cremind.atlassianRedirectUri is customized, change it to the HTTPS "
            f"/api/oauth/callback URL and allow that exact URI in the "
            f"{_ATLASSIAN_CONSOLE} before linking Jira or Confluence."
        ),
        note(
            "Then run these commands in order from a machine with helm and kubectl "
            "access, replacing <release> and <namespace> with the NAME and "
            "NAMESPACE the first command prints. The Deployment and Service carry "
            "the release name, or <release>-cremind when the release name does not "
            "contain cremind. Upgrade through Helm so the proxy sidecar, Service, "
            "probes and URLs change together; editing only the container "
            "environment breaks routing."
        ),
        command("helm list --all-namespaces"),
        command(
            f"helm upgrade <release> {CHART_REFERENCE} --version {chart_version} "
            "--namespace <namespace> --reuse-values --set cremind.ssl=auto"
        ),
        _chart_note(chart_version),
        command(
            "kubectl --namespace <namespace> rollout status "
            "deployment/<release> --timeout=5m"
        ),
        note(
            "Only if you reach Cremind through kubectl port-forward: the rollout "
            "replaced the pod and closed the old tunnel, so open it again, adding "
            "1455:1455 and 6080:6080 if your previous port-forward had them."
        ),
        command("kubectl --namespace <namespace> port-forward svc/<release> 1515:80"),
        note(
            f"When the rollout finishes Cremind answers at {https_url}. Tabs that "
            "joined this switch move there on their own; if a browser warns about "
            "the certificate, trust the Cremind CA on that device first. Keep the "
            "system-directory PVC and proxy.enabled=true so the CA and the relay "
            "for old HTTP links survive later restarts."
        ),
    ]


def _docker_steps(https_url: str) -> list[dict]:
    """Docker Compose on a host Cremind cannot reach into.

    The per-OS paths are the installer's own (``$CREMIND_INSTALL_DIR/docker``),
    not ``~/.cremind`` — a Docker install leaves that directory untouched.
    """
    return [
        note(
            "On the Docker host, edit the .env file next to docker-compose.yml in "
            "the installer's docker folder: ~/.local/share/cremind/docker on "
            "Linux, ~/Library/Application Support/Cremind/docker on macOS, "
            "%LOCALAPPDATA%\\Cremind\\docker on Windows (or wherever you keep the "
            "Compose project)."
        ),
        note(
            "In that file set CREMIND_SSL=auto and change APP_URL to the HTTPS "
            "origin. If CORS_ALLOWED_ORIGINS is set, keep the HTTP origin and add "
            "the HTTPS one. Add a LAN hostname or IP to CREMIND_SSL_AUTO_HOSTS if "
            "you reach Cremind that way."
        ),
        note(
            "If Jira or Confluence linking is used, set "
            "CREMIND_ATLASSIAN_REDIRECT_URI to the HTTPS /api/oauth/callback URL "
            f"and allow that exact URI in the {_ATLASSIAN_CONSOLE} before linking "
            "an account."
        ),
        note(
            "Then recreate the container from that folder. Keep the "
            "system-directory volume mounted: it holds the CA and the transition, "
            "so both survive the replacement."
        ),
        command(DOCKER_RECREATE_COMMAND),
        note(
            f"When the container is back Cremind answers at {https_url}; tabs that "
            "joined this switch move there on their own. If a browser warns about "
            "the certificate, trust the Cremind CA on that device first."
        ),
    ]


def _reverse_proxy_steps(restart_supported: bool, https_url: str) -> list[dict]:
    """A proxy owns the public origin; Cremind serves only its loopback port.

    Activation never persists APP_URL for an external manager and never
    schedules a restart here, so the restart is the operator's to run.
    """
    return [
        note(
            "Cremind serves only its loopback port here; the reverse proxy in "
            "front of it owns the public HTTPS address. Give the proxy a "
            "certificate for the public hostname, forward requests with the public "
            "Host header, and let it assert the HTTPS scheme only from its own "
            "address (FORWARDED_ALLOW_IPS in Cremind's .env)."
        ),
        note(
            "In Cremind's .env (in its system directory) set APP_URL to the HTTPS "
            "origin and, if CORS_ALLOWED_ORIGINS is set, keep the HTTP origin and "
            "add the HTTPS one. CREMIND_SSL stays unset: the proxy, not Cremind, "
            "terminates TLS."
        ),
        note(
            "Change CREMIND_ATLASSIAN_REDIRECT_URI to the public HTTPS "
            "/api/oauth/callback URL and allow that exact URI in the "
            f"{_ATLASSIAN_CONSOLE} before linking Jira or Confluence."
        ),
        note(
            "Keep the proxy's HTTP endpoint forwarding page requests to Cremind so "
            "old links reach the recovery page; Cremind refuses plaintext API "
            "requests after activation, so do not add a blanket HTTP-to-HTTPS "
            "redirect."
        ),
        note(
            "Reload the proxy, then restart Cremind so it reads the new APP_URL:"
            if restart_supported else
            "Reload the proxy, then stop the running Cremind process (Ctrl+C in "
            "the terminal that runs it) and start it again so it reads the new "
            "APP_URL:"
        ),
        command(RESTART_COMMAND if restart_supported else SERVE_COMMAND),
        note(
            f"Cremind then answers at {https_url} through the proxy; tabs that "
            "joined this switch move there on their own. Devices need only the "
            "proxy certificate's normal trust chain."
        ),
    ]


def _unsupervised_native_steps(activating: bool, https_url: str) -> list[dict]:
    """A native install with nothing to bring the process back up.

    The opening note is phase-aware: before activation it is a warning about
    what will be needed, after it a statement of where the install stands.
    """
    return [
        note(
            "HTTPS settings are saved, but nothing supervises this server, so it "
            "cannot restart itself. Stop the running process (Ctrl+C in the "
            "terminal that runs it), then start it again from the same "
            "installation:"
            if activating else
            "After activation this server cannot restart itself because nothing "
            "supervises it. Stop the running process (Ctrl+C in the terminal that "
            "runs it), then start it again from the same installation:"
        ),
        command(SERVE_COMMAND),
        note(
            f"Cremind then answers at {https_url}; tabs that joined this switch "
            "move there on their own once the secure listener answers. If a "
            "browser warns about the certificate, trust the Cremind CA on that "
            "device first."
        ),
    ]


def deployment_steps(
    *,
    manager: str,
    install_mode: str,
    edge: bool,
    restart_supported: bool,
    activating: bool,
    https_url: str,
    chart_version: str,
) -> list[dict]:
    """What this deployment has to do to finish the switch to HTTPS.

    ``chart_version`` is this build's own, in the chart's SemVer2 spelling —
    see :func:`running_chart_version`. Empty for a supervised native install
    and for Electron: those restart themselves, so there is nothing for the
    operator to run.
    """
    if manager == "external":
        if edge:
            return _ingress_steps(https_url, chart_version)
        if install_mode == "docker":
            return _docker_steps(https_url)
        if install_mode == "kubernetes":
            return _kubernetes_steps(https_url, chart_version)
        return _reverse_proxy_steps(restart_supported, https_url)
    if manager == "native" and not restart_supported:
        return _unsupervised_native_steps(activating, https_url)
    return []


def certificate_repair_steps(
    *, manager: str, install_mode: str, restart_supported: bool
) -> list[dict]:
    """How to load a replaced certificate, for a server already on HTTPS.

    Deliberately *only* the reload: the certificate itself is replaced outside
    Cremind, and repeating the enable runbook to a server that already serves
    HTTPS is what made this page confusing.
    """
    if install_mode == "docker":
        return [
            note(
                "Once the replacement certificate is in place, recreate the "
                "container so it loads it:"
            ),
            command(DOCKER_RECREATE_COMMAND),
        ]
    if install_mode == "kubernetes":
        return [
            note(
                "Once the replacement certificate is in place, restart the pod so "
                "it loads it:"
            ),
            command(
                "kubectl --namespace <namespace> rollout restart "
                "deployment/<release>"
            ),
            command(
                "kubectl --namespace <namespace> rollout status "
                "deployment/<release> --timeout=5m"
            ),
        ]
    if manager == "electron":
        return [
            note(
                "Quit and reopen the Cremind desktop app after replacing the "
                "certificate."
            ),
        ]
    if restart_supported:
        return [
            note(
                "Once the replacement certificate is in place, restart Cremind so "
                "it loads it:"
            ),
            command(RESTART_COMMAND),
        ]
    return [
        note(
            "Once the replacement certificate is in place, stop the running "
            "process (Ctrl+C in the terminal that runs it) and start it again:"
        ),
        command(SERVE_COMMAND),
    ]


def atlassian_callback_step(uri: str) -> dict:
    """The moved OAuth callback.

    A note, not a command: this is pasted into a web console, not a shell.
    """
    return note(
        f"Jira or Confluence linking now uses the callback {uri}. Add that exact "
        f"URI to the allowed redirect URIs in the {_ATLASSIAN_CONSOLE} before "
        "linking an account."
    )
