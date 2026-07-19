#!/usr/bin/env python3
"""
카타르항공 도하<->서울 운항 모니터 - 데이터 수집 스크립트
GitHub Actions에서 매시간 실행되어 docs/data.json 을 갱신한다.

설계 원칙:
  - 정합성 우선: 확인되지 않은 상태를 '정상'으로 단정하지 않는다.
    조회 실패(네트워크/차단)와 '해당일 미운항'을 구분한다.
  - 견고성: HTTP 요청은 재시도한다. 개별 편/부가 소스 실패가 전체를 무너뜨리지 않는다.
  - 열화(degraded) 표시: 실시간 확인이 하나도 안 되면 data.json에 degraded=True를
    기록해 프론트가 '스케줄 기준·확인 중'으로 정직하게 표시하도록 한다.

데이터 출처(화면에 실제로 표시되는 것):
  - FlightStats(Cirium) 비공식 JSON 엔드포인트 — 운항 상태·시각(공항 게이트 기준)
  - SafeAirspace — 카타르 영공 폐쇄·권고
  - 카타르항공 발행 스케줄(코드 내 하드코딩) — '예매 가능' 향후편

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
        "route": "도하 (DOH) → 서울 (ICN)", "route_en": "Doha (DOH) → Seoul (ICN)",
        "origin_tz": "doha", "arr_tz": "seoul",
        "sched_dep": "02:20", "sched_arr": "17:05",
        "labels": {"dep": "출발 (도하)", "arr": "도착 (서울)"},
        "labels_en": {"dep": "Departure (Doha)", "arr": "Arrival (Seoul)"},
        "daily": True,
        "note": "매일 운항", "note_en": "Daily",
    },
    "QR859": {
        "route": "서울 (ICN) → 도하 (DOH)", "route_en": "Seoul (ICN) → Doha (DOH)",
        "origin_tz": "seoul", "arr_tz": "doha",
        "sched_dep": "01:20", "sched_arr": "05:20",
        "labels": {"dep": "출발 (서울)", "arr": "도착 (도하)"},
        "labels_en": {"dep": "Departure (Seoul)", "arr": "Arrival (Doha)"},
        "daily": True,
        "note": "매일 운항", "note_en": "Daily",
    },
    "QR862": {
        "route": "도하 (DOH) → 서울 (ICN)", "route_en": "Doha (DOH) → Seoul (ICN)",
        "origin_tz": "doha", "arr_tz": "seoul",
        "sched_dep": "19:45", "sched_arr": "익일 10:30",
        "labels": {"dep": "출발 (도하)", "arr": "도착 (서울)"},
        "labels_en": {"dep": "Departure (Doha)", "arr": "Arrival (Seoul)"},
        "daily": False,
        "note": "매주 목요일 운항",
        "note_en": "Weekly on Thursdays",
    },
}

# 핵심편 외 도하<->서울 임시·추가편 스캔 후보. 실제 편성 확인 시 자동 추가된다.
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
    body = http_get(url)             # 네트워크 실패 시 FetchError 전파
    try:
        raw = json.loads(body)
    except ValueError as e:          # 200이지만 비-JSON(차단·레이트리밋 HTML 등) → 조회 실패로 취급
        raise FetchError(f"non-JSON response: {e}")
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


def to_local_dt(utc_str, tz):
    """UTC ISO 문자열 → 해당 tz의 aware datetime. 실패 시 None."""
    if not utc_str:
        return None
    try:
        return datetime.fromisoformat(utc_str.replace("Z", "+00:00")).astimezone(tz)
    except ValueError:
        return None


def fmt_rel(dt, base_date):
    """시각을 'HH:MM'로, 도착이 출발일보다 뒤면 '익일 HH:MM'(+N일)로 표기.
    야간편(예: QR862 도하 19:45 출발 → 서울 익일 10:30 도착)의 날짜 넘김을 잃지 않는다."""
    if dt is None:
        return None
    hm = dt.strftime("%H:%M")
    delta = (dt.date() - base_date).days
    if delta <= 0:
        return hm
    if delta == 1:
        return "익일 " + hm
    return f"+{delta}일 " + hm


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

    # 표시 정책:
    #  - 카타르항공 발행 스케줄 기준으로 '오늘 이후'만 표시(도착 완료편은 제외).
    #  - 현재 진행 중(비행 중)과 미래 예정편만 남긴다.
    #  - 오늘 ±3일 안은 FlightStats로 실시간 상태(비행중/지연/결항)를 확인,
    #    ±3일 밖은 발행 스케줄(운항 예정)로 채워 카타르항공 검색 결과와 동일 범위로 맞춘다.
    horizon = 7 if cfg["daily"] else 14   # 매일편 1주, 비정기편은 향후 2주 내 운항 요일
    for offset in range(0, horizon + 1):
        d = today_local + timedelta(days=offset)
        if not cfg["daily"] and d.weekday() != 3:   # 비정기편은 운항 요일(목)만
            continue
        entry = {
            "date": d.isoformat(),
            "label": f"{d.month}/{d.day} ({DOW_KR[d.weekday()]})",
            "label_en": f"{DOW_EN[d.weekday()]} {d.month}/{d.day}",
            "dep": cfg["sched_dep"], "arr": cfg["sched_arr"],   # 시각은 카타르항공 스케줄 기준
            "kind": "plan", "cls": "plan", "delay": 0, "confirmed": False,
        }
        if offset <= 3:   # FlightStats 실시간 확인 가능 범위
            try:
                fs = fetch_flight(num, d)
                health["ok"] += 1
            except FetchError:
                fs = None
                health["err"] += 1
                net_error = True
                entry["kind"], entry["cls"] = "checking", "plan"
            else:
                if fs:
                    code = fs["code"]
                    confirmed_any = True
                    entry["confirmed"] = True   # 이 날짜 상태가 실데이터로 확인됨(영공 폐쇄 판정의 근거가 됨)
                    worst = max(fs["delay_dep"], fs["delay_arr"])
                    entry["delay"] = worst
                    # ±3일 이내: 실제(예상→예정) 시각을 반영 → 스케줄 변경/지연(예: 17:05→17:15)까지 표시.
                    #  날짜 넘김(야간편)은 출발일 기준으로 '익일' 표기를 유지한다.
                    dep_dt = to_local_dt(fs["dep_est_utc"], tz) or to_local_dt(fs["dep_sched_utc"], tz)
                    arr_dt = to_local_dt(fs["arr_est_utc"], arr_tz) or to_local_dt(fs["arr_sched_utc"], arr_tz)
                    dep_t = fmt_rel(dep_dt, d)
                    arr_t = fmt_rel(arr_dt, d)
                    if dep_t:
                        entry["dep"] = dep_t
                    if arr_t:
                        entry["arr"] = arr_t
                    if code == "C":
                        entry["kind"], entry["cls"] = "cancelled", "crit"
                        alerts.append({"flight": fno, "date": d.isoformat(), "type": "cancelled"})
                        badge = {"state": "crit", "kind": "cancelled"}
                    elif code in ("D", "R"):
                        entry["kind"], entry["cls"] = "diverted", "crit"
                        alerts.append({"flight": fno, "date": d.isoformat(), "type": "diverted"})
                        badge = {"state": "crit", "kind": "diverted"}
                    elif code == "A":          # 현재 비행 중
                        entry["kind"], entry["cls"] = "inflight", "good"
                    elif code == "L":          # 도착 완료 — 당일 편은 숨기지 않고 '착륙'으로 표시
                        entry["kind"], entry["cls"] = "landed", "good"
                    elif worst >= DELAY_ALERT_MIN and code == "S":
                        entry["kind"], entry["cls"] = "delayed", "warn"
                        alerts.append({"flight": fno, "date": d.isoformat(), "type": "delay", "minutes": worst})
                        if badge is None or badge.get("state") == "good":
                            badge = {"state": "warn", "kind": "delayed", "delay": worst}
                    elif code == "S":          # 정시 예정(확인됨)
                        entry["kind"], entry["cls"] = "sched", "good"
                    else:                      # 알 수 없는/새 상태 코드 → '정상'으로 오인하지 않고 '확인 중'
                        entry["kind"], entry["cls"] = "checking", "plan"
                # fs None(±3 내 데이터 없음) → '예매 가능'(plan) 유지
        # offset > 3 → 발행 스케줄(운항 예정) 그대로 표시
        days.append(entry)

    # 배지 결정: 확정 이상상태 > 확정 정상 > 확인 실패(check) > 정상(추정)
    if badge is None:
        if confirmed_any:
            badge = {"state": "good", "kind": "normal"}
        elif net_error:
            badge = {"state": "check", "kind": "checking"}   # 확인 실패
        else:
            badge = {"state": "good", "kind": "normal"}

    # 실시간 위치(adsb.lol)는 화면에 표시하지 않으므로 수집하지 않는다(불필요한 외부요청 제거).
    out_cfg = {k: v for k, v in cfg.items() if k not in ("origin_tz", "arr_tz")}
    return {**out_cfg, "badge": badge, "days": days, "position": None,
            "fr24": f"https://www.flightradar24.com/data/flights/qr{num}"}, confirmed_any


def discover_extra_flights(now_utc, alerts, health):
    """핵심편 외 도하<->서울 QR 임시·추가편을 스캔. 실패해도 전체에 영향 없음."""
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
                route, route_en = "도하 (DOH) → 서울 (ICN)", "Doha (DOH) → Seoul (ICN)"
                labels = {"dep": "출발 (도하)", "arr": "도착 (서울)"}
                labels_en = {"dep": "Departure (Doha)", "arr": "Arrival (Seoul)"}
            else:
                route, route_en = "서울 (ICN) → 도하 (DOH)", "Seoul (ICN) → Doha (DOH)"
                labels = {"dep": "출발 (서울)", "arr": "도착 (도하)"}
                labels_en = {"dep": "Departure (Seoul)", "arr": "Arrival (Doha)"}
            found["QR" + num] = {
                "route": route, "route_en": route_en, "labels": labels, "labels_en": labels_en,
                "daily": False, "temp": True,
                "note": "임시·추가 편성 (자동 감지)", "note_en": "Temporary / extra service (auto-detected)",
                "badge": badge, "days": days, "position": None,
                "fr24": f"https://www.flightradar24.com/data/flights/qr{num}",
            }
        except Exception as e:  # noqa: BLE001
            print(f"[warn] extra scan QR{num}: {e}", file=sys.stderr)
            continue
    return found


def _find_valid_until(text, start, end):
    """코드 매치 주변(뒤 380자·앞 160자)에서 유효기간 문구를 찾는다. 다양한 날짜 형식 지원."""
    win = text[end:end + 380] + "  " + text[max(0, start - 160):start]
    m = re.search(
        r"valid\s+(?:through|until|till|to)\s+"
        r"([A-Za-z]{3,}\.?\s+\d{1,2},?\s*\d{4}|\d{1,2}\s+[A-Za-z]{3,}\.?\s+\d{4}|"
        r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})",
        win, re.IGNORECASE)
    return m.group(1).strip(" .,") if m else None


def parse_advisories(text):
    """SafeAirspace 본문에서 권고 코드(CZIB/NOTAM)와 유효기간을 추출.
    서술문 오매칭을 막기 위해 접두 기관명은 단일 대문자 단어로 제한한다."""
    advisories = []
    seen = set()
    # EASA CZIB (예: "EASA CZIB 2026-07")
    for m in re.finditer(r"EASA\s+CZIB\s*([0-9]{4}-[0-9]{1,3}|[0-9]{2,}[0-9\-/]*)", text, re.IGNORECASE):
        code = re.sub(r"\s+", " ", m.group(0)).strip(" .,")
        key = code.lower()
        if key in seen:
            continue
        seen.add(key)
        advisories.append({"code": code, "valid_until": _find_valid_until(text, m.start(), m.end())})
    # NOTAM: (선택) 기관명 + NOTAM + 식별자. 예: "France NOTAM LFFF F1553/26", "NOTAM A1234/26"
    for m in re.finditer(
            r"(?:([A-Z][A-Za-z]{1,15})\s+)?NOTAM\s+((?:[A-Z]{1,4}\s+)?[A-Z]?[0-9]{2,4}/[0-9]{2})", text):
        num = re.sub(r"\s+", " ", m.group(2)).strip()
        key = "notam:" + num.lower()
        if key in seen:
            continue
        seen.add(key)
        auth = (m.group(1) + " ") if m.group(1) else ""
        advisories.append({"code": (auth + "NOTAM " + num).strip(),
                           "valid_until": _find_valid_until(text, m.start(), m.end())})
        if len(advisories) >= 8:
            break
    return advisories[:8]


def _airspace_open_stated(low):
    """현재 '영공이 열림/정상' 진술 감지. 공항(Hamad)만 열려 있다는 서술이 영공 폐쇄를 덮지 않도록
    'open'은 영공(airspace/FIR/OTDF/overflight) 문맥으로 한정한다."""
    return bool(
        re.search(r"(?:airspace|fir|otdf|overflight)[a-z /()]{0,40}\b(?:remains?|is|are|currently|now|still)\s+open", low)
        or re.search(r"operating\s+(?:largely\s+|mostly\s+)?normally", low)
    )


def _airspace_closure_stated(low):
    """'현재형' 영공 폐쇄 진술 감지(다양한 실제 표현 포함). 과거형('FIR closed and reopened')·
    명사형('airspace closures')은 배제하되, 실제 폐쇄 문구는 폭넓게 잡는다. (열림 진술이 있으면 main에서 무효화)"""
    pats = [
        r"(?:airspace|fir|otdf)[a-z /()]{0,40}\b(?:is|are|remains?|currently|now|has\s+been|been)\b[a-z /]{0,15}clos",
        r"clos(?:ed|ure|es|ing)\s+(?:of\s+)?(?:its\s+|the\s+|all\s+)?(?:airspace|fir|otdf|overflight)",  # "closed its airspace"
        r"airspace\s+closure[a-z /]{0,20}(?:in\s+effect|until|effective|remains)",
        r"clos(?:ed|ure)[a-z /]{0,15}until\s+\d",                     # closed until <일자>
        r"(?:all\s+)?(?:flights?|operations?|traffic)\s+(?:are\s+)?(?:currently\s+|been\s+)?suspend",
        r"suspend\w*\s+(?:all\s+)?(?:flights?|operations?|overflight|traffic)",
        r"complete(?:ly)?\s+clos",
        r"closed\s+to\s+all\s+(?:traffic|flights?|operations?)",
        r"no[- ]fly\s+zone",
    ]
    return any(re.search(p, low) for p in pats)


def _airspace_warning_stated(low):
    """항공당국의 '현재 회피/위험' 강한 권고 문구. 권고 코드 파싱이 실패하더라도 최소 '주의(caution)'를
    유지해 '거짓 정상(green)'을 방지하는 안전망."""
    return bool(re.search(
        r"do not operate|not to operate|not operate (?:within|in|at|there)|"
        r"should not (?:enter|operate|fly|use)|advis\w* (?:operators?\s+)?(?:to\s+)?(?:not|against)|"
        r"avoid(?:ing)?\s+(?:the\s+|all\s+|overflying\s+)?(?:airspace|overflight|region|country|qatar)|"
        r"high[- ]risk|extreme(?:ly)?\s+(?:high\s+)?risk|unsafe|dangerous",
        low))


def _risk_elevated(risk_desc, risk_level):
    """SafeAirspace 위험등급이 '주의' 이상인지. (등급 줄 텍스트에만 적용 — 본문 서술 오탐 방지)"""
    txt = f"{risk_desc or ''} {risk_level or ''}".lower()
    if re.search(r"caution|danger|do not|high|avoid|warning|no\s*fly", txt):
        return True
    if re.search(r"\b(two|three|four|five|2|3|4|5)\b", txt):  # 등급 숫자 상승
        return True
    return False


def _airspace_unavailable():
    """영공 상태를 확인하지 못했을 때의 값. ok=False → main에서 'unknown'(회색)으로 처리.
    '정상(green)'으로 절대 떨어지지 않게 하고, 이전 값을 되살리지도 않는다(끈적임 방지)."""
    return {
        "risk_level": None, "risk_desc": None,
        "keyword_closed": False, "open_stated": False, "closure_stated": False,
        "warning_stated": False, "advisories": [],
        "source": "https://safeairspace.net/qatar/",
        "checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": False,
    }


def fetch_airspace(prev):
    """SafeAirspace 카타르 페이지에서 위험 등급·(현재형)폐쇄 진술·권고 유효기간 추출.

    폐쇄(red) 판정은 '결항 정황'이 아니라 공역 당국 집계(SafeAirspace)의 **현재형 폐쇄 진술**에만
    근거한다(자동 감지). 과거/재개 서술 오탐을 막기 위해 현재 '열림' 진술이 있으면 폐쇄로 보지 않는다.
    ★ 200이라도 실제 카타르 영공 페이지가 아니면(차단·JS쉘·오류 페이지) ok=False로 처리해
      '거짓 정상(green)'을 방지한다. 실패/미검증 시 이전 값을 되살리지 않고 unknown으로 둔다."""
    try:
        html = http_get("https://safeairspace.net/qatar/")
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
        low = text.lower()
        # 콘텐츠 검증(sentinel): 실제 카타르 영공 페이지인지 확인. 아니면 신뢰 불가 → unknown.
        if not ("qatar" in low and ("airspace" in low or "otdf" in low or " fir" in low
                                    or "risk" in low or "notam" in low or "easa" in low)):
            print("[warn] airspace: page content unrecognized (sentinel missing)", file=sys.stderr)
            return _airspace_unavailable()
        m = re.search(r"(?:Risk\s*)?Level[:\s]*([A-Za-z]+)(?:\s*[-–—:]\s*([A-Za-z][A-Za-z ]{0,20}))?", text)
        risk_level = m.group(1).strip() if m else None
        risk_desc = (m.group(2).strip() if (m and m.group(2)) else None)
        open_stated = _airspace_open_stated(low)
        closure_stated = _airspace_closure_stated(low)
        warning_stated = _airspace_warning_stated(low)
        closed = closure_stated and not open_stated   # 현재 폐쇄 진술 + 열림 진술 없음일 때만
        return {
            "risk_level": risk_level,
            "risk_desc": risk_desc,            # 등급 서술(예: "Caution")
            "keyword_closed": closed,          # 현재형 폐쇄 진술 확인(자동 감지)
            "open_stated": open_stated,        # 현재 '정상/열림' 명시 여부(투명성용)
            "closure_stated": closure_stated,  # 현재형 폐쇄 문구 매칭 여부(투명성용)
            "warning_stated": warning_stated,  # 회피/고위험 강한 권고 문구(거짓 정상 방지)
            "advisories": parse_advisories(text),
            "source": "https://safeairspace.net/qatar/",
            "checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ok": True,
        }
    except Exception as e:  # noqa: BLE001
        print(f"[warn] airspace fetch failed: {e}", file=sys.stderr)
        return _airspace_unavailable()


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

    # 영공·공항 폐쇄 레벨 판정: open(정상·초록) / caution(회피권고·주황) / closed(폐쇄 확정·빨강)
    #
    # ★ 핵심 원칙: 폐쇄(빨강)는 '결항·지연 정황'으로 추정하지 않는다. 오직
    #    (1) 운영자 공식확인(manual_notice.json 의 airport_status="closed"), 또는
    #    (2) 공역 당국 집계(SafeAirspace)의 '현재형' 폐쇄 진술(자동 감지)
    #    에만 근거한다. 결항·지연은 폐쇄와 분리해 '항공 운항 특기사항'(alerts)으로 별도 표기.
    override = None
    override_source = None
    mn_path = ROOT / "docs" / "manual_notice.json"
    if mn_path.exists():
        try:
            _mn = json.loads(mn_path.read_text(encoding="utf-8"))
            _ov = str(_mn.get("airspace_status") or "").strip().lower()
            if _ov in ("closed", "open"):
                override = _ov
                override_source = _mn.get("airspace_status_source") or None
        except Exception as e:  # noqa: BLE001
            print(f"[warn] manual airspace_status parse: {e}", file=sys.stderr)

    # 실제 운항 데이터 교차검증: 오늘·내일 한국 노선의 확정 결항/회항 수.
    #   운영자 상시 개입이 어려운 현황판이므로, 영공 소스가 애매해도 '실제 다수 결항'을 자동 반영한다.
    doha_today = now_utc.astimezone(TZ_DOHA).date()
    today_iso = doha_today.isoformat()
    tom_iso = (doha_today + timedelta(days=1)).isoformat()
    cancel_flights = set()
    for fno in FLIGHTS:
        for day in (flights_out.get(fno) or {}).get("days", []):
            if day.get("confirmed") and day.get("kind") in ("cancelled", "diverted") \
                    and day.get("date") in (today_iso, tom_iso):
                cancel_flights.add(fno)
    # 서로 다른 편 수로 센다(한 편의 이틀 연속 결항을 2로 오인해 '폐쇄'로 격상하지 않도록).
    cancel_recent = len(cancel_flights)

    # 영공 경보 신호(폐쇄 진술·회피/고위험 문구·권고 코드·상승 위험등급) 존재 여부
    concern = bool(airspace.get("keyword_closed") or airspace.get("warning_stated")
                   or airspace.get("advisories")
                   or _risk_elevated(airspace.get("risk_desc"), airspace.get("risk_level")))

    if override in ("closed", "open"):
        level = override                     # 운영자 공식확인(최우선)
    elif airspace.get("keyword_closed"):     # SafeAirspace 현재형 폐쇄 진술(자동 감지)
        level = "closed"
    elif cancel_recent >= 2 and concern:
        level = "closed"                     # 교차검증: 영공 경보 발효 중 한국 노선 다수 결항 → 사실상 폐쇄
    elif not airspace.get("ok"):
        level = "caution" if cancel_recent >= 2 else "unknown"
    elif concern or cancel_recent >= 2:
        # 권고/회피·고위험 문구/상승등급, 또는 다수 결항 → '주의'(거짓 정상 방지)
        level = "caution"
    else:
        level = "open"                       # 특이 신호 없음 → 정상
    airspace["level"] = level
    airspace["cancel_recent"] = cancel_recent   # 교차검증 근거(투명성)
    airspace["closed"] = (level == "closed")
    airspace["status"] = level
    airspace["override"] = override          # 자동/운영자 구분 투명성
    if override_source:
        airspace["override_source"] = override_source

    # 열화 판정: 실시간으로 '확인된 편'이 하나도 없으면 degraded.
    #   네트워크 오류(err>0)뿐 아니라, FlightStats가 200이지만 빈/변경된 응답을 주는 경우
    #   (endpoint 스키마 변경 등)도 confirmed_total==0 으로 잡아 '정상'으로 오인하지 않는다.
    degraded = (confirmed_total == 0)

    # 긴급 Travel Update: 운영자가 docs/manual_notice.json 을 편집하면 표 위에 즉시 표시된다.
    # (카타르항공/하마드공항 공식 공지를 붙여넣는 용도. 자동 스크랩은 JS 렌더 페이지라 신뢰도가 낮아
    #  공식 링크는 프론트에서 항상 제공하고, 구체 문구는 이 파일로 운영자가 관리한다.)
    travel_updates = []
    mn = ROOT / "docs" / "manual_notice.json"
    if mn.exists():
        try:
            md = json.loads(mn.read_text(encoding="utf-8"))
            for it in (md.get("items") or []):
                if it.get("title") or it.get("title_en") or it.get("title_ar"):
                    travel_updates.append({
                        "title": it.get("title", ""),
                        "title_en": it.get("title_en", ""),
                        "title_ar": it.get("title_ar", ""),
                        "url": it.get("url", "https://www.qatarairways.com/en/travel-alerts.html"),
                    })
        except Exception as e:  # noqa: BLE001
            print(f"[warn] manual_notice parse: {e}", file=sys.stderr)

    out = {
        "generated_at_utc": now_utc.isoformat(timespec="seconds"),
        "generated_at_doha": now_utc.astimezone(TZ_DOHA).strftime("%Y-%m-%d %H:%M"),
        "generated_at_seoul": now_utc.astimezone(TZ_SEOUL).strftime("%Y-%m-%d %H:%M"),
        "degraded": degraded,
        "fetch_health": health,
        "airspace": airspace,
        "alerts": alerts,
        "travel_updates": travel_updates,
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
