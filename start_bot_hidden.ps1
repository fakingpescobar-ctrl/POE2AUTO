$py = Join-Path $env:USERPROFILE "anaconda3\python.exe"
if (!(Test-Path $py)) { $py = "python" }
Set-Location "C:\Projects\POE2AUTO"
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'bot\.py' } | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 600
& $py bot.py *>&1 | Set-Content -Path "C:\Users\OLD\AppData\Local\Temp\opencode\bot_stderr.txt" -Encoding UTF8
