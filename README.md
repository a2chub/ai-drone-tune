# ai-drone-tune

Betaflight を搭載した FPV レース用ドローンのための、Blackbox ベースの自動チューニングツールです。

機体を USB で接続すると、次の処理を順に行います。

1. **Blackbox のダウンロード**: オンボード Dataflash の内容を MSP で読み出します。保存先は
   `<機体名>/<機体名>_BF<バージョン>_<日時>.bbl` です。同じ場所に `diff all` のバックアップとメタデータ JSON も保存します。
2. **ダウンロード完了の検証と Blackbox の消去**: 次の条件をすべて満たした場合だけ Flash を消去します。
   - サイズが完全に一致する
   - (任意) 2 回読んだ結果の SHA-256 が一致する
   - ディスクから読み戻した内容が一致する
   - ログヘッダがファイル内に存在する
3. **解析**: ノイズ解析 (Gyro/D-term のスペクトル、モーターノイズとフレーム共振の判別、フィルター遅延、RPM フィルターの効き)、
   ステップ応答 (Wiener デコンボリューション)、プロップウォッシュ、高スロットル時の発振、モーター飽和を調べます。
4. **チューニング**: 解析結果から、フィルター・PID・FF・TPA・Dynamic Idle などの変更案を作り、FC に適用します。
   - `auto`: 完全自動モード。信頼度が閾値以上の変更だけを適用し、保存します。
   - `approve`: 承認ゲートモード。変更を 1 件ずつ理由付きで表示し、`y`/`n`/値の編集/全承認 から選びます。
   - `manual`: マニュアル設定モード。日本語・英語の指示、または CLI 構文で変更できます。
5. **安全策**: 変更前に `diff all` をバックアップします。`set` のたびに `get` で読み戻して検証します。
   変更履歴を残すので `aidt rollback` で元に戻せます。

参考にしたリポジトリ: [betaflight](https://github.com/betaflight/betaflight)、
[betaflight-configurator](https://github.com/betaflight/betaflight-configurator)、
[blackbox-log-viewer](https://github.com/betaflight/blackbox-log-viewer)。
BBL のデコーダはファームウェアのエンコーダ (`blackbox_encoding.c`) とビューアのパーサ (`flightlog_parser.js`) に合わせてあり、
エンコード→デコードの往復がビット単位で一致することをテストで確認しています。

## インストール

```bash
pip install -e .            # numpy, pyserial
pip install -e ".[plot]"    # グラフ(PNG)出力 (matplotlib)
pip install -e ".[ai]"      # manual モードの自然文を Claude で解釈 (任意)
```

Python 3.10 以上が必要です。Linux ではシリアルポートの権限に `contrib/linux/99-betaflight.rules` を使えます。

## 使い方

```bash
aidt watch                  # USB 接続を待ち受け、接続されたら DL → 消去 → 解析 → チューニング
aidt watch --mode auto      # 完全自動
aidt watch --mode approve   # 承認ゲート (既定)
aidt manual                 # 対話型マニュアル設定

aidt tune --mode approve    # 接続済みの機体で 1 回だけ実行
aidt download [--no-erase] [--verify]
aidt analyze LOG.bbl        # オフライン解析 (レポート .md / .json / .png と変更案を出力)
aidt apply  X.proposal.json # 保存された変更案を後から適用 (--only p_roll,d_roll など)
aidt set p_roll=50 "d_pitch +5%" "最大レート 800"
aidt get dyn_notch
aidt backup | aidt restore FILE.diff.txt | aidt rollback
aidt rates --type ACTUAL --rc-rate 7 --srate 67 --target 800
aidt demo --yes             # 機体なしで全工程を試す (FC エミュレータ + 飛行シミュレータ)
```

データはすべて `~/ai-drone-tune/` (または `--home`、環境変数 `AIDT_HOME`) の下に保存します。

```
logs/<機体名>/<機体名>_BF4.5.1_20260926-143012.bbl   (+ .diff.txt, .json)
reports/<機体名>/...report.md / .analysis.json / .png / .proposal.json
backups/<機体名>/..._before.diff.txt
history/<機体名>/<日時>.json                          (rollback 用)
```

### LLM から使う (Claude Code スキル)

`.claude/skills/aidt-tuning/` に、Claude Code 用のスキルを同梱しています。
機体をつなぐ PC でこのリポジトリを Claude Code で開くと、チャットで次のように進められます。

- 「機体をつないだ、ログを見て」→ 状態確認 → ダウンロード・解析 → 説明 → 変更案 → **了承後に**適用
- 「プロップウォッシュが気になる」→ 解析結果と感想を突き合わせ、小さな変更を提案
- データが足りないとき → 不足している理由、推奨する Blackbox/FC 設定、不足分だけの飛行手順 (モード・スロットル開度・回数・時間) を指示
- レース記録 →「3 分 × 4 ヒート」などに合わせて Flash に収まる記録レートを提案し、レース後に解析
- 「今どういう状況？」→ 履歴・ジャーナル・前回比較から状況レポートを作成

スキルは、非対話で JSON を返す次のコマンドを使います (人が使うこともできます)。
FC に書き込むコマンドは `--yes` がないと実行されません。

| コマンド | 内容 |
|---|---|
| `aidt status --json` | 接続機体、Flash 使用量、Blackbox 設定、前回の変更、ジャーナル末尾 |
| `aidt propose [--file X] [--purpose race] --json` | DL → 検証 → 消去 → 解析 → データ十分性判定 → 変更案。設定は書き込まない |
| `aidt check X.bbl [--purpose race] --json` | ログが解析に足りるか。不足項目と、それを補う推奨設定・飛行内容 |
| `aidt plan [--from-log X] [--purpose race --race-duration 180 --heats 3] --json` | フライトプランと推奨 FC 設定 (Flash 容量の見積もり付き) |
| `aidt preflight [--purpose race ...] --json` | 接続中の FC の記録設定と Flash の空きを確認し、プランを作成 |
| `aidt apply P.json --only a,b --yes --json` / `aidt set ... --dry-run --json` | 変更案の適用 / 直接変更 (事前確認) |
| `aidt compare A.bbl B.bbl --json` / `aidt history --craft Q --json` | 前後比較 / 機体の履歴 |
| `aidt journal add --craft Q --kind feedback "..."` / `aidt journal show --craft Q` | 機体ごとのチューニングジャーナル |
| `aidt --emulate sim.bbl <command>` | 機体なしで上記を試す (FC エミュレータ) |

永続設定: `aidt config --set mode=auto erase_after_download=true auto_min_confidence=0.6 max_step=0.1`

### マニュアルモードで使える指示の例

| 入力 | 意味 |
|---|---|
| `p_roll=52`, `set d_pitch = 40`, `gyro_lpf1_type PT2` | CLI の値を直接指定する (すべての CLI 設定に対応) |
| `d_pitch +10%`, `f_yaw -20`, `p_roll *1.1` | 相対的に変更する |
| `ロールのPを5%上げて`, `pitch D down 10%`, `ヨーのIを少し上げて` | 軸と項 (P/I/D/FF) を指定して変更する |
| `最大レート 800`, `yaw rate 600` | 現在の rates_type のまま、最大角速度が目標値になるよう計算する |
| `プロップウォッシュを減らしたい`, `バウンスバック`, `モーターが熱い`, `ノイズ`, `遅延を減らしたい`, `キビキビ`, `滑らか`, `ドリフト`, `高スロットルで揺れる`, `マスターを5%下げて` | 意図から変更案を作る |
| `show` / `undo p_roll` / `get d_roll` / `apply` / `quit` | 変更案の確認・取り消し・値の確認・適用・中止 |

`--llm` を付けると、ルールで解釈できなかった自然文を Claude に渡し、同じ形式の変更案として受け取ります。
変更案は通常の検証と承認フローを通ります。認証には `ANTHROPIC_API_KEY` を使います。

## 対応している設定

- **FC 接続時**: CLI の `get` から、全設定の現在値・範囲・選択肢・所属 (profile / rateprofile) を取得します。
  そのため Configurator や CLI で変更できる設定はほぼすべて、manual モード・`aidt set`・変更案の適用で扱えます
  (フィルター、PID、FF、Rate、TPA、Anti-Gravity、I-term relax、Dynamic Idle、RPM フィルター、Dynamic Notch、モーター、OSD など)。
  値は範囲と選択肢で検証してから設定し、PID/Rate プロファイルは自動で切り替えます。
- **自動チューニングの対象**:
  - `gyro_lpf1/2`、`dterm_lpf1/2`
  - `dyn_notch_count/q/min_hz/max_hz`、`rpm_filter_q/min_hz`
  - `p/i/d/f_<axis>`。D は `d_max_*` / `d_min_*` も含め、ファームウェアのバージョンに合わせて扱います
  - `tpa_rate/breakpoint`、`dyn_idle_min_rpm`
  - `simplified_*`。スライダーで上書きされるのを防ぐため OFF にします
  - `dshot_bidir` は ESC に依存するため、「要手動」の提案にとどめ、自動では適用しません。
- **Rate** はパイロットの好みなので自動では変えません。manual モードで目標値を指定してください。
- SD カードに記録する FC では、`aidt msc` でマスストレージモードにしてから `aidt import <ドライブ>` で取り込みます。

## USB 接続時の自動起動

`aidt watch` を常駐させます。

- Linux: `contrib/linux/aidt-watch.service` (systemd --user) と `99-betaflight.rules` (udev) を使います。
- macOS: `contrib/macos/com.aidt.watch.plist` (launchd) を使います。
- Windows: `contrib/windows/install-task.ps1` (タスクスケジューラ) を使います。

バックグラウンドで動かすと端末がないため、`approve` モードでは変更案とレポートの保存までを行います。
内容を確認してから `aidt apply` で適用してください。
対話しながら承認したい場合は、端末で `aidt watch` を実行してください。

同じ機体は、保存後の再起動で USB が再接続されても再処理しません。8 秒以上抜かれた後に、改めて処理対象になります。

## 解析とルールの概要

| 解析 | 内容 |
|---|---|
| ノイズ | 飛行中 (離陸前後を除く) の Welch PSD を計算し、100Hz 以上の RMS を Gyro のフィルター前・後と D-term で比べます。フィルター遅延は相互相関から求めます。 |
| ピーク判別 | スロットル別スペクトログラムと eRPM を使います。モーターの基本波と倍音に一致するピークはモーターノイズ、スロットルに依存しない一定周波数のピークはフレーム共振と判定します。 |
| ステップ応答 | setpoint→gyro のデコンボリューションで求めます (PID-Analyzer と同じ手法)。オーバーシュート、遅延、立ち上がり時間、整定時間、定常値、振動回数を出します。 |
| 挙動 | スロットルを急に絞った後の 20–90Hz 誤差 (プロップウォッシュ)、高/中スロットルの発振比、モーター飽和率を調べます。 |

ルールは、1 回の変更を最大 15% までにする、フィルターを PID より先に見る、D-term ノイズに余裕があるときだけ D を上げる、という方針です。
それぞれの変更案には理由と信頼度 (飛行時間と解析窓数から算出) が付きます。
「飛ぶ → 接続 → 変更 → 飛ぶ」を繰り返すことで、少しずつ最適な値に近づけます。
閾値は `tuning/recommender.py` の `Thresholds` で調整できます。

**おすすめの設定**:
- `blackbox_sample_rate` は 1/2 以上にします
- `debug_mode` は NONE のままで構いません (BF 4.3 以降は gyroUnfilt を記録します)
- 双方向 DShot を有効にします
- 30 秒以上、スロットルの上げ下げ・フリップ・ロールを含めて飛ばします

## 開発

```bash
pip install -e ".[dev,plot]"
pytest            # BBL の往復、MSP、DL/消去、設定の解析と適用、解析、推奨、指示解釈、パイプライン全体
```

構成:

```
src/ai_drone_tune/
  fc/         msp.py (MSP v1/v2)  cli_session.py  flight_controller.py  blackbox_download.py  ports.py  emulator.py
  blackbox/   stream.py (各エンコーディング)  parser.py (BBL パーサ)  writer.py (BBL エンコーダ)
  analysis/   flight_data.py  spectrum.py  noise.py  step_response.py  behaviour.py  report.py
  tuning/     config.py (get/diff/ヘッダ解析)  recommender.py  instructions.py  apply.py  rates.py  changes.py
  analysis/data_quality.py (データ十分性)  tuning/flight_plan.py (フライトプラン・推奨記録設定)
  pipeline.py (モード制御・watch・propose)  report.py  compare.py  journal.py  sim.py  cli.py
.claude/skills/aidt-tuning/  (Claude Code スキル: SKILL.md + reference/)
```

> ⚠️ 自動チューニングの結果は、必ずプロペラを外した状態でのモーター温度チェックや短いテスト飛行で確認してください。
> 異常があれば `aidt rollback` で元に戻せます。
