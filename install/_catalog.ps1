# AUTO-GENERATED from install/catalog.toml. Do not edit by hand.
# Regenerate with: python install/scripts/build_catalog.py
# Source SHA-256:  b32d2d3547d8f9ae0b3994fc509b57ef046c26b3cf4052b59ac6e296532cc5c5

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
$script:ModeIds = @('docker', 'native')
$script:Modes = [ordered]@{
    'docker' = [ordered]@{
        Label       = 'Docker'
        Description = 'sandboxed VNC desktop with a bundled storage stack'
        Hint        = 'The agent runs inside a container with its own GUI. Observe at http://<host>:6080/vnc.html.'
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

