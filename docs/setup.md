# セットアップ

aidt は [uv](https://docs.astral.sh/uv/) で環境を作って使います。Python 3.10 以上が必要ですが、uv が自動で用意します。

## 1. uv のインストール

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

インストール後、`uv --version` で確認します。

## 2. リポジトリのクローンと環境の作成

```bash
git clone https://github.com/a2chub/ai-drone-tune.git
cd ai-drone-tune

make setup               # = uv sync --extra plot (依存 + グラフ出力用の matplotlib)
uv run aidt --version    # 動作確認
```

`make help` で、使えるターゲットの一覧を表示できます (下の「make ターゲット一覧」も参照)。
`make` がない環境 (Windows など) では、表の uv コマンドを直接実行してください。

| コマンド | 入るもの |
|---|---|
| `uv sync` | 本体 (numpy, pyserial) と開発ツール (pytest, ruff) |
| `uv sync --extra plot` | + matplotlib (解析レポートのグラフ PNG) |
| `uv sync --all-extras` | + anthropic (manual モードの `--llm`: 自然文を Claude で解釈) |

- 環境はリポジトリ内の `.venv/` に作られ、`uv.lock` で依存のバージョンが固定されます。
- 依存の更新を取り込むときは、`git pull` の後にもう一度 `uv sync --extra plot` を実行します。
- **Claude Code スキルはこの環境 (`uv run aidt`) を使います。** スキルで使う場合は、この手順までで準備完了です。

## 3. `aidt` コマンドとしてインストール (任意)

リポジトリの外から `aidt` を直接呼びたい場合と、USB 接続時の自動起動 (常駐) に使う場合に必要です。

```bash
cd ai-drone-tune
make install                   # = uv tool install --reinstall ".[plot]" (~/.local/bin/aidt に入る)
aidt --version

# 更新・削除
uv tool install --reinstall ".[plot]"
uv tool uninstall ai-drone-tune
```

`~/.local/bin` が PATH に入っていない場合は、`uv tool update-shell` を実行してください。

## 4. シリアルポートの権限 (Linux)

```bash
make udev      # = sudo cp contrib/linux/99-betaflight.rules /etc/udev/rules.d/ && sudo udevadm control --reload-rules
```

または、ユーザーを `dialout` グループに追加します (`sudo usermod -aG dialout $USER`、再ログインが必要)。

macOS と Windows では、通常は追加の設定は不要です。
Windows で COM ポートが出ない場合は、Betaflight Configurator 付属のドライバ (ImpulseRC Driver Fixer など) を使ってください。

## 5. USB 接続時の自動起動 (任意)

3 のインストールを済ませてから、OS ごとの設定ファイルを使って `aidt watch` を常駐させます。

| OS | 使うファイル | 手順 |
|---|---|---|
| Linux | `contrib/linux/aidt-watch.service` (systemd --user)、`99-betaflight.rules` (udev) | `cp contrib/linux/aidt-watch.service ~/.config/systemd/user/` → `systemctl --user daemon-reload` → `systemctl --user enable --now aidt-watch.service` |
| macOS | `contrib/macos/com.aidt.watch.plist` (launchd) | ファイル内の aidt のパス (`which aidt`) を書き換えてから、`cp ... ~/Library/LaunchAgents/` → `launchctl load ~/Library/LaunchAgents/com.aidt.watch.plist` |
| Windows | `contrib/windows/install-task.ps1` (タスクスケジューラ) | `powershell -ExecutionPolicy Bypass -File contrib\windows\install-task.ps1` |

Linux では `make autostart` を実行すると、インストールから systemd --user の有効化までを一度に行います。
止めるときは `make autostart-off` を実行します。

常駐させると端末がありません。そのため `approve` モードでは、変更案とレポートの保存までを行います。
内容を確認してから `aidt apply` で適用してください。
完全自動にする場合は `aidt config --set mode=auto` を実行します。

## 6. 機体なしで動作確認

```bash
make demo                    # = uv run aidt demo --yes (FC エミュレータ + 飛行シミュレータで全工程を実行)
```

## 7. 開発者向け: テストと lint

```bash
make setup-all      # = uv sync --all-extras (開発ツール pytest, ruff も入る)
make test           # = uv run pytest (BBL の往復、MSP、DL/消去、設定の解析と適用、解析、推奨、指示解釈、パイプライン全体)
make lint           # = uv run ruff check src tests
make ci             # CI と同じチェック (uv lock --check + lint + test)
```

依存を追加するときは `uv add <パッケージ>` を使います。開発用は `uv add --dev <パッケージ>`、任意機能用は `uv add --optional plot <パッケージ>` です。
`uv.lock` もコミットしてください。
CI (GitHub Actions) は `uv sync --locked` → `uv run pytest` で実行されます。

## make ターゲット一覧

`make help` でも同じ一覧を表示できます。変数はコマンドラインで指定します (例: `make watch MODE=auto`、`make check FILE=logs/x.bbl PURPOSE=race`)。

| 分類 | ターゲット | 内容 |
|---|---|---|
| セットアップ | `setup` / `setup-all` | 環境を作成 (`uv sync --extra plot` / `--all-extras`) |
| | `update` | `git pull` して環境を更新 |
| | `install` / `uninstall` | `aidt` コマンドのインストール / 削除 (`uv tool`) |
| | `udev` | [Linux] シリアルポート権限の udev ルールを導入 |
| | `autostart` / `autostart-off` | [Linux] USB 接続時の自動起動 (systemd --user) を有効化 / 無効化 |
| 起動 | `watch` / `watch-auto` | USB 接続を待ち受けて DL→解析→チューニング (`MODE=`) / 完全自動 |
| | `tune` / `manual` | 接続中の機体で 1 回実行 / 対話型マニュアル設定 |
| | `skill` | Claude Code を起動してスキルで対話チューニング |
| | `demo` | 機体なしで全工程を試す |
| 機体 / ログ | `ports` / `status` | FC の一覧 / 状態 |
| | `download` / `propose` | ダウンロード / DL→解析→変更案 (書き込みなし) |
| | `analyze` / `check` | ログの解析 / データ十分性の判定 (`FILE=` 必須) |
| | `plan` / `preflight` | フライトプラン / 接続 FC の記録設定チェック (`PURPOSE=race DURATION= HEATS=`) |
| | `backup` / `rollback` / `history` / `logs` | バックアップ / 元に戻す / 履歴 (`CRAFT=`) / ログ一覧 |
| 開発 | `test` / `lint` / `lint-fix` / `ci` | テスト / 静的チェック / 自動修正 / CI 相当 |
| | `sim` | 合成ログを生成 (`OUT=` `SIM_DURATION=`) |
| | `clean` / `distclean` | キャッシュ削除 / `.venv` も削除 |

| 変数 | 意味 |
|---|---|
| `PORT` | シリアルポート (省略時は自動検出) |
| `MODE` | `auto` / `approve` / `manual` |
| `FILE` | 対象ログ (.bbl) |
| `PURPOSE`, `DURATION`, `HEATS` | `tuning` / `race`、レース 1 ヒートの秒数、ヒート数 |
| `CRAFT` | 機体名 (`history`) |
| `AIDT_HOME` | データ保存先 (既定 `~/ai-drone-tune`) |
| `ARGS` | aidt にそのまま渡す追加オプション (例: `ARGS="--no-erase --verify"`) |
