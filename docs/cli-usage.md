# コマンドの使い方 (ローカル利用)

端末から aidt を直接使う方法です。LLM と対話しながら使う場合は [Skill の使い方](skill-usage.md) を参照してください。

- **リポジトリ内の環境で使う場合**: `uv run aidt ...`
- **`uv tool install` 済みの場合**: `aidt ...`

準備は [セットアップ](setup.md) を参照してください。以下の例では `aidt` と書きます。

## 基本の流れ

```bash
aidt watch                  # USB 接続を待ち受け、接続されたら DL → 検証 → 消去 → 解析 → チューニング
aidt watch --mode approve   # 承認ゲート (既定): 変更を 1 件ずつ y/n/編集/全承認
aidt watch --mode auto      # 完全自動: 信頼度が閾値以上の変更だけを適用・保存
aidt manual                 # 対話型マニュアル設定
aidt tune --mode approve    # 接続済みの機体で 1 回だけ実行 (--file X.bbl で既存ログを使う)
```

### モード

| モード | 動作 |
|---|---|
| `auto` | 信頼度が閾値 (`auto_min_confidence`) 以上の変更だけを適用し、保存します。飛行ログ記録後に FC の設定が変わっていた場合は適用しません。 |
| `approve` | 変更を 1 件ずつ理由付きで表示します。`y` (承認) / `n` (却下) / `e` (値を編集) / `a` (残りを全部承認) / `q` (中止) から選びます。 |
| `manual` | 日本語・英語の指示、または CLI 構文で変更します (下の表)。`show` で確認し、`apply` で適用します。 |

### マニュアルモードで使える指示

| 入力 | 意味 |
|---|---|
| `p_roll=52`, `set d_pitch = 40`, `gyro_lpf1_type PT2` | CLI の値を直接指定する (すべての CLI 設定に対応) |
| `d_pitch +10%`, `f_yaw -20`, `p_roll *1.1` | 相対的に変更する |
| `ロールのPを5%上げて`, `pitch D down 10%`, `ヨーのIを少し上げて` | 軸と項 (P/I/D/FF) を指定して変更する |
| `最大レート 800`, `yaw rate 600` | 現在の rates_type のまま、最大角速度が目標値になるよう計算する |
| `プロップウォッシュを減らしたい`、`バウンスバック`、`モーターが熱い`、`ノイズ`、`遅延を減らしたい`、`キビキビ`、`滑らか`、`ドリフト`、`高スロットルで揺れる`、`マスターを5%下げて` | 意図から変更案を作る |
| `show` / `undo p_roll` / `get d_roll` / `apply` / `quit` | 変更案の確認 / 取り消し / 値の確認 / 適用 / 中止 |

`--llm` を付けると、ルールで解釈できなかった自然文を Claude に渡し、同じ形式の変更案として受け取ります。
変更案は通常の検証と承認フローを通ります。
使うには `uv sync --all-extras` で環境を作り、`ANTHROPIC_API_KEY` を設定しておく必要があります。

## コマンド一覧

### 機体の接続・ダウンロード

| コマンド | 内容 |
|---|---|
| `aidt ports` | 接続中の FC の一覧 |
| `aidt info` / `aidt status --json` | 機体情報と Flash 使用量 / それに加えて Blackbox 設定、前回の変更、ジャーナル |
| `aidt download [--no-erase] [--verify] [--json]` | ダウンロード → 検証 → 消去。`--verify` を付けると 2 回読んで比較します |
| `aidt msc` → `aidt import <ドライブ>` | SD カードに記録する FC: マスストレージモードで再起動し、ログを取り込みます |

### 解析・計画

| コマンド | 内容 |
|---|---|
| `aidt analyze LOG.bbl` | オフライン解析。レポート (.md / .json / .png) と変更案を出力します |
| `aidt propose [--file X.bbl] [--purpose race] [--json]` | DL → 解析 → データ十分性判定 → 変更案。FC には書き込みません |
| `aidt check LOG.bbl [--purpose race]` | ログが解析に足りるか。不足項目と、それを補う推奨設定・飛行内容を表示します |
| `aidt plan [--from-log X.bbl] [--purpose race --race-duration 180 --heats 3]` | フライトプランと推奨 FC 設定 (Flash 容量の見積もり付き) |
| `aidt preflight [--purpose race ...]` | 接続中の FC の記録設定と Flash の空きを確認し、プランを作成します |
| `aidt compare BEFORE.bbl AFTER.bbl` | 2 フライトの指標を比較します (改善/悪化) |
| `aidt history --craft <機体名>` / `aidt journal show --craft <機体名>` | ログ・変更・レポートの履歴 / チューニングジャーナル |
| `aidt journal add --craft <機体名> --kind feedback "プロップウォッシュが残る"` | 感想やメモを記録します |

### 設定の変更 (FC に書き込む)

書き込み系のコマンドは確認を求めます。スクリプトから使う場合は `--yes` が必要です。
アーム中の機体には書き込みません。

| コマンド | 内容 |
|---|---|
| `aidt apply X.proposal.json [--only p_roll,d_roll] [--yes]` | 保存された変更案を適用します |
| `aidt set p_roll=50 "d_pitch +5%" "最大レート 800" [--dry-run] [--yes]` | 直接変更します。`--dry-run` を付けると解釈と検証の結果だけを表示します |
| `aidt get dyn_notch [--json]` | 設定値を部分一致で表示します (範囲・選択肢付き) |
| `aidt backup` / `aidt restore FILE.diff.txt [--yes]` | `diff all` の保存 / 復元 |
| `aidt rollback [--yes]` | 直前に適用した変更を元に戻します |

### その他

| コマンド | 内容 |
|---|---|
| `aidt rates --type ACTUAL --rc-rate 7 --srate 67 [--target 800]` | Rate カーブの計算 |
| `aidt simulate out.bbl` / `aidt demo --yes` | 合成ログの生成 / 機体なしで全工程を実行 |
| `aidt --emulate sim.bbl <command>` | FC エミュレータを相手に任意のコマンドを試します |
| `aidt config --set mode=auto erase_after_download=true auto_min_confidence=0.6 max_step=0.1` | 永続設定 (`~/.config/ai-drone-tune/config.json`) |

ほとんどのコマンドは `--json` を付けると機械可読な出力になります。その場合、進捗は stderr に出ます。

## データの保存先

`~/ai-drone-tune/` の下に保存します (`--home` または環境変数 `AIDT_HOME` で変更できます)。

```
logs/<機体名>/<機体名>_BF4.5.1_20260926-143012.bbl   (+ .diff.txt, .json)
reports/<機体名>/...report.md / .analysis.json / .png / .proposal.json
backups/<機体名>/..._before.diff.txt
history/<機体名>/<日時>.json                          (rollback 用)
journal/<機体名>.jsonl                               (チューニングジャーナル)
```

## 対応している設定

- **FC 接続時**: CLI の `get` から、全設定の現在値・範囲・選択肢・所属 (profile / rateprofile) を取得します。
  そのため Configurator や CLI で変更できる設定は、ほぼすべて `manual` / `set` / `apply` で扱えます。
  値は範囲と選択肢で検証してから設定し、PID/Rate プロファイルは自動で切り替えます。
- **自動チューニングの対象**:
  - `gyro_lpf1/2`、`dterm_lpf1/2`
  - `dyn_notch_count/q/min_hz/max_hz`、`rpm_filter_q/min_hz`
  - `p/i/d/f_<axis>`。D は `d_max_*` / `d_min_*` も含め、ファームウェアのバージョンに合わせて扱います
  - `tpa_rate/breakpoint`、`dyn_idle_min_rpm`
  - `simplified_*`。スライダーで上書きされるのを防ぐため OFF にします
- `dshot_bidir` は ESC に依存するため、「要手動」の提案にとどめます。
- Rate はパイロットの好みなので、自動では変えません。

## 解析に向いたデータを取るために

- `blackbox_sample_rate` は約 2kHz にします (8kHz ループなら `1/4`、4kHz ループなら `1/2`)。`aidt preflight` が推奨値を出します。
- 30 秒以上、Acro モードで飛ばします。スロットルの上げ下げ、各軸のスナップ入力、パンチアウト、スロットルカットを入れてください。
  具体的な手順は `aidt plan` で出せます。
- 双方向 DShot を有効にします (RPM フィルターとモーター回転数の記録)。

> ⚠️ 設定を変更した後は、プロペラを外した状態でのモーター温度チェックや、短いテスト飛行で確認してください。
> 異常があれば `aidt rollback` で元に戻せます。
