# AUTO-GENERATED from install/catalog.toml. Do not edit by hand.
# Regenerate with: python install/scripts/build_catalog.py
# Source SHA-256:  918c3655f0cf44a166d84e0964d05463ea9d0d2de58f3d5bce1c0b736879f81a

$script:CatalogSchema = 1

# ── Deployments ──
$script:DeploymentIds = @('local', 'server', 'custom')
$script:Deployments = [ordered]@{
    'local' = [ordered]@{
        Label        = 'Local'
        Short        = 'this machine only'
        Description  = 'bind to 127.0.0.1, only this machine can reach it'
        EnvValue     = 'local'
        DeployHost   = '127.0.0.1'
        RequiresHost = $false
        Order        = 10
    }
    'server' = [ordered]@{
        Label        = 'Server'
        Short        = 'reachable from other devices'
        Description  = 'bind to all interfaces, reachable from other devices'
        EnvValue     = 'production'
        DeployHost   = '0.0.0.0'
        RequiresHost = $true
        Order        = 20
    }
    'custom' = [ordered]@{
        Label        = 'Custom (advanced)'
        Short        = 'I''ll configure host, URL, and CORS myself'
        Description  = 'advanced setup — choose where Cremind listens and how it''s reached'
        EnvValue     = 'custom'
        DeployHost   = ''
        RequiresHost = $false
        Order        = 30
    }
}

# ── Custom-deployment advanced fields ──
$script:CustomFieldIds = @('listen_host', 'public_url', 'allowed_origins', 'wizard_preset')
$script:CustomFields = [ordered]@{
    'listen_host' = [ordered]@{
        Key     = 'listen_host'
        Prompt  = 'Where should Cremind listen for connections?'
        Hint    = 'Use 127.0.0.1 to only allow this machine, or 0.0.0.0 to allow other devices / containers on the network.'
        Default = '0.0.0.0'
        Choices = @()
    }
    'public_url' = [ordered]@{
        Key     = 'public_url'
        Prompt  = 'What URL will you use to open Cremind in a browser?'
        Hint    = 'This is the address users type into their browser. Inside a container it''s usually http://localhost:1515. On a server it might be http://my-box.lan:1515 or https://cremind.example.com.'
        Default = 'http://localhost:1515'
        Choices = @()
    }
    'allowed_origins' = [ordered]@{
        Key     = 'allowed_origins'
        Prompt  = 'Which web origins should be allowed to talk to the API?'
        Hint    = 'A comma-separated list of URLs the browser UI will be served from. Usually the same as the public URL above. Leave blank to use the public URL plus localhost variants.'
        Default = ''
        Choices = @()
    }
    'wizard_preset' = [ordered]@{
        Key     = 'wizard_preset'
        Prompt  = 'Which preset should pre-fill the Setup Wizard?'
        Hint    = 'Presets fill in sensible defaults for the next setup screens — you can always edit any field afterwards. Pick `local` for a single-machine setup, `docker` for a docker-compose stack, or `server` for an external Postgres/Qdrant.'
        Default = 'local'
        Choices = @('local', 'docker', 'server')
    }
}

# ── Install modes ──
$script:ModeIds = @('docker', 'native', 'kubernetes')
$script:Modes = [ordered]@{
    'docker' = [ordered]@{
        Label       = 'Docker'
        Description = 'sandboxed container with a bundled storage stack (optional VNC desktop)'
        Hint        = 'The agent runs inside a container. You can add a VNC desktop so it has its own GUI (observe at http://<host>:6080/vnc.html), or install the smaller headless image.'
        Badge       = 'recommended'
        Requires    = @('docker')
        Order       = 10
    }
    'native' = [ordered]@{
        Label       = 'Native'
        Description = 'Python venv at ~/.cremind/venv with embedded storage'
        Hint        = 'Simpler, but the agent shares your desktop and home directory.'
        Badge       = ''
        Requires    = @()
        Order       = 20
    }
    'kubernetes' = [ordered]@{
        Label       = 'Kubernetes'
        Description = 'Helm release in an existing cluster (in-cluster Postgres, optional VNC desktop)'
        Hint        = 'Installs the cremind Helm chart into a kubeconfig context you pick. Needs kubectl and helm on PATH; you reach Cremind through a kubectl port-forward at http://localhost:1515.'
        Badge       = ''
        Requires    = @('kubectl', 'helm')
        Order       = 30
    }
}

# ── Docker desktop UI ──
$script:DockerDesktop = [ordered]@{
    Prompt  = 'Install the VNC Desktop UI?'
    Hint    = 'Adds an XFCE desktop inside the container so you can watch the agent work at http://<host>:6080/vnc.html. Answer No to install the smaller headless image (cremind/cremind).'
    Default = $true
}

# ── VNC password ──
$script:VncPasswordPrompt = [ordered]@{
    Prompt = 'Choose a password for the VNC Desktop'
    Hint   = '6-8 characters, from letters, digits and @ % _ + = : , . - — VNC ignores anything past the 8th character. You will sign in with it at http://<host>:6080/vnc.html. Leave empty when re-installing to keep the current password.'
}

# ── Kubernetes prompts ──
$script:Kubernetes = [ordered]@{
    ContextPrompt    = 'Which kubeconfig context should Cremind be installed into?'
    ContextHint      = 'Every helm and kubectl command runs with --kube-context set to this choice (and --kubeconfig, for a context from a file kubectl does not read on its own), never the ambient current-context. Every kubeconfig under ~/.kube is listed, not just kubectl''s own; each row shows the API server, and its file when several are in play, so a look-alike cluster stands out.'
    NamespacePrompt  = 'Which namespace should the Helm release go into?'
    NamespaceHint    = 'Created if it does not exist. Lowercase letters, digits and hyphens, up to 63 characters.'
    NamespaceDefault = 'cremind'
    AdvancedPrompt   = 'Use the recommended Helm options, or customize them?'
    AdvancedHint     = 'Recommended: release name cremind, app URL derived from the port-forward, the legacy Bitnami Postgres image, Postgres data kept on uninstall, no extra --set values.'
    AdvancedFields   = @(
        [ordered]@{
            Key     = 'release_name'
            Prompt  = 'What should the Helm release be called?'
            Hint    = 'Shown by helm list. Objects are named after it (a name containing cremind is used as-is, otherwise -cremind is appended). Lowercase letters, digits and hyphens, up to 53 characters.'
            Default = 'cremind'
            Choices = @()
        }
        [ordered]@{
            Key     = 'app_url'
            Prompt  = 'What URL will you use to open Cremind in a browser?'
            Hint    = 'Leave blank to let the chart derive it from the port-forward (http://localhost:1515, or https:// when HTTPS is on). Set it only when an ingress or load balancer serves Cremind at another address.'
            Default = ''
            Choices = @()
        }
        [ordered]@{
            Key     = 'legacy_postgres_image'
            Prompt  = 'Use the legacy Bitnami Postgres image (docker.io/bitnamilegacy/postgresql)?'
            Hint    = 'Bitnami moved its free images to the bitnamilegacy namespace, so the chart''s default Postgres image no longer resolves on Docker Hub. Answer no only if your cluster mirrors the current Bitnami catalog.'
            Default = 'yes'
            Choices = @('yes', 'no')
        }
        [ordered]@{
            Key     = 'delete_postgres_data'
            Prompt  = 'Delete the Postgres volume when the release is uninstalled?'
            Hint    = 'no keeps the data-cremind-postgresql-0 claim after helm uninstall (the chart default) so a reinstall picks the data back up; yes sets the retention policy to Delete.'
            Default = 'no'
            Choices = @('yes', 'no')
        }
        [ordered]@{
            Key     = 'extra_set'
            Prompt  = 'Any extra --set values for helm?'
            Hint    = 'The value of one helm --set, e.g. ingress.enabled=true,ingress.host=cremind.example.com. Applied last, so it overrides the installer''s own values. Leave blank for none.'
            Default = ''
            Choices = @()
        }
    )
}

# ── Mode rules ──
$script:ModeRules = [ordered]@{
    'docker' = [ordered]@{
        AllowedServiceModes = @('docker', 'native')
        DefaultServiceMode  = 'docker'
    }
    'native' = [ordered]@{
        AllowedServiceModes = @('native', 'external')
        DefaultServiceMode  = 'external'
    }
    'custom' = [ordered]@{
        AllowedServiceModes = @('docker', 'native', 'external')
        DefaultServiceMode  = 'external'
    }
    'kubernetes' = [ordered]@{
        AllowedServiceModes = @('external')
        DefaultServiceMode  = 'external'
    }
}

# ── Service modes ──
$script:ServiceModeIds = @('docker', 'native', 'external')
$script:ServiceModes = [ordered]@{
    'docker' = [ordered]@{
        Label               = 'Docker'
        DescriptionTemplate = 'Cremind starts a {service} container alongside itself.'
    }
    'native' = [ordered]@{
        Label               = 'Native'
        DescriptionTemplate = 'Cremind runs {service} locally (no extra container).'
    }
    'external' = [ordered]@{
        Label               = 'External'
        DescriptionTemplate = 'Connect to an existing {service} instance.'
    }
}

