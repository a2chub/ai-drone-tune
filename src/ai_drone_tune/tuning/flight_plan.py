"""Flight plans and blackbox/FC settings for analysis flights and race recording.

``build_plan()`` returns a structured plan (JSON-friendly) that the pilot can
follow and an LLM can explain / adapt:

* ``purpose="tuning"``: a dedicated test flight that excites everything the
  analysis needs (throttle sweeps, snaps on each axis, punch-outs, chops).
  With ``focus`` (segment ids from a data-quality assessment) only the
  missing parts are planned.
* ``purpose="race"``: record real race / practice laps end-to-end at the best
  sample rate that fits the flash for the planned duration.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ..analysis.data_quality import DEFAULT_BYTES_PER_FRAME, recommend_sample_rate
from .config import FCConfig


@dataclass
class Segment:
    id: str
    title: str
    duration_s: float
    throttle: str
    sticks: str
    repeat: int
    why: str


SEGMENTS: dict[str, Segment] = {s.id: s for s in [
    Segment("mode_acro", "Acro モードの確認", 0, "-",
            "Angle/Horizon を OFF にし、Acro (レート) モード + Air Mode で飛ぶ。アーム前に OSD で確認",
            1, "セルフレベルが働くと PID の応答が正しく測れないため"),
    Segment("hover", "ホバリング", 10, "ホバー付近 (約 30〜45%)",
            "スティックはほぼ中立。高度を一定に保つ", 1,
            "基準となるノイズレベルと、ホバー時の振動を測るため"),
    Segment("throttle_sweep", "スロットルスイープ", 8, "最低 → 最大までゆっくり上げ、ゆっくり戻す (各 3〜4 秒)",
            "ロール/ピッチは中立のまま、まっすぐ上昇・下降", 3,
            "スロットルとノイズ周波数の関係から、モーターノイズとフレーム共振を見分けるため"),
    Segment("roll_snaps", "ロールのスナップ入力", 12, "ホバー〜中程度 (40〜60%)",
            "ロールスティックを素早くフル (または 70% 以上) に倒して 0.3〜0.5 秒保持し、中立に戻す。左右交互に",
            8, "ステップ応答 (オーバーシュート・遅れ) を測るため"),
    Segment("pitch_snaps", "ピッチのスナップ入力", 12, "ホバー〜中程度 (40〜60%)",
            "ピッチスティックを素早く大きく倒して 0.3〜0.5 秒保持し、戻す。前後交互に (フリップになっても可)",
            8, "ピッチ軸のステップ応答を測るため"),
    Segment("yaw_snaps", "ヨーのスナップ入力", 8, "ホバー付近",
            "ヨーを素早く大きく入れて 0.5 秒保持し、戻す。左右交互に", 6,
            "ヨー軸のステップ応答を測るため"),
    Segment("flips_rolls", "フリップ/ロール", 10, "中程度、回転中は 20〜30% まで絞る",
            "フリップとロールを各 3 回以上", 1, "大きな入力時の追従性・飽和を見るため"),
    Segment("punch_outs", "パンチアウト", 6, "1〜2 秒フルスロットル → 通常に戻す", "まっすぐ上昇", 3,
            "高スロットル時のノイズ・発振 (TPA の要否) とモーター飽和を見るため"),
    Segment("propwash_chops", "スロットルカット (プロップウォッシュ)", 10,
            "上昇後にスロットルを 0〜10% まで一気に絞り、落下中に再びスロットルを入れる",
            "スプリットS やダイブの引き起こしでも可", 4,
            "プロップウォッシュ時の揺れを測り、D や Dynamic Idle を判断するため"),
    Segment("cruise", "通常飛行", 20, "普段どおり", "普段の飛び方 (旋回・ダイブ・ゲートを想定した動き)", 1,
            "実際の飛行に近い状態のデータを得るため"),
    Segment("race_laps", "レース/練習走行", 120, "普段どおり", "レース・練習走行をアームからディスアームまで通しで記録", 1,
            "実戦でのノイズ・プロップウォッシュ・飽和を評価するため"),
]}

TUNING_ORDER = ["mode_acro", "hover", "throttle_sweep", "roll_snaps", "pitch_snaps", "yaw_snaps",
                "flips_rolls", "punch_outs", "propwash_chops", "cruise"]


def recommended_fc_settings(cfg: FCConfig | None, purpose: str, pid_hz: float | None = None,
                            flash_total: int | None = None, planned_s: float | None = None,
                            bytes_per_frame: float = DEFAULT_BYTES_PER_FRAME) -> list[dict]:
    """Blackbox / FC settings for the given purpose, compared to ``cfg`` when available."""
    cfg = cfg or FCConfig()
    out: list[dict] = []

    def want(name: str, value: str, reason: str, only_if_present: bool = True) -> None:
        cur = cfg.get(name)
        if only_if_present and cfg.values and cur is None:
            return
        if cur is not None and str(cur).upper() == value.upper():
            return
        out.append({"name": name, "current": cur, "recommended": value, "reason": reason})

    want("blackbox_device", "SPIFLASH" if cfg.get("blackbox_device", "SPIFLASH") != "SDCARD" else "SDCARD",
         "Blackbox の記録先を有効にする")
    want("blackbox_mode", "NORMAL", "アーム中のみ記録 (Flash を無駄にしない)")
    rate = recommend_sample_rate(pid_hz, purpose, bytes_per_frame, flash_total, planned_s)
    if rate:
        why = ("チューニング解析には 1.6〜4kHz の記録が必要 (約 2kHz を推奨)" if purpose == "tuning"
               else "予定時間を Flash に収めつつ、できるだけ高いレートで記録する")
        want("blackbox_sample_rate", rate, why, only_if_present=False)
    for field, reason in (("gyrounfilt", "フィルター前 Gyro: フィルター効果とノイズ源の判定に必須"),
                          ("setpoint", "setpoint: ステップ応答の計算に必須"),
                          ("pids", "PID 項: D-term ノイズ評価に必要"),
                          ("motors", "モーター出力: 飽和・離陸判定に必要"),
                          ("rpm", "eRPM: モーターノイズの判別に必要")):
        want(f"blackbox_disable_{field}", "OFF", reason)
    fw = cfg.firmware or ""
    if cfg.get("debug_mode") is not None:
        legacy = fw.startswith("4.2") or fw.startswith("4.1") or fw.startswith("4.0")
        want("debug_mode", "GYRO_SCALED" if legacy else "NONE",
             "BF4.2 以前はフィルター前 Gyro を debug で記録する" if legacy else "debug 記録は不要 (容量節約)")
    if purpose == "race":
        for field in ("acc", "attitude", "mag", "alt", "gps"):
            want(f"blackbox_disable_{field}", "ON", "解析に使わないフィールドを止めて記録時間を延ばす")
    if cfg.get("dshot_bidir") is not None and not cfg.is_on("dshot_bidir"):
        out.append({"name": "dshot_bidir", "current": cfg.get("dshot_bidir"), "recommended": "ON",
                    "reason": "RPM フィルターとモーター回転数の記録に必要 (ESC が BLHeli_32/Bluejay/AM32 の場合のみ)",
                    "advisory": True})
    if cfg.values and not (cfg.get("craft_name") or "").strip():
        out.append({"name": "craft_name", "current": "", "recommended": "<機体名>",
                    "reason": "ログのファイル名と履歴の管理に使う", "advisory": True})
    return out


def build_plan(purpose: str = "tuning", focus: list[str] | None = None, cfg: FCConfig | None = None,
               pid_hz: float | None = None, flash_total: int | None = None, flash_used: int | None = None,
               race_duration_s: float = 180.0, heats: int = 1,
               bytes_per_frame: float = DEFAULT_BYTES_PER_FRAME) -> dict:
    if purpose == "tuning":
        ids = [i for i in TUNING_ORDER if not focus or i in focus or i in ("mode_acro", "hover")]
        if focus and "hover" in ids and ids == ["mode_acro", "hover"]:
            ids = TUNING_ORDER
        segs = [SEGMENTS[i] for i in ids]
        airborne = sum(s.duration_s * (s.repeat if s.id not in ("roll_snaps", "pitch_snaps", "yaw_snaps") else 1)
                       for s in segs)
        planned = max(45.0, airborne + 15)
        plan = {
            "purpose": "tuning",
            "goal": "チューニング解析用のテスト飛行 (フィルター/PID/FF/TPA の判断材料を 1 パックで集める)",
            "prerequisites": [
                "満充電のバッテリー、損傷のないプロペラ (曲がり・欠けがあるとノイズが増える)",
                "風の弱い広い場所。安全な高度 (5〜15m) を確保",
                "飛行前に Blackbox を消去 (前回ログはダウンロード済みであること)",
                "OSD に Blackbox の残量が表示されていれば確認",
            ],
            "flight_mode": {"mode": "ACRO (Angle/Horizon OFF)", "air_mode": "ON",
                            "notes": ["セルフレベル系モードは使わない", "1 回のアーム〜ディスアームで最後まで飛ぶ (ログが分割されない)"]},
            "segments": [asdict(s) for s in segs],
            "planned_airborne_s": round(planned),
            "batteries": 1,
            "after_flight": [
                "着陸後すぐにモーターの温度を触って確認 (熱すぎる場合は D/フィルターを見直す)",
                "USB を接続して aidt propose (またはスキル) でダウンロード・解析",
                "飛行の感想 (プロップウォッシュ、バウンスバック、もっさり感など) をメモ",
            ],
        }
    elif purpose == "race":
        planned = race_duration_s * max(1, heats)
        plan = {
            "purpose": "race",
            "goal": "レース/練習走行をそのまま記録し、実戦条件でのノイズ・プロップウォッシュ・飽和を評価する",
            "prerequisites": [
                "ヒート前に Blackbox を消去、または残り容量を確認 (容量不足でログが途切れないように)",
                f"想定記録時間: {race_duration_s:.0f} 秒 x {heats} ヒート",
                "ヒートごとにアーム〜ディスアームで 1 ログになる。ヒート番号とコース状況をジャーナルに記録",
            ],
            "flight_mode": {"mode": "普段のレース設定 (Acro)", "air_mode": "ON",
                            "notes": ["レースの飛び方は変えない", "クラッシュ時はディスアームしてログを閉じる"]},
            "segments": [asdict(SEGMENTS["race_laps"]) | {"duration_s": race_duration_s, "repeat": heats}],
            "planned_airborne_s": round(planned),
            "batteries": heats,
            "after_flight": [
                "ヒートごと、または全ヒート後にダウンロード (容量が足りなければヒートごと)",
                "コーナー出口・ダイブ後の揺れ、直線でのパワー不足など体感をメモ",
                "データが解析に不足していたら、別途チューニング用テスト飛行を行う",
            ],
        }
    else:
        raise ValueError("purpose must be 'tuning' or 'race'")

    plan["fc_settings"] = recommended_fc_settings(cfg, purpose, pid_hz, flash_total, planned, bytes_per_frame)
    if flash_total:
        rate_name = next((s["recommended"] for s in plan["fc_settings"] if s["name"] == "blackbox_sample_rate"),
                         cfg.get("blackbox_sample_rate") if cfg else None)
        denom = {"1/1": 1, "1/2": 2, "1/4": 4, "1/8": 8, "1/16": 16}.get(str(rate_name), 2)
        budget = {"flash_total_bytes": flash_total, "flash_used_bytes": flash_used}
        if pid_hz:
            bps = pid_hz / denom * bytes_per_frame
            free = flash_total - (flash_used or 0)
            budget.update(log_rate_hz=round(pid_hz / denom), est_bytes_per_s=round(bps),
                          est_capacity_s=round(flash_total / bps), est_free_s=round(free / bps),
                          fits_plan=free / bps >= planned)
            nyq = pid_hz / denom / 2
            if nyq < 500:
                plan.setdefault("analysis_limits", []).append(
                    f"記録レート {pid_hz / denom:.0f}Hz ではノイズ解析は {nyq:.0f}Hz までになる"
                    " (モーターノイズの上側は見えない)。PID/フィルターの詳細判断はチューニング飛行 (約 2kHz) で行う")
            if not budget["fits_plan"]:
                plan.setdefault("analysis_limits", []).append(
                    "Flash の空きが予定時間に足りない。飛行前に aidt download で消去するか、ヒートごとにダウンロードする")
        plan["flash_budget"] = budget
    return plan
