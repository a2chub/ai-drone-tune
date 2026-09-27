# ai-drone-tune

Betaflight を搭載した FPV レース用ドローンのための、Blackbox ベースの自動チューニングツール (`aidt`) です。
端末からコマンドで使うことも、Claude Code のスキルとして LLM と対話しながら使うこともできます。

## 使い方

| マニュアル | 内容 |
|---|---|
| [セットアップ](docs/setup.md) | uv でのインストール、シリアル権限、USB 接続時の自動起動、テストの実行 |
| [コマンドの使い方](docs/cli-usage.md) | 端末から `aidt` を使う方法: モード、コマンド一覧、データの保存先、対応設定 |
| [Skill の使い方](docs/skill-usage.md) | Claude Code と対話しながらチューニング・計画・レポート作成を進める方法 |

最短の手順 (`make help` でターゲット一覧を表示):

```bash
git clone https://github.com/a2chub/ai-drone-tune.git && cd ai-drone-tune
make setup     # uv で環境を作成 (uv sync --extra plot)
make demo      # 機体なしで全工程を試す
make watch     # 機体を USB 接続すると、DL → 解析 → チューニング
make skill     # Claude Code を起動し、スキルで対話しながらチューニング
```

`make` が使えない環境 (Windows など) では、`uv sync --extra plot` と `uv run aidt ...` で同じことができます。

## できること

1. **Blackbox のダウンロード**: FC の Dataflash を MSP で読み出し、`<機体名>/<機体名>_BF<バージョン>_<日時>.bbl` に保存します。
   同じ場所に `diff all` のバックアップとメタデータ JSON も保存します。
2. **検証してから消去**: 次の条件をすべて満たした場合だけ Flash を消去します。
   - サイズが一致する
   - (任意) 2 回読んだ結果のハッシュが一致する
   - ディスクから読み戻した内容が一致する
   - ログヘッダが存在する
3. **解析**:
   - ノイズ: Gyro のフィルター前後と D-term のスペクトル、モーターノイズとフレーム共振の判別、フィルター遅延、RPM フィルターの効き
   - ステップ応答
   - プロップウォッシュ、高スロットル時の発振、モーター飽和
4. **チューニング**: フィルター・PID・FF・TPA・Dynamic Idle などの変更案を作り、FC に適用します。モードは 3 つです。
   - `auto`: 完全自動
   - `approve`: 承認ゲート
   - `manual`: 日本語・英語の指示で変更
5. **データ不足時のアドバイス**: ログが解析に足りない場合は、推奨する FC 設定 (記録レート・記録フィールド) と飛行手順を示します。
   飛行手順には、モード・スロットル開度・回数・時間が含まれます。レース記録用のプランも作れます。
6. **安全策**:
   - 変更前にバックアップを取ります
   - 書き込んだ値を読み戻して検証します
   - アーム中は書き込みません
   - 変更履歴を残し、rollback できます
   - 機体ごとのチューニングジャーナルを残します

設定は CLI の `get` から全項目の範囲・選択肢を取得します。そのため、Configurator や CLI で変更できる設定はほぼすべて扱えます。

## 解析とチューニングの考え方

| 解析 | 内容 |
|---|---|
| ノイズ | 飛行中の Welch PSD を計算し、100Hz 以上の RMS を Gyro のフィルター前・後と D-term で比べます。フィルター遅延は相互相関から求めます。 |
| ピーク判別 | スロットル別スペクトログラムと eRPM を使います。モーターの基本波と倍音に一致するピークはモーターノイズ、スロットルに依存しない一定周波数のピークはフレーム共振と判定します。 |
| ステップ応答 | setpoint→gyro のデコンボリューションで求めます (PID-Analyzer と同じ手法)。オーバーシュート、遅延、立ち上がり、整定時間、定常値、振動回数を出します。 |
| 挙動 | スロットルを急に絞った後の 20〜90Hz 誤差 (プロップウォッシュ)、高/中スロットルの発振比、モーター飽和率を調べます。 |
| データ十分性 | 飛行時間、記録レート、記録フィールド、スロットル域、各軸の入力回数、スロットルカット、Angle/Horizon の使用、Flash 容量を確認します。 |

推奨ルールは次の方針です。

- 1 回の変更は最大 15% まで
- フィルターを PID より先に見る
- D-term ノイズに余裕があるときだけ D を上げる

各変更案には理由と信頼度が付きます。「飛ぶ → 接続 → 変更 → 飛ぶ」を繰り返して、少しずつ最適な値に近づけます。

BBL のデコーダは、ファームウェアのエンコーダ (`blackbox_encoding.c`) とビューアのパーサ (`flightlog_parser.js`) に合わせて実装しました。
エンコード→デコードの往復がビット単位で一致することをテストで確認しています。

参考にしたリポジトリ:

- [betaflight](https://github.com/betaflight/betaflight)
- [betaflight-configurator](https://github.com/betaflight/betaflight-configurator)
- [blackbox-log-viewer](https://github.com/betaflight/blackbox-log-viewer)

## 構成

```
src/ai_drone_tune/
  fc/         msp.py (MSP v1/v2)  cli_session.py  flight_controller.py  blackbox_download.py  ports.py  emulator.py
  blackbox/   stream.py (各エンコーディング)  parser.py (BBL パーサ)  writer.py (BBL エンコーダ)
  analysis/   flight_data.py  spectrum.py  noise.py  step_response.py  behaviour.py  data_quality.py  report.py
  tuning/     config.py  recommender.py  instructions.py  apply.py  rates.py  changes.py  flight_plan.py
  pipeline.py (モード制御・watch・propose)  report.py  compare.py  journal.py  sim.py  cli.py
.claude/skills/aidt-tuning/   Claude Code スキル (SKILL.md + reference/)
contrib/                      自動起動用の設定 (systemd/udev, launchd, タスクスケジューラ)
docs/                         マニュアル
```

開発 (テスト・lint) の手順は [セットアップ](docs/setup.md#7-開発者向け-テストと-lint) を参照してください。

> ⚠️ 実機での動作確認はまだ限定的です。設定を変更した後は、プロペラを外した状態でのモーター温度チェックや、短いテスト飛行で確認してください。
> 異常があれば `aidt rollback` で元に戻せます。
