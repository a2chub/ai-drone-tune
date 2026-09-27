# aidt の指標の読み方

目安の値は 5 インチのレース機を想定している。機体サイズ、プロペラ、モーター KV で変わるので、絶対値だけで判断しない。
**前回からの変化** (`aidt compare`) を重視する。

## data_quality

| 項目 | 意味 |
|---|---|
| `sufficient` | 解析に使えるか。blocker が 1 件でもあれば false |
| `score` | 0〜100。blocker -35、warning -12、info -3 |
| `metrics.airborne_s` | 離陸前後を除いた飛行時間。チューニングは 30 秒以上、レースは 60 秒以上 |
| `metrics.sample_rate_hz` | 記録レート。チューニングは 1.6〜4kHz (2kHz 推奨)。1kHz 未満だとモーターノイズが見えない |
| `metrics.throttle_time_s` | スロットル帯ごとの秒数。4 帯以上に 1 秒以上あるのが望ましい |
| `metrics.stick_snaps` | 軸ごとの素早い入力の回数。roll/pitch 10 回以上、yaw 6 回以上 |
| `metrics.throttle_chops` / `punch_outs` | 急にスロットルを絞った回数 / パンチアウトの回数。3 回以上 / 2 回以上 |
| `metrics.angle_horizon_fraction` | セルフレベル系モードで飛んだ割合。0.2 を超えると PID 評価に不向き |
| `missing_segments` | 次の飛行で追加すべき動作 (flight_plan の segment id) |
| `recommended_settings` | 記録設定の推奨値 |

## analysis.noise.axes[]

| 項目 | 目安 | 解釈 |
|---|---|---|
| `raw_hf_rms` | – | フィルター前の Gyro の 100Hz 以上の RMS (deg/s)。機体・プロペラ・モーターの状態そのもの。急に増えたらプロペラの損傷やベアリングを疑う |
| `filtered_hf_rms` | < 1.0 きれい / > 2.5 多い | フィルター後に残ったノイズ。多いと D-term とモーターの発熱につながる |
| `attenuation_db` | -15〜-25 が一般的 | フィルターによる減衰量 |
| `filter_delay_ms` | 0.6〜1.5 | フィルターの遅延。小さいほど応答が速いが、ノイズとのトレードオフになる |
| `dterm_hf_pct` | < 1.5 余裕あり / > 5 多い / > 8 モーターが熱くなる危険 | D 項の 80Hz 以上のノイズ (出力の %) |
| `raw_peaks[].kind` | motor / frame_resonance / unknown | motor: RPM フィルターと動的ノッチの担当。frame_resonance: 動的ノッチで対処 (ネジの緩みやアームの共振も確認する) |

`noise.motor.rpm_filter_attenuation_db` はモーター基本周波数での減衰量 (dB)。RPM フィルターが有効なら -20dB 以下が目安で、-12dB より弱ければ Q やハーモニクスを見直す。

## analysis.step_response[]

| 項目 | 目安 | 解釈 |
|---|---|---|
| `overshoot_pct` | roll/pitch 3〜10% | > 15%: P に対して D が不足している (またはフィードフォワードが過剰)。< 3% で rise が遅い: P 不足 |
| `delay_ms` (50% 到達) | 10〜20ms | 大きい: フィルター遅延、P 不足、フィードフォワード不足 |
| `rise_ms` (10→90%) | 15〜30ms | 大きい: 応答が鈍い |
| `ringing` | 0〜1 | 2 以上: 発振ぎみ (P を下げる、または D を上げる) |
| `steady` | 0.95〜1.05 | < 0.9: I 不足、または追従不足 |
| `tracking_lag_ms` | < 25 | setpoint に対する Gyro の遅れ。大きく、かつオーバーシュートが小さければ FF を上げる余地がある |
| `windows_used` | ≥ 40 で信頼できる | 解析に使えた窓の数。少なければスナップ入力を増やしてもらう |

yaw は D を使わないのが一般的で、オーバーシュートが 20〜30% あっても珍しくない。

## analysis.behaviour

| 項目 | 目安 | 解釈 |
|---|---|---|
| `propwash_ratio` | < 1.2 良好 / > 1.4 要改善 | スロットルを絞った直後の揺れ (誤差) が、普段の何倍か |
| `high_throttle_osc_ratio` | < 1.5 | 高スロットルと中スロットルの発振の比。大きければ TPA を強める |
| `motor_saturation_pct` | < 5% | モーターが上限に張り付いた割合。PID では直せない (Rate を下げる、パワー不足を疑う) |
| `iterm_saturation_pct` | – | I 項が大きく溜まった割合。重心のずれ、風、機体の歪みを疑う |
