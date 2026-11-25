"""สร้าง dashboards/rlgl.json  แก้ panel ที่ไฟล์นี้แล้วรัน: python make_dashboard.py"""
import json
import pathlib

DATASOURCE = {"type": "influxdb", "uid": "rlgl-influx"}
RANGE = "|> range(start: v.timeRangeStart, stop: v.timeRangeStop)"
panels = []


def source(measurement, field):
    return (f'from(bucket: "rlgl")\n  {RANGE}\n'
            f'  |> filter(fn: (r) => r._measurement == "{measurement}" and r._field == "{field}")')


def add(kind, title, query, x, y, w, h, **extra):
    panel = {
        "id": len(panels) + 1,
        "type": kind,
        "title": title,
        "datasource": DATASOURCE,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"refId": "A", "datasource": DATASOURCE, "query": query}],
    }
    panel.update(extra)
    panels.append(panel)


def row(title, y):
    panels.append({"id": len(panels) + 1, "type": "row", "title": title,
                   "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "collapsed": False})


def steps(*pairs):
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for v, c in pairs]}


def latest(measurement, field):
    return (source(measurement, field) +
            '\n  |> group()\n  |> sort(columns: ["_time"])\n  |> last()\n  |> keep(columns: ["_value"])')


def stat(title, measurement, field, x, y, w=4, unit=None, mappings=None, thresholds=None, text_field=False):
    defaults = {"thresholds": thresholds or steps((None, "blue")), "noValue": "--"}
    if unit:
        defaults["unit"] = unit
    if mappings:
        defaults["mappings"] = [{"type": "value", "options": mappings}]
    options = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "/.*/" if text_field else "", "values": False},
               "colorMode": "background" if mappings else "value", "graphMode": "none"}
    add("stat", title, latest(measurement, field), x, y, w, 4,
        fieldConfig={"defaults": defaults, "overrides": []}, options=options)


def series(title, measurement, field, x, y, w=12, fn="mean", unit=None, limit=None, name=None):
    query = (source(measurement, field) +
             f'\n  |> aggregateWindow(every: v.windowPeriod, fn: {fn}, createEmpty: false)'
             '\n  |> keep(columns: ["_time", "_value"])')
    defaults = {"displayName": name or field, "custom": {"lineWidth": 2, "fillOpacity": 8, "showPoints": "never", "insertNulls": 5000, "spanNulls": False}}
    if unit:
        defaults["unit"] = unit
    if limit is not None:
        defaults["thresholds"] = steps((None, "green"), (limit, "red"))
        defaults["custom"]["thresholdsStyle"] = {"mode": "line"}
    add("timeseries", title, query, x, y, w, 7, fieldConfig={"defaults": defaults, "overrides": []})


def table(title, query, x, y, w, h, window=None, overrides=None, columns=None):
    extra = {"fieldConfig": {"defaults": {}, "overrides": overrides or []}}
    if columns:
        extra["transformations"] = [{"id": "organize", "options": {
            "indexByName": {name: position for position, name in enumerate(columns)}}}]
    if window:
        extra["timeFrom"] = window
    add("table", title, query, x, y, w, h, **extra)


def history(measurement, columns):
    keep = ", ".join(f'"{c}"' for c in ["_time"] + columns)
    return (f'from(bucket: "rlgl")\n  {RANGE}\n'
            f'  |> filter(fn: (r) => r._measurement == "{measurement}")\n'
            '  |> keep(columns: ["_time", "_field", "_value"])\n'
            '  |> map(fn: (r) => ({r with _value: string(v: r._value)}))\n'
            '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
            '  |> group()\n'
            f'  |> keep(columns: [{keep}])\n'
            '  |> sort(columns: ["_time"], desc: true)\n'
            '  |> limit(n: 50)')


STATE_COLORS = {
    "IDLE": "#808080", "CALIBRATE": "#808080", "GREEN_LIGHT": "green", "TRANSITION": "red",
    "RED_LIGHT": "red", "GAME_OVER": "dark-red", "WIN": "blue",
}

row("สด: เกมและผู้เล่น", 0)
stat("สถานะเกม", "game_state", "state", 0, 1, w=8, text_field=True,
     mappings={k: {"color": v, "text": k} for k, v in STATE_COLORS.items()})
stat("เวลาที่เหลือ", "game_state", "remaining_s", 8, 1, unit="suffix: s")
stat("ชีพจร (BPM)", "heartrate", "bpm", 12, 1,
     thresholds=steps((None, "green"), (120, "orange"), (150, "red")))
stat("ระยะผู้เล่น", "distance", "distance_cm", 16, 1, unit="suffix: cm")
stat("FPS กล้อง", "vision", "fps", 20, 1)

series("ชีพจร", "heartrate", "bpm", 0, 5, name="BPM")
series("ระยะผู้เล่น", "distance", "distance_cm", 12, 5, name="cm")
# เส้นเกณฑ์เป็นค่าตัวอย่าง ต้องปรับตามผล calibrate
series("ข้อมือ: acc_peak (เส้นเกณฑ์ตัวอย่าง 0.3 g)", "imu", "acc_peak", 0, 12, fn="max", limit=0.3, name="acc_peak (g)")
series("ข้อมือ: gyro_rms", "imu", "gyro_rms", 12, 12, fn="max", name="gyro_rms (deg/s)")
series("กล้อง: motion_score (เส้นเกณฑ์ตัวอย่าง 0.05)", "vision", "motion_score", 0, 19, fn="max", limit=0.05,
       name="motion_score")
series("กล้อง: FPS", "vision", "fps", 12, 19, name="fps")

row("ประวัติ (ตารางย้อนหลัง 7 วัน)", 26)
table("ผลแต่ละรอบ", history("game_result", ["round_id", "result", "reason", "duration_s"]), 0, 27, 12, 9, window="7d",
      columns=["_time", "result", "reason", "duration_s", "round_id"])
table("สรุปของรอบ", history("game_summary", ["round_id", "bpm_avg", "bpm_max", "red_motion_max",
                                             "red_acc_peak_max", "red_light_count"]), 12, 27, 12, 9, window="7d",
      columns=["_time", "bpm_avg", "bpm_max", "red_motion_max", "red_acc_peak_max", "red_light_count", "round_id"])
table("คลิปวิดีโอ", history("media", ["round_id", "status", "video_url"]), 0, 36, 12, 8, window="7d",
      columns=["_time", "round_id", "status", "video_url"],
      overrides=[{"matcher": {"id": "byName", "options": "video_url"},
                  "properties": [{"id": "links", "value": [{"title": "เปิดวิดีโอ", "url": "${__value.raw}",
                                                           "targetBlank": True}]}]}])
table("สถานะอุปกรณ์",
      source("device_status", "status") +
      '\n  |> group(columns: ["device_id"])\n  |> sort(columns: ["_time"])\n  |> last()\n  |> group()'
      '\n  |> keep(columns: ["device_id", "_value", "_time"])\n  |> rename(columns: {_value: "status"})',
      12, 36, 12, 8, window="7d", columns=["device_id", "status", "_time"])

dashboard = {
    "uid": "rlgl-main",
    "title": "Red Light / Green Light",
    "timezone": "browser",
    "schemaVersion": 40,
    "refresh": "1s",
    "time": {"from": "now-5m", "to": "now"},
    "timepicker": {"refresh_intervals": ["1s", "5s", "10s", "30s", "1m"]},
    "panels": panels,
}

target = pathlib.Path(__file__).resolve().parent / "dashboards" / "rlgl.json"
target.parent.mkdir(exist_ok=True)
target.write_text(json.dumps(dashboard, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"{target.name}: {len(panels)} panels")
