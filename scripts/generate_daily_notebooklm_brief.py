#!/usr/bin/env python3
"""
Project Meridian — 09:00 KST Daily NotebookLM Macro & Market Brief Generator
============================================================================
매일 오전 09:00:00 KST (meridian_daily_brief.timer) 자동 실행:
  1) 08:45 Morning Pipeline + 08:50 Premarket Calibration이 반영된 SSOT 로드
  2) 한국·미국·중국·일본·유럽 5대 권역 Macro Key Index + 국제 상품·에너지 시장 +
     전일 미 증시 마감 복기 + 당일 한국 증시 09:00 전망 종합 Daily Brief (.md) 생성
  3) NotebookLM 스타일 2인 대담(남성 모더레이터 민준 & 여성 수석 전략가 서연) 대본 및
     고음질 한국어 뉴럴 오디오 파일 (.mp3) 생성
  4) Google Drive NotebookLM 전용 폴더 동기화 + 이메일(.mp3/.md 첨부) + 텔레그램(오디오/문서) 발송
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from src.reporting.notebooklm_daily_brief import (
    build_notebooklm_brief_markdown,
    build_two_host_dialogue_script,
    extract_daily_brief_snapshot,
    format_dialogue_transcript,
    sync_brief_to_google_drive_notebooklm,
    synthesize_two_host_audio_mp3,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("DailyNotebookLMBrief")


def _load_json_safe(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"JSON 로드 실패 ({path.name}): {e}")
        return {}


def generate_daily_brief(
    results_dir: Path | None = None,
    output_dir: Path | None = None,
    skip_audio: bool = False,
    skip_notify: bool = False,
    report_date: str | None = None,
) -> dict[str, Any]:
    res_dir = results_dir or (PROJECT_ROOT / "results")
    out_dir = output_dir or (res_dir / "daily_brief")
    out_dir.mkdir(parents=True, exist_ok=True)

    signal_cache = _load_json_safe(res_dir / "signal_cache.json")
    latest_signals = _load_json_safe(res_dir / "latest_signals.json")
    pipeline_state = _load_json_safe(res_dir / "pipeline_state.json")
    kis_portfolio = _load_json_safe(res_dir / "kis_portfolio.json")

    date_str = report_date or datetime.now().strftime("%Y-%m-%d")
    snap = extract_daily_brief_snapshot(
        signal_cache=signal_cache,
        latest_signals=latest_signals,
        pipeline_state=pipeline_state,
        kis_portfolio=kis_portfolio,
        report_date=date_str,
    )

    brief_md = build_notebooklm_brief_markdown(snap)
    dialogue = build_two_host_dialogue_script(snap)
    dialogue_txt = format_dialogue_transcript(dialogue, date_str=date_str)

    md_path = out_dir / f"Meridian_Daily_Brief_{date_str}.md"
    md_latest = out_dir / "Meridian_Daily_Brief_Latest.md"
    dlg_path = out_dir / f"Meridian_Daily_Brief_Dialogue_{date_str}.txt"
    dlg_latest = out_dir / "Meridian_Daily_Brief_Dialogue_Latest.txt"
    mp3_path = out_dir / f"Meridian_Daily_Brief_{date_str}.mp3"
    mp3_latest = out_dir / "Meridian_Daily_Brief_Latest.mp3"

    full_source_md = brief_md + "\n---\n\n" + dialogue_txt
    md_path.write_text(full_source_md, encoding="utf-8")
    shutil.copyfile(md_path, md_latest)
    dlg_path.write_text(dialogue_txt, encoding="utf-8")
    shutil.copyfile(dlg_path, dlg_latest)
    logger.info(f"✅ [Daily Brief Doc] 생성 완료: {md_path}")

    audio_ok = False
    if not skip_audio:
        try:
            synthesize_two_host_audio_mp3(dialogue, mp3_path)
            shutil.copyfile(mp3_path, mp3_latest)
            audio_ok = mp3_path.exists() and mp3_path.stat().st_size > 0
        except Exception as audio_err:
            logger.error(f"❌ [Audio Brief] MP3 합성 실패: {audio_err}", exc_info=True)

    drive_sync = {"synced": False}
    if not skip_notify:
        drive_sync = sync_brief_to_google_drive_notebooklm(full_source_md, report_date=date_str)

        # 1. Send Email with HTML body + MP3 & Markdown attachments
        try:
            from src.interface.email_notifier import MeridianEmail

            emailer = MeridianEmail()
            attachments = [str(md_path)]
            if audio_ok:
                attachments.insert(0, str(mp3_path))
            subj = (
                f"[Meridian Daily Brief] {date_str} 글로벌 5대권역 매크로 · 상품/에너지 · "
                f"미 증시 · 한국 증시 09:00 전망 (NotebookLM 2인 대담 오디오 첨부)"
            )
            emailer.send_html_report(
                markdown_content=full_source_md,
                subject=subj,
                attachments=attachments,
            )
        except Exception as em_err:
            logger.warning(f"  ⚠️ [Email] 데일리 브리프 이메일 전송 실패: {em_err}")

        # 2. Send Telegram Summary + Playable Audio + NotebookLM Markdown Document
        try:
            from src.interface.telegram_notifier import MeridianTelegram

            tg = MeridianTelegram()
            us = snap["us"]
            kr = snap["kr"]
            cm = snap["commodities"]
            rg = snap["regimes"]
            tg_summary = (
                f"🎙️ [Meridian 09:00 Daily Brief & Audio] ({date_str})\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"• 글로벌/KR 레짐: {rg['global'].upper()} / {rg['kr'].upper()} (OIS {rg['ois'] or 0:.1f}점)\n"
                f"• 미국 마감: S&P500 {us['sp500_chg_1d'] or 0:+.2f}% | NQ {us['nasdaq_chg_1d'] or 0:+.2f}% | SOX {us['sox_chg_1d'] or 0:+.2f}% | VIX {us['vix'] or 0:.2f}\n"
                f"• 금리/환율: 미10Y {us['us10y'] or 0:.2f}% | DXY {us['dxy'] or 0:.2f} | USD/KRW {kr['usdkrw'] or 0:,.1f}원\n"
                f"• 아시아/유럽: 닛케이 {snap['jp']['nikkei_chg_1d'] or 0:+.2f}% | 항셍 {snap['cn']['hangseng_chg_1d'] or 0:+.2f}% | EUR/USD {snap['eu']['eurusd'] or 0:.4f}\n"
                f"• 상품/에너지: WTI ${cm['wti'] or 0:.2f}({cm['wti_chg_1d'] or 0:+.2f}%) | 금 ${cm['gold'] or 0:,.1f} | 구리 ${cm['copper'] or 0:.2f}\n"
                f"• 한국장 선행: 야간선물 {kr['night_fut_chg_1d'] or 0:+.2f}% | EWY {kr['ewy_chg_1d'] or 0:+.2f}%\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🎧 아래 첨부된 2인 대담 오디오(.mp3)와 NotebookLM 소스 문서(.md)를 확인하세요."
            )
            tg._send(tg_summary)
            if audio_ok:
                tg.send_audio(
                    str(mp3_path),
                    title=f"Meridian Daily Brief ({date_str})",
                    performer="Project Meridian AI (민준 & 서연)",
                    caption=f"🎧 {date_str} 09:00 KST 글로벌 매크로 & 한국증시 2인 대담 오디오 브리프",
                )
            tg.send_document(
                str(md_path),
                caption=f"📄 {date_str} NotebookLM 연동용 Daily Brief 원문 (.md)",
            )
        except Exception as tg_err:
            logger.warning(f"  ⚠️ [Telegram] 데일리 브리프 텔레그램 전송 실패: {tg_err}")

    return {
        "date": date_str,
        "md_path": str(md_path),
        "dialogue_path": str(dlg_path),
        "mp3_path": str(mp3_path) if audio_ok else None,
        "audio_ok": audio_ok,
        "drive_sync": drive_sync,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Project Meridian 09:00 KST Daily NotebookLM Brief & Audio")
    parser.add_argument("--date", type=str, default=None, help="Target date YYYY-MM-DD")
    parser.add_argument("--skip-audio", action="store_true", help="Skip TTS MP3 synthesis")
    parser.add_argument("--skip-notify", action="store_true", help="Skip Email/Telegram/Drive delivery")
    args = parser.parse_args()

    res = generate_daily_brief(
        skip_audio=args.skip_audio,
        skip_notify=args.skip_notify,
        report_date=args.date,
    )
    logger.info(f"🎉 Daily Brief 완료: {json.dumps(res, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
