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
Resolved image reference (registry/repository:tag), tag defaulting to appVersion.
*/}}
{{- define "cremind.image" -}}
{{- $tag := default .Chart.AppVersion .Values.image.tag -}}
{{- if .Values.image.registry -}}
{{- printf "%s/%s:%s" .Values.image.registry .Values.image.repository $tag -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end -}}
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
