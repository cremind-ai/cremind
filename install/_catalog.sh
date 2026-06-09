# AUTO-GENERATED from install/catalog.toml. Do not edit by hand.
# Regenerate with: python install/scripts/build_catalog.py
# Source SHA-256:  a69551b4dc46210a0aa24a54901fcb1ff5da891cfd41962f59f60f8d7861620d

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
CUSTOM_FIELD_HINT_public_url="This is the address users type into their browser. Inside a container it's usually http://localhost:1112. On a server it might be http://my-box.lan:1112 or https://cremind.example.com."
CUSTOM_FIELD_DEFAULT_public_url="http://localhost:1112"
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
MODE_DESC_docker="sandboxed VNC desktop with a bundled storage stack"
MODE_HINT_docker="The agent runs inside a container with its own GUI. Observe at http://<host>:6080/vnc.html."
MODE_BADGE_docker="recommended"
MODE_REQUIRES_docker="docker"
MODE_LABEL_native="Native"
MODE_DESC_native="Python venv at ~/.cremind/venv with embedded storage"
MODE_HINT_native="Simpler, but the agent shares your desktop and home directory."
MODE_BADGE_native=""
MODE_REQUIRES_native=""
MODE_LABEL_kubernetes="Kubernetes"
MODE_DESC_kubernetes="Helm-deployed pod; storage and vector DBs are in-cluster services"
MODE_HINT_kubernetes="Single-replica Deployment with shared external state."
MODE_BADGE_kubernetes=""
MODE_REQUIRES_kubernetes="kubernetes"

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

