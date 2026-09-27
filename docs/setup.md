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

uv sync --extra plot     # 依存 + グラフ (PNG) 出力用の matplotlib
uv run aidt --version    # 動作確認
```

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
uv tool install ".[plot]"      # ~/.local/bin/aidt に入る
aidt --version

# 更新・削除
uv tool install --reinstall ".[plot]"
uv tool uninstall ai-drone-tune
```

`~/.local/bin` が PATH に入っていない場合は、`uv tool update-shell` を実行してください。

## 4. シリアルポートの権限 (Linux)

```bash
sudo cp contrib/linux/99-betaflight.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
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

常駐させると端末がありません。そのため `approve` モードでは、変更案とレポートの保存までを行います。
内容を確認してから `aidt apply` で適用してください。
完全自動にする場合は `aidt config --set mode=auto` を実行します。

## 6. 機体なしで動作確認

```bash
uv run aidt demo --yes       # FC エミュレータ + 飛行シミュレータで全工程を実行
```

## 7. 開発者向け: テストと lint

```bash
uv sync --all-extras                  # 開発ツール (pytest, ruff) も入る
uv run pytest                         # BBL の往復、MSP、DL/消去、設定の解析と適用、解析、推奨、指示解釈、パイプライン全体
uv run ruff check src tests
```

依存を追加するときは `uv add <パッケージ>` を使います。開発用は `uv add --dev <パッケージ>`、任意機能用は `uv add --optional plot <パッケージ>` です。
`uv.lock` もコミットしてください。
CI (GitHub Actions) は `uv sync --locked` → `uv run pytest` で実行されます。
