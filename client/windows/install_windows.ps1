<#
  Встановлення робота Kru (Bonsai Robot) на Windows 10/11.

  Найпростіше: двічі клацни install_windows.cmd.
  Або з PowerShell (можна одразу вказати сервер і токен):
    powershell -ExecutionPolicy Bypass -File install_windows.ps1 -Server ws://100.x.y.z:8765/ws -Token ТОКЕН

  Що робить: за потреби ставить Python (winget), створює окреме середовище в %LOCALAPPDATA%\BonsaiRobot,
  ставить робота, малює іконку, додає ярлики в меню «Пуск» і на робочий стіл, вмикає автозапуск і запускає робота.
#>
param(
    [string]$Server = "",
    [string]$Token = "",
    [switch]$NoAutostart,
    [switch]$NoDesktopShortcut,
    [switch]$NoLaunch
)
$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$Here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Client = Split-Path -Parent $Here
$Prefix = Join-Path $env:LOCALAPPDATA "BonsaiRobot"
$Venv   = Join-Path $Prefix "venv"
$Py     = Join-Path $Venv "Scripts\python.exe"
$PyW    = Join-Path $Venv "Scripts\pythonw.exe"
$Ico    = Join-Path $Prefix "robot.ico"

function Get-PythonPath {
    # py-лаунчер або python з PATH (але не «заглушка» Microsoft Store), версія ≥ 3.10
    $probe = "import sys; print(sys.executable if sys.version_info >= (3, 10) else '')"
    foreach ($cmd in @("py", "python")) {
        if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { continue }
        try {
            if ($cmd -eq "py") { $out = & py -3 -c $probe 2>$null } else { $out = & python -c $probe 2>$null }
            if ($LASTEXITCODE -eq 0 -and $out -and (Test-Path $out.Trim())) { return $out.Trim() }
        } catch {}
    }
    return $null
}

Write-Host "==> Шукаю Python..."
$python = Get-PythonPath
if (-not $python) {
    Write-Host "==> Python не знайдено — встановлюю Python 3.12 через winget (лише для поточного користувача)..."
    winget install -e --id Python.Python.3.12 --scope user --accept-source-agreements --accept-package-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
    $python = Get-PythonPath
    if (-not $python) {
        $candidate = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
        if (Test-Path $candidate) { $python = $candidate }
    }
    if (-not $python) {
        throw "Не вдалося встановити Python. Постав його з https://www.python.org (галочка «Add python.exe to PATH») і запусти цей скрипт ще раз."
    }
}
Write-Host "    $python"

# якщо робот уже працює — закриваємо, щоб оновлення не блокувалося зайнятими файлами
if (Test-Path $PyW) {
    Write-Host "==> Закриваю запущеного робота (якщо працює)..."
    Start-Process -FilePath $PyW -ArgumentList "-m", "bonsai_robot", "--quit" -Wait -ErrorAction SilentlyContinue
}

Write-Host "==> Середовище: $Venv"
New-Item -ItemType Directory -Force -Path $Prefix | Out-Null
if (-not (Test-Path $Py)) { & $python -m venv $Venv }
& $Py -m pip install --upgrade pip --quiet --disable-pip-version-check
& $Py -m pip install --upgrade "$Client" --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw "Не вдалося встановити робота (pip install)." }

Write-Host "==> Іконка"
$env:QT_QPA_PLATFORM = "offscreen"
& $Py -m bonsai_robot.icon --ico $Ico
Remove-Item Env:\QT_QPA_PLATFORM -ErrorAction SilentlyContinue

Write-Host "==> Ярлики"
$shell = New-Object -ComObject WScript.Shell
function New-RobotShortcut([string]$path) {
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $PyW
    $lnk.Arguments = "-m bonsai_robot"
    $lnk.WorkingDirectory = $Prefix
    $lnk.IconLocation = "$Ico,0"
    $lnk.Description = "Kru — робот-помічник"
    $lnk.Save()
}
New-RobotShortcut (Join-Path ([Environment]::GetFolderPath("Programs")) "Bonsai Robot.lnk")
if (-not $NoDesktopShortcut) {
    New-RobotShortcut (Join-Path ([Environment]::GetFolderPath("Desktop")) "Bonsai Robot.lnk")
}

if ($Server) {
    Write-Host "==> Налаштування: $Server"
    $code = "import sys; from bonsai_robot.settings import Settings; s = Settings.load(); s.server_url = sys.argv[1]; s.token = sys.argv[2] if len(sys.argv) > 2 else s.token; s.save()"
    if ($Token) { & $Py -c $code $Server $Token } else { & $Py -c $code $Server }
}

if (-not $NoAutostart) {
    Write-Host "==> Автозапуск разом із Windows (вимкнути: ПКМ по роботу → Налаштування)"
    & $Py -c "from bonsai_robot.winplatform import set_autostart; set_autostart(True)"
}

if (-not $NoLaunch) {
    Write-Host "==> Запускаю робота"
    Start-Process -FilePath $PyW -ArgumentList "-m", "bonsai_robot" -WorkingDirectory $Prefix
}

Write-Host ""
Write-Host "Готово! Kru з'явиться в правому нижньому куті екрана."
if (-not $Server) {
    Write-Host "При першому запуску відкриються налаштування: вкажи адресу сервера (ws://IP:8765/ws) і токен."
}
Write-Host "Гаряча клавіша: Ctrl+Alt+B — показати/сховати поле вводу."
