"""
Wave 2 Track F — Ticker master (KRX/KIS listing master + US exchange masters).

Builds ``results/ticker_master.json`` from the KIS public master files and exposes a lookup API:

    from src.data_collection.ticker_master import get_ticker_master
    tm = get_ticker_master()
    tm.get('252670')          # TickerInfo(code, name, market, is_etf, leverage=-2.0, underlying='KOSPI200', ...)
    tm.is_leveraged('TQQQ')   # True  (|leverage| > 1)
    tm.is_inverse('114800')   # True  (leverage < 0)
    tm.leverage('005930')     # 1.0
    tm.name('122630')         # 'KODEX 레버리지'

Sources (KIS open-trading-api ``stocks_info`` layout):
  * kospi_code.mst / kosdaq_code.mst (cp949, fixed width). Per line (bytes, CR/LF stripped):
      [0:9] short code, [9:21] ISIN, [21:-W] name (40 bytes), [-W:] part2 with W = 227 (KOSPI) / 221 (KOSDAQ).
      part2 offsets (computed from the KIS field_specs): group code [0:2] (ST stock, EF ETF, EN ETN, BC, RT, ...),
      halted / liquidation-trading / administrative flags, listing date.
  * {nas,nys,ams}mst.cod (cp949, TAB separated, 24 columns): symbol=4, korean name=6, english name=7,
      security type=8 (1 index, 2 stock, 3 ETP, 4 warrant), ETP kind=22 (001 ETF, 002 ETN, ...).

Leverage is parsed from names ('레버리지' → 2, '인버스' → -1, '인버스2X' → -2, 'PROSHARES QQQ -3X' → -3,
'ULTRAPRO SHORT' → -3, 'BULL 3X' → 3 ...) and overridden by STRUCTURAL_ETP (prospectus facts, L0).

Degraded mode: if the cache is missing, unreadable, or older than ``ticker_master.ttl_hours`` the lookup
falls back to STRUCTURAL_ETP + config.universe (+ src.utils.ticker_canonical for legacy codes) and
``status()['degraded']`` is True. A stale (but readable) cache is still consulted — leverage factors of a
listed ETP never change — but stays flagged degraded.

Network is only used by :func:`download_and_build` (CLI). Never called from trading paths or tests.
"""
from __future__ import annotations

import io
import json
import logging
import re
import threading
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_PATH = _PROJECT_ROOT / 'results' / 'ticker_master.json'
KIS_MASTER_BASE = 'https://new.real.download.dws.co.kr/common/master/'
KR_MASTER_FILES = {'KOSPI': 'kospi_code.mst.zip', 'KOSDAQ': 'kosdaq_code.mst.zip'}
US_MASTER_FILES = {'NAS': 'nasmst.cod.zip', 'NYS': 'nysmst.cod.zip', 'AMS': 'amsmst.cod.zip'}

# part2 widths (bytes) and offsets of the fields we use — derived from KIS field_specs.
_KR_LAYOUT = {
    'KOSPI': {'part2': 227, 'halted': 60, 'liquidation': 61, 'admin': 62, 'listed_date': (105, 113)},
    'KOSDAQ': {'part2': 221, 'halted': 55, 'liquidation': 56, 'admin': 57, 'listed_date': (100, 108)},
}
_KR_GROUP_MARKET = {'EF': 'ETF', 'EN': 'ETN'}

# ─────────────────────────────────────────────────────────────
# L0 structural facts — the ONLY place leveraged/inverse ETP literals may live.
# (prospectus leverage factor, name, underlying). Used to override name parsing and as
# the degraded-mode fallback when the master cache is unavailable.
# ─────────────────────────────────────────────────────────────
STRUCTURAL_ETP: Dict[str, tuple] = {  # hardcode-ok: L0 prospectus facts (Wave2 TrackF SSOT)
    # KR (KRX)
    '122630': (2.0, 'KODEX 레버리지', 'KOSPI200'),
    '233740': (2.0, 'KODEX 코스닥150레버리지', 'KOSDAQ150'),
    '114800': (-1.0, 'KODEX 인버스', 'KOSPI200'),
    '252670': (-2.0, 'KODEX 200선물인버스2X', 'KOSPI200'),
    '252710': (-2.0, 'TIGER 200선물인버스2X', 'KOSPI200'),
    '251340': (-1.0, 'KODEX 코스닥150선물인버스', 'KOSDAQ150'),
    '453010': (2.0, 'TIGER 미국나스닥100레버리지', 'NASDAQ100'),
    '0193L0': (-2.0, 'PLUS 삼성전자선물단일종목인버스2X', '005930'),
    '0197X0': (-2.0, 'SOL SK하이닉스선물단일종목인버스2X', '000660'),
    # US
    'TQQQ': (3.0, 'PROSHARES ULTRAPRO QQQ', 'NASDAQ100'),
    'SQQQ': (-3.0, 'PROSHARES ULTRAPRO SHORT QQQ', 'NASDAQ100'),
    'QLD': (2.0, 'PROSHARES ULTRA QQQ', 'NASDAQ100'),
    'QID': (-2.0, 'PROSHARES ULTRASHORT QQQ', 'NASDAQ100'),
    'PSQ': (-1.0, 'PROSHARES SHORT QQQ', 'NASDAQ100'),
    'UPRO': (3.0, 'PROSHARES ULTRAPRO S&P500', 'SP500'),
    'SPXU': (-3.0, 'PROSHARES ULTRAPRO SHORT S&P500', 'SP500'),
    'SPXS': (-3.0, 'DIREXION DAILY S&P 500 BEAR 3X', 'SP500'),
    'SPXL': (3.0, 'DIREXION DAILY S&P 500 BULL 3X', 'SP500'),
    'SSO': (2.0, 'PROSHARES ULTRA S&P500', 'SP500'),
    'SDS': (-2.0, 'PROSHARES ULTRASHORT S&P500', 'SP500'),
    'SH': (-1.0, 'PROSHARES SHORT S&P500', 'SP500'),
    'SOXL': (3.0, 'DIREXION DAILY SEMICONDUCTOR BULL 3X', 'SOX'),
    'SOXS': (-3.0, 'DIREXION DAILY SEMICONDUCTOR BEAR 3X', 'SOX'),
    'TECL': (3.0, 'DIREXION DAILY TECHNOLOGY BULL 3X', 'XLK'),
    'TNA': (3.0, 'DIREXION DAILY SMALL CAP BULL 3X', 'R2000'),
    'TZA': (-3.0, 'DIREXION DAILY SMALL CAP BEAR 3X', 'R2000'),
}

# Pre-Wave-2 call-site literal sets, centralized verbatim (behavior-preserving fallbacks).
# execution_engine._allow_overnight: overnight hold only when regime-aligned.
OVERNIGHT_BULL_ETPS = frozenset({'233740', '122630', 'TQQQ', 'SOXL', 'UPRO', 'QLD', 'SSO'})  # hardcode-ok: legacy fallback set
OVERNIGHT_BEAR_ETPS = frozenset({'252670', '114800', 'SQQQ', 'SOXS', 'SPXS', 'QID', 'SDS'})  # hardcode-ok: legacy fallback set
# realtime_exit_monitor.AlertZone: widened SL + time-filtered stop. NOT master-extended (would loosen risk).
EXIT_GROUP1_ETPS = frozenset({'252670', '233740', 'TQQQ', 'SOXL', 'SQQQ'})  # hardcode-ok: legacy fallback set


@dataclass(frozen=True)
class TickerInfo:
    code: str
    name: str
    market: str                 # KOSPI | KOSDAQ | ETF | ETN | US
    is_etf: bool
    leverage: float             # 1.0 plain, 2.0 / 3.0 leveraged, -1.0 / -2.0 / -3.0 inverse
    underlying: Optional[str]
    listed: Optional[bool]      # None = unknown (degraded fallback)
    as_of: Optional[str]
    exchange: str = ''          # KRX | NAS | NYS | AMS
    is_etn: bool = False
    halted: bool = False
    name_en: str = ''
    listed_date: str = ''       # YYYYMMDD (KRX only)
    source: str = 'master'      # master | master_stale | structural | universe


# ─────────────────────────────────────────────────────────────
# Name parsing
# ─────────────────────────────────────────────────────────────
_KR_INV_N = re.compile(r'인버스(\d(?:\.\d)?)X?')
_KR_NX = re.compile(r'(?<![A-Z0-9.])(-?)(\d(?:\.\d)?)X(?![A-Z])')
_US_NX = re.compile(r'(?<![A-Z0-9.])(-?)(\d(?:\.\d+)?)X(?![A-Z])')
_US_BOND_SHORT = re.compile(
    r'ULTRA[- ]?SHORT[- ]?(TERM|DURATION|INCOME|BOND|BD|MUNI|TREASURY|FIXED|GOV|INVESTMENT|TAX|CREDIT|CORP)'
    r'|SHORT[- ](TERM|DURATION|MATURITY|TREASURY|BOND|CORP|MUNI|HIGH|INTERM|DATED|VOL|SHRT|HORIZON)'
    r'|SHORT[- ]?T-BILL|LONG[/ -]SHORT|ULTRA[- ]SMALL')


def parse_kr_leverage(name: str, is_etp: bool = True) -> float:
    """Leverage factor from a KRX ETF/ETN name (spaces ignored; 40-byte names may truncate '2X' → '2')."""
    if not is_etp or not name:
        return 1.0
    n = name.replace(' ', '').upper()
    if '인버스' in n:
        m = _KR_INV_N.search(n)
        return -float(m.group(1)) if m else -1.0
    m = _KR_NX.search(n)
    if m and m.group(1) == '-' and 0.0 < float(m.group(2)) <= 5.0:
        return -float(m.group(2))
    if '레버리지' in n:
        return float(m.group(2)) if m and float(m.group(2)) > 1 else 2.0
    if m and 1.0 < float(m.group(2)) <= 5.0:
        return float(m.group(2))
    return 1.0


def parse_us_leverage(kor_name: str, eng_name: str, is_etp: bool = True) -> float:
    """Leverage factor from KIS US master names (Korean abbreviation e.g. 'PROSHARES QQQ -3X' + English name)."""
    if not is_etp:
        return 1.0
    eng = (eng_name or '').upper()
    for s in (kor_name or '', eng):
        u = s.upper()
        m = _US_NX.search(u)
        if m and 0.0 < float(m.group(2)) <= 5.0:
            neg = m.group(1) == '-' or bool(re.search(r'\b(SHORT|BEAR|INVERSE)\b', _US_BOND_SHORT.sub('', u)))
            return -float(m.group(2)) if neg else float(m.group(2))
    clean = _US_BOND_SHORT.sub('', eng)
    if 'PROSHARES' in clean:  # ULTRA / ULTRAPRO / ULTRASHORT are ProShares product-line names
        if 'ULTRAPRO SHORT' in clean:
            return -3.0
        if 'ULTRAPRO' in clean:
            return 3.0
        if re.search(r'\bULTRA ?SHORT\b', clean):
            return -2.0
        if re.search(r'\bULTRA\b', clean):
            return 2.0
    if re.search(r'\b(SHORT|INVERSE|BEAR)\b', clean) or '인버스' in (kor_name or ''):
        return -1.0
    return 1.0


_KR_UNDERLYING = [
    (re.compile(r'코스닥150'), 'KOSDAQ150'),
    (re.compile(r'나스닥100'), 'NASDAQ100'),
    (re.compile(r'S&P500'), 'SP500'),
    (re.compile(r'삼성전자.*단일종목'), '005930'),
    (re.compile(r'SK하이닉스.*단일종목'), '000660'),
    (re.compile(r'(?<!\d)200(선물|레버리지|인버스|$)'), 'KOSPI200'),
    (re.compile(r'미국달러선물'), 'USDKRW'),
    (re.compile(r'WTI|원유'), 'WTI'),
    (re.compile(r'골드|금선물'), 'GOLD'),
]
_US_UNDERLYING = [
    (re.compile(r'\bQQQ\b|NASDAQ'), 'NASDAQ100'),
    (re.compile(r'S&P ?500|\bSPX\b'), 'SP500'),
    (re.compile(r'SEMICONDUCTOR|\bSOX\b'), 'SOX'),
    (re.compile(r'\bDOW\b'), 'DJIA'),
    (re.compile(r'RUSSELL ?2000|SMALL ?CAP'), 'R2000'),
]


def derive_underlying(name: str, market: str) -> Optional[str]:
    if not name:
        return None
    if market == 'US':
        u = name.upper()
        for rx, tag in _US_UNDERLYING:
            if rx.search(u):
                return tag
        return None
    n = name.replace(' ', '')
    for rx, tag in _KR_UNDERLYING:
        if rx.search(n):
            return tag
    return None


# ─────────────────────────────────────────────────────────────
# Parsers (bytes in → TickerInfo list out). Pure; used by tests with fixture bytes.
# ─────────────────────────────────────────────────────────────
def _unzip_first(raw: bytes) -> bytes:
    if raw[:2] == b'PK':
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            return z.read(z.namelist()[0])
    return raw


def parse_kr_master(raw: bytes, board: str, as_of: Optional[str] = None) -> List[TickerInfo]:
    """Parse kospi_code.mst / kosdaq_code.mst (raw or zipped bytes)."""
    lay = _KR_LAYOUT[board]
    w = lay['part2']
    out: List[TickerInfo] = []
    for line in _unzip_first(raw).split(b'\n'):
        line = line.rstrip(b'\r')
        if len(line) <= w + 21:
            continue
        p2 = line[-w:].decode('ascii', errors='replace')
        code = line[0:9].decode('ascii', errors='replace').strip()
        name = line[21:-w].decode('cp949', errors='replace').strip()
        if not code:
            continue
        group = p2[0:2]
        market = _KR_GROUP_MARKET.get(group, board)
        is_etp = group in ('EF', 'EN')
        lev = parse_kr_leverage(name, is_etp)
        if code in STRUCTURAL_ETP:
            lev = STRUCTURAL_ETP[code][0]
        out.append(TickerInfo(
            code=code, name=name, market=market, is_etf=(group == 'EF'), leverage=lev,
            underlying=(STRUCTURAL_ETP[code][2] if code in STRUCTURAL_ETP else derive_underlying(name, market)) if is_etp else None,
            listed=True, as_of=as_of, exchange='KRX', is_etn=(group == 'EN'),
            halted=(p2[lay['halted']] == 'Y'),
            listed_date=p2[lay['listed_date'][0]:lay['listed_date'][1]].strip(), source='master'))
    return out


def parse_us_master(raw: bytes, exchange: str, as_of: Optional[str] = None) -> List[TickerInfo]:
    """Parse {nas,nys,ams}mst.cod (raw or zipped bytes)."""
    out: List[TickerInfo] = []
    for line in _unzip_first(raw).split(b'\n'):
        line = line.rstrip(b'\r')
        if not line.strip():
            continue
        cols = line.decode('cp949', errors='replace').split('\t')
        if len(cols) < 23:
            continue
        sym = cols[4].strip().upper()
        if not sym:
            continue
        sec_type = cols[8].strip()
        kind = cols[22].strip()
        is_etp = sec_type == '3'
        lev = parse_us_leverage(cols[6], cols[7], is_etp)
        if sym in STRUCTURAL_ETP:
            lev = STRUCTURAL_ETP[sym][0]
        out.append(TickerInfo(
            code=sym, name=cols[6].strip(), market='US', is_etf=is_etp and kind != '002', leverage=lev,
            underlying=(STRUCTURAL_ETP[sym][2] if sym in STRUCTURAL_ETP else derive_underlying(cols[7], 'US')) if is_etp else None,
            listed=True, as_of=as_of, exchange=exchange, is_etn=(kind == '002'), name_en=cols[7].strip(),
            source='master'))
    return out


def build_master(kr: Dict[str, bytes], us: Dict[str, bytes], as_of: Optional[str] = None) -> Dict:
    as_of = as_of or datetime.now(timezone.utc).isoformat()
    tickers: Dict[str, Dict] = {}
    counts: Dict[str, int] = {}
    for board, raw in kr.items():
        rows = parse_kr_master(raw, board, as_of)
        counts[board] = len(rows)
        for r in rows:
            tickers[r.code] = asdict(r)
    for exch, raw in us.items():
        rows = parse_us_master(raw, exch, as_of)
        counts[exch] = len(rows)
        for r in rows:
            tickers.setdefault(r.code, asdict(r))  # first exchange wins on duplicate symbols
    return {'version': 1, 'as_of': as_of, 'counts': counts, 'tickers': tickers}


def download_and_build(out_path: Path = CACHE_PATH, timeout: float = 60.0,
                       boards: Iterable[str] = ('KOSPI', 'KOSDAQ'),
                       exchanges: Iterable[str] = ('NAS', 'NYS', 'AMS')) -> Dict:
    """Download the KIS master files and atomically write ``out_path``. Network required (CLI only)."""
    import os
    import tempfile
    import urllib.request

    def _fetch(fname: str) -> bytes:
        with urllib.request.urlopen(KIS_MASTER_BASE + fname, timeout=timeout) as r:  # noqa: S310 (fixed https host)
            return r.read()

    kr = {b: _fetch(KR_MASTER_FILES[b]) for b in boards}
    us = {e: _fetch(US_MASTER_FILES[e]) for e in exchanges}
    data = build_master(kr, us)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(out_path.parent), prefix='.ticker_master.', suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    os.chmod(tmp, 0o644)
    os.replace(tmp, out_path)
    return data


# ─────────────────────────────────────────────────────────────
# Lookup
# ─────────────────────────────────────────────────────────────
def _fallback_table() -> Dict[str, TickerInfo]:
    tbl: Dict[str, TickerInfo] = {}
    try:
        from config.universe import Universe
        for attr in dir(Universe):
            group = getattr(Universe, attr)
            if not (attr.isupper() and isinstance(group, dict)):
                continue
            for info in group.values():
                code = getattr(info, 'ticker', None)
                if not code:
                    continue
                us = getattr(info, 'market', 'KR') == 'US'
                name = getattr(info, 'name', '')
                lev = parse_us_leverage(name, name) if us else parse_kr_leverage(name)
                tbl[code] = TickerInfo(code=code, name=name, market='US' if us else 'ETF', is_etf=True,
                                       leverage=lev, underlying=derive_underlying(name, 'US' if us else 'ETF'),
                                       listed=None, as_of=None, exchange='' if us else 'KRX', source='universe')
    except Exception as e:  # universe import failure must not break lookups
        logger.debug(f"  [TickerMaster] universe fallback unavailable: {e}")
    for code, (lev, name, und) in STRUCTURAL_ETP.items():
        us = not code[0].isdigit()
        tbl[code] = TickerInfo(code=code, name=name, market='US' if us else 'ETF', is_etf=True, leverage=lev,
                               underlying=und, listed=None, as_of=None, exchange='' if us else 'KRX',
                               source='structural')
    return tbl


class TickerMaster:
    def __init__(self, cache_path: Optional[Path] = None, ttl_hours: Optional[float] = None, clock=None):
        self._path = Path(cache_path) if cache_path else CACHE_PATH
        self._ttl_hours = ttl_hours
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._loaded = False
        self._tickers: Dict[str, Dict] = {}
        self._as_of: Optional[datetime] = None
        self._load_error: Optional[str] = None
        self._fallback = _fallback_table()

    def _ttl(self) -> float:
        if self._ttl_hours is not None:
            return float(self._ttl_hours)
        try:
            from src.params import pget
            return float(pget('ticker_master.ttl_hours', 168.0))
        except Exception:
            return 168.0

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            try:
                data = json.loads(self._path.read_text(encoding='utf-8'))
                self._tickers = data.get('tickers', {}) or {}
                ts = data.get('as_of')
                self._as_of = datetime.fromisoformat(ts) if ts else None
                if self._as_of is not None and self._as_of.tzinfo is None:
                    self._as_of = self._as_of.replace(tzinfo=timezone.utc)
            except FileNotFoundError:
                self._load_error = 'cache_missing'
            except Exception as e:
                self._load_error = f'cache_unreadable: {e}'
                self._tickers = {}
            self._loaded = True

    def reload(self) -> None:
        with self._lock:
            self._loaded = False
            self._tickers, self._as_of, self._load_error = {}, None, None
        self._ensure_loaded()

    def is_fresh(self) -> bool:
        self._ensure_loaded()
        if not self._tickers or self._as_of is None:
            return False
        age_h = (self._clock() - self._as_of).total_seconds() / 3600.0
        return age_h <= self._ttl()

    @property
    def degraded(self) -> bool:
        return not self.is_fresh()

    def status(self) -> Dict:
        self._ensure_loaded()
        return {'path': str(self._path), 'n_tickers': len(self._tickers),
                'as_of': self._as_of.isoformat() if self._as_of else None,
                'fresh': self.is_fresh(), 'degraded': self.degraded, 'error': self._load_error,
                'fallback_size': len(self._fallback)}

    @staticmethod
    def _norm(code) -> str:
        raw = str(code or '').strip().upper()
        if raw.isdigit() and len(raw) < 6:
            raw = raw.zfill(6)
        try:
            from src.utils.ticker_canonical import canonical_ticker
            return canonical_ticker(raw, context='ticker_master')
        except Exception:
            return raw

    def get(self, code) -> Optional[TickerInfo]:
        c = self._norm(code)
        if not c:
            return None
        self._ensure_loaded()
        rec = self._tickers.get(c)
        if rec is not None:
            fields = {k: rec.get(k) for k in TickerInfo.__dataclass_fields__ if k in rec}
            fields['source'] = 'master' if self.is_fresh() else 'master_stale'
            try:
                return TickerInfo(**fields)
            except TypeError:
                pass
        fb = self._fallback.get(c)
        if fb is not None and self.is_fresh() and c[0].isdigit():
            # fresh KRX master without this code → delisted (keep structural leverage fact)
            return TickerInfo(**{**asdict(fb), 'listed': False})
        return fb

    def leverage(self, code) -> Optional[float]:
        info = self.get(code)
        return None if info is None else float(info.leverage)

    def is_leveraged(self, code) -> bool:
        lev = self.leverage(code)
        return lev is not None and abs(lev) > 1.0

    def is_inverse(self, code) -> bool:
        lev = self.leverage(code)
        return lev is not None and lev < 0

    def name(self, code) -> Optional[str]:
        info = self.get(code)
        return info.name if info else None

    def master_leverage(self, code) -> Optional[float]:
        """Leverage from a FRESH master only (None when degraded) — for opt-in classification extensions."""
        if not self.is_fresh():
            return None
        self._ensure_loaded()
        rec = self._tickers.get(self._norm(code))
        return None if rec is None else float(rec.get('leverage', 1.0))


_TM: Optional[TickerMaster] = None
_TM_LOCK = threading.Lock()


def get_ticker_master() -> TickerMaster:
    global _TM
    if _TM is None:
        with _TM_LOCK:
            if _TM is None:
                _TM = TickerMaster()
    return _TM


def reset_ticker_master(tm: Optional[TickerMaster] = None) -> None:
    global _TM
    with _TM_LOCK:
        _TM = tm


def overnight_etp_direction(code) -> Optional[str]:
    """'bull' | 'bear' | None for execution_engine._allow_overnight.

    Exactly the pre-Wave-2 literal sets; additionally classifies other leveraged/inverse ETPs from a
    FRESH master only when ``ticker_master.extend_overnight_classification`` == 1 (default 0 → identical
    behavior). The extension can only restrict overnight holds (tighten).
    """
    c = str(code or '').upper()
    if c in OVERNIGHT_BULL_ETPS:
        return 'bull'
    if c in OVERNIGHT_BEAR_ETPS:
        return 'bear'
    try:
        from src.params import pget
        if int(pget('ticker_master.extend_overnight_classification', 0)) != 1:
            return None
        lev = get_ticker_master().master_leverage(c)
    except Exception:
        return None
    if lev is None:
        return None
    if lev >= 2.0:
        return 'bull'
    if lev < 0:
        return 'bear'
    return None


def _main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description='Build ticker master cache from KIS master files')
    ap.add_argument('--out', default=str(CACHE_PATH))
    ap.add_argument('--timeout', type=float, default=60.0)
    a = ap.parse_args(argv)
    data = download_and_build(Path(a.out), timeout=a.timeout)
    lev = sum(1 for t in data['tickers'].values() if abs(float(t['leverage'])) > 1)
    inv = sum(1 for t in data['tickers'].values() if float(t['leverage']) < 0)
    print(json.dumps({'out': a.out, 'counts': data['counts'], 'total': len(data['tickers']),
                      'leveraged': lev, 'inverse': inv}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(_main())
