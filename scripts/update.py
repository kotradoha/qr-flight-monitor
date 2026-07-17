#!/usr/bin/env python3
"""
카타르항공 도하<->인천 운항 모니터 - 데이터 수집 스크립트
GitHub Actions에서 매시간 실행되어 docs/data.json 을 갱신한다.

설계 원칙:
  - 정합성 우선: 확인되지 않은 상태를 '정상'으로 단정하지 않는다.
    조회 실패(네트워크/차단)와 '해당일 미운항'을 구분한다.
  - 견고성: HTTP 요청은 재시도한다. 개별 편/부가 소스 실패가 전체를 무너뜨리지 않는다.
  - 열화(degraded) 표시: 실시간 확인이 하나도 안 되면 data.json에 degraded=True를
    기록해 프론트가 '스케줄 기준·확인 중'으로 정직하게 표시하도록 한다.

데이터 출처:
  - FlightStats(Cirium) 비공식 JSON 엔드포인트 (v2 웹페이지가 사용하는 것과 동일)
  - SafeAirspace (카타르 영공 폐쇄·권고)
  - adsb.lol (실시간 항공기 위치, 커뮤니티 ADS-B)

status kind (프론트에서 한/영 라벨로 변환):
  sched / inflight / landed / cancelled / diverted / delayed
  plan(정기 스케줄 예정) / no_service(스케줄상 미운항) / checking(확인 실패·대기)
"""
import json
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "docs" / "data.json"

TZ_DOHA = ZoneInfo("Asia/Qatar")
TZ_SEOUL = ZoneInfo("Asia/Seoul")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# 감시 대상 핵심 편. arr_tz를 명시해 문자열 파싱 의존을 제거한다.
FLIGHTS = {
    "QR858": {
        "route": "도하 (DOH) → 인천 (ICN)", "route_en": "Doha (DOH) → Incheon (ICN)",
        "origin_tz": "doha", "arr_tz": "seoul",
        "sched_dep": "02:20", "sched_arr": "17:05",
        "labels": {"dep": "출발 (도하)", "arr": "도착 (인천)"},
        "labels_en": {"dep": "Departure (Doha)", "arr": "Arrival (Incheon)"},
        "daily": True,
        "note": "매일 운항 · A350-1000", "note_en": "Daily · A350-1000",
    },
    "QR859": {
        "route": "인천 (ICN) → 도하 (DOH)", "route_en": "Incheon (ICN) → Doha (DOH)",
        "origin_tz": "seoul", "arr_tz": "doha",
        "sched_dep": "01:20", "sched_arr": "05:20",
        "labels": {"dep": "출발 (인천)", "arr": "도착 (도하)"},
        "labels_en": {"dep": "Departure (Incheon)", "arr": "Arrival (Doha)"},
        "daily": True,
        "note": "매일 운항 · A350-1000", "note_en": "Daily · A350-1000",
    },
    "QR862": {
        "route": "도하 (DOH) → 인천 (ICN)", "route_en": "Doha (DOH) → Incheon (ICN)",
        "origin_tz": "doha", "arr_tz": "seoul",
        "sched_dep": "19:45", "sched_arr": "익일 10:30",
        "labels": {"dep": "출발 (도하)", "arr": "도착 (인천)"},
        "labels_en": {"dep": "Departure (Doha)", "arr": "Arrival (Incheon)"},
        "daily": False,
        "note": "비정기 운항 (최근 목요일 위주) · 스케줄에 없는 날은 결항 아님",
        "note_en": "Not daily (recently Thursdays) · absence from schedule is not a cancellation",
    },
}

# 핵심편 외 도하<->인천 임시·추가편 스캔 후보. 실제 편성 확인 시 자동 추가된다.
EXTRA_CANDIDATES = ["860", "864", "866", "868", "870", "888"]
ROUTE_APS = {"DOH", "ICN"}

DOW_KR = ["월", "화", "수", "목", "금", "토", "일"]
DOW_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DELAY_ALERT_MIN = 20  # 분


class FetchError(Exception):
    """네트워크/HTTP 실패 (조회 자체가 안 됨). '해당일 미운항'과 구분하기 위함."""


def tz_of(key):
    return TZ_DOHA if key == "doha" else TZ_SEOUL


def http_get(url, timeout=25, retries=3):
    """지수적 백오프로 재시도. 최종 실패 시 FetchError."""
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
    raise FetchError(str(last))


def fetch_flight(number, d):
    """FlightStats JSON 조회.
    반환: 편 dict(성공) / None(HTTP는 됐으나 해당일 편 데이터 없음).
    네트워크·HTTP 실패 시 FetchError 발생(호출부에서 '확인 실패'로 처리)."""
    url = (f"https://www.flightstats.com/v2/api-next/flight-tracker/"
           f"QR/{number}/{d.year}/{d.month}/{d.day}")
    raw = json.loads(http_get(url))  # 실패 시 FetchError 전파
    data = raw.get("data") or {}
    if not data or not data.get("status"):
        return None
    sched = data.get("schedule") or {}
    status = data.get("status") or {}
    note = data.get("flightNote") or {}
    delay = status.get("delay") or {}

    def mins(side):
        try:
            return int(((delay.get(side) or {}).get("minutes")) or 0)
        except (TypeError, ValueError):
            return 0

    code = (status.get("statusCode") or "U").upper()
    if note.get("canceled"):
        code = "C"
    return {
        "code": code,
        "dep_ap": ((data.get("departureAirport") or {}).get("fs") or "").upper(),
        "arr_ap": ((data.get("arrivalAirport") or {}).get("fs") or "").upper(),
        "dep_sched_utc": sched.get("scheduledDepartureUTC"),
        "arr_sched_utc": sched.get("scheduledArrivalUTC"),
        "dep_est_utc": sched.get("estimatedActualDepartureUTC"),
        "arr_est_utc": sched.get("estimatedActualArrivalUTC"),
        "delay_dep": mins("departure"),
        "delay_arr": mins("arrival"),
    }


def to_local(utc_str, tz):
    if not utc_str:
        return None
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        return dt.astimezone(tz).strftime("%H:%M")
    except ValueError:
        return None


def classify(fs, entry, fno, offset, d, alerts):
    """편 dict를 받아 entry 상태를 채우고, 배지 반영용 badge kind를 반환."""
    code = fs["code"]
    worst = max(fs["delay_dep"], fs["delay_arr"])
    entry["confirmed"] = True
    entry["delay"] = worst
    if code == "C":
        entry["kind"], entry["cls"] = "cancelled", "crit"
        alerts.append({"flight": fno, "date": d.isoformat(), "type": "cancelled"})
        return "cancelled" if offset >= 0 else None
    if code in ("D", "R"):
        entry["kind"], entry["cls"] = "diverted", "crit"
        alerts.append({"flight": fno, "date": d.isoformat(), "type": "diverted"})
        return "diverted"
    if worst >= DELAY_ALERT_MIN and code in ("S", "A"):
        entry["kind"], entry["cls"] = "delayed", "warn"
        alerts.append({"flight": fno, "date": d.isoformat(), "type": "delay", "minutes": worst})
        return ("delayed", worst) if offset >= 0 else None
    entry["kind"] = {"S": "sched", "A": "inflight", "L": "landed"}.get(code, "sched")
    entry["cls"] = "good"
    return None


def build_core_flight(fno, cfg, now_utc, alerts, health):
    num = fno[2:]
    tz = tz_of(cfg["origin_tz"])
    arr_tz = tz_of(cfg["arr_tz"])
    today_local = now_utc.astimezone(tz).date()
    days = []
    badge = None                # 확정 상태가 있으면 여기에 설정
    confirmed_any = False
    net_error = False

    # 매일 운항이면 향후 1주, 비정기(예: QR862 목요일)면 2주 범위에서 해당 요일만 표시
    hi = 7 if cfg["daily"] else 15
    for offset in range(-1, hi):
        d = today_local + timedelta(days=offset)
        # 비정기편은 운항 요일(QR862=목요일)만 남긴다
        if not cfg["daily"] and d.weekday() != 3:
            continue
        entry = {
            "date": d.isoformat(),
            "label": f"{d.month}/{d.day} ({DOW_KR[d.weekday()]})",
            "label_en": f"{DOW_EN[d.weekday()]} {d.month}/{d.day}",
            "dep": cfg["sched_dep"], "arr": cfg["sched_arr"],
            "kind": "plan" if cfg["daily"] else "checking",
            "cls": "plan", "delay": 0, "confirmed": False,
        }
        if -1 <= offset <= 3:   # FlightStats 개별 편 조회 가능 범위(오늘 ±3일)
            try:
                fs = fetch_flight(num, d)
                health["ok"] += 1
            except FetchError:
                fs = None
                health["err"] += 1
                net_error = True
                # 조회 실패: '미운항'으로 단정하지 않고 '확인 중'으로 둔다
                entry["kind"], entry["cls"] = "checking", "plan"
            else:
                if fs:
                    dep_t = to_local(fs["dep_est_utc"], tz) or to_local(fs["dep_sched_utc"], tz)
                    arr_t = to_local(fs["arr_est_utc"], arr_tz) or to_local(fs["arr_sched_utc"], arr_tz)
                    entry["dep"] = dep_t or cfg["sched_dep"]
                    entry["arr"] = arr_t or cfg["sched_arr"]
                    result = classify(fs, entry, fno, offset, d, alerts)
                    confirmed_any = True
                    if offset >= 0 and result is not None:
                        if isinstance(result, tuple):
                            badge = {"state": "warn", "kind": "delayed", "delay": result[1]}
                        elif result == "cancelled":
                            badge = {"state": "crit", "kind": "cancelled"}
                        elif result == "diverted":
                            badge = {"state": "crit", "kind": "diverted"}
                elif not cfg["daily"]:
                    entry["kind"], entry["cls"] = "no_service", "plan"
                    entry["dep"] = entry["arr"] = "—"
        if offset == -1 and not entry["confirmed"]:
            continue
        days.append(entry)

    # 배지 결정: 확정 이상상태 > 확정 정상 > 확인 실패(check) > 정상(추정)
    if badge is None:
        if confirmed_any:
            badge = {"state": "good", "kind": "normal"}
        elif net_error:
            badge = {"state": "check", "kind": "checking"}   # 확인 실패
        else:
            badge = {"state": "good", "kind": "normal"}

    position = safe_position(f"QTR{num}")
    if position and badge == {"state": "good", "kind": "normal"}:
        badge = {"state": "good", "kind": "inflight"}

    out_cfg = {k: v for k, v in cfg.items() if k not in ("origin_tz", "arr_tz")}
    return {**out_cfg, "badge": badge, "days": days, "position": position,
            "fr24": f"https://www.flightradar24.com/data/flights/qr{num}"}, confirmed_any


def safe_position(callsign):
    try:
        raw = json.loads(http_get(f"https://api.adsb.lol/v2/callsign/{callsign}", retries=2))
        ac = (raw.get("ac") or [])
        if not ac:
            return None
        a = ac[0]
        if a.get("lat") is None:
            return None
        return {"lat": a.get("lat"), "lon": a.get("lon"), "alt_ft": a.get("alt_baro"),
                "gs_kt": a.get("gs"), "callsign": callsign,
                "seen_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "source": "adsb.lol"}
    except Exception as e:  # noqa: BLE001
        print(f"[warn] position {callsign}: {e}", file=sys.stderr)
        return None


def discover_extra_flights(now_utc, alerts, health):
    """핵심편 외 도하<->인천 QR 임시·추가편을 스캔. 실패해도 전체에 영향 없음."""
    core = {f[2:] for f in FLIGHTS}
    doha_today = now_utc.astimezone(TZ_DOHA).date()
    found = {}
    for num in EXTRA_CANDIDATES:
        if num in core:
            continue
        try:
            days, direction, badge = [], None, {"state": "good", "kind": "normal"}
            for offset in range(0, 4):
                d = doha_today + timedelta(days=offset)
                try:
                    fs = fetch_flight(num, d)
                    health["ok"] += 1
                except FetchError:
                    health["err"] += 1
                    continue
                if not fs or fs["dep_ap"] not in ROUTE_APS or fs["arr_ap"] not in ROUTE_APS \
                        or fs["dep_ap"] == fs["arr_ap"]:
                    continue
                dep_tz = TZ_SEOUL if fs["dep_ap"] == "ICN" else TZ_DOHA
                arr_tz = TZ_SEOUL if fs["arr_ap"] == "ICN" else TZ_DOHA
                if direction is None:
                    direction = (fs["dep_ap"], fs["arr_ap"])
                entry = {
                    "date": d.isoformat(),
                    "label": f"{d.month}/{d.day} ({DOW_KR[d.weekday()]})",
                    "label_en": f"{DOW_EN[d.weekday()]} {d.month}/{d.day}",
                    "dep": to_local(fs["dep_est_utc"], dep_tz) or to_local(fs["dep_sched_utc"], dep_tz) or "—",
                    "arr": to_local(fs["arr_est_utc"], arr_tz) or to_local(fs["arr_sched_utc"], arr_tz) or "—",
                    "kind": "sched", "cls": "good", "delay": 0, "confirmed": True,
                }
                res = classify(fs, entry, "QR" + num, offset, d, alerts)
                if res is not None:
                    if isinstance(res, tuple):
                        badge = {"state": "warn", "kind": "delayed", "delay": res[1]}
                    elif res == "cancelled":
                        badge = {"state": "crit", "kind": "cancelled"}
                    elif res == "diverted":
                        badge = {"state": "crit", "kind": "diverted"}
                days.append(entry)

            if not days or direction is None:
                continue
            arr_ap = direction[1]
            if arr_ap == "ICN":
                route, route_en = "도하 (DOH) → 인천 (ICN)", "Doha (DOH) → Incheon (ICN)"
                labels = {"dep": "출발 (도하)", "arr": "도착 (인천)"}
                labels_en = {"dep": "Departure (Doha)", "arr": "Arrival (Incheon)"}
            else:
                route, route_en = "인천 (ICN) → 도하 (DOH)", "Incheon (ICN) → Doha (DOH)"
                labels = {"dep": "출발 (인천)", "arr": "도착 (도하)"}
                labels_en = {"dep": "Departure (Incheon)", "arr": "Arrival (Doha)"}
            found["QR" + num] = {
                "route": route, "route_en": route_en, "labels": labels, "labels_en": labels_en,
                "daily": False, "temp": True,
                "note": "임시·추가 편성 (자동 감지)", "note_en": "Temporary / extra service (auto-detected)",
                "badge": badge, "days": days, "position": safe_position(f"QTR{num}"),
                "fr24": f"https://www.flightradar24.com/data/flights/qr{num}",
            }
        except Exception as e:  # noqa: BLE001
            print(f"[warn] extra scan QR{num}: {e}", file=sys.stderr)
            continue
    return found


def _find_valid_until(text, pos):
    """pos 이후 220자 창에서 유효기간 문구를 찾는다."""
    win = text[pos:pos + 220]
    m = re.search(
        r"valid\s+(?:through|until|to)\s+"
        r"([A-Za-z]+\s+\d{1,2},?\s*\d{4}|\d{1,2}\s+[A-Za-z]+\s+\d{4}|\d{4}-\d{2}-\d{2})",
        win, re.IGNORECASE)
    return m.group(1).strip() if m else None


def parse_advisories(text):
    """SafeAirspace 본문에서 권고 코드(CZIB/NOTAM)와 유효기간을 추출.
    서술문 오매칭을 막기 위해 접두 기관명은 단일 대문자 단어로 제한한다."""
    advisories = []
    seen = set()
    # EASA CZIB
    for m in re.finditer(r"EASA\s+CZIB\s*([0-9]{4}-[0-9]{1,3}|[0-9]{2,}[0-9\-/]*)", text, re.IGNORECASE):
        code = re.sub(r"\s+", " ", m.group(0)).strip(" .,")
        key = code.lower()
        if key in seen:
            continue
        seen.add(key)
        advisories.append({"code": code, "valid_until": _find_valid_until(text, m.end())})
    # NOTAM: (선택) 단일 기관 대문자 단어 + NOTAM + 식별자
    for m in re.finditer(
            r"(?:([A-Z][A-Za-z]{1,15})\s+)?NOTAM\s+([A-Z]{0,4}\s?[A-Z]?[0-9]{2,4}/[0-9]{2})", text):
        num = re.sub(r"\s+", " ", m.group(2)).strip()
        key = "notam:" + num.lower()
        if key in seen:
            continue
        seen.add(key)
        auth = (m.group(1) + " ") if m.group(1) else ""
        advisories.append({"code": (auth + "NOTAM " + num).strip(),
                           "valid_until": _find_valid_until(text, m.end())})
        if len(advisories) >= 6:
            break
    return advisories[:6]


def fetch_airspace(prev):
    """SafeAirspace 카타르 페이지에서 위험 등급·폐쇄 여부·권고 유효기간 추출. 실패 시 이전 값 유지."""
    try:
        html = http_get("https://safeairspace.net/qatar/")
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
        low = text.lower()
        m = re.search(r"(?:Risk\s*)?Level[:\s]*([A-Za-z]+)", text)
        closed = bool(re.search(
            r"airspace\s+clos|fully\s+clos|closed\s+to\s+all|complete\s+clos|"
            r"suspend(ed)?\s+all\s+flight|no\s+overflight", low))
        return {
            "risk_level": m.group(1).strip() if m else None,
            "keyword_closed": closed,      # 폐쇄 '가능성' 신호 (레벨 판정은 main에서 운항현실과 종합)
            "advisories": parse_advisories(text),
            "source": "https://safeairspace.net/qatar/",
            "checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ok": True,
        }
    except Exception as e:  # noqa: BLE001
        print(f"[warn] airspace fetch failed: {e}", file=sys.stderr)
        old = (prev or {}).get("airspace") or {"level": "unknown"}
        old["ok"] = False
        return old


def main():
    prev = None
    if DATA_PATH.exists():
        try:
            prev = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prev = None

    now_utc = datetime.now(timezone.utc)
    alerts = []
    health = {"ok": 0, "err": 0}
    flights_out = {}
    confirmed_total = 0

    for fno, cfg in FLIGHTS.items():
        try:
            entry, confirmed_any = build_core_flight(fno, cfg, now_utc, alerts, health)
            flights_out[fno] = entry
            confirmed_total += 1 if confirmed_any else 0
        except Exception as e:  # noqa: BLE001
            print(f"[warn] build {fno} failed: {e}", file=sys.stderr)
            # 개별 편 실패 시 이전 데이터 유지(있으면)
            if prev and (prev.get("flights") or {}).get(fno):
                flights_out[fno] = prev["flights"][fno]

    core_order = list(FLIGHTS.keys())
    try:
        extra = discover_extra_flights(now_utc, alerts, health)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] discover_extra_flights failed: {e}", file=sys.stderr)
        extra = {}
    flights_out.update(extra)
    order = core_order + sorted(extra.keys())

    airspace = fetch_airspace(prev)

    # 영공 상태 레벨 판정: open(정상·초록) / caution(폐쇄 가능·예상·주황) / closed(폐쇄 확정·빨강)
    # 판정 근거는 '운항 현실'을 최우선으로 삼아 서술문 키워드 오탐을 방지한다.
    doha_today = now_utc.astimezone(TZ_DOHA).date()
    today_iso, tom_iso = doha_today.isoformat(), (doha_today + timedelta(days=1)).isoformat()
    cancel_recent = 0
    for fno in FLIGHTS:
        for day in (flights_out.get(fno) or {}).get("days", []):
            if day.get("confirmed") and day.get("kind") in ("cancelled", "diverted") \
                    and day.get("date") in (today_iso, tom_iso):
                cancel_recent += 1

    if not airspace.get("ok"):
        level = "unknown"
    elif cancel_recent >= 2:
        level = "closed"    # 오늘·내일 다수 결항/회항 → 사실상 폐쇄 확정
    elif airspace.get("keyword_closed") or airspace.get("advisories") or cancel_recent >= 1:
        level = "caution"   # 회피 권고 발효 또는 일부 차질 → 폐쇄 가능성(예상)
    else:
        level = "open"      # 특이 신호 없음 → 정상
    airspace["level"] = level
    airspace["closed"] = (level == "closed")
    airspace["status"] = level

    # 열화 판정: 실시간 확인이 하나도 안 됐고 조회 오류가 있었으면 degraded
    degraded = (confirmed_total == 0 and health["err"] > 0)

    out = {
        "generated_at_utc": now_utc.isoformat(timespec="seconds"),
        "generated_at_doha": now_utc.astimezone(TZ_DOHA).strftime("%Y-%m-%d %H:%M"),
        "generated_at_seoul": now_utc.astimezone(TZ_SEOUL).strftime("%Y-%m-%d %H:%M"),
        "degraded": degraded,
        "fetch_health": health,
        "airspace": airspace,
        "alerts": alerts,
        "order": order,
        "flights": flights_out,
    }
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {DATA_PATH} — confirmed_flights={confirmed_total} "
          f"alerts={len(alerts)} fetch_ok={health['ok']} fetch_err={health['err']} "
          f"degraded={degraded}")


if __name__ == "__main__":
    main()
