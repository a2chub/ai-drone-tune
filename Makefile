# ai-drone-tune — 便利ターゲット集 (`make help` で一覧)
#
# 変数 (コマンドラインで上書き):  make watch MODE=auto   make analyze FILE=logs/x.bbl
#   PORT=/dev/ttyACM0   シリアルポート (省略時は自動検出)
#   MODE=approve        auto / approve / manual
#   FILE=path.bbl       対象ログ
#   PURPOSE=tuning      tuning / race
#   DURATION=180 HEATS=1  レース記録の想定 (秒 / ヒート数)
#   CRAFT=<機体名>      history の対象機体
#   AIDT_HOME=...       データ保存先 (既定 ~/ai-drone-tune)
#   ARGS="..."          aidt にそのまま渡す追加オプション

SHELL := /bin/bash
.DEFAULT_GOAL := help

UV       ?= uv
AIDT     := $(UV) run aidt
PORT     ?=
MODE     ?=
FILE     ?=
PURPOSE  ?= tuning
DURATION ?= 180
HEATS    ?= 1
ARGS     ?=
CRAFT    ?=
OUT      ?= sim.bbl
SIM_DURATION ?= 30
EXTRAS   ?= --extra plot

# `make demo AIDT_HOME=/tmp/x` もコマンドに渡るようにする
ifdef AIDT_HOME
export AIDT_HOME
endif

port_opt    = $(if $(PORT),--port $(PORT))
mode_opt    = $(if $(MODE),--mode $(MODE))
file_opt    = $(if $(FILE),--file $(FILE))
purpose_opt = --purpose $(PURPOSE) $(if $(filter race,$(PURPOSE)),--race-duration $(DURATION) --heats $(HEATS))
require_file = @if [ -z "$(FILE)" ]; then echo "FILE=<path.bbl> を指定してください (例: make $@ FILE=logs/QUAD/QUAD_BF4.5.1_xxx.bbl)"; exit 2; fi

##@ セットアップ
.PHONY: help
help: ## このヘルプを表示
	@awk 'BEGIN {FS = ":.*##"; printf "使い方: make \033[36m<target>\033[0m [VAR=value]\n"} \
	  /^[a-zA-Z0-9_-]+:.*?##/ { printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2 } \
	  /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) }' $(MAKEFILE_LIST)
	@printf "\n変数: PORT MODE FILE PURPOSE DURATION HEATS CRAFT AIDT_HOME ARGS (詳細は Makefile 先頭)\n"

.PHONY: check-uv
check-uv:
	@command -v $(UV) >/dev/null 2>&1 || { \
	  echo "uv が見つかりません。インストール: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }

.PHONY: setup
setup: check-uv ## 環境を作成 (依存 + グラフ出力 + 開発ツール)
	$(UV) sync $(EXTRAS)
	@$(AIDT) --version >/dev/null && echo "✓ セットアップ完了: make demo で動作確認できます"

.PHONY: setup-all
setup-all: check-uv ## 全機能 (manual モードの --llm 用 anthropic も含む)
	$(UV) sync --all-extras

.PHONY: update
update: check-uv ## git pull して環境を更新
	git pull --ff-only
	$(UV) sync $(EXTRAS)

.PHONY: install
install: check-uv ## `aidt` コマンドをインストール (~/.local/bin, 常駐・自動起動用)
	$(UV) tool install --reinstall ".[plot]"
	@echo "✓ $$(command -v aidt || echo '~/.local/bin/aidt') (PATH に無ければ: uv tool update-shell)"

.PHONY: uninstall
uninstall: ## `aidt` コマンドをアンインストール
	-$(UV) tool uninstall ai-drone-tune

.PHONY: udev
udev: ## [Linux] シリアルポート権限の udev ルールを導入 (sudo)
	sudo cp contrib/linux/99-betaflight.rules /etc/udev/rules.d/
	sudo udevadm control --reload-rules && sudo udevadm trigger
	@echo "✓ udev ルールを導入しました (機体を挿し直してください)"

.PHONY: autostart
autostart: install ## [Linux] USB 接続時の自動起動 (systemd --user) を有効化
	mkdir -p ~/.config/systemd/user
	cp contrib/linux/aidt-watch.service ~/.config/systemd/user/
	systemctl --user daemon-reload
	systemctl --user enable --now aidt-watch.service
	@echo "✓ aidt watch を常駐させました (ログ: journalctl --user -u aidt-watch -f)"

.PHONY: autostart-off
autostart-off: ## [Linux] 自動起動を無効化
	-systemctl --user disable --now aidt-watch.service

##@ 起動
.PHONY: watch
watch: ## USB 接続を待ち受けて DL→解析→チューニング (MODE=auto|approve|manual)
	$(AIDT) watch $(mode_opt) $(ARGS)

.PHONY: watch-auto
watch-auto: ## 完全自動モードで待ち受け
	$(AIDT) watch --mode auto $(ARGS)

.PHONY: tune
tune: ## 接続中の機体で 1 回実行 (FILE= で既存ログを使用)
	$(AIDT) tune $(port_opt) $(mode_opt) $(file_opt) $(ARGS)

.PHONY: manual
manual: ## 対話型マニュアル設定 (日本語/英語の指示)
	$(AIDT) manual $(port_opt) $(file_opt) $(ARGS)

.PHONY: skill
skill: ## Claude Code を起動してスキルで対話チューニング
	@command -v claude >/dev/null 2>&1 || { echo "Claude Code (claude) が見つかりません: https://docs.claude.com/claude-code"; exit 1; }
	claude

.PHONY: demo
demo: ## 機体なしで全工程を試す (FC エミュレータ + 飛行シミュレータ)
	$(AIDT) demo --yes $(ARGS)

##@ 機体 / ログ
.PHONY: ports
ports: ## 接続中の FC を一覧
	$(AIDT) ports

.PHONY: status
status: ## 機体・Flash・前回の変更などの状態
	$(AIDT) status $(port_opt) --json $(ARGS)

.PHONY: download
download: ## Blackbox をダウンロード (検証後に消去)
	$(AIDT) download $(port_opt) $(ARGS)

.PHONY: propose
propose: ## DL→解析→データ十分性→変更案 (書き込みなし, FILE= でオフライン)
	$(AIDT) propose $(port_opt) $(file_opt) $(purpose_opt) $(ARGS)

.PHONY: analyze
analyze: ## ログをオフライン解析してレポート出力 (FILE= 必須)
	$(require_file)
	$(AIDT) analyze $(FILE) $(ARGS)

.PHONY: check
check: ## ログが解析に足りるか判定 (FILE= 必須, PURPOSE=race も可)
	$(require_file)
	$(AIDT) check $(FILE) $(purpose_opt) $(ARGS)

.PHONY: plan
plan: ## フライトプラン (FILE= を渡すと不足分だけ, PURPOSE=race DURATION= HEATS=)
	$(AIDT) plan $(purpose_opt) $(if $(FILE),--from-log $(FILE)) $(ARGS)

.PHONY: preflight
preflight: ## 接続中 FC の記録設定と Flash 空きをチェックしてプラン作成
	$(AIDT) preflight $(port_opt) $(purpose_opt) $(ARGS)

.PHONY: backup
backup: ## 設定 (diff all) をバックアップ
	$(AIDT) backup $(port_opt)

.PHONY: rollback
rollback: ## 直前の変更を元に戻す (確認あり)
	$(AIDT) rollback $(port_opt) $(ARGS)

.PHONY: history
history: ## 機体の履歴 (CRAFT=<機体名>)
	$(AIDT) history $(if $(CRAFT),--craft $(CRAFT)) $(ARGS)

.PHONY: logs
logs: ## ダウンロード済みログの一覧
	@ls -lt $${AIDT_HOME:-$$HOME/ai-drone-tune}/logs/*/*.bbl 2>/dev/null || echo "ログはまだありません"

##@ 開発
.PHONY: test
test: ## テストを実行
	$(UV) run pytest -q $(ARGS)

.PHONY: lint
lint: ## ruff で静的チェック
	$(UV) run ruff check src tests

.PHONY: lint-fix
lint-fix: ## ruff で自動修正
	$(UV) run ruff check --fix src tests

.PHONY: ci
ci: ## CI と同じチェック (lock 検証 + lint + test)
	$(UV) lock --check
	$(MAKE) lint test

.PHONY: sim
sim: ## 合成ログを生成 (OUT=sim.bbl SIM_DURATION=30)
	$(AIDT) simulate $(OUT) --duration $(SIM_DURATION) $(ARGS)

.PHONY: clean
clean: ## キャッシュ・ビルド生成物を削除 (.venv と取得ログは残す)
	rm -rf .pytest_cache .ruff_cache build dist src/*.egg-info
	find . -name __pycache__ -type d -prune -not -path "./.venv/*" -exec rm -rf {} +

.PHONY: distclean
distclean: clean ## .venv も削除 (make setup で再作成)
	rm -rf .venv
