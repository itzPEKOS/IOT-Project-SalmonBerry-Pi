<#
ล้างข้อมูลทดสอบ ก่อนเริ่มเก็บข้อมูลจริง (ใช้ครั้งเดียว) ลบแล้วกู้คืนไม่ได้

  .\clear_test_data.cmd            แสดงว่ามีอะไรบ้าง แล้วถามยืนยันทีละส่วน (ค่าเริ่มต้นคือไม่ลบ)
  .\clear_test_data.cmd -DryRun    แสดงอย่างเดียว ไม่ลบอะไร

ส่วนที่ล้างได้
  influx   ข้อมูลทั้งหมดใน InfluxDB bucket ตั้งแต่เริ่มจนถึงตอนนี้ (ต้องเปิด Docker stack ใน infra อยู่)
  players  ฐานข้อมูลผู้เล่นจริง laptop\web\data\rlgl.db (ชื่อ, ผลรอบ, อันดับ) ต้องปิด backend ก่อน
  dev      ข้อมูลของ run.cmd -Sim: data\dev.db และ data\dev-clips
  clips    คลิปที่เก็บใน laptop\web\clips (ตอนยังไม่ได้ตั้ง Supabase)

ไม่ได้ล้างคลิปใน Supabase (ถ้ามีคลิปทดสอบ ให้ลบในหน้า Storage ของ Supabase เอง)
#>
param(
    [switch]$DryRun,
    [switch]$Yes,  # ไม่ถาม (ใช้ในการทดสอบสคริปต์) ระวัง: ลบทันที
    [string[]]$Only = @("influx", "players", "dev", "clips"),  # เช่น -Only clips หรือ -Only players,dev
    [string]$Bucket
)

$ErrorActionPreference = "Stop"
# ผ่าน run แบบ -File (เช่นจาก .cmd) รายการคั่นด้วย , จะมาเป็นข้อความเดียว จึงแยกเอง
$parts = "influx", "players", "dev", "clips"
$Only = @($Only | ForEach-Object { $_ -split "," } | ForEach-Object { $_.Trim().ToLower() } | Where-Object { $_ })
$unknown = @($Only | Where-Object { $parts -notcontains $_ })
if ($unknown) {
    Write-Host "ไม่รู้จัก: $($unknown -join ', ') (ใช้ได้: $($parts -join ', '))" -ForegroundColor Red
    exit 1
}
$laptop = $PSScriptRoot
$infra = Join-Path $laptop "infra"
$data = Join-Path $laptop "web\data"
function Say($text, $color = "Gray") { Write-Host $text -ForegroundColor $color }

function Confirm-Delete($what) {
    if ($DryRun) { Say "  (DryRun: ไม่ลบ)" DarkGray; return $false }
    if ($Yes) { return $true }
    $answer = Read-Host "  ลบ $what ? พิมพ์ y เพื่อลบ (Enter = ไม่ลบ)"
    return $answer -eq "y"
}

function Read-EnvValue($name, $default) {
    $file = Join-Path $infra ".env"
    if (Test-Path $file) {
        $found = Select-String -Path $file -Pattern "^\s*$name\s*=\s*(.+)$" | Select-Object -First 1
        if ($found) { return $found.Matches[0].Groups[1].Value.Trim() }
    }
    return $default
}

function Get-Size($paths) {
    $bytes = ($paths | Where-Object { Test-Path $_ } | Get-ChildItem -Recurse -File -ErrorAction SilentlyContinue |
        Measure-Object -Property Length -Sum).Sum
    if (-not $bytes) { return "0 MB" }
    return "{0:N1} MB" -f ($bytes / 1MB)
}

function Test-InUse($paths) {
    # มีโปรแกรม (เช่น backend) เปิดไฟล์ฐานข้อมูลอยู่ไหม: ลองเปิดแบบห้ามคนอื่นใช้ร่วม
    foreach ($path in $paths) {
        if (-not (Test-Path $path -PathType Leaf)) { continue }
        try { [IO.File]::Open($path, "Open", "ReadWrite", "None").Close() } catch { return $true }
    }
    return $false
}

# ส่ง Flux query เข้า container เป็น base64 (กันปัญหาเครื่องหมายคำพูดและ BOM ของ PowerShell 5.1)
function Invoke-Flux($flux) {
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($flux))
    Push-Location $infra
    try {
        $out = & docker compose exec -T influxdb sh -c "echo $encoded | base64 -d > /tmp/rlgl-q.flux && influx query --raw -f /tmp/rlgl-q.flux"
        if ($LASTEXITCODE -ne 0) { throw "query failed" }
        return $out
    } finally { Pop-Location }
}

function Get-CsvValue($lines, $column) {
    $rows = @($lines | Where-Object { $_ -and -not $_.StartsWith("#") })
    if ($rows.Count -lt 2) { return $null }
    $header = $rows[0].Split(",")
    $index = [array]::IndexOf($header, $column)
    # ใส่ , นำหน้า: ถ้ามีค่าเดียว PowerShell จะไม่แปลงเป็น string เดี่ยว (ไม่งั้น [0] ได้ตัวอักษรแรก)
    return , @($rows | Select-Object -Skip 1 | ForEach-Object { $_.Split(",")[$index] })
}

Say ""
Say "ล้างข้อมูลทดสอบ (ลบแล้วกู้คืนไม่ได้)" Yellow
Say "ใช้ก่อนเริ่มเก็บข้อมูลจริงเท่านั้น: ถ้าเก็บของจริงไปแล้ว ข้อมูลจริงในช่วงเดียวกันจะหายด้วย" Yellow
Say ""

# ---------- InfluxDB ----------
if ($Only -contains "influx") {
    if (-not $Bucket) { $Bucket = Read-EnvValue "INFLUX_BUCKET" "rlgl" }
    $org = Read-EnvValue "INFLUX_ORG" "rlgl"
    Say "[influx] InfluxDB bucket '$Bucket'" Cyan
    $running = $false
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        Push-Location $infra
        try { $running = (& docker compose ps --services --status running 2>$null) -contains "influxdb" } catch { $running = $false }
        finally { Pop-Location }
    }
    if (-not $running) {
        Say "  ข้าม: InfluxDB ไม่ได้เปิดอยู่ (เปิด Docker stack ใน laptop\infra ก่อน แล้วรันใหม่)" DarkYellow
    } else {
        $stop = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        try {
            $total = Get-CsvValue (Invoke-Flux "from(bucket: `"$Bucket`") |> range(start: 0, stop: $stop) |> group() |> count()") "_value"
            $perTable = Invoke-Flux ("from(bucket: `"$Bucket`") |> range(start: 0, stop: $stop) |> group(columns: [`"_measurement`"]) " +
                "|> count() |> group() |> keep(columns: [`"_measurement`", `"_value`"])")
            $first = Get-CsvValue (Invoke-Flux ("from(bucket: `"$Bucket`") |> range(start: 0, stop: $stop) |> keep(columns: [`"_time`"]) " +
                "|> group() |> sort(columns: [`"_time`"]) |> limit(n: 1)")) "_time"
        } catch {
            Say "  อ่าน bucket ไม่ได้ (ชื่อ bucket ผิด หรือ InfluxDB ยังไม่พร้อม): $_" Red
            $total = $null
        }
        $count = if ($total) { [int64]$total[0] } else { 0 }
        if ($count -eq 0) {
            Say "  ว่างอยู่แล้ว" DarkGray
        } else {
            Say ("  {0:N0} ค่า ตั้งแต่ {1} ถึงตอนนี้ ({2} UTC)" -f $count, ($first | Select-Object -First 1), $stop)
            $names = Get-CsvValue $perTable "_measurement"
            $values = Get-CsvValue $perTable "_value"
            for ($i = 0; $i -lt $names.Count; $i++) { Say ("    {0,-15} {1,10:N0}" -f $names[$i], [int64]$values[$i]) DarkGray }
            if (Confirm-Delete "ข้อมูลทั้งหมดใน bucket '$Bucket' ก่อน $stop") {
                Push-Location $infra
                try {
                    & docker compose exec -T influxdb influx delete --org $org --bucket $Bucket --start 1970-01-01T00:00:00Z --stop $stop
                    if ($LASTEXITCODE -ne 0) { throw "influx delete failed" }
                } finally { Pop-Location }
                $left = Get-CsvValue (Invoke-Flux "from(bucket: `"$Bucket`") |> range(start: 0, stop: $stop) |> group() |> count()") "_value"
                $leftCount = if ($left) { [int64]$left[0] } else { 0 }
                if ($leftCount -eq 0) { Say "  ลบแล้ว" Green } else { Say "  ยังเหลือ $leftCount ค่า ลองรันใหม่อีกครั้ง" Red }
            }
        }
    }
    Say ""
}

# ---------- ฐานข้อมูลผู้เล่นจริง ----------
if ($Only -contains "players") {
    $db = Join-Path $data "rlgl.db"
    $files = @($db, "$db-wal", "$db-shm")
    Say "[players] ฐานข้อมูลผู้เล่นจริง web\data\rlgl.db" Cyan
    if (-not (Test-Path $db)) {
        Say "  ยังไม่มีไฟล์" DarkGray
    } else {
        $summary = & python -c "import sqlite3,sys; d=sqlite3.connect(sys.argv[1]); print(d.execute('select count(*) from players').fetchone()[0], d.execute('select count(*) from rounds').fetchone()[0])" $db
        $parts = "$summary".Split(" ")
        Say "  ผู้เล่น $($parts[0]) คน, $($parts[1]) รอบ ($(Get-Size $files))"
        if (Test-InUse $files) {
            Say "  ข้าม: backend ยังเปิดไฟล์นี้อยู่ ปิดก่อน (Ctrl+C ในหน้าต่าง run.cmd) แล้วรันใหม่" DarkYellow
        } elseif (Confirm-Delete "ชื่อผู้เล่นและผลทุกรอบใน rlgl.db") {
            $files | Where-Object { Test-Path $_ } | Remove-Item -Force
            Say "  ลบแล้ว (backend จะสร้างไฟล์ใหม่ตอนเปิดครั้งถัดไป)" Green
        }
    }
    Say ""
}

# ---------- ข้อมูลของ run.cmd -Sim ----------
if ($Only -contains "dev") {
    $dev = @((Join-Path $data "dev.db"), (Join-Path $data "dev.db-wal"), (Join-Path $data "dev.db-shm"), (Join-Path $data "dev-clips"))
    $present = @($dev | Where-Object { Test-Path $_ })
    Say "[dev] ข้อมูลของ run.cmd -Sim (data\dev.db, data\dev-clips)" Cyan
    if (-not $present) {
        Say "  ไม่มี" DarkGray
    } else {
        Say "  $(Get-Size $present)"
        if (Test-InUse $dev) {
            Say "  ข้าม: backend (run.cmd -Sim) ยังเปิดไฟล์นี้อยู่ ปิดก่อนแล้วรันใหม่" DarkYellow
        } elseif (Confirm-Delete "ข้อมูลทดสอบของ -Sim") {
            $present | Remove-Item -Recurse -Force
            Say "  ลบแล้ว" Green
        }
    }
    Say ""
}

# ---------- คลิปที่เก็บใน laptop ----------
if ($Only -contains "clips") {
    $clips = Join-Path $laptop "web\clips"
    $videos = @(Get-ChildItem $clips -Filter *.mp4 -File -ErrorAction SilentlyContinue)
    Say "[clips] คลิปใน web\clips (เก็บไว้ตอนยังไม่ได้ตั้ง Supabase)" Cyan
    if (-not $videos) {
        Say "  ไม่มี" DarkGray
    } else {
        Say "  $($videos.Count) ไฟล์ ($(Get-Size $clips))"
        if (Confirm-Delete "คลิปทั้งหมดใน web\clips") {
            $videos | Remove-Item -Force
            Say "  ลบแล้ว" Green
        }
    }
    Say ""
}

Say "คลิปทดสอบใน Supabase (ถ้ามี) ต้องลบเองในหน้า Storage ของ Supabase" DarkGray
