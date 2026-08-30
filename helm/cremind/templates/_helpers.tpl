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
Effective CREMIND_SSL mode ("" or "auto"). The first-class cremind.ssl value
wins; when it is unset a CREMIND_SSL entry in cremind.extraEnv is honoured,
because that was the only way to ask for in-pod TLS before this knob existed
and those releases must keep rendering. cremind.validateSsl rejects
contradictions and unsupported values.
*/}}
{{- define "cremind.sslMode" -}}
{{- $mode := .Values.cremind.ssl | default "" -}}
{{- if not $mode -}}
{{- range .Values.cremind.extraEnv -}}
{{- if eq .name "CREMIND_SSL" -}}
{{- $mode = (.value | default "") -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $mode -}}
{{- end -}}

{{/*
Effective reverse-proxy toggle. In-pod TLS and the nginx sidecar are mutually
exclusive: the sidecar is plaintext on both sides, never mounts the system PVC
that holds the certificate, could not read the root-owned 0600 key if it did,
and the certificate does not exist until the app's first boot. There is no
valid (ssl, proxy) pairing to choose between, so the chart bypasses the sidecar
automatically instead of failing and demanding a second flag. Returns "true" or
"" so it can be used directly in `if`.
*/}}
{{- define "cremind.proxyEnabled" -}}
{{- if and .Values.proxy.enabled (not (include "cremind.sslMode" .)) -}}true{{- end -}}
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
{{- if not (has $mode (list "" "auto")) -}}
{{- fail (printf "cremind.ssl (or CREMIND_SSL via extraEnv) must be \"\" or \"auto\", got %q. Bring-your-own-certificate TLS is not supported in-pod — terminate TLS at the Ingress instead." $mode) -}}
{{- end -}}
{{- if .Values.cremind.ssl -}}
{{- range .Values.cremind.extraEnv -}}
{{- if and (eq .name "CREMIND_SSL") (ne (.value | default "") $.Values.cremind.ssl) -}}
{{- fail "CREMIND_SSL in cremind.extraEnv contradicts cremind.ssl. Remove the extraEnv entry — cremind.ssl is the supported knob." -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if and $mode .Values.ingress.enabled -}}
{{- fail "cremind.ssl and ingress.enabled are mutually exclusive. Terminate TLS at the edge with ingress.tls, or disable the Ingress to use in-pod TLS — an Ingress controller speaks plain HTTP to the backend and cannot portably re-encrypt to the pod's private CA." -}}
{{- end -}}
{{- if and $mode (hasPrefix "http://" (.Values.cremind.appUrl | default "")) -}}
{{- fail "cremind.appUrl is http:// but the pod serves TLS (cremind.ssl=auto). Use https:// or leave appUrl blank to auto-derive https://localhost:1515." -}}
{{- end -}}
{{- end -}}
