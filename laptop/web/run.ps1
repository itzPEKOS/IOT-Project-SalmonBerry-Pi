<#
เปิด backend และหน้าเว็บเกมในคำสั่งเดียว (backend เปิดหน้าเว็บให้เอง ไม่ต้องรัน frontend แยก)

  .\run.ps1                 ใช้งานจริง: เปิดเฉพาะเครื่องนี้ แล้วเปิด browser ให้
  .\run.ps1 -Lan            ให้ Pi และเครื่องอื่นในวงต่อเข้ามาได้ (Pi ต้องส่งวิดีโอเข้ามา)
  .\run.ps1 -Sim            ลองหน้าเว็บด้วยตัวจำลอง แยกขาดจากของจริง: ไม่ต่อ broker ไม่อัป cloud
                            ฐานข้อมูล data/dev.db คลิป data/dev-clips/
  .\run.ps1 -Dev            ฐานข้อมูลแยก + เปิด /dev/publish แต่ยังต่อ broker (ใช้กับตัวจำลองผ่าน MQTT เอง)

ตัวเลือกอื่น: -Port 8000, -Rounds 5 (จำนวนรอบของตัวจำลอง), -NoBrowser
กด Ctrl+C เพื่อปิด (ตัวจำลองจะถูกปิดด้วย)
#>
param(
    [int]$Port = 8000,
    [switch]$Lan,
    [switch]$Dev,
    [switch]$Sim,
    [int]$Rounds = 5,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$web = $PSScriptRoot
$simScript = Join-Path (Split-Path $web -Parent) "sim\simulate.py"
if ($Sim) { $Dev = $true }  # ตัวจำลองส่งข้อมูลผ่าน /dev/publish

function Say($text, $color = "Gray") { Write-Host $text -ForegroundColor $color }

# ---------- ตรวจของที่ต้องมี ----------
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Say "ไม่พบ python ใน PATH: ติดตั้ง Python 3.11 ขึ้นไปก่อน" Red
    exit 1
}
$missing = & python -c "import importlib.util as u; print(' '.join(m for m in ['fastapi','uvicorn','multipart','paho','httpx','websockets'] if u.find_spec(m) is None))"
if ($missing) {
    Say "ยังไม่ได้ลง package: $missing" Red
    Say "รันคำสั่งนี้ในโฟลเดอร์ laptop\web แล้วลองใหม่:" Yellow
    Say "  python -m pip install -r backend\requirements.txt" Yellow
    exit 1
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Say "คำเตือน: ไม่พบ ffmpeg จะแปลงวิดีโอจาก Pi ไม่ได้ (หน้าเว็บยังใช้ได้)" Yellow
}

$used = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($used) {
    $owner = Get-Process -Id $used.OwningProcess -ErrorAction SilentlyContinue
    Say "port $Port ถูกใช้อยู่โดย $($owner.ProcessName) (PID $($used.OwningProcess))" Red
    Say "ปิดโปรแกรมนั้นก่อน หรือใช้ port อื่น เช่น .\run.ps1 -Port 8001" Yellow
    exit 1
}

# ---------- ค่าตั้ง (ค่าอื่นอ่านจาก laptop\infra\.env) ----------
$listen = if ($Lan) { "0.0.0.0" } else { "127.0.0.1" }
# ค่าที่สคริปต์เปลี่ยน จะถูกคืนค่าเดิมตอนปิด (ถ้ารัน .\run.ps1 ในหน้าต่างเดิม จะไม่ค้างโหมดทดลองไปรอบถัดไป)
$changed = "PYTHONIOENCODING", "RLGL_DEV", "RLGL_DB_PATH", "RLGL_ISOLATED", "RLGL_CLIPS_DIR"
$original = @{}
foreach ($name in $changed) { $original[$name] = [Environment]::GetEnvironmentVariable($name) }
$env:PYTHONIOENCODING = "utf-8"
if ($Dev) {
    $env:RLGL_DEV = "1"
    $env:RLGL_DB_PATH = Join-Path $web "data\dev.db"
    Say "โหมดทดลอง: ใช้ฐานข้อมูล data\dev.db และเปิด /dev/publish" Cyan
}
if ($Sim) {
    $env:RLGL_ISOLATED = "1"
    $env:RLGL_CLIPS_DIR = Join-Path $web "data\dev-clips"
    Say "แยกขาดจากของจริง: ไม่ต่อ MQTT broker, ไม่อัปวิดีโอขึ้น cloud (คลิปทดสอบอยู่ที่ data\dev-clips)" Cyan
}

# ที่อยู่ broker: environment variable ก่อน แล้วค่อย .env (เหมือนที่ backend อ่าน)
$envFile = Join-Path (Split-Path $web -Parent) "infra\.env"
function Setting($name, $default) {
    $value = [Environment]::GetEnvironmentVariable($name)
    if ($value) { return $value }
    if (Test-Path $envFile) {
        $found = Select-String -Path $envFile -Pattern "^\s*$name\s*=\s*(.+)$" | Select-Object -First 1
        if ($found) { return $found.Matches[0].Groups[1].Value.Trim() }
    }
    return $default
}
if (-not $Sim) {
    $broker = Setting "RLGL_MQTT_HOST" "127.0.0.1"
    $brokerPort = [int](Setting "RLGL_MQTT_PORT" "1883")
    $probe = New-Object Net.Sockets.TcpClient
    try {
        $wait = $probe.BeginConnect($broker, $brokerPort, $null, $null)
        $reachable = $wait.AsyncWaitHandle.WaitOne(1500) -and $probe.Connected
    } catch { $reachable = $false } finally { $probe.Close() }
    if (-not $reachable) {
        Say "คำเตือน: ต่อ MQTT broker ที่ ${broker}:$brokerPort ไม่ได้ หน้าเว็บจะไม่ได้ข้อมูลจากอุปกรณ์จริง" Yellow
        Say "         (เปิด Docker stack ใน laptop\infra หรือ broker บน Pi ก่อน หรือใช้ -Sim เพื่อลองด้วยข้อมูลจำลอง)" Yellow
    }
}

# ---------- เปิด backend ----------
$local = "http://127.0.0.1:$Port"
Say "กำลังเปิด backend ที่ ${listen}:$Port ..." Cyan
$server = Start-Process python -NoNewWindow -PassThru -WorkingDirectory $web -ArgumentList @(
    "-m", "uvicorn", "backend.app:make_app", "--factory", "--host", $listen, "--port", "$Port")
$simulator = $null
try {
    $health = $null
    for ($i = 0; $i -lt 60 -and -not $server.HasExited; $i++) {
        try { $health = Invoke-RestMethod "$local/api/health" -TimeoutSec 2; break }
        catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $health) {
        Say "backend เปิดไม่สำเร็จ ดูข้อความ error ด้านบน" Red
        exit 1
    }
    if ($Sim -and $health.isolated -ne $true) {  # กันข้อมูลจำลองหลุดไปปนของจริง
        Say "backend ไม่ได้อยู่ในโหมดแยกขาด จึงไม่เปิดตัวจำลอง" Red
        exit 1
    }

    Say ""
    Say "พร้อมแล้ว" Green
    Say "  จอผู้เล่น : $local/" Green
    Say "  จอคนดู   : $local/?role=viewer" Green
    if ($Lan) {
        $ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
            Select-Object -ExpandProperty IPAddress
        foreach ($ip in $ips) { Say "  จากเครื่องอื่น/Pi: http://${ip}:$Port/  (ส่งวิดีโอที่ http://${ip}:$Port/clips)" Green }
        Say "  ถ้าเครื่องอื่นเข้าไม่ได้ ให้อนุญาต port $Port ใน Windows Firewall เฉพาะเครือข่ายของกลุ่ม" Yellow
    }
    Say "กด Ctrl+C เพื่อปิด" Gray
    Say ""

    if (-not $NoBrowser) { Start-Process "$local/" }

    if ($Sim) {
        Say "เปิดตัวจำลอง $Rounds รอบ (เล่นตามโหมดที่เลือกบนหน้าเว็บ) ในหน้าต่างใหม่" Cyan
        $simulator = Start-Process python -PassThru -WorkingDirectory (Split-Path $simScript -Parent) -ArgumentList @(
            "-B", $simScript, "--via-backend", $local, "--mode", "follow", "--rounds", "$Rounds", "--gap", "4")
    }

    Wait-Process -Id $server.Id
} finally {
    if ($simulator -and -not $simulator.HasExited) { Stop-Process -Id $simulator.Id -ErrorAction SilentlyContinue }
    if (-not $server.HasExited) { Stop-Process -Id $server.Id -ErrorAction SilentlyContinue }
    foreach ($name in $changed) { [Environment]::SetEnvironmentVariable($name, $original[$name]) }
}
