# フライトプランの伝え方と調整

`aidt plan --json`、`aidt preflight --json`、`aidt propose --json` の `flight_plan` がベースになる。
構造は次のとおり。

- `segments[]`: `id`、`title`、`duration_s`、`throttle`、`sticks`、`repeat`、`why`
- `fc_settings[]`: `name`、`current`、`recommended`、`reason`、`advisory?`
- `flash_budget`: 容量と見込み記録時間
- `prerequisites`、`flight_mode`、`after_flight`

## 伝え方

- 飛行中に覚えられる量にする。「①ホバー 10 秒 → ②スロットルをゆっくり上下 3 回 → ③ロールを左右交互にパチッと 8 回 …」のように番号付きの短い手順で示す。
- それぞれ「なぜ必要か」を一言添える (`why`)。
- 不足分だけを補う短いプラン (`--from-log` や `missing_segments`) を優先する。全部を毎回やり直させない。
- 総飛行時間と電池本数を明示する。1 パックに収まらなければ分ける (各パックが別ログになるので問題ない)。

## 状況に応じた調整

| 状況 | 調整 |
|---|---|
| 狭い場所・低高度しか取れない | パンチアウトは 1 秒以内に。スロットルカットは「上昇 → 0% → すぐ戻す」の小さい版を回数多めに |
| フリップ/ロールが不安 | ピッチ/ロールのスナップ入力 (70% 程度の入力を 0.3 秒) で代用する |
| 風が強い | ホバーとスナップ入力を風上向きで行う。I 系の判断は保留にする |
| 初心者 | Acro での飛行に不安があれば、無理にプランを実行させない (Angle モードのログではステップ応答を判断できないと説明する) |
| レース直前で時間がない | race プランで本番をそのまま記録し、チューニング用の飛行は後日にする |

## segment id

| id | 内容 |
|---|---|
| `mode_acro` | Acro + Air Mode の確認 |
| `hover` | ホバー 10 秒 |
| `throttle_sweep` | スロットルをゆっくり上下 ×3 |
| `roll_snaps` / `pitch_snaps` / `yaw_snaps` | 各軸のスナップ入力 |
| `flips_rolls` | フリップ/ロール |
| `punch_outs` | パンチアウト ×3 |
| `propwash_chops` | スロットルカット・ダイブ ×4 |
| `cruise` | 通常飛行 |
| `race_laps` | レース走行の記録 |

## FC 設定を反映するとき

- `fc_settings` のうち `advisory` でないものを示し、了承を得てから `aidt set name=value ... --yes` で反映する。
- `blackbox_sample_rate` は FC の PID ループ周波数に対する分周比。記録レート = PID ループ周波数 ÷ 分母。
  - チューニング飛行: 約 2kHz
  - レース: Flash に収まる最大レート
- 飛行前に Flash を空にしておく (`aidt download` はダウンロード・検証の後に消去する)。
