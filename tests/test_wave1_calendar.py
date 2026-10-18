"""
Wave 1 — Track B: 캘린더(US/KRX) 연도 무관화 + systemd 타이머 설치 스크립트 불변식.

모두 오프라인/결정적. holidays 라이브러리 버전(로컬 0.90 / 운영 0.103) 차이에 무관해야 한다.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import src.utils.market_calendar as mc
from src.utils.market_calendar import MarketCalendar

ROOT = Path(__file__).resolve().parents[1]


def _weekday_set(dates):
    return {d for d in dates if datetime.strptime(d, '%Y%m%d').weekday() < 5}


@pytest.fixture
def fresh_us_cache(monkeypatch):
    monkeypatch.setattr(mc, '_US_HOLIDAY_CACHE', {})
    monkeypatch.setattr(mc, '_US_HALF_DAY_CACHE', {})


# ══════════════════════════════════════════════════════════════
# US (NYSE) calendar
# ══════════════════════════════════════════════════════════════

# NYSE 공식 휴장일 (2024~2027)
NYSE_HOLIDAYS = {
    2024: {'20240101', '20240115', '20240219', '20240329', '20240527', '20240619', '20240704',
           '20240902', '20241128', '20241225'},
    2025: {'20250101', '20250109', '20250120', '20250217', '20250418', '20250526', '20250619',
           '20250704', '20250901', '20251127', '20251225'},
    2026: {'20260101', '20260119', '20260216', '20260403', '20260525', '20260619', '20260703',
           '20260907', '20261126', '20261225'},
    2027: {'20270101', '20270118', '20270215', '20270326', '20270531', '20270618', '20270705',
           '20270906', '20271125', '20271224'},
}

# NYSE 조기폐장(13:00 ET)
NYSE_HALF_DAYS = {
    2024: {'20240703', '20241129', '20241224'},
    2025: {'20250703', '20251128', '20251224'},
    2026: {'20261127', '20261224'},          # 7/2 는 정상 거래일 (7/3 = 독립기념일 대체 휴장)
    2027: {'20271126'},                      # 7/3=토, 12/24=성탄절 대체 휴장
    2028: {'20280703', '20281124'},          # 12/24=일
}


@pytest.mark.parametrize('year', sorted(NYSE_HOLIDAYS))
def test_us_holidays_known_years(year):
    assert set(mc.get_us_holidays(year)) == NYSE_HOLIDAYS[year]


@pytest.mark.parametrize('year', sorted(NYSE_HALF_DAYS))
def test_us_half_days_known_years(year):
    assert set(mc.get_us_half_days(year)) == NYSE_HALF_DAYS[year]


def test_us_2026_july_and_2027_christmas_edge_cases():
    # 2026-07-03: 독립기념일 대체 '휴장' (반일 아님), 2026-07-02: 정상 거래(반일 아님 — 기존 하드코딩 버그 수정)
    assert mc.is_us_trading_day('20260703') is False
    assert mc.is_us_half_day('20260703') is False
    assert mc.is_us_trading_day('20260702') is True
    assert mc.is_us_half_day('20260702') is False
    # 2026-11-27 / 12-24 반일
    assert mc.is_us_half_day('2026-11-27') is True
    assert mc.is_us_half_day('2026-12-24') is True
    # 2027-12-24: 성탄절(토) 대체 휴장 → 반일 아님
    assert mc.is_us_trading_day('2027-12-24') is False
    assert mc.is_us_half_day('2027-12-24') is False
    assert mc.is_us_half_day('2027-11-26') is True


def test_us_backward_compat_constants():
    from src.utils.market_calendar import US_HOLIDAYS_2026, US_HALF_DAYS_2026
    assert US_HOLIDAYS_2026 == NYSE_HOLIDAYS[2026]
    assert US_HALF_DAYS_2026 == NYSE_HALF_DAYS[2026]
    assert '20260702' not in US_HALF_DAYS_2026


def test_us_trading_day_is_year_agnostic():
    assert mc.is_us_trading_day('20270118') is False   # MLK 2027
    assert mc.is_us_trading_day('2027-03-26') is False  # Good Friday 2027
    assert mc.is_us_trading_day('20280704') is False
    assert mc.is_us_trading_day('20300419') is False   # Good Friday 2030
    assert mc.is_us_trading_day('20270706') is True
    assert mc.is_us_trading_day('20270102') is False   # 토요일
    assert mc.is_us_holiday('20271125') is True
    assert mc.is_us_holiday('20271126') is False


def test_us_session_state_closed_on_2027_holiday():
    # 2027-01-18(월, MLK) 23:00 KST → 미국 거래일 = 1/18 → 휴장
    assert mc.get_us_session_state(datetime(2027, 1, 18, 23, 0)) == (False, 'closed')


def test_full_market_status_uses_computed_calendar():
    st = mc.get_full_market_status()
    for k in ('us_trading_date', 'us_open', 'us_holiday', 'us_half_day', 'kr_open'):
        assert k in st
    us_ds = st['us_trading_date'].replace('-', '')
    assert st['us_holiday'] == (us_ds in mc.get_us_holidays(int(us_ds[:4])))


@pytest.mark.parametrize('year,expected', [
    (2011, date(2011, 4, 24)), (2024, date(2024, 3, 31)), (2025, date(2025, 4, 20)),
    (2026, date(2026, 4, 5)), (2027, date(2027, 3, 28)), (2038, date(2038, 4, 25)),
    (2285, date(2285, 3, 22)),
])
def test_easter_computus(year, expected):
    assert mc._easter_sunday(year) == expected


@pytest.mark.parametrize('year', range(2024, 2031))
def test_nyse_rule_fallback_equals_holidays_lib(year):
    holidays = pytest.importorskip('holidays')
    lib = {d.strftime('%Y%m%d') for d in holidays.NYSE(years=[year - 1, year, year + 1]) if d.year == year}
    assert set(mc._nyse_rule_holidays(year)) == lib


def test_nyse_new_year_saturday_rule():
    # 2022-01-01(토): NYSE 는 2021-12-31(금)을 휴장하지 않음, 2028-01-01(토)도 동일
    assert '20211231' not in mc._nyse_rule_holidays(2021)
    assert not any(d.startswith('20280101') for d in mc._nyse_rule_holidays(2028))
    # 2023-01-01(일) → 1/2(월) 대체
    assert '20230102' in mc._nyse_rule_holidays(2023)


def test_us_calendar_falls_back_when_holidays_missing(monkeypatch, fresh_us_cache):
    monkeypatch.setitem(sys.modules, 'holidays', None)  # import holidays → ImportError
    assert set(mc.get_us_holidays(2027)) == NYSE_HOLIDAYS[2027]
    assert set(mc.get_us_half_days(2026)) == NYSE_HALF_DAYS[2026]
    assert mc.is_us_trading_day('20250109') is False  # 특별 휴장(Carter)도 재현


# ══════════════════════════════════════════════════════════════
# KRX calendar
# ══════════════════════════════════════════════════════════════

# 2027 KRX 휴장 평일 (holidays 0.103 기준 + KRX 연말 휴장). 로컬 0.90 에서도 동일해야 함.
KRX_2027_WEEKDAY_CLOSURES = {
    '20270101', '20270208', '20270209', '20270301', '20270503', '20270505', '20270513',
    '20270719', '20270816', '20270914', '20270915', '20270916', '20271004', '20271011',
    '20271227', '20271231',
}
KRX_2026_WEEKDAY_CLOSURES = {
    '20260101', '20260216', '20260217', '20260218', '20260302', '20260501', '20260505',
    '20260525', '20260603', '20260717', '20260817', '20260924', '20260925', '20261005',
    '20261009', '20261225', '20261231',
}


@pytest.fixture
def cal():
    return MarketCalendar()


def _weekday_closures(c: MarketCalendar, year: int):
    return _weekday_set(c._closures_for_year(year))


def test_krx_2026_known_dates(cal):
    for d in ('2026-09-24', '2026-09-25', '2026-12-31', '2026-05-01', '2026-06-03', '2026-07-17'):
        assert cal.is_trading_day(d) is False, d
    assert cal.is_trading_day('2026-09-26') is False  # 토요일(추석 다음날)
    assert cal.is_trading_day('2026-12-30') is True
    assert cal.is_trading_day('2026-09-28') is True
    assert _weekday_closures(cal, 2026) == KRX_2026_WEEKDAY_CLOSURES


def test_krx_2027_works_without_code_edit(cal):
    # 설날(2/6~2/8) + 대체(2/9), 노동절(토)→5/3, 제헌절(토)→7/19, 연말 12/31
    for d in ('2027-02-08', '2027-02-09', '2027-05-03', '2027-07-19', '2027-09-15',
              '2027-10-11', '2027-12-27', '2027-12-31'):
        assert cal.is_trading_day(d) is False, d
    for d in ('2027-02-10', '2027-05-04', '2027-07-20', '2027-12-30', '2027-01-04'):
        assert cal.is_trading_day(d) is True, d
    assert _weekday_closures(cal, 2027) == KRX_2027_WEEKDAY_CLOSURES


@pytest.mark.parametrize('year,closed,open_', [
    (2022, '20221230', '20221229'),   # 12/31=토 → 12/30(금) 휴장
    (2023, '20231229', '20231228'),   # 12/31=일 → 12/29(금) 휴장 (KRX 공지와 일치)
    (2024, '20241231', '20241230'),
    (2028, '20281229', '20281228'),   # 12/31=일
])
def test_krx_year_end_closure_is_last_business_day(cal, year, closed, open_):
    assert cal.is_trading_day(closed) is False
    assert cal.is_trading_day(open_) is True
    assert '연말' in (cal.get_holiday_name(closed) or '')


def test_krx_year_end_not_pushed_back_by_static_entry(cal):
    # 레거시 _KRX_ONLY_HOLIDAYS 의 20261231 이 연말 휴장을 12/30 으로 밀어내면 안 됨
    assert '20261231' in MarketCalendar._KRX_ONLY_HOLIDAYS
    assert cal.is_trading_day('20261230') is True


def test_krx_may_first_every_year(cal):
    for y in (2024, 2025, 2026, 2029):
        d = date(y, 5, 1)
        if d.weekday() < 5:
            assert cal.is_trading_day(d.strftime('%Y%m%d')) is False, y


def test_krx_navigation_across_holidays(cal):
    assert cal.get_previous_trading_day('20270104') == '20261230'
    assert cal.get_next_trading_day('20260923') == '20260928'
    assert cal.trading_days_since('20260923', reference=datetime(2026, 9, 28, 10, 0)) == 1


def test_krx_market_status_year_end(cal):
    st = cal.get_market_status(datetime(2026, 12, 31, 10, 0))
    assert st['status'] == 'closed' and st['is_holiday'] is True
    assert '연말' in st['reason']
    st2 = cal.get_market_status(datetime(2027, 2, 9, 10, 0))
    assert st2['status'] == 'closed'


def test_krx_all_holidays_backward_compat(cal):
    ah = cal.all_holidays
    assert isinstance(ah, set)
    assert {'20261231', '20260924'} <= ah


def test_krx_fallback_without_holidays_lib_matches(monkeypatch, cal):
    lib_2026, lib_2027 = _weekday_closures(cal, 2026), _weekday_closures(cal, 2027)
    monkeypatch.setitem(sys.modules, 'holidays', None)
    nolib = MarketCalendar()
    assert nolib._holidays_mod is None
    assert _weekday_closures(nolib, 2026) == lib_2026
    assert _weekday_closures(nolib, 2027) == lib_2027


def test_krx_no_lib_uncovered_year_is_loud_and_keeps_solar(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, 'holidays', None)
    nolib = MarketCalendar()
    with caplog.at_level(logging.CRITICAL, logger='src.utils.market_calendar'):
        assert nolib.is_trading_day('20300101') is False
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)
    assert nolib.is_trading_day('20301231') is False  # 연말 규칙은 라이브러리 없이도 적용


def test_krx_statutory_overlay_does_not_double_shift(monkeypatch):
    # 라이브러리(신버전)가 노동절 대체휴일을 이미 제공하면 보강 로직이 5/4 로 추가 이동시키지 않아야 함
    fake = types.SimpleNamespace(
        SouthKorea=lambda years=None: {date(2027, 5, 1): '노동절', date(2027, 5, 3): '노동절 대체 휴일'},
    )
    monkeypatch.setitem(sys.modules, 'holidays', fake)
    c = MarketCalendar()
    assert c.is_trading_day('20270503') is False
    assert c.is_trading_day('20270504') is True
    assert c.is_trading_day('20270719') is False  # 라이브러리에 없는 제헌절은 보강(토→월)


# ══════════════════════════════════════════════════════════════
# systemd installer (render-only; sudo/systemctl 절대 미호출)
# ══════════════════════════════════════════════════════════════

INSTALLER = ROOT / 'scripts' / 'install_systemd_services.sh'
DROPIN_SCRIPT = ROOT / 'scripts' / 'ops' / 'install_us_regular_dst_dropin.sh'
TRADING_TIMERS = {'meridian_morning', 'meridian_premarket_calibration', 'meridian_closing',
                  'meridian_aftermarket', 'meridian_us_premarket', 'meridian_us_regular'}
DATA_TIMERS = {'meridian_night_futures', 'meridian_token_refresh', 'meridian_collect', 'meridian_evening',
               'meridian_weekly_retrain', 'meridian_weekly_validate', 'meridian_automated_audit',
               'meridian_frontier_lab'}


def _render(tmp_path: Path):
    units = tmp_path / 'units'
    fakebin = tmp_path / 'bin'
    fakebin.mkdir(exist_ok=True)
    marker = tmp_path / 'forbidden_called'
    for name in ('sudo', 'systemctl'):
        p = fakebin / name
        p.write_text(f'#!/bin/bash\necho "$0 $@" >> "{marker}"\nexit 99\n')
        p.chmod(0o755)
    env = dict(os.environ, MERIDIAN_SYSTEMD_RENDER_ONLY='1', MERIDIAN_SYSTEMD_DIR=str(units),
               PATH=f"{fakebin}:{os.environ.get('PATH', '')}")
    res = subprocess.run(['bash', str(INSTALLER)], env=env, capture_output=True, text=True, timeout=60)
    return units, res, marker


def _timer_lines(path: Path, key: str):
    return [ln.split('=', 1)[1].strip() for ln in path.read_text().splitlines() if ln.startswith(key + '=')]


def test_installer_scripts_syntax():
    for s in (INSTALLER, DROPIN_SCRIPT):
        r = subprocess.run(['bash', '-n', str(s)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_installer_render_persistent_policy_and_no_duplicates(tmp_path):
    units, res, marker = _render(tmp_path)
    assert res.returncode == 0, res.stderr
    assert not marker.exists(), marker.read_text() if marker.exists() else ''
    timers = {p.stem: p for p in units.glob('*.timer')}
    assert TRADING_TIMERS | DATA_TIMERS == set(timers)
    for name, p in timers.items():
        cal = _timer_lines(p, 'OnCalendar')
        pers = _timer_lines(p, 'Persistent')
        assert cal and len(cal) == len(set(cal)), (name, cal)
        assert len(pers) == 1, (name, pers)
        expected = 'false' if name in TRADING_TIMERS else 'true'
        assert pers[0] == expected, (name, pers)


def test_installer_us_regular_has_both_dst_triggers(tmp_path):
    units, res, _ = _render(tmp_path)
    assert res.returncode == 0, res.stderr
    cal = _timer_lines(units / 'meridian_us_regular.timer', 'OnCalendar')
    assert cal == ['*-*-* 22:30:10 Asia/Seoul', '*-*-* 23:25:00 Asia/Seoul']


def test_installer_is_idempotent_and_removes_legacy_dropin(tmp_path):
    dropin = tmp_path / 'units' / 'meridian_us_regular.timer.d' / '10-dst-winter.conf'
    dropin.parent.mkdir(parents=True)
    dropin.write_text('[Timer]\nOnCalendar=*-*-* 23:25:00 Asia/Seoul\nPersistent=false\n')
    _, res1, _ = _render(tmp_path)
    assert res1.returncode == 0, res1.stderr
    assert not dropin.exists()
    assert 'changed=1' in res1.stdout
    _, res2, marker = _render(tmp_path)
    assert res2.returncode == 0, res2.stderr
    assert 'changed=0' in res2.stdout and 'updated:' not in res2.stdout
    assert not marker.exists()


def test_installer_never_restarts_active_timers():
    code = [ln for ln in INSTALLER.read_text().splitlines() if not ln.lstrip().startswith('#')]
    body = '\n'.join(code)
    assert 'systemctl restart' not in body
    assert re.search(r'is-active --quiet "\$t\.timer"', body)
    # daemon-reload 는 enable/start 루프보다 먼저
    assert body.index('daemon-reload') < body.index('systemctl enable')


def test_dropin_script_is_guarded_and_does_not_restart():
    code = '\n'.join(ln for ln in DROPIN_SCRIPT.read_text().splitlines() if not ln.lstrip().startswith('#'))
    assert 'systemctl restart' not in code
    assert '23:25:00' in code and 'nothing to do' in code
