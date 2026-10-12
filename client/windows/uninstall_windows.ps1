<#
  Видалення персонажа Kru з Windows. Налаштування в %APPDATA%\bonsai-robot залишаються.
#>
$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$Prefix = Join-Path $env:LOCALAPPDATA "BonsaiRobot"
$Py  = Join-Path $Prefix "venv\Scripts\python.exe"
$PyW = Join-Path $Prefix "venv\Scripts\pythonw.exe"

if (Test-Path $PyW) {
    Start-Process -FilePath $PyW -ArgumentList "-m", "bonsai_robot", "--quit" -Wait
    & $Py -c "from bonsai_robot.winplatform import set_autostart; set_autostart(False)"
}
foreach ($name in @("Kru", "BonsaiRobot")) {
    Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name $name -ErrorAction SilentlyContinue
}
foreach ($dir in @([Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("Desktop"))) {
    foreach ($lnk in @("Kru.lnk", "Bonsai Robot.lnk")) { Remove-Item (Join-Path $dir $lnk) -ErrorAction SilentlyContinue }
}
Start-Sleep -Seconds 1
Remove-Item $Prefix -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "Kru видалено."
