{{/*
Expand the name of the chart.
*/}}
{{- define "cremind.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully qualified app name.
*/}}
{{- define "cremind.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "cremind.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels.
*/}}
{{- define "cremind.labels" -}}
helm.sh/chart: {{ include "cremind.chart" . }}
{{ include "cremind.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/*
Selector labels.
*/}}
{{- define "cremind.selectorLabels" -}}
app.kubernetes.io/name: {{ include "cremind.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
ServiceAccount name.
*/}}
{{- define "cremind.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "cremind.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Private ClusterIP Service referenced by the chart's Ingress. Keeping it
separate from the user-facing Service lets nginx trust forwarded scheme
headers on one listener without letting a public port-forward or LoadBalancer
client spoof HTTPS. Multi-tenant clusters can restrict its peers with
ingress.trustedProxyCidrs.
*/}}
{{- define "cremind.ingressServiceName" -}}
{{- $base := include "cremind.fullname" . | trunc 55 | trimSuffix "-" -}}
{{- printf "%s-ingress" $base -}}
{{- end -}}

{{/* Keep upgrades using --reuse-values safe when edgePort did not exist. */}}
{{- define "cremind.edgePort" -}}
{{- .Values.proxy.edgePort | default 8082 -}}
{{- end -}}

{{/* Edge HTTPS may use ingress.tls or a controller-managed default cert. */}}
{{- define "cremind.ingressHttps" -}}
{{- $tlsConfigured := gt (len (.Values.ingress.tls | default list)) 0 -}}
{{- $httpsAppUrl := hasPrefix "https://" (.Values.cremind.appUrl | default "") -}}
{{- if and .Values.ingress.enabled (or $tlsConfigured $httpsAppUrl) -}}true{{- end -}}
{{- end -}}

{{/*
Image repository, auto-selected from the desktop flavor toggle unless
explicitly overridden via image.repository:
  image.repository set     -> that value (wins over the toggle)
  desktop.enabled true      -> cremind/cremind-desktop
  desktop.enabled false     -> cremind/cremind
*/}}
{{- define "cremind.imageRepository" -}}
{{- if .Values.image.repository -}}
{{- .Values.image.repository -}}
{{- else if .Values.desktop.enabled -}}
cremind/cremind-desktop
{{- else -}}
cremind/cremind
{{- end -}}
{{- end -}}

{{/*
Resolved image reference (registry/repository:tag), tag defaulting to appVersion.
*/}}
{{- define "cremind.image" -}}
{{- $tag := default .Chart.AppVersion .Values.image.tag -}}
{{- $repo := include "cremind.imageRepository" . -}}
{{- if .Values.image.registry -}}
{{- printf "%s/%s:%s" .Values.image.registry $repo $tag -}}
{{- else -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end -}}
{{- end -}}

{{/*
Release channel for the in-app updater (CREMIND_UPGRADE_CHANNEL). Derived from
the SAME effective image tag cremind.image resolves (appVersion unless
image.tag overrides it): an RC build (…rcN…, only installable via
`helm install --devel`) → "test"; a stable build → "production". Mirrors the
rc/final split in app/upgrade/channel.py matches_channel. There is no values
knob by design — override via cremind.extraEnv if you must.
*/}}
{{- define "cremind.upgradeChannel" -}}
{{- $tag := default .Chart.AppVersion .Values.image.tag -}}
{{- if regexMatch "rc[0-9]+" $tag -}}test{{- else -}}production{{- end -}}
{{- end -}}

{{/*
PVC claim names (honour existingClaim when set).
*/}}
{{- define "cremind.systemClaimName" -}}
{{- default (printf "%s-system" (include "cremind.fullname" .)) .Values.persistence.system.existingClaim -}}
{{- end -}}

{{- define "cremind.venvClaimName" -}}
{{- default (printf "%s-venv" (include "cremind.fullname" .)) .Values.persistence.venv.existingClaim -}}
{{- end -}}

{{- define "cremind.workClaimName" -}}
{{- default (printf "%s-work" (include "cremind.fullname" .)) .Values.persistence.work.existingClaim -}}
{{- end -}}

{{/*
Guard: CREMIND_DB_PROVIDER must never be set on Kubernetes. Setting it flips
bootstrap_exists() to true and the server boots fully, SKIPPING the Setup
Wizard — violating the K8s deployment contract. Fail the render loudly if it
appears in cremind.extraEnv.
*/}}
{{- define "cremind.assertNoDbProvider" -}}
{{- range .Values.cremind.extraEnv -}}
{{- if eq .name "CREMIND_DB_PROVIDER" -}}
{{- fail "CREMIND_DB_PROVIDER must not be set on Kubernetes: it makes the server boot fully and skip the Setup Wizard. Configure PostgreSQL through the wizard instead." -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Effective CREMIND_SSL mode ("", "auto" or "after-setup"). The first-class
cremind.ssl value wins (true = after-setup, false/none = HTTP); when it is unset a CREMIND_SSL entry in
cremind.extraEnv is honoured, because that was the only way to ask for in-pod
TLS before this knob existed and those releases must keep rendering.
cremind.validateSsl rejects contradictions and unsupported values.

Everything downstream keys on this being TRUTHY, i.e. "the app owns TLS on
1515", and renders the STEADY STATE (https APP_URL, https Atlassian callback,
Service port named https). Under after-setup the pod serves plain HTTP for the
one stretch between first boot and the Setup Wizard finishing, and the server
logs that phase; the rendered manifests describe the install it becomes, which
is what an agent card / OAuth callback / CORS origin has to say.
*/}}
{{- define "cremind.normalizeSslMode" -}}
{{- $value := lower (trim (. | default "" | toString)) -}}
{{- if has $value (list "" "none" "false" "0" "no") -}}
{{- "" -}}
{{- else if has $value (list "true" "1" "yes") -}}
{{- "auto" -}}
{{- else -}}
{{- $value -}}
{{- end -}}
{{- end -}}

{{- /* The chart's true choice is intentionally guided after-setup. Legacy
       CREMIND_SSL=true meant auto in the backend, so keep first-class value
       normalization separate from the compatibility path above. */ -}}
{{- define "cremind.normalizeChartSslMode" -}}
{{- $value := lower (trim (. | default "" | toString)) -}}
{{- if has $value (list "" "none" "false" "0" "no") -}}
{{- "" -}}
{{- else if has $value (list "true" "1" "yes") -}}
{{- "after-setup" -}}
{{- else -}}
{{- $value -}}
{{- end -}}
{{- end -}}

{{- define "cremind.sslMode" -}}
{{- $mode := .Values.cremind.ssl | default "" -}}
{{- if kindIs "bool" .Values.cremind.ssl -}}
{{- $mode = ternary "after-setup" "" .Values.cremind.ssl -}}
{{- else if $mode -}}
{{- $mode = include "cremind.normalizeChartSslMode" $mode -}}
{{- else -}}
{{- range .Values.cremind.extraEnv -}}
{{- if eq .name "CREMIND_SSL" -}}
{{- $mode = include "cremind.normalizeSslMode" (.value | default "") -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $mode -}}
{{- end -}}

{{/*
Sidecar mode selection. proxy.enabled runs the nginx sidecar in exactly ONE of
two modes, keyed on cremind.ssl; the toggles below are mutually exclusive and
each returns "true" or "" so it can be used directly in `if`.

  ssl unset -> cremind.proxyEnabled: the L7 reverse proxy (default.conf in the
               proxy ConfigMap) — one plaintext entry point routing /vnc/ +
               /websockify to noVNC and everything else to the app.

  ssl set   -> cremind.relayEnabled: a LAYER-4 TCP passthrough (an nginx
               stream{} proxy_pass to 127.0.0.1:1515). Bytes are relayed
               untouched, so the app's TLS — and after-setup's pre-flip plain
               HTTP — runs end-to-end between the browser and the app. The
               sidecar never terminates, inspects or re-encrypts anything and
               needs no certificate material, which is why the old objections
               to pairing ssl with the (L7, plaintext) sidecar do not apply.

Why relay at all, instead of the Service targeting the app directly as this
chart previously did under ssl? Because kubectl >= 1.23 tears down the ENTIRE
port-forward tunnel on the first refused in-pod dial. The app container exits
and restarts mid-flow — the Setup Wizard's after-setup https flip, every
in-app upgrade — leaving a 15-40s listener gap in which a single browser
request would kill the user's tunnel. The relay container never restarts
across those events: it always ACCEPTS the dial (and merely closes the
connection when the upstream is down), so the tunnel survives and the browser
simply retries. The Service targets the relay whenever ssl is on and
proxy.enabled; proxy.enabled=false restores the direct Service->app wiring.
*/}}
{{- define "cremind.proxyEnabled" -}}
{{- if and .Values.proxy.enabled (not (include "cremind.sslMode" .)) -}}true{{- end -}}
{{- end -}}

{{- define "cremind.relayEnabled" -}}
{{- if and .Values.proxy.enabled (include "cremind.sslMode" .) -}}true{{- end -}}
{{- end -}}

{{/*
Either sidecar mode — gates the container, its ConfigMap and its volume.
Equivalent to proxy.enabled today, but spelled as the union so the gate stays
correct if the modes ever diverge.
*/}}
{{- define "cremind.sidecarEnabled" -}}
{{- if or (include "cremind.proxyEnabled" .) (include "cremind.relayEnabled" .) -}}true{{- end -}}
{{- end -}}

{{/*
Guards for in-pod TLS. Fail the render on: an unsupported cremind.ssl value; an
extraEnv CREMIND_SSL that CONTRADICTS cremind.ssl (the pod's env: beats
envFrom:, so the ConfigMap would advertise a mode the container does not run);
ingress together with in-pod TLS (the controller speaks plain HTTP to the
backend and cannot portably re-encrypt to a private CA — terminate at the edge
OR in the pod); and an explicit http:// appUrl while the pod serves https
(which the server only catches as a boot-time warning).
*/}}
{{- define "cremind.validateSsl" -}}
{{- $mode := include "cremind.sslMode" . -}}
{{- if not (has $mode (list "" "auto" "after-setup")) -}}
{{- fail (printf "cremind.ssl must be false/none (HTTP), true (after-setup), \"\", \"auto\" or \"after-setup\"; CREMIND_SSL via extraEnv accepts the same string modes. Got %q. Bring-your-own-certificate TLS is not supported in-pod — terminate TLS at the Ingress instead." $mode) -}}
{{- end -}}
{{- if or (kindIs "bool" .Values.cremind.ssl) .Values.cremind.ssl -}}
{{- range .Values.cremind.extraEnv -}}
{{- if and (eq .name "CREMIND_SSL") (ne (include "cremind.normalizeSslMode" (.value | default "")) $mode) -}}
{{- fail "CREMIND_SSL in cremind.extraEnv contradicts cremind.ssl. Remove the extraEnv entry — cremind.ssl is the supported knob." -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if and $mode .Values.ingress.enabled -}}
{{- fail "cremind.ssl (auto or after-setup) and ingress.enabled are mutually exclusive. Terminate TLS at the edge with ingress.tls, or disable the Ingress to use in-pod TLS — an Ingress controller speaks plain HTTP to the backend and cannot portably re-encrypt to the pod's private CA." -}}
{{- end -}}
{{- if and $mode (hasPrefix "http://" (.Values.cremind.appUrl | default "")) -}}
{{- fail "cremind.appUrl is http:// but the pod serves TLS (cremind.ssl=auto or after-setup). Use https:// or leave appUrl blank to auto-derive https://localhost:1515. Under after-setup the plain-HTTP stretch before the Setup Wizard finishes is temporary; APP_URL states the steady state." -}}
{{- end -}}
{{- if and .Values.ingress.enabled (gt (len (.Values.ingress.tls | default list)) 0) (hasPrefix "http://" (.Values.cremind.appUrl | default "")) -}}
{{- fail "cremind.appUrl is http:// but ingress.tls configures edge HTTPS. Use the certificate's https:// hostname or leave appUrl blank to auto-derive it." -}}
{{- end -}}
{{- $edgePort := int (include "cremind.edgePort" .) -}}
{{- if and .Values.ingress.enabled .Values.proxy.enabled (or (lt $edgePort 1024) (gt $edgePort 65535)) -}}
{{- fail "proxy.edgePort must be an unprivileged TCP port from 1024 through 65535." -}}
{{- end -}}
{{- $edgeCollisions := list (int .Values.proxy.port) (int .Values.cremind.apiPort) (int .Values.cremind.uiPort) (int .Values.cremind.codexCallbackPort) -}}
{{- if .Values.desktop.enabled -}}
{{- $edgeCollisions = append $edgeCollisions 80 -}}
{{- $edgeCollisions = append $edgeCollisions 5900 -}}
{{- end -}}
{{- if and .Values.ingress.enabled .Values.proxy.enabled (has $edgePort $edgeCollisions) -}}
{{- fail "proxy.edgePort must differ from proxy.port and every Cremind/noVNC container listener. The separate listener is the trust boundary that prevents direct HTTP clients from spoofing the Ingress HTTPS scheme." -}}
{{- end -}}
{{- $trustedProxyCidrs := .Values.ingress.trustedProxyCidrs | default "" | toString -}}
{{- if $trustedProxyCidrs -}}
{{- range splitList "," $trustedProxyCidrs -}}
{{- $trustedProxy := trim . -}}
{{- if or (not $trustedProxy) (not (regexMatch "^[0-9A-Fa-f:.*]+(/[0-9]{1,3})?$" $trustedProxy)) -}}
{{- fail "ingress.trustedProxyCidrs must contain only comma-separated IP addresses or CIDR networks." -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- range .Values.cremind.extraEnv -}}
{{- if eq .name "FORWARDED_ALLOW_IPS" -}}
{{- if and $.Values.ingress.enabled $.Values.proxy.enabled -}}
{{- fail "Do not set FORWARDED_ALLOW_IPS in cremind.extraEnv when the Ingress sidecar is enabled. Set ingress.trustedProxyCidrs for controller-to-sidecar trust; Cremind must trust only the sidecar's loopback connection." -}}
{{- end -}}
{{- if and $trustedProxyCidrs (ne $trustedProxyCidrs (.value | default "" | toString)) -}}
{{- fail "FORWARDED_ALLOW_IPS in cremind.extraEnv contradicts ingress.trustedProxyCidrs. Keep the trusted proxy addresses in one place." -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if and (include "cremind.ingressHttps" .) (not $trustedProxyCidrs) -}}
{{- fail "Ingress TLS requires ingress.trustedProxyCidrs. List only the Ingress controller source IPs/CIDRs; the dedicated ClusterIP edge listener is otherwise reachable by other pods that could spoof HTTPS. FORWARDED_ALLOW_IPS in cremind.extraEnv is not a substitute because the sidecar and app have different peers." -}}
{{- end -}}
{{- if and .Values.ingress.enabled (or (contains "*" $trustedProxyCidrs) (contains "/0" $trustedProxyCidrs)) -}}
{{- fail "Wildcard or /0 FORWARDED_ALLOW_IPS is unsafe with Ingress TLS. Set ingress.trustedProxyCidrs to only the Ingress controller source IPs/CIDRs." -}}
{{- end -}}
{{- end -}}
