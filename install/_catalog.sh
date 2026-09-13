# AUTO-GENERATED from install/catalog.toml. Do not edit by hand.
# Regenerate with: python install/scripts/build_catalog.py
# Source SHA-256:  38ee43526a42ade7e82271dbd714e48eb47fca76f18e654f076c903ef190f19b

CATALOG_SCHEMA=1

# ── Deployments ──
DEPLOYMENT_IDS="local server custom"
DEPLOYMENT_LABEL_local="Local"
DEPLOYMENT_SHORT_local="this machine only"
DEPLOYMENT_DESC_local="bind to 127.0.0.1, only this machine can reach it"
DEPLOYMENT_ENV_VALUE_local="local"
DEPLOYMENT_HOST_local="127.0.0.1"
DEPLOYMENT_REQUIRES_HOST_local=0
DEPLOYMENT_LABEL_server="Server"
DEPLOYMENT_SHORT_server="reachable from other devices"
DEPLOYMENT_DESC_server="bind to all interfaces, reachable from other devices"
DEPLOYMENT_ENV_VALUE_server="production"
DEPLOYMENT_HOST_server="0.0.0.0"
DEPLOYMENT_REQUIRES_HOST_server=1
DEPLOYMENT_LABEL_custom="Custom (advanced)"
DEPLOYMENT_SHORT_custom="I'll configure host, URL, and CORS myself"
DEPLOYMENT_DESC_custom="advanced setup — choose where Cremind listens and how it's reached"
DEPLOYMENT_ENV_VALUE_custom="custom"
DEPLOYMENT_HOST_custom=""
DEPLOYMENT_REQUIRES_HOST_custom=0

# ── Custom-deployment advanced fields ──
CUSTOM_FIELD_IDS="listen_host public_url allowed_origins wizard_preset"
CUSTOM_FIELD_PROMPT_listen_host="Where should Cremind listen for connections?"
CUSTOM_FIELD_HINT_listen_host="Use 127.0.0.1 to only allow this machine, or 0.0.0.0 to allow other devices / containers on the network."
CUSTOM_FIELD_DEFAULT_listen_host="0.0.0.0"
CUSTOM_FIELD_CHOICES_listen_host=""
CUSTOM_FIELD_PROMPT_public_url="What URL will you use to open Cremind in a browser?"
CUSTOM_FIELD_HINT_public_url="This is the address users type into their browser. Inside a container it's usually http://localhost:1515. On a server it might be http://my-box.lan:1515 or https://cremind.example.com."
CUSTOM_FIELD_DEFAULT_public_url="http://localhost:1515"
CUSTOM_FIELD_CHOICES_public_url=""
CUSTOM_FIELD_PROMPT_allowed_origins="Which web origins should be allowed to talk to the API?"
CUSTOM_FIELD_HINT_allowed_origins="A comma-separated list of URLs the browser UI will be served from. Usually the same as the public URL above. Leave blank to use the public URL plus localhost variants."
CUSTOM_FIELD_DEFAULT_allowed_origins=""
CUSTOM_FIELD_CHOICES_allowed_origins=""
CUSTOM_FIELD_PROMPT_wizard_preset="Which preset should pre-fill the Setup Wizard?"
CUSTOM_FIELD_HINT_wizard_preset="Presets fill in sensible defaults for the next setup screens — you can always edit any field afterwards. Pick \`local\` for a single-machine setup, \`docker\` for a docker-compose stack, or \`server\` for an external Postgres/Qdrant."
CUSTOM_FIELD_DEFAULT_wizard_preset="local"
CUSTOM_FIELD_CHOICES_wizard_preset="local docker server"

# ── Install modes ──
MODE_IDS="docker native kubernetes"
MODE_LABEL_docker="Docker"
MODE_DESC_docker="sandboxed container with a bundled storage stack (optional VNC desktop)"
MODE_HINT_docker="The agent runs inside a container. You can add a VNC desktop so it has its own GUI (observe at http://<host>:6080/vnc.html), or install the smaller headless image."
MODE_BADGE_docker="recommended"
MODE_REQUIRES_docker="docker"
MODE_LABEL_native="Native"
MODE_DESC_native="Python venv at ~/.cremind/venv with embedded storage"
MODE_HINT_native="Simpler, but the agent shares your desktop and home directory."
MODE_BADGE_native=""
MODE_REQUIRES_native=""
MODE_LABEL_kubernetes="Kubernetes"
MODE_DESC_kubernetes="Helm release in an existing cluster (in-cluster Postgres, optional VNC desktop)"
MODE_HINT_kubernetes="Installs the cremind Helm chart into a kubeconfig context you pick. Needs kubectl and helm on PATH; you reach Cremind through a kubectl port-forward at http://localhost:1515."
MODE_BADGE_kubernetes=""
MODE_REQUIRES_kubernetes="kubectl helm"

# ── Docker desktop UI ──
DOCKER_DESKTOP_PROMPT="Install the VNC Desktop UI?"
DOCKER_DESKTOP_HINT="Adds an XFCE desktop inside the container so you can watch the agent work at http://<host>:6080/vnc.html. Answer No to install the smaller headless image (cremind/cremind)."
DOCKER_DESKTOP_DEFAULT=1

# ── VNC password ──
VNC_PASSWORD_PROMPT="Choose a password for the VNC Desktop"
VNC_PASSWORD_HINT="6-8 characters, from letters, digits and @ % _ + = : , . - — VNC ignores anything past the 8th character. You will sign in with it at http://<host>:6080/vnc.html. Leave empty when re-installing to keep the current password."

# ── Kubernetes prompts ──
K8S_CONTEXT_PROMPT="Which kubeconfig context should Cremind be installed into?"
K8S_CONTEXT_HINT="Every helm and kubectl command runs with --kube-context set to this choice, never the ambient current-context. The current context is preselected; each row shows the API server so a look-alike cluster stands out."
K8S_NAMESPACE_PROMPT="Which namespace should the Helm release go into?"
K8S_NAMESPACE_HINT="Created if it does not exist. Lowercase letters, digits and hyphens, up to 63 characters."
K8S_NAMESPACE_DEFAULT="cremind"
K8S_ADVANCED_PROMPT="Use the recommended Helm options, or customize them?"
K8S_ADVANCED_HINT="Recommended: release name cremind, app URL derived from the port-forward, the legacy Bitnami Postgres image, Postgres data kept on uninstall, no extra --set values."
K8S_FIELD_IDS="release_name app_url legacy_postgres_image delete_postgres_data extra_set"
K8S_FIELD_PROMPT_release_name="What should the Helm release be called?"
K8S_FIELD_HINT_release_name="Shown by helm list. Objects are named after it (a name containing cremind is used as-is, otherwise -cremind is appended). Lowercase letters, digits and hyphens, up to 53 characters."
K8S_FIELD_DEFAULT_release_name="cremind"
K8S_FIELD_CHOICES_release_name=""
K8S_FIELD_PROMPT_app_url="What URL will you use to open Cremind in a browser?"
K8S_FIELD_HINT_app_url="Leave blank to let the chart derive it from the port-forward (http://localhost:1515, or https:// when HTTPS is on). Set it only when an ingress or load balancer serves Cremind at another address."
K8S_FIELD_DEFAULT_app_url=""
K8S_FIELD_CHOICES_app_url=""
K8S_FIELD_PROMPT_legacy_postgres_image="Use the legacy Bitnami Postgres image (docker.io/bitnamilegacy/postgresql)?"
K8S_FIELD_HINT_legacy_postgres_image="Bitnami moved its free images to the bitnamilegacy namespace, so the chart's default Postgres image no longer resolves on Docker Hub. Answer no only if your cluster mirrors the current Bitnami catalog."
K8S_FIELD_DEFAULT_legacy_postgres_image="yes"
K8S_FIELD_CHOICES_legacy_postgres_image="yes no"
K8S_FIELD_PROMPT_delete_postgres_data="Delete the Postgres volume when the release is uninstalled?"
K8S_FIELD_HINT_delete_postgres_data="no keeps the data-cremind-postgresql-0 claim after helm uninstall (the chart default) so a reinstall picks the data back up; yes sets the retention policy to Delete."
K8S_FIELD_DEFAULT_delete_postgres_data="no"
K8S_FIELD_CHOICES_delete_postgres_data="yes no"
K8S_FIELD_PROMPT_extra_set="Any extra --set values for helm?"
K8S_FIELD_HINT_extra_set="The value of one helm --set, e.g. ingress.enabled=true,ingress.host=cremind.example.com. Applied last, so it overrides the installer's own values. Leave blank for none."
K8S_FIELD_DEFAULT_extra_set=""
K8S_FIELD_CHOICES_extra_set=""

# ── Mode rules ──
MODE_RULE_ALLOWED_docker="docker native"
MODE_RULE_DEFAULT_docker="docker"
MODE_RULE_ALLOWED_native="native external"
MODE_RULE_DEFAULT_native="external"
MODE_RULE_ALLOWED_custom="docker native external"
MODE_RULE_DEFAULT_custom="external"
MODE_RULE_ALLOWED_kubernetes="external"
MODE_RULE_DEFAULT_kubernetes="external"

# ── Service modes ──
SERVICE_MODE_IDS="docker native external"
SERVICE_MODE_LABEL_docker="Docker"
SERVICE_MODE_DESC_TMPL_docker="Cremind starts a {service} container alongside itself."
SERVICE_MODE_LABEL_native="Native"
SERVICE_MODE_DESC_TMPL_native="Cremind runs {service} locally (no extra container)."
SERVICE_MODE_LABEL_external="External"
SERVICE_MODE_DESC_TMPL_external="Connect to an existing {service} instance."

# ── Helpers ──
catalog_get() {
    # Usage: catalog_get PREFIX KEY  →  prints the value of $PREFIX_KEY.
    local _name="${1}_${2}"
    eval "printf %s \"\${${_name}-}\""
}

