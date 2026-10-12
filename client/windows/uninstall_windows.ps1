<#
  Видалення персонажа Kru з Windows. Налаштування в %APPDATA%\kru-robot залишаються.
#>
$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$Prefix = Join-Path $env:LOCALAPPDATA "Kru"
$Py  = Join-Path $Prefix "venv\Scripts\python.exe"
$PyW = Join-Path $Prefix "venv\Scripts\pythonw.exe"

if (Test-Path $PyW) {
    Start-Process -FilePath $PyW -ArgumentList "-m", "kru_robot", "--quit" -Wait
    & $Py -c "from kru_robot.winplatform import set_autostart; set_autostart(False)"
}
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "Kru" -ErrorAction SilentlyContinue
foreach ($dir in @([Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("Desktop"))) {
    Remove-Item (Join-Path $dir "Kru.lnk") -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 1
Remove-Item $Prefix -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "Kru видалено."
