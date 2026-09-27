---
name: aidt-tuning
description: Betaflight FPV ドローンを aidt (ai-drone-tune) で対話的にチューニングする。Blackbox のダウンロード/解析、フィルター・PID・FF・Rate 等の変更提案と適用、解析データが不足しているときの FC 設定・フライトプランの指示、レース時の記録計画、チューニング状況レポートの作成に使う。「機体をつないだ」「ログを見て」「チューニングして」「プロップウォッシュが出る」「どう飛べばいい」「今どういう状況」「レースのデータを取りたい」などで起動する。
---

# aidt チューニングスキル

aidt が計測・検証・書き込みといった確実さが要る処理を担い、あなた (LLM) は判断・説明・パイロットとの対話を担う。
数値は aidt の JSON 出力を根拠にし、推測で埋めない。

## 前提

- FC に触れられるのは、機体が USB 接続された PC 上の Claude Code だけ。接続がない環境 (クラウドなど) では `--file` で既存ログを使うオフライン作業に限る。
- `aidt --version` が失敗したら、リポジトリで `pip install -e ".[plot]"` を実行する。
- すべてのコマンドで `--json` を使う。stdout が JSON、進捗は stderr に出る。
- データは `~/ai-drone-tune/` (`--home` で変更可) に保存される。
- 注意: CLI モードに入ったコマンドは終了時に FC が再起動する。連続実行するときは 3〜5 秒あける。不要な接続はしない。

## 絶対に守ること (安全)

1. **FC に書き込む前に、変更内容と理由を示してユーザーの明示的な了承を得る。** 了承後に限り `aidt apply ... --yes` / `aidt set ... --yes` を実行する。`propose` / `check` / `plan` / `status` / `compare` / `history` は読み取り専用なので確認なしで実行してよい。
2. 1 回に変える量は小さく (目安: 各設定 ±15% 以内、PID は 5〜10%)。複数の系統 (フィルターと PID など) を同時に大きく変えない。
3. 書き込み後は、プロペラを外してモーターを短時間回し温度を確認するよう、またはホバーで短いテスト飛行をするよう促す。異常時の戻し方 (`aidt rollback --yes`) も伝える。
4. `advisory: true` の変更 (`dshot_bidir`、機体名など) はハードウェアやユーザーの事情に依存する。自動では適用せず、条件 (ESC ファームウェアなど) を確認してから扱う。
5. データ品質が `sufficient: false` のときは PID の変更を提案しない。まず「何が足りないか」と「どう飛べばよいか」を伝える (下のワークフロー B)。
6. Rate はパイロットの好み。ユーザーが望んだときだけ変える。

## ワークフロー

### A. 機体を接続したとき (基本ループ)

1. `aidt status --json` で機体・Flash 使用量・前回の変更・ジャーナル末尾を把握する。
2. `aidt propose --json` を実行する。処理内容は、ダウンロード → 検証 → 消去 → 解析 → データ品質判定 → 変更案 (proposal ファイル) の作成。FC には書き込まない。
3. 結果を次の順で読み、ユーザーに日本語で簡潔に説明する。
   - `data_quality`: 不足があれば B へ
   - `analysis.noise`
   - `analysis.step_response`
   - `analysis.behaviour`
   - `recommendations`

   指標の読み方は [reference/metrics.md](reference/metrics.md) を参照する。
4. パイロットの感想 (プロップウォッシュ、バウンスバック、もっさり、モーターの熱など) を聞く。解析と照らし合わせて変更案を調整する。症状と設定の対応は [reference/tuning-guide.md](reference/tuning-guide.md) を参照する。
   - 変更案から一部だけ採用するとき: `aidt apply <proposal> --only a,b --yes`
   - 独自の変更を加えるとき: まず `aidt set <...> --dry-run --json` で確認し、了承後に `--yes` で適用する。
5. 了承を得てから適用する。`apply`/`set` はバックアップ・値の読み戻し検証・保存・履歴記録・ジャーナル記録まで自動で行う。
6. 次の飛行で確かめることを伝える。前回と比べる場合は `aidt compare <前回.bbl> <今回.bbl> --json` を使う。
7. パイロットの感想は `aidt journal add --craft <機体名> --kind feedback <内容>` で記録する。

### B. 解析に必要なデータが足りないとき

`data_quality.issues` の各項目には、`settings` (推奨する FC 設定) と `segments` (不足を補う飛行内容) が付いている。

1. 不足の理由を具体的に説明する。例: 「ヨーのスナップ入力が 1 回しかないので、ヨーのステップ応答を評価できません」。
2. FC 設定の不足 (記録レート、`blackbox_disable_*`、`debug_mode` など) があれば `aidt preflight --json` で現在値と推奨値を確認する。推奨値を示して了承を得たら、`aidt set name=value ... --yes` で反映する。
3. `flight_plan` (propose の出力、または `aidt plan --from-log <file> --json`) をもとに、次の飛行手順を具体的に指示する。
   - 伝える項目: モード (Acro + Air Mode)、各動作のスロットル開度・操作・回数・時間、総飛行時間、電池本数、飛行前の Flash 消去
   - 不足している部分だけに絞った短いプランにする
   - パイロットの技量や飛行場所の制約に合わせて調整してよい。例: フリップが難しいならピッチのスナップ入力で代用する
   - 詳細は [reference/flight-plans.md](reference/flight-plans.md) を参照する
4. 飛行前チェックとして `aidt preflight --json` の `checks` を確認する (Flash が空か、アームしていないか)。

### C. レース/練習走行のデータ取得と解析

1. 飛ぶ前に `aidt preflight --purpose race --race-duration <秒> --heats <本数> --json` を実行する。予定時間が Flash に収まる記録レートと、止めてよいフィールドを提案する。了承後に `aidt set ... --yes` で反映する。
2. ヒートごとに 1 ログになることと、容量が足りなければヒートごとにダウンロードすることを伝える。ヒート番号や状況は `aidt journal add --kind race ...` で記録する。
3. 飛行後は `aidt propose --purpose race --json` を実行する。レースのデータは入力が偏るため、ステップ応答は参考程度に扱う。ノイズ、プロップウォッシュ比、飽和、スロットル分布を重視する。
4. PID の判断に必要なデータが足りなければ、チューニング用のテスト飛行 (`aidt plan --json`) を別途提案する。

### D. 状況レポートの作成

次のコマンドで材料を集める。

- `aidt history --craft <機体名> --json`
- `aidt journal show --craft <機体名> --json`
- 最新と前回の比較: `aidt compare ... --json`
- 最新のレポート (`*.analysis.json` / `*.report.md` / `*.png`)

[reference/report-template.md](reference/report-template.md) の構成でまとめる。

### E. ユーザーの指示で直接変更する (マニュアル)

「ロールの D を少し上げて」「最大レート 850」「dyn_notch_count=2」のような指示を受けたとき:

1. `aidt set "<指示またはCLI構文>" --dry-run --json` で解釈結果と検証結果を確認する。
2. 内容をユーザーに示し、了承を得てから `--yes` で適用する。
3. aidt が解釈できない指示 (`unparsed`) は、あなたが CLI 設定名と値に翻訳してから同じ手順で扱う。設定名は `aidt get <部分一致> --json` で確認できる (範囲と選択肢も返る)。

### F. 元に戻す

- 直前の適用を戻す: `aidt rollback --yes`
- 任意の時点に戻す: `aidt restore <backups/...before.diff.txt>`

どちらも実行前に了承を得る。

## コマンド早見表

| 目的 | コマンド |
|---|---|
| 現状把握 | `aidt status --json` |
| DL + 解析 + 提案 (書き込みなし) | `aidt propose [--file X.bbl] [--purpose race] --json` |
| データ十分性チェック | `aidt check X.bbl [--purpose race] --json` |
| フライトプラン | `aidt plan [--from-log X.bbl] [--purpose race --race-duration 150 --heats 3] --json` |
| 接続 FC の記録設定チェック | `aidt preflight [--purpose race ...] --json` |
| 提案の適用 | `aidt apply <proposal.json> [--only a,b] --yes --json` |
| 直接変更 | `aidt set p_roll=48 "d_pitch +5%" --dry-run --json` → `--yes` |
| 設定値・範囲の確認 | `aidt get dterm_lpf --json` |
| 比較 / 履歴 / ジャーナル | `aidt compare A B --json` / `aidt history --craft Q --json` / `aidt journal show --craft Q --json` |
| 元に戻す | `aidt rollback --yes` |
