"""Render the Helm transport options and verify every dependent listener."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
HELM = shutil.which("helm")
pytestmark = pytest.mark.skipif(not HELM, reason="Helm is not installed")


def _render(*values: str, succeeds: bool = True, set_strings: tuple[str, ...] = ()):
    command = [HELM, "template", "transport-test", str(ROOT / "helm/cremind"),
               "--set", "fullnameOverride=cremind",
               "--set", "readinessProbe.enabled=true", "--set", "livenessProbe.enabled=true"]
    for value in values:
        command.extend(["--set", value])
    for value in set_strings:
        command.extend(["--set-string", value])
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=30)
    if not succeeds:
        assert result.returncode != 0, result.stdout
        return result.stderr
    assert result.returncode == 0, result.stderr
    resources = [document for document in yaml.safe_load_all(result.stdout) if document]
    ingress = next((d for d in resources if d["kind"] == "Ingress"), None)
    ingress_service = next(
        (d for d in resources if d["kind"] == "Service" and d["metadata"]["name"] == "cremind-ingress"),
        None,
    )
    proxy = next(
        (d["data"] for d in resources if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "cremind-proxy"),
        None,
    )
    return {
        "deployment": next(d for d in resources if d["kind"] == "Deployment" and d["metadata"]["name"] == "cremind"),
        "env": next(d["data"] for d in resources if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "cremind-env"),
        "proxy": proxy,
        "service": next(d for d in resources if d["kind"] == "Service" and d["metadata"]["name"] == "cremind"),
        "ingress": ingress,
        "ingress_service": ingress_service,
    }


@pytest.mark.parametrize("value", [None, "", "false", "none"])
def test_http_default_and_opt_out(value) -> None:
    rendered = _render(*([] if value is None else [f"cremind.ssl={value}"]))
    assert rendered["env"]["APP_URL"] == "http://localhost:1515"
    assert "CREMIND_SSL" not in rendered["env"]
    assert "default.conf" in rendered["proxy"]
    assert rendered["service"]["spec"]["ports"][0]["targetPort"] == "http"
    app = rendered["deployment"]["spec"]["template"]["spec"]["containers"][0]
    assert app["readinessProbe"]["httpGet"].get("scheme", "HTTP") == "HTTP"


@pytest.mark.parametrize("value, mode", [("true", "after-setup"), ("after-setup", "after-setup"), ("auto", "auto")])
def test_https_updates_proxy_service_urls_and_probes_together(value, mode) -> None:
    rendered = _render(f"cremind.ssl={value}")
    assert rendered["env"]["CREMIND_SSL"] == mode
    assert rendered["env"]["APP_URL"] == "https://localhost:1515"
    assert rendered["env"]["CREMIND_ATLASSIAN_REDIRECT_URI"].startswith("https://")
    assert "stream {" in rendered["proxy"]["nginx.conf"]
    ports = rendered["service"]["spec"]["ports"]
    assert ports[0]["name"] == "https"
    assert ports[0]["targetPort"] == "relay"
    assert any(port["name"] == "novnc" and port["port"] == 6080 for port in ports)
    app = rendered["deployment"]["spec"]["template"]["spec"]["containers"][0]
    for probe in (app["readinessProbe"], app["livenessProbe"]):
        if mode == "after-setup":
            assert probe["tcpSocket"] == {"port": "ui"}
            assert "httpGet" not in probe
            assert "appProtocol" not in ports[0]
        else:
            assert probe["httpGet"]["scheme"] == "HTTPS"
            assert ports[0]["appProtocol"] == "https"


def test_legacy_extra_env_keeps_https() -> None:
    rendered = _render("cremind.extraEnv[0].name=CREMIND_SSL", "cremind.extraEnv[0].value=auto")
    assert rendered["env"]["CREMIND_SSL"] == "auto"
    assert rendered["env"]["APP_URL"].startswith("https://")


@pytest.mark.parametrize("value", ["none", "false", "0", "no"])
def test_legacy_http_aliases_keep_every_listener_plain(value) -> None:
    rendered = _render(
        "cremind.extraEnv[0].name=CREMIND_SSL",
        set_strings=(f"cremind.extraEnv[0].value={value}",),
    )
    assert "CREMIND_SSL" not in rendered["env"]
    assert rendered["env"]["APP_URL"] == "http://localhost:1515"
    assert "default.conf" in rendered["proxy"]
    assert rendered["service"]["spec"]["ports"][0]["targetPort"] == "http"


@pytest.mark.parametrize("value", ["true", "false", "none", "auto"])
def test_explicit_mode_rejects_conflicting_legacy_env(value) -> None:
    legacy = "auto" if value != "auto" else "after-setup"
    error = _render(f"cremind.ssl={value}", "cremind.extraEnv[0].name=CREMIND_SSL",
                    f"cremind.extraEnv[0].value={legacy}", succeeds=False)
    assert "contradicts" in error


def test_boolean_https_accepts_matching_legacy_mode() -> None:
    rendered = _render("cremind.ssl=true", "cremind.extraEnv[0].name=CREMIND_SSL",
                       "cremind.extraEnv[0].value=after-setup")
    assert rendered["env"]["CREMIND_SSL"] == "after-setup"


def test_string_true_keeps_the_guided_chart_semantics() -> None:
    rendered = _render(set_strings=("cremind.ssl=true",))
    assert rendered["env"]["CREMIND_SSL"] == "after-setup"


@pytest.mark.parametrize(
    "first_class, legacy",
    [("none", "false"), ("false", "none"), ("auto", "true")],
)
def test_alias_modes_accept_equivalent_legacy_values(first_class, legacy) -> None:
    rendered = _render(
        f"cremind.ssl={first_class}",
        "cremind.extraEnv[0].name=CREMIND_SSL",
        set_strings=(f"cremind.extraEnv[0].value={legacy}",),
    )
    expected = "auto" if first_class == "auto" else None
    assert rendered["env"].get("CREMIND_SSL") == expected


@pytest.mark.parametrize("incompatible", ["ingress.enabled=true", "cremind.appUrl=http://cremind.lan:1515"])
def test_boolean_https_rejects_incompatible_routing(incompatible) -> None:
    _render("cremind.ssl=true", incompatible, succeeds=False)


def test_ingress_marks_edge_tls_without_enabling_in_pod_tls() -> None:
    rendered = _render(
        "ingress.enabled=true",
        "ingress.host=cremind.example.com",
        "service.type=LoadBalancer",
    )
    assert rendered["env"]["CREMIND_TLS_TERMINATION"] == "edge"
    assert rendered["env"]["APP_URL"] == "http://cremind.example.com"
    assert "CREMIND_SSL" not in rendered["env"]
    assert rendered["ingress"]["metadata"].get("annotations") is None
    assert rendered["service"]["spec"]["type"] == "LoadBalancer"
    assert rendered["service"]["spec"]["ports"][0]["targetPort"] == "http"
    assert all(port["targetPort"] != "edge-http" for port in rendered["service"]["spec"]["ports"])
    assert rendered["ingress"]["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"] == {
        "name": "cremind-ingress",
        "port": {"number": 80},
    }
    assert rendered["ingress_service"]["spec"]["type"] == "ClusterIP"
    assert rendered["ingress_service"]["spec"]["ports"][0]["targetPort"] == "edge-http"
    proxy_ports = rendered["deployment"]["spec"]["template"]["spec"]["containers"][1]["ports"]
    assert {port["name"]: port["containerPort"] for port in proxy_ports} == {
        "http": 8080,
        "edge-http": 8082,
    }
    config = rendered["proxy"]["default.conf"]
    assert '"8082:https" https;' in config
    assert any(line.split() == ["default", "$scheme;"] for line in config.splitlines())
    assert "default $http_x_forwarded_proto" not in config
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in config
    assert "$proxy_add_x_forwarded_for" not in config
    assert "proxy_set_header Host $forwarded_host;" in config
    assert "proxy_set_header Host $host;" not in config
    recovery_map = config.split("$edge_plaintext_recovery", 1)[1]
    assert '"8082:http"  1;' not in recovery_map
    special_map = config.split("$plaintext_special_recovery", 1)[1]
    assert '"8080:http"' not in special_map


def test_ingress_preserves_custom_public_port_and_upgrade_default() -> None:
    rendered = _render(
        "ingress.enabled=true",
        "ingress.host=cremind.example.com",
        "ingress.tls[0].hosts[0]=cremind.example.com",
        "ingress.tls[0].secretName=cremind-tls",
        "cremind.appUrl=https://cremind.example.com:8443",
        "proxy.edgePort=null",
        set_strings=("ingress.trustedProxyCidrs=10.42.1.0/24",),
    )
    assert rendered["env"]["APP_URL"] == "https://cremind.example.com:8443"
    config = rendered["proxy"]["default.conf"]
    assert '"8082:https" https;' in config
    assert "map $http_host $forwarded_host" in config
    proxy_ports = rendered["deployment"]["spec"]["template"]["spec"]["containers"][1]["ports"]
    assert any(port == {"name": "edge-http", "containerPort": 8082} for port in proxy_ports)


def test_ingress_tls_keeps_plaintext_recovery_reachable() -> None:
    rendered = _render(
        "ingress.enabled=true",
        "ingress.host=cremind.example.com",
        "ingress.tls[0].hosts[0]=cremind.example.com",
        "ingress.tls[0].secretName=cremind-tls",
        set_strings=("ingress.trustedProxyCidrs=10.42.1.0/24",),
    )
    assert rendered["env"]["CREMIND_TLS_TERMINATION"] == "edge"
    assert rendered["env"]["APP_URL"] == "https://cremind.example.com"
    assert "CREMIND_SSL" not in rendered["env"]
    assert rendered["ingress"]["spec"]["tls"] == [
        {"hosts": ["cremind.example.com"], "secretName": "cremind-tls"}
    ]
    annotations = rendered["ingress"]["metadata"]["annotations"]
    assert annotations["nginx.ingress.kubernetes.io/ssl-redirect"] == "false"
    assert annotations["nginx.ingress.kubernetes.io/force-ssl-redirect"] == "false"
    config = rendered["proxy"]["default.conf"]
    recovery_map = config.split("$edge_plaintext_recovery", 1)[1]
    assert '"8082:http"  1;' in recovery_map
    special_map = config.split("$plaintext_special_recovery", 1)[1]
    assert '"8080:http"' in special_map
    assert '"8082:http"' in special_map
    assert "error_page 418 = @cremind_recovery;" in config
    assert config.count("if ($edge_plaintext_recovery) { return 418; }") == 1
    assert config.count("if ($plaintext_special_recovery) { return 418; }") == 3
    assert "location @cremind_recovery" in config
    assert "proxy_set_header X-Forwarded-Proto $forwarded_proto;" in config


def test_ingress_preserves_explicit_redirect_policy() -> None:
    rendered = _render(
        "ingress.enabled=true",
        "ingress.tls[0].hosts[0]=cremind.example.com",
        "ingress.tls[0].secretName=cremind-tls",
        set_strings=(
            r"ingress.annotations.nginx\.ingress\.kubernetes\.io/ssl-redirect=true",
            r"ingress.annotations.nginx\.ingress\.kubernetes\.io/force-ssl-redirect=true",
            "ingress.trustedProxyCidrs=10.42.1.0/24",
        ),
    )
    annotations = rendered["ingress"]["metadata"]["annotations"]
    assert annotations["nginx.ingress.kubernetes.io/ssl-redirect"] == "true"
    assert annotations["nginx.ingress.kubernetes.io/force-ssl-redirect"] == "true"


def test_ingress_edge_listener_must_be_separate() -> None:
    error = _render(
        "ingress.enabled=true",
        "proxy.edgePort=8080",
        succeeds=False,
    )
    assert "proxy.edgePort must differ" in error

    error = _render(
        "ingress.enabled=true",
        "proxy.edgePort=443",
        succeeds=False,
    )
    assert "unprivileged TCP port" in error


def test_ingress_tls_requires_narrow_proxy_trust() -> None:
    error = _render(
        "ingress.enabled=true",
        "ingress.tls[0].hosts[0]=cremind.example.com",
        "ingress.tls[0].secretName=cremind-tls",
        succeeds=False,
    )
    assert "ingress.trustedProxyCidrs" in error

    rendered = _render(
        "ingress.enabled=true",
        "ingress.tls[0].hosts[0]=cremind.example.com",
        "ingress.tls[0].secretName=cremind-tls",
        "proxy.enabled=false",
        set_strings=("ingress.trustedProxyCidrs=10.42.0.0/16",),
    )
    assert rendered["env"]["FORWARDED_ALLOW_IPS"] == "10.42.0.0/16"
    assert rendered["ingress_service"]["spec"]["ports"][0]["targetPort"] == "ui"
    assert rendered["proxy"] is None


def test_ingress_tls_without_sidecar_accepts_matching_explicit_proxy_trust_env() -> None:
    rendered = _render(
        "ingress.enabled=true",
        "ingress.tls[0].hosts[0]=cremind.example.com",
        "ingress.tls[0].secretName=cremind-tls",
        "proxy.enabled=false",
        "cremind.extraEnv[0].name=FORWARDED_ALLOW_IPS",
        set_strings=(
            "cremind.extraEnv[0].value=10.42.0.0/16",
            "ingress.trustedProxyCidrs=10.42.0.0/16",
        ),
    )
    app = rendered["deployment"]["spec"]["template"]["spec"]["containers"][0]
    forwarded = next(item for item in app["env"] if item["name"] == "FORWARDED_ALLOW_IPS")
    assert forwarded["value"] == "10.42.0.0/16"


def test_ingress_tls_does_not_accept_extra_env_as_sidecar_trust() -> None:
    error = _render(
        "ingress.enabled=true",
        "ingress.tls[0].hosts[0]=cremind.example.com",
        "ingress.tls[0].secretName=cremind-tls",
        "cremind.extraEnv[0].name=FORWARDED_ALLOW_IPS",
        succeeds=False,
        set_strings=("cremind.extraEnv[0].value=10.42.0.0/16",),
    )
    assert "ingress.trustedProxyCidrs" in error


def test_default_sidecar_separates_controller_and_app_trust() -> None:
    rendered = _render(
        "ingress.enabled=true",
        "ingress.tls[0].hosts[0]=cremind.example.com",
        "ingress.tls[0].secretName=cremind-tls",
        set_strings=("ingress.trustedProxyCidrs=10.42.0.0/16",),
    )
    config = rendered["proxy"]["default.conf"]
    assert '"10.42.0.0/16" 1;' in config
    assert 'set_real_ip_from "10.42.0.0/16";' in config
    assert "if ($reject_untrusted_edge) { return 403; }" in config
    # The application sees nginx on pod loopback; trusting controller CIDRs at
    # this second hop would make the sanitized scheme invisible to Starlette.
    assert rendered["env"]["FORWARDED_ALLOW_IPS"] == "127.0.0.1"


def test_sidecar_can_restrict_edge_listener_to_controller_cidr() -> None:
    rendered = _render(
        "ingress.enabled=true",
        "ingress.tls[0].hosts[0]=cremind.example.com",
        "ingress.tls[0].secretName=cremind-tls",
        set_strings=("ingress.trustedProxyCidrs=10.42.0.0/16",),
    )
    config = rendered["proxy"]["default.conf"]
    assert "geo $realip_remote_addr $edge_peer_trusted" in config
    assert '"10.42.0.0/16" 1;' in config
    assert 'set_real_ip_from "10.42.0.0/16";' in config
    assert "real_ip_recursive on;" in config
    assert "if ($reject_untrusted_edge) { return 403; }" in config


@pytest.mark.parametrize("trusted", ["*", "0.0.0.0/0", "::/0"])
def test_ingress_tls_rejects_wildcard_proxy_trust(trusted: str) -> None:
    error = _render(
        "ingress.enabled=true",
        "ingress.tls[0].hosts[0]=cremind.example.com",
        "ingress.tls[0].secretName=cremind-tls",
        "proxy.enabled=false",
        succeeds=False,
        set_strings=(f"ingress.trustedProxyCidrs={trusted}",),
    )
    assert "FORWARDED_ALLOW_IPS is unsafe" in error


def test_ingress_rejects_malformed_controller_cidr() -> None:
    error = _render(
        "ingress.enabled=true",
        succeeds=False,
        set_strings=("ingress.trustedProxyCidrs=10.42.0.0/16;allow all",),
    )
    assert "comma-separated IP addresses or CIDR" in error


def test_ingress_tls_rejects_plaintext_public_url() -> None:
    error = _render(
        "ingress.enabled=true",
        "ingress.tls[0].hosts[0]=cremind.example.com",
        "ingress.tls[0].secretName=cremind-tls",
        "cremind.appUrl=http://cremind.example.com",
        succeeds=False,
    )
    assert "ingress.tls configures edge HTTPS" in error


def test_external_edge_certificate_also_gets_recovery_policy() -> None:
    rendered = _render(
        "ingress.enabled=true",
        "cremind.appUrl=https://cremind.example.com:8443",
        set_strings=("ingress.trustedProxyCidrs=10.42.1.0/24",),
    )
    annotations = rendered["ingress"]["metadata"]["annotations"]
    assert annotations["nginx.ingress.kubernetes.io/ssl-redirect"] == "false"
    assert annotations["nginx.ingress.kubernetes.io/force-ssl-redirect"] == "false"
