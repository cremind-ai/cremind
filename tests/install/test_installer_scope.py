"""install.ps1 must read its variables the same way however it is started.

Passing parameters to the one-liner means running the script as a script
block: ``& ([scriptblock]::Create((iwr -useb https://cremind.io/install.ps1)))
-Channel test``. The script then runs in a child scope while ``$script:``
still names the caller's scope, so it splits in two: a top-level ``$X = ...``
is invisible as ``$script:X``, a function's ``$script:X = ...`` is shadowed by
the top level's own ``$X``, and ``Set-Variable -Scope Script`` on a parameter
lands somewhere nobody reads. A fresh Windows install died this way in
``Invoke-NativeLogged`` ("The variable '$script:LogFile' cannot be retrieved
because it has not been set"), after its TUI answers had already been
silently dropped and asked again. Run as a file, or through ``iwr | iex`` at
the prompt, both names are one scope, which is why nothing caught it.

The rules the script keeps (its "Script scope" note): a variable that is ever
reached through script scope is assigned through it everywhere, and
parameters never are.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PS1 = REPO_ROOT / "install" / "install.ps1"
CATALOG = REPO_ROOT / "install" / "_catalog.ps1"

STARTS = ["script block", "file"]


def _ps1() -> str:
    return PS1.read_text(encoding="utf-8")


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        pytest.skip("no PowerShell available")
    return executable


def _quoted(path: Path) -> str:
    """A path as a single-quoted PowerShell string body."""
    return str(path).replace("'", "''")


def _run(script: Path, start: str, *args: str) -> subprocess.CompletedProcess[str]:
    if start == "file":
        command = [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                   "-File", str(script), *args]
    else:
        # The reported invocation, with the text read from disk instead of iwr.
        command = [_powershell(), "-NoProfile", "-NonInteractive", "-Command",
                   f"& ([scriptblock]::Create([System.IO.File]::ReadAllText('{_quoted(script)}')))"
                   + "".join(f" {arg}" for arg in args)]
    return subprocess.run(command, capture_output=True, text=True, timeout=120)


def _write(path: Path, *lines: str) -> Path:
    path.write_text("\n".join(lines).replace("\r\n", "\n") + "\n", encoding="utf-8", newline="\n")
    return path


# ── the rules, checked on the whole script ────────────────────────────────

_SCOPE_CHECK = r"""
param([string] $InstallPs1, [string] $CatalogPs1)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-ScriptAst([string] $Path) {
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw "parse errors in ${Path}: $($errors[0].Message)" }
    $ast
}
function Test-InFunction($Node) {
    for ($p = $Node.Parent; $p; $p = $p.Parent) {
        if ($p -is [System.Management.Automation.Language.FunctionDefinitionAst]) { return $true }
    }
    $false
}
function Get-Where($Node) {
    '{0}:{1}' -f (Split-Path -Leaf $Node.Extent.File), $Node.Extent.StartLineNumber
}

$main = Get-ScriptAst $InstallPs1
# The catalog is dot-sourced at the top level, so its assignments count too.
$asts = @($main, (Get-ScriptAst $CatalogPs1))
$params = @{}
foreach ($p in $main.ParamBlock.Parameters) { $params[$p.Name.VariablePath.UserPath] = $true }

# Every name reached through script scope: $script:X, or *-Variable -Scope Script.
$noArgument = @('ValueOnly', 'Force', 'PassThru', 'WhatIf', 'Confirm', 'Verbose', 'Debug')
$scriptVars = @{}
foreach ($ast in $asts) {
    $refs = $ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.VariableExpressionAst] -and $n.VariablePath.IsScript }, $true)
    foreach ($v in $refs) {
        $scriptVars[($v.VariablePath.UserPath -replace '^(?i)script:', '')] = Get-Where $v
    }
    $calls = $ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.CommandAst] -and
        $n.GetCommandName() -match '^(?i)(Get|Set|New|Remove|Clear)-Variable$' }, $true)
    foreach ($c in $calls) {
        $scope = $null; $name = $null; $positional = @()
        $els = $c.CommandElements
        for ($i = 1; $i -lt $els.Count; $i++) {
            $el = $els[$i]
            if ($el -is [System.Management.Automation.Language.CommandParameterAst]) {
                $value = $el.Argument
                if (-not $value -and $noArgument -notcontains $el.ParameterName -and ($i + 1) -lt $els.Count) {
                    $i++
                    $value = $els[$i]
                }
                if ($el.ParameterName -eq 'Scope') { $scope = $value }
                elseif ($el.ParameterName -eq 'Name') { $name = $value }
            } else {
                $positional += $el
            }
        }
        if (-not $name -and $positional.Count) { $name = $positional[0] }
        if (-not $scope -or $scope.Extent.Text.Trim([char[]]"'`"") -ne 'Script') { continue }
        if ($name -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
            $scriptVars[$name.Value] = Get-Where $c
        } else {
            "$(Get-Where $c): -Scope Script with a computed name, which nothing can check"
        }
    }
}

foreach ($n in @($scriptVars.Keys)) {
    if ($params.ContainsKey($n)) { "$($scriptVars[$n]): parameter `$$n is reached through script scope" }
}
foreach ($ast in $asts) {
    $assignments = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)
    foreach ($a in $assignments) {
        if (Test-InFunction $a) { continue }
        $targets = @($a.Left)
        if ($a.Left -is [System.Management.Automation.Language.ArrayLiteralAst]) { $targets = @($a.Left.Elements) }
        foreach ($t in $targets) {
            while ($t -is [System.Management.Automation.Language.ConvertExpressionAst]) { $t = $t.Child }
            if ($t -is [System.Management.Automation.Language.VariableExpressionAst] -and
                $t.VariablePath.IsUnqualified -and $scriptVars.ContainsKey($t.VariablePath.UserPath)) {
                "$(Get-Where $a): `$$($t.VariablePath.UserPath) is assigned without `$script: but reached through script scope"
            }
        }
    }
    $loops = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.ForEachStatementAst] }, $true)
    foreach ($f in $loops) {
        if (Test-InFunction $f) { continue }
        $v = $f.Variable
        if ($v.VariablePath.IsUnqualified -and $scriptVars.ContainsKey($v.VariablePath.UserPath)) {
            "$(Get-Where $f): foreach assigns `$$($v.VariablePath.UserPath) without `$script: but it is reached through script scope"
        }
    }
}
"""


def test_script_scope_is_used_the_same_way_everywhere(tmp_path) -> None:
    check = tmp_path / "scope-check.ps1"
    check.write_text(_SCOPE_CHECK, encoding="ascii", newline="\n")

    result = _run(check, "file", str(PS1), str(CATALOG))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", "\n" + result.stdout


# ── the reported crash, and the answers it lost first ─────────────────────


@pytest.mark.parametrize("start", STARTS)
def test_a_native_step_is_logged_however_the_script_starts(tmp_path, start) -> None:
    """``Invoke-NativeLogged`` writes to the log the top level named."""
    ps1 = _ps1()
    assignment = re.search(r"(?m)^\$(?:script:)?LogFile\s*=.*$", ps1)
    function = re.search(r"(?ms)^function Invoke-NativeLogged \{\r?\n.*?^\}\r?$", ps1)
    assert assignment and function, "the log setup moved; update this test"
    script = _write(
        tmp_path / "native-step.ps1",
        "Set-StrictMode -Version Latest",
        "$ErrorActionPreference = 'Stop'",
        f"$CremindInstallDir = '{_quoted(tmp_path)}'",
        assignment.group(0).strip(),
        function.group(0),
        "Invoke-NativeLogged { Write-Output 'a native step said hello' }",
        "'finished'",
    )

    result = _run(script, start)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "finished" in result.stdout
    assert "a native step said hello" in (tmp_path / "install.log").read_text(encoding="utf-8-sig")


@pytest.mark.parametrize("start", STARTS)
def test_the_tui_answers_reach_the_parameters_however_the_script_starts(tmp_path, start) -> None:
    """The real parameter block, reader and top-level hand-off. Only the TUI
    launch is stood in for. A value passed on the command line still wins."""
    ps1 = _ps1()
    params = re.search(r"(?ms)^\[CmdletBinding\(\)\]\r?\nparam\(\r?\n.*?^\)\r?$", ps1)
    reader = re.search(r"(?ms)^function Read-TuiSelections \{\r?\n.*?^\}\r?$", ps1)
    hand_off = re.search(r"(?ms)^\$script:TuiApplied = \$false\r?\n.*?^\}\r?$", ps1)
    assert params and reader and hand_off, "the TUI hand-off moved; update this test"
    answers = _write(
        tmp_path / "tui-out",
        "DEPLOYMENT='server'",
        "SSL_CHOICE='after-setup'",
        "CUSTOM_listen_host='0.0.0.0'",
        "CUSTOM_public_url='https://from-the-tui.test'",
    )
    script = _write(
        tmp_path / "tui-hand-off.ps1",
        params.group(0),
        "Set-StrictMode -Version Latest",
        "$ErrorActionPreference = 'Stop'",
        reader.group(0),
        "function Invoke-InstallerTuiBootstrap {",
        "    param([Parameter(Mandatory)][hashtable] $Selections)",
        f"    Read-TuiSelections -Path '{_quoted(answers)}' -Into $Selections",
        "    $script:TuiApplied = $true",
        "}",
        hand_off.group(0),
        '"deployment=$Deployment ssl=$Ssl listen=$ListenHost url=$PublicUrl applied=$TuiApplied"',
    )

    result = _run(script, start, "-Deployment", "custom", "-PublicUrl", "https://cli.test")

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == (
        "deployment=custom ssl=after-setup listen=0.0.0.0 url=https://cli.test applied=True"
    )
