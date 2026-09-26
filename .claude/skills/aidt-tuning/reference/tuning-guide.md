# 症状と設定の対応 (Betaflight 4.3〜4.6 / 2025.x)

原則は次の 3 つ。

- **ノイズ → フィルター → PID の順に見る。** ノイズが多いまま D を上げない。
- **1 回に 1 系統だけ、5〜15% ずつ変える。** 飛ぶたびに `compare` で効果を確認する。
- **ハードウェアの問題は設定で直さない。** プロペラ、モーター、ネジの緩み、ソフトマウントを先に確認する。

## パイロットの訴えと対応

| 訴え | 確認する指標 | 主な対応 (aidt set の例) |
|---|---|---|
| プロップウォッシュ (ダイブ後・急旋回後の揺れ) | propwash_ratio、dterm_hf_pct | D を 5〜10% 上げる (4.6 以降は `d_<axis>`、4.5 以前は `d_min_<axis>`)。`dyn_idle_min_rpm`=30 前後 (双方向 DShot が必要)。`iterm_relax_cutoff` の調整。D-term ノイズが多ければ先にフィルターを改善する |
| バウンスバック (フリップ後の跳ね返り) | overshoot_pct、ringing | D を上げる、または P を少し下げる。I-term relax (`iterm_relax`=RP、`iterm_relax_cutoff`)。FF が過大なら下げる |
| もっさり・遅れる | delay_ms、rise_ms、tracking_lag_ms、filter_delay_ms | FF を上げる、P を上げる。ノイズに余裕があればフィルターのカットオフを上げる |
| 小刻みな振動・ジェロ映像 | filtered_hf_rms、ピーク | 動的ノッチの範囲・個数、LPF を下げる。frame_resonance ならネジ・アームを点検 |
| モーターが熱い | dterm_hf_pct | D を下げる、dterm LPF を下げる、プロペラを点検 |
| 高スロットルで揺れる | high_throttle_osc_ratio | `tpa_rate` を +10、`tpa_breakpoint` を下げる |
| 直線でパワーが足りない・ラインがぶれる | motor_saturation_pct、steady | 飽和なら Rate や機材の見直し。steady が低ければ I を上げる |
| ヨーが流れる・止まらない | yaw の steady、overshoot | ヨーの I を上げる、ヨーの P を調整する |
| 風に流される・水平を保てない | iterm_saturation_pct | I を上げる。重心の確認 |

## フィルターの考え方

- **RPM フィルター有効 (`dshot_bidir`=ON + eRPM がログにある)**: 動的ノッチは 1〜2 個、Q 500 前後で、frame_resonance を狙う。gyro LPF はノイズに余裕があれば上げて遅延を減らす。
- **RPM フィルターなし**: 動的ノッチは 3〜4 個、Q 300 前後。LPF は控えめにする。双方向 DShot の導入を提案する (ESC が BLHeli_32 / Bluejay / AM32 の場合)。
- **`simplified_*` スライダーが ON の場合**: 生の値を直接変えると、Configurator でスライダーを動かしたときに上書きされる。aidt は必要に応じて OFF を提案するので、その意味をユーザーに説明する。

## D の名前の違い

| ファームウェア | 設定名 |
|---|---|
| BF 4.3〜4.5 | `d_<axis>` = 最大 D、`d_min_<axis>` = 基本 D (0 で無効) |
| BF 4.6 / 2025.x | `d_<axis>` = 基本 D、`d_max_<axis>` = 最大 D |

aidt は存在する方を自動で両方スケールする。手動で指定するときは `aidt get d_ --json` で確認する。
