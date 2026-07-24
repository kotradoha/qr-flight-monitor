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
import os
import re
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, date, timedelta, timezone
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
        "daily": False, "dow": 3,   # 목요일(Mon=0)
        "note": "매주 목요일 운항",
        "note_en": "Weekly on Thursdays",
    },
    "QR863": {
        "route": "서울 (ICN) → 도하 (DOH)", "route_en": "Seoul (ICN) → Doha (DOH)",
        "origin_tz": "seoul", "arr_tz": "doha",
        "sched_dep": "18:30", "sched_arr": "22:30",
        "labels": {"dep": "출발 (서울)", "arr": "도착 (도하)"},
        "labels_en": {"dep": "Departure (Seoul)", "arr": "Arrival (Doha)"},
        "daily": False, "dow": 4,   # 금요일
        "note": "매주 금요일 운항",
        "note_en": "Weekly on Fridays",
    },
}

# 핵심편 외 도하<->서울 임시·추가편 스캔 후보. 실제 편성 확인 시 자동 추가된다.
# 짝수(DOH→ICN)·홀수(ICN→DOH) 양방향을 모두 포함해 향후 신규편을 어느 방향이든 감지한다.
EXTRA_CANDIDATES = ["860", "861", "864", "865", "866", "867",
                    "868", "869", "870", "871", "872", "873", "888", "889"]
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
    delayed_now = fs["delay_dep"] >= DELAY_ALERT_MIN or fs["delay_arr"] >= DELAY_ALERT_MIN
    if delayed_now and code == "S":            # 아직 출발 전 → '지연' 상태
        entry["kind"], entry["cls"] = "delayed", "warn"
        entry["delay_dep"], entry["delay_arr"] = fs["delay_dep"], fs["delay_arr"]  # 출발·도착 각각
        alerts.append({"flight": fno, "date": d.isoformat(), "type": "delay",
                       "minutes": worst, "dep": fs["delay_dep"], "arr": fs["delay_arr"]})
        return ("delayed", worst) if offset >= 0 else None
    # 비행 중(A)·도착 완료(L): 기본 상태는 유지하되, 지연이 크면 함께 표기
    entry["kind"] = {"S": "sched", "A": "inflight", "L": "landed"}.get(code, "sched")
    entry["cls"] = "good"
    if delayed_now and code in ("A", "L"):
        entry["delay_dep"], entry["delay_arr"] = fs["delay_dep"], fs["delay_arr"]  # 상태색은 녹색 유지
        if code == "A":
            alerts.append({"flight": fno, "date": d.isoformat(), "type": "delay",
                           "minutes": worst, "dep": fs["delay_dep"], "arr": fs["delay_arr"]})
            return ("delayed", worst)
    return None


def _mark_suspended(fdict, note, note_en, until, source, url=None):
    """정기편을 '임시 미운영'으로 표시한다. 카드(표)는 그대로 두고, 미확정(plan)·확인중 예정일을
    'suspended'로 바꾼다. 확정 결항/지연 등 실데이터가 있는 날은 건드리지 않는다."""
    fdict["suspended"] = True
    fdict["suspended_source"] = source          # "operator"(운영자 지정) | "auto"(자동 감지)
    if note:
        fdict["suspended_note"] = note
        fdict["suspended_note_en"] = note_en or note
    if until:
        fdict["suspended_until"] = until
    if url and str(url).startswith(("http://", "https://")):
        fdict["suspended_url"] = url            # 공식 운휴/변경 공지 링크(있으면 배너에 표시)
    for day in fdict.get("days", []):
        if not day.get("confirmed") and day.get("kind") in ("plan", "checking"):
            day["kind"], day["cls"] = "suspended", "susp"


def build_core_flight(fno, cfg, now_utc, alerts, health):
    num = fno[2:]
    tz = tz_of(cfg["origin_tz"])
    arr_tz = tz_of(cfg["arr_tz"])
    today_local = now_utc.astimezone(tz).date()
    days = []
    badge = None                # 확정 상태가 있으면 여기에 설정
    confirmed_any = False
    net_error = False
    sched_checked = 0           # 근접(±3일) 예정일 중 조회 '성공' 건수
    sched_absent = 0            # 그 중 해당 편이 아예 없던(None) 건수 → 운휴 신호
    sched_obs = {}              # FlightStats '예정(scheduled)' 출발시각 관측(정기 스케줄 변경 감지용)

    # 표시 정책:
    #  - '도착일이 오늘 이후'인 편만 표시한다(도착 당일까지 유지 → 오늘 도착 완료편도 남는다).
    #    · 야간편(예: QR862 목 출발 → 금 도착)은 어제 출발했어도 오늘 도착했으면 오늘 하루는 남긴다.
    #  - 오늘 ±3일 안은 FlightStats로 실시간 상태(비행중/도착완료/지연/결항)를 확인,
    #    ±3일 밖은 발행 스케줄(운항 예정)로 채워 카타르항공 검색 결과와 동일 범위로 맞춘다.
    horizon = 7 if cfg["daily"] else 14   # 매일편 1주, 비정기편은 향후 2주 내 운항 요일
    overnight = 1 if str(cfg.get("sched_arr", "")).strip().startswith("익일") else 0  # 도착이 익일인 야간편
    for offset in range(-1, horizon + 1):   # -1: 어제 출발해 오늘 도착한 야간편을 놓치지 않도록
        d = today_local + timedelta(days=offset)
        if not cfg["daily"] and d.weekday() != cfg.get("dow", 3):   # 비정기편은 지정 운항 요일만
            continue
        # 도착일이 이미 지난 편은 제외 — 단 '도착일'은 출발지 날짜뿐 아니라 도착지 시간대로도 판단한다.
        #   (인천→도하 저녁편 등: 서울이 자정을 넘겨도 도하 도착 당일이면 계속 보여야 함)
        origin_keep = not ((d + timedelta(days=overnight)) < today_local)
        arr_keep = True
        try:
            _dh, _dm = map(int, str(cfg["sched_dep"]).split(":"))
            _sa = str(cfg["sched_arr"]).replace("익일", "").strip()
            _ah, _am = map(int, _sa.split(":"))
            _dep_dt = datetime(d.year, d.month, d.day, _dh, _dm, tzinfo=tz)
            _base = _dep_dt.astimezone(arr_tz).date()
            for _off in (0, 1):
                _cd = _base + timedelta(days=_off)
                _cand = datetime(_cd.year, _cd.month, _cd.day, _ah, _am, tzinfo=arr_tz)
                if timedelta(hours=1) <= (_cand - _dep_dt) <= timedelta(hours=15):
                    arr_keep = not (_cand.date() < now_utc.astimezone(arr_tz).date())
                    break
        except (ValueError, KeyError):
            arr_keep = True   # 계산 실패 시 유지 판단은 출발지 기준(origin_keep)에 맡김
        if not (origin_keep or arr_keep):
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
                sched_checked += 1   # 조회 자체는 성공(네트워크 정상)
                if fs:
                    code = fs["code"]
                    confirmed_any = True
                    entry["confirmed"] = True   # 이 날짜 상태가 실데이터로 확인됨(영공 폐쇄 판정의 근거가 됨)
                    # 정기 스케줄 변경 감지: FlightStats '예정' 출발시각(지연 아님)을 관측해 하드코딩값과 비교.
                    _sd = to_local(fs.get("dep_sched_utc"), tz)
                    if _sd:
                        sched_obs[_sd] = sched_obs.get(_sd, 0) + 1
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
                    elif code == "A":          # 현재 비행 중 — 상태는 녹색 유지, 지연이 크면 지연만 별도 표기
                        entry["kind"], entry["cls"] = "inflight", "good"
                        if fs["delay_dep"] >= DELAY_ALERT_MIN or fs["delay_arr"] >= DELAY_ALERT_MIN:
                            entry["delay_dep"], entry["delay_arr"] = fs["delay_dep"], fs["delay_arr"]
                            alerts.append({"flight": fno, "date": d.isoformat(), "type": "delay",
                                           "minutes": worst, "dep": fs["delay_dep"], "arr": fs["delay_arr"]})
                            if badge is None or badge.get("state") == "good":
                                badge = {"state": "warn", "kind": "delayed", "delay": worst}
                    elif code == "L":          # 도착 완료 — 당일 편은 숨기지 않고 '착륙'으로 표시(지연 도착이면 함께)
                        entry["kind"], entry["cls"] = "landed", "good"
                        if fs["delay_dep"] >= DELAY_ALERT_MIN or fs["delay_arr"] >= DELAY_ALERT_MIN:
                            entry["delay_dep"], entry["delay_arr"] = fs["delay_dep"], fs["delay_arr"]
                    elif (fs["delay_dep"] >= DELAY_ALERT_MIN or fs["delay_arr"] >= DELAY_ALERT_MIN) and code == "S":
                        entry["kind"], entry["cls"] = "delayed", "warn"
                        entry["delay_dep"], entry["delay_arr"] = fs["delay_dep"], fs["delay_arr"]  # 출발·도착 각각
                        alerts.append({"flight": fno, "date": d.isoformat(), "type": "delay",
                                       "minutes": worst, "dep": fs["delay_dep"], "arr": fs["delay_arr"]})
                        if badge is None or badge.get("state") == "good":
                            badge = {"state": "warn", "kind": "delayed", "delay": worst}
                    elif code == "S":          # 정시 예정(확인됨)
                        entry["kind"], entry["cls"] = "sched", "good"
                    else:                      # 알 수 없는/새 상태 코드 → '정상'으로 오인하지 않고 '확인 중'
                        entry["kind"], entry["cls"] = "checking", "plan"
                else:
                    sched_absent += 1          # 조회는 됐으나 해당일 편 없음(운휴 후보)
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

    # 임시 운휴(미운영) 자동 신호: 근접 예정일을 2일 이상 조회 성공했는데 '모두' 편이 없으면
    #   해당 정기편이 그 기간 운항하지 않는 것으로 본다(단발 데이터 누락 오탐 방지 위해 2건 이상 요구).
    #   최종 확정은 main()에서 파이프라인이 열화(degraded)가 아닐 때만 반영한다.
    susp_auto = (sched_checked >= 2 and sched_absent == sched_checked)

    # 정기 스케줄 변경 감지: 가장 많이 관측된 '예정' 출발시각이 하드코딩값과 다르고 2일 이상 일관되면 신호.
    #   (단발 이상치·지연은 걸러진다. 지연은 '예정'이 아닌 '예상' 시각이라 여기 영향 없음.)
    sched_change = None
    if sched_obs:
        top, cnt = max(sched_obs.items(), key=lambda kv: kv[1])
        if cnt >= 2 and top != cfg["sched_dep"]:
            sched_change = top

    # 실시간 위치(adsb.lol)는 화면에 표시하지 않으므로 수집하지 않는다(불필요한 외부요청 제거).
    out_cfg = {k: v for k, v in cfg.items() if k not in ("origin_tz", "arr_tz")}
    return {**out_cfg, "badge": badge, "days": days, "position": None,
            "_susp_auto": susp_auto, "_sched_change": sched_change}, confirmed_any


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
            for offset in range(-1, 4):   # -1: 어제 출발해 오늘 도착한 편도 당일까진 유지
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
                # 도착일이 이미 지난 편은 제외(도착 당일까지만 유지). 도착시각 확인되면 그 날짜로 판정.
                _arr_dt = to_local_dt(fs["arr_est_utc"], arr_tz) or to_local_dt(fs["arr_sched_utc"], arr_tz)
                if _arr_dt is not None and _arr_dt.date() < now_utc.astimezone(arr_tz).date():
                    continue
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


ICN_BASE = "http://apis.data.go.kr/B551177/StatusOfPassengerFlightsOdp"


def fetch_icn_board(key, now_utc, arrivals=True, timeout=12):
    """인천공항 공식 '여객편 운항현황(다국어)' API에서 카타르항공(QR) 편 상태를 가져온다(한국 쪽 전광판).
    arrivals=True: 인천 '도착'(QR858·QR862 확인), False: 인천 '출발'(QR859·QR863 확인).
    API가 '오늘(서울)'만 제공하므로 각 편에 서울 당일 날짜를 키로 붙인다.
    반환: {"편명@서울날짜(YYYY-MM-DD)": {status(remark), sched, est, airport}} / 실패·미검증 시 {}.
    ※ 키가 없거나 접속 실패/한도초과여도 조용히 빈 dict — 대시보드 표시는 절대 깨지지 않는다."""
    if not key:
        return {}
    op_date = now_utc.astimezone(TZ_SEOUL).date().isoformat()
    op = "getPassengerArrivalsOdp" if arrivals else "getPassengerDeparturesOdp"
    qs = urllib.parse.urlencode({
        "serviceKey": key, "airline": "QR", "lang": "E", "type": "json",
        "from_time": "0000", "to_time": "2400", "numOfRows": "300", "pageNo": "1",
    })
    url = f"{ICN_BASE}/{op}?{qs}"
    try:
        body = http_get(url, timeout=timeout, retries=1)   # 파이프라인 지연 방지: 짧게, 재시도 최소
    except FetchError as e:
        print(f"[warn] ICN board fetch ({op}) failed: {e}", file=sys.stderr)
        return {}
    try:
        raw = json.loads(body)
    except ValueError as e:
        print(f"[warn] ICN board ({op}) non-JSON: {e}", file=sys.stderr)
        return {}
    resp = raw.get("response") or {}
    hdr = resp.get("header") or {}
    if str(hdr.get("resultCode") or "").strip() not in ("", "00", "0"):
        print(f"[warn] ICN board ({op}) resultCode={hdr.get('resultCode')} msg={hdr.get('resultMsg')}",
              file=sys.stderr)
        return {}
    out = {}
    try:
        body_obj = resp.get("body") or {}
        items = body_obj.get("items") or {}
        item = items.get("item") if isinstance(items, dict) else items
        if isinstance(item, dict):
            item = [item]
        for it in (item or []):
            if not isinstance(it, dict):
                continue
            fid = str(it.get("flightId") or "").replace(" ", "").upper()
            if fid not in KOREA_FLIGHTS:      # 우리 4편만 기록
                continue
            out[f"{fid}@{op_date}"] = {
                "status": str(it.get("remark") or "").strip(),
                "sched": str(it.get("scheduleDateTime") or "").strip(),
                "est": str(it.get("estimatedDateTime") or "").strip(),
                "airport": str(it.get("airportCode") or it.get("airport") or "").strip(),
            }
    except Exception as e:  # noqa: BLE001
        print(f"[warn] ICN board ({op}) parse: {e}", file=sys.stderr)
    return out


HIA_BASE = "https://dohahamadairport.com/webservices/fids"


def _hhmm_tz(ts, tz):
    """유닉스초 → 해당 tz의 'HH:MM'. 실패 시 빈 문자열."""
    try:
        return datetime.fromtimestamp(int(ts), tz).strftime("%H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def fetch_hamad_board(now_utc, arrivals=False, timeout=12):
    """하마드공항(도하) 공식 전광판 FIDS API에서 QR 편 상태를 가져온다(도하 쪽 전광판, 키 불필요).
    arrivals=False: 도하 '출발'(QR858·QR862), True: 도하 '도착'(QR859·QR863).
    조회창을 '어제~내일(도하)'로 넓혀, 비행 중이라 도착이 내일 새벽인 편·이미 지나 빠진 편도 포함한다.
    반환: {"편명@도하날짜(YYYY-MM-DD)": {status, statusCode, sched, est, other}} / 실패·미검증 시 {}."""
    dnow = now_utc.astimezone(TZ_DOHA)
    start = (dnow - timedelta(days=1)).strftime("%d-%m-%Y")
    end = (dnow + timedelta(days=1)).strftime("%d-%m-%Y")
    typ = "arrivals" if arrivals else "departures"
    qs = urllib.parse.urlencode({"type": typ,
                                 "startTime": f"{start} 00:00:00", "endTime": f"{end} 23:59:59"})
    url = f"{HIA_BASE}?{qs}"
    try:
        raw = json.loads(http_get(url, timeout=timeout, retries=1))
    except (FetchError, ValueError) as e:
        print(f"[warn] HIA board ({typ}) failed: {e}", file=sys.stderr)
        return {}
    out = {}
    try:
        for it in (raw.get("flights") or []):
            if not isinstance(it, dict):
                continue
            fid = str(it.get("flightNumber") or "").replace(" ", "").upper()
            if fid not in KOREA_FLIGHTS:      # 우리 4편만 기록(데이터 경량화)
                continue
            sched_ts = it.get("scheduledTime")
            try:                                   # 예정 시각(유닉스초)으로 도하 현지 운항 날짜를 키에 붙임
                op_date = datetime.fromtimestamp(int(sched_ts), TZ_DOHA).date().isoformat()
            except (TypeError, ValueError, OSError, OverflowError):
                continue
            en = (it.get("lang") or {}).get("en") or {}
            out[f"{fid}@{op_date}"] = {
                "status": str(en.get("flightStatus") or "").strip(),
                "statusCode": str(it.get("statusCode") or "").strip(),
                "sched": _hhmm_tz(sched_ts, TZ_DOHA),
                "est": _hhmm_tz(it.get("estimateTime") or it.get("actualTimeOfDep")
                                or it.get("latestTime"), TZ_DOHA),
                "other": str((it.get("originCode") if arrivals else it.get("destinationCode")) or "").strip(),
            }
    except Exception as e:  # noqa: BLE001
        print(f"[warn] HIA board ({typ}) parse: {e}", file=sys.stderr)
    return out


def fetch_qr_alerts():
    """카타르항공 Travel Updates(travel-alerts.html)에서 '운항 중단·재개' 관련 공지만 자동 감지(보조).
    일반 안내(파워뱅크·비자·수하물·네트워크 확장 등)는 제외한다.
    반환: [{title,title_en,title_ar,date,until,url,source}] (제목은 페이지 영문 그대로). 실패/미검증 시 빈 목록.
    ※ 신뢰 채널은 운영자 지정(manual_notice.json 의 qr_notices)이며, 이 자동 감지는 어디까지나 보조 수단이다.
       페이지가 JS 렌더/구조 변경 등으로 읽히지 않으면 조용히 빈 목록을 반환한다(거짓 표기 방지)."""
    OP = re.compile(
        r"suspend|cancel|resume|disrupt|divert|halt|grounded|"
        r"not\s+operat|will\s+not\s+fly|temporar\w{0,3}\s+(?:stop|hold)",
        re.IGNORECASE)
    GEN = re.compile(
        r"power\s*bank|visa|baggage|check[- ]?in|network|expansion|expand|"
        r"loyalty|privilege|wi-?fi|lounge|meal|menu|entertainment",
        re.IGNORECASE)
    try:
        html = http_get("https://www.qatarairways.com/en/travel-alerts.html")
    except FetchError:
        return []
    low = html.lower()
    # 콘텐츠 검증(sentinel): 실제 Travel Alerts 페이지인지 확인. 아니면 신뢰 불가 → 빈 목록.
    if not ("qatar" in low and ("alert" in low or "travel" in low)):
        return []
    out = []
    seen = set()
    for m in re.finditer(r"<h[1-4][^>]*>(.*?)</h[1-4]>", html, re.IGNORECASE | re.DOTALL):
        txt = re.sub(r"<[^>]+>", " ", m.group(1))
        txt = re.sub(r"\s+", " ", txt).strip()
        if not txt or len(txt) > 140:
            continue
        if not OP.search(txt) or GEN.search(txt):   # 운항 관련만, 일반 안내는 제외
            continue
        key = txt.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "title": txt, "title_en": txt, "title_ar": txt,
            "date": "", "until": "",
            "url": "https://www.qatarairways.com/en/travel-alerts.html",
            "source": "auto",
        })
        if len(out) >= 6:
            break
    return out


KOREA_FLIGHTS = ("QR858", "QR859", "QR862", "QR863")


def compute_integrity(out, prev, today_iso, tom_iso):
    """빅시그널(영공 폐쇄·한국 노선 결항) 표시의 신뢰도 자가감사.
    기존 판정 로직·값은 전혀 바꾸지 않고, '조치 필요(material)' 여부와 근거만 별도 기록한다.
    시간 몇 분 어긋남 같은 사소한 건 다루지 않는다 — 크게 잘못 표시될 수 있는 신호만 잡는다.
    스케줄 모니터(시간별)가 이 값을 힌트로 읽어 즉시 알림 여부를 판단한다. 실패해도 파이프라인 무영향."""
    flags = []
    try:
        air = out.get("airspace") or {}
        level = air.get("level")
        flights = out.get("flights") or {}

        def cancels(d):
            s = set()
            fl = (d or {}).get("flights") or {}
            for fno in KOREA_FLIGHTS:
                for day in (fl.get(fno) or {}).get("days", []):
                    if day.get("confirmed") and day.get("kind") in ("cancelled", "diverted") \
                            and day.get("date") in (today_iso, tom_iso):
                        s.add(fno + "@" + str(day.get("date")))
            return s

        # 1) 영공 레벨 변화(이전 대비): closed 진입/이탈은 high, 그 외 전이는 info
        prev_level = ((prev or {}).get("airspace") or {}).get("level")
        if prev_level and prev_level != level:
            sev = "high" if (level == "closed" or prev_level == "closed") else "info"
            flags.append({"code": "airspace_level_change", "severity": sev,
                          "detail": f"{prev_level} → {level}"})

        # 2) 한국 노선 신규 결항/회항(오늘·내일) — 이전 대비 새로 생긴 것만
        new_c = cancels(out) - cancels(prev)
        if new_c:
            flags.append({"code": "new_cancellation", "severity": "high",
                          "detail": ", ".join(sorted(new_c))})

        # 3) 거짓 정상(false green) 위험: 영공이 open인데 회피/폐쇄 경보 신호가 잡힘
        if level == "open" and (air.get("warning_stated") or air.get("keyword_closed")):
            flags.append({"code": "false_green_risk", "severity": "high",
                          "detail": "영공 open 표시 중 회피/폐쇄 경보 신호 감지 — 확인 필요"})

        # 4) 빅시그널 신뢰 저하(열화): 실시간 확인된 편이 하나도 없음
        if out.get("degraded"):
            flags.append({"code": "degraded", "severity": "warn",
                          "detail": "실시간 확인된 편이 없어 표시가 스케줄 기준일 수 있음"})

        # 5) 표시 시각 타당성: 도착이 출발보다 빠르거나 소요시간이 비정상(FlightStats 이상치 가능)
        for fno in KOREA_FLIGHTS:
            for day in (flights.get(fno) or {}).get("days", []):
                if not day.get("confirmed"):
                    continue
                dep, arr = str(day.get("dep") or ""), str(day.get("arr") or "")
                mdep, marr = re.search(r"(\d{1,2}):(\d{2})", dep), re.search(r"(\d{1,2}):(\d{2})", arr)
                if not (mdep and marr):
                    continue
                dmin = int(mdep.group(1)) * 60 + int(mdep.group(2))
                amin = int(marr.group(1)) * 60 + int(marr.group(2))
                if "익일" in arr or "+" in arr:
                    amin += 24 * 60
                dur = amin - dmin
                # 도하-서울 편도는 대략 8~11시간(입국 방향 시차 포함). 크게 벗어나면 이상치.
                if dur <= 0 or dur > 20 * 60:
                    flags.append({"code": "time_sanity", "severity": "warn",
                                  "detail": f"{fno} {day.get('date')} 출/도착 시각 비정상({dep}→{arr})"})
                    break

        material = any(f["severity"] == "high" for f in flags)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:160], "flags": [], "material": False}
    return {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "flags": flags,
        "material": material,
    }


def _map_board_status(status):
    """전광판 상태 문구(영어) → 내부 kind. 알 수 없으면 None(덮어쓰지 않음)."""
    s = (status or "").strip().lower()
    if not s:
        return "sched"                       # 빈칸 = 아직 예정(운항 예정)
    if "cancel" in s:
        return "cancelled"
    if "divert" in s or "return to" in s:
        return "diverted"
    if any(w in s for w in ("arriv", "landed", "bag", "delivered")):
        return "landed"
    if any(w in s for w in ("depart", "airborne", "en route", "en-route", "take off", "takeoff")):
        return "inflight"
    if any(w in s for w in ("board", "gate", "on time", "schedul", "estimat", "delay",
                            "final", "check", "open", "close", "confirm", "wait")):
        return "sched"
    return None


def _hhmm_norm(t):
    """'HH:MM' 또는 'HHMM' → 'HH:MM'. 형식이 아니면 ''."""
    m = re.match(r"^(\d{1,2}):?(\d{2})$", str(t or "").strip())
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""


def _op_dates(d, cfg):
    """day 엔트리(출발일 d) 기준으로 출발공항·도착공항 현지 운항 날짜를 계산.
    반환: (dep_date_iso, arr_date_iso). dep는 출발공항 현지(=출발일 d), arr는 도착공항 현지 날짜.
    전광판 레코드를 '편명@날짜'로 정확히 매칭하기 위한 것. 계산 실패 시 arr는 None."""
    origin_tz = tz_of(cfg["origin_tz"])
    arr_tz = tz_of(cfg["arr_tz"])
    try:
        dh, dm = map(int, str(cfg["sched_dep"]).split(":"))
        sa = str(cfg["sched_arr"]).replace("익일", "").strip()
        ah, am = map(int, sa.split(":"))
    except (ValueError, KeyError):
        return d.isoformat(), None
    dep_dt = datetime(d.year, d.month, d.day, dh, dm, tzinfo=origin_tz)
    base = dep_dt.astimezone(arr_tz).date()
    arr_iso = None
    for off in (0, 1):
        cd = base + timedelta(days=off)
        cand = datetime(cd.year, cd.month, cd.day, ah, am, tzinfo=arr_tz)
        if timedelta(hours=1) <= (cand - dep_dt) <= timedelta(hours=15):
            arr_iso = cand.date().isoformat()
            break
    return d.isoformat(), arr_iso


def apply_board_display(flights_out):
    """전광판 우선 표시: 각 편의 각 날짜 행에 '날짜별로 정확히 매칭돼 붙은' 전광판(day['board'])이 있으면
    상태·시각을 그 값으로 표시한다. 전광판 레코드가 없으면(연결 실패 또는 조회창 밖) FlightStats 기본값을 그대로 둔다.
    안전장치: 임시 미운영 편 제외, 상태 다운그레이드 방지, 결항·회항은 전광판 확정, 알 수 없는 문구 무시."""
    RANK = {"plan": -1, "checking": -1, "sched": 0, "delayed": 1, "inflight": 1,
            "landed": 2, "diverted": 3, "cancelled": 3}
    CLS = {"sched": "good", "inflight": "good", "landed": "good", "delayed": "warn",
           "cancelled": "crit", "diverted": "crit"}

    def _bt(b):
        return _hhmm_norm((b or {}).get("est") or (b or {}).get("sched"))

    for fno in KOREA_FLIGHTS:
        f = flights_out.get(fno)
        if not isinstance(f, dict):
            continue
        cfg = FLIGHTS.get(fno) or {}
        origin = cfg.get("origin_tz")
        overnight = 1 if str(cfg.get("sched_arr", "")).strip().startswith("익일") else 0
        for day in f.get("days", []):
            if day.get("kind") == "suspended":
                continue
            board = day.get("board") or {}
            doha_b, korea_b = board.get("doha"), board.get("korea")
            # 도하 출발편은 도하=출발·인천=도착, 인천 출발편은 그 반대
            dep_b, arr_b = (doha_b, korea_b) if origin == "doha" else (korea_b, doha_b)
            if not (dep_b or arr_b):
                continue
            touched = False
            if dep_b and _bt(dep_b):
                day["dep"] = _bt(dep_b); touched = True
            if arr_b and _bt(arr_b):
                v = _bt(arr_b); day["arr"] = ("익일 " + v) if overnight else v; touched = True
            bkinds = []
            if dep_b:
                k = _map_board_status(dep_b.get("status"))
                if k:
                    bkinds.append(k)
            if arr_b:
                k = _map_board_status(arr_b.get("status"))
                if k:
                    bkinds.append(k)
            final = None
            if "cancelled" in bkinds:
                final = "cancelled"
            elif "diverted" in bkinds:
                final = "diverted"
            elif bkinds:
                cur = day.get("kind")
                final = max(bkinds + ([cur] if cur in RANK else []), key=lambda k: RANK.get(k, 0))
            if final:
                day["kind"] = final
                day["cls"] = CLS.get(final, "good")
                day["confirmed"] = True
                touched = True
            if touched:
                day["board_ok"] = True


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

    # ── 임시 운휴(미운영) 반영 ─────────────────────────────────────────────
    # 정기편이 한동안 사라져도 표(카드)를 지우지 않고 '임시 미운영'으로 보여준다.
    #   (1) 운영자 지정: manual_notice.json 의 suspended_flights (주간편·원거리 등 확실할 때).
    #   (2) 자동 감지: 근접 예정일이 모두 '편 없음'(_susp_auto) 이고 파이프라인이 정상일 때만.
    #       (전체 조회 실패로 인한 착시를 막기 위해 degraded 상태에서는 자동 반영하지 않는다.)
    susp_manual = {}
    _mn_path = ROOT / "docs" / "manual_notice.json"
    if _mn_path.exists():
        try:
            _sf = (json.loads(_mn_path.read_text(encoding="utf-8")).get("suspended_flights")) or {}
            if isinstance(_sf, list):
                _sf = {k: True for k in _sf}
            if isinstance(_sf, dict):
                for k, v in _sf.items():
                    susp_manual[str(k).upper()] = v
        except Exception as e:  # noqa: BLE001
            print(f"[warn] suspended_flights parse: {e}", file=sys.stderr)

    pipeline_ok = (confirmed_total > 0)
    for fno in core_order:
        fdict = flights_out.get(fno)
        if not fdict:
            continue
        auto = bool(fdict.pop("_susp_auto", False))
        mv = susp_manual.get(fno)
        if mv not in (None, False):
            note = note_en = until = url = None
            if isinstance(mv, dict):
                note = mv.get("note") or None
                note_en = mv.get("note_en") or note
                until = mv.get("until") or None
                url = mv.get("url") or None
            _mark_suspended(fdict, note, note_en, until, "operator", url)
        elif auto and pipeline_ok:
            _mark_suspended(fdict, None, None, None, "auto")
    # 유지보수 신호 수집: 정기 스케줄 변경 의심 + 임시·추가편 감지(운영자 안내용).
    maintenance = {"extra_flights": sorted(extra.keys()), "schedule_review": []}
    for fno in core_order:
        fdict = flights_out.get(fno)
        ch = fdict.pop("_sched_change", None) if isinstance(fdict, dict) else None
        if ch:
            maintenance["schedule_review"].append(
                {"flight": fno, "field": "dep",
                 "expected": FLIGHTS[fno]["sched_dep"], "observed": ch})
    for fdict in flights_out.values():   # 예외 경로 대비 임시키 정리
        if isinstance(fdict, dict):
            fdict.pop("_susp_auto", None)
            fdict.pop("_sched_change", None)

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
    qr_notices = []
    _qn_seen = set()
    today_doha_date = now_utc.astimezone(TZ_DOHA).date()
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
            # 카타르항공 Travel Updates 공지(운항 관련) — 운영자 지정분(신뢰 채널).
            #   until(안내 종료일)이 지난 항목은 자동으로 제외한다(안내된 시점까지만 표시).
            for it in (md.get("qr_notices") or []):
                title = (it.get("title") or it.get("title_en") or "").strip()
                if not title:
                    continue
                until = str(it.get("until") or "").strip()
                if until:
                    try:
                        if datetime.strptime(until[:10], "%Y-%m-%d").date() < today_doha_date:
                            continue   # 안내 종료일이 지남 → 표시하지 않음
                    except ValueError:
                        pass           # 형식 오류 시 만료 처리하지 않고 그대로 표시
                qr_notices.append({
                    "title": it.get("title", ""), "title_en": it.get("title_en", ""),
                    "title_ar": it.get("title_ar", ""), "date": str(it.get("date") or ""),
                    "until": until,
                    "url": it.get("url") or "https://www.qatarairways.com/en/travel-alerts.html",
                    "source": "operator",
                })
                _qn_seen.add(re.sub(r"\s+", "", (it.get("title_en") or title)).lower())
        except Exception as e:  # noqa: BLE001
            print(f"[warn] manual_notice parse: {e}", file=sys.stderr)

    # 자동 감지(보조): 카타르항공 Travel Updates 페이지에서 '운항 중단·재개' 공지가 감지되면 추가.
    #   운영자 지정분과 중복(제목 유사)이면 건너뛴다. 실패/미검증 시 조용히 넘어간다(보조 채널).
    try:
        for a in fetch_qr_alerts():
            k = re.sub(r"\s+", "", a.get("title_en") or "").lower()
            if not k or any(k in s or s in k for s in _qn_seen):
                continue
            _qn_seen.add(k)
            qr_notices.append(a)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] qr_alerts scan: {e}", file=sys.stderr)

    # ── 공식 전광판 대조 데이터: 도하(하마드)+한국(인천) 양쪽, 편·날짜별로 정확히 매칭해 부착 ──────
    #   조회창을 어제~내일(하마드)로 넓혀, 비행 중이라 도착이 내일 새벽인 편·이미 지나 빠진 편까지 포함한다.
    #   각 편의 '날짜 행'마다 실제 출발/도착 날짜를 계산해 '편명@날짜'로 정확히 매칭한다.
    #   어느 쪽이든 접속·키 실패 시 조용히 건너뛰며 화면은 전혀 영향받지 않는다(그 편은 FlightStats 유지).
    boards = {"checked_at_utc": now_utc.isoformat(timespec="seconds"),
              "hamad": {"ok": False}, "icn": {"ok": False}}
    ham_dep = ham_arr = icn_arr = icn_dep = {}
    try:
        ham_dep = fetch_hamad_board(now_utc, arrivals=False)   # 도하 출발(QR858·QR862)
        ham_arr = fetch_hamad_board(now_utc, arrivals=True)    # 도하 도착(QR859·QR863)
        if ham_dep or ham_arr:
            boards["hamad"] = {"ok": True, "departures": ham_dep, "arrivals": ham_arr}
    except Exception as e:  # noqa: BLE001
        print(f"[warn] HIA board stage: {e}", file=sys.stderr)
    icn_key = (os.environ.get("ICN_API_KEY") or "").strip()
    if icn_key:
        try:
            icn_arr = fetch_icn_board(icn_key, now_utc, arrivals=True)    # 인천 도착(QR858·QR862)
            icn_dep = fetch_icn_board(icn_key, now_utc, arrivals=False)   # 인천 출발(QR859·QR863)
            if icn_arr or icn_dep:
                boards["icn"] = {"ok": True, "arrivals": icn_arr, "departures": icn_dep}
        except Exception as e:  # noqa: BLE001
            print(f"[warn] ICN board stage: {e}", file=sys.stderr)

    # 편·날짜별 부착: 각 날짜 행의 실제 출발/도착 날짜를 계산해 '편명@날짜' 레코드를 찾아 day['board']에 붙인다.
    for fno in KOREA_FLIGHTS:
        f = flights_out.get(fno)
        if not isinstance(f, dict):
            continue
        cfg = FLIGHTS.get(fno) or {}
        origin = cfg.get("origin_tz")
        today_o = now_utc.astimezone(tz_of(origin)).date()
        nearest = None
        for day in f.get("days", []):
            try:
                dd = date.fromisoformat(day["date"])
            except (ValueError, KeyError):
                continue
            dep_iso, arr_iso = _op_dates(dd, cfg)
            if origin == "doha":   # QR858·862: 출발=하마드(출발일), 도착=인천(도착일)
                doha_rec = ham_dep.get(f"{fno}@{dep_iso}")
                korea_rec = icn_arr.get(f"{fno}@{arr_iso}") if arr_iso else None
            else:                  # QR859·863: 출발=인천(출발일), 도착=하마드(도착일)
                korea_rec = icn_dep.get(f"{fno}@{dep_iso}")
                doha_rec = ham_arr.get(f"{fno}@{arr_iso}") if arr_iso else None
            bd = {}
            if doha_rec:
                bd["doha"] = {**doha_rec, "source": "HIA"}
            if korea_rec:
                bd["korea"] = {**korea_rec, "source": "ICN"}
            if bd:
                day["board"] = bd
                if nearest is None or abs((dd - today_o).days) < nearest[0]:
                    nearest = (abs((dd - today_o).days), bd)
        if nearest:
            f["board"] = nearest[1]   # 모니터 호환용: 가장 가까운 인스턴스의 전광판

    # 전광판 우선 표시: 붙은 전광판 값으로 상태·시각을 표시(없으면 FlightStats 유지). 실패해도 표시 안 깨지게 try.
    try:
        apply_board_display(flights_out)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] apply_board_display: {e}", file=sys.stderr)

    out = {
        "generated_at_utc": now_utc.isoformat(timespec="seconds"),
        "generated_at_doha": now_utc.astimezone(TZ_DOHA).strftime("%Y-%m-%d %H:%M"),
        "generated_at_seoul": now_utc.astimezone(TZ_SEOUL).strftime("%Y-%m-%d %H:%M"),
        "degraded": degraded,
        "fetch_health": health,
        "airspace": airspace,
        "alerts": alerts,
        "travel_updates": travel_updates,
        "qr_notices": qr_notices,
        "order": order,
        "flights": flights_out,
        "maintenance": maintenance,
        "boards": boards,
    }
    # 빅시그널 자가감사(영공 폐쇄·한국 노선 결항 표시의 신뢰도). 기존 값은 안 바꾸고 별도 기록만 한다.
    out["integrity"] = compute_integrity(out, prev, today_iso, tom_iso)
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {DATA_PATH} — confirmed_flights={confirmed_total} "
          f"alerts={len(alerts)} fetch_ok={health['ok']} fetch_err={health['err']} "
          f"degraded={degraded}")


if __name__ == "__main__":
    main()
