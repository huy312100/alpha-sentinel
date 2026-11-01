.PHONY: help install lint test audit ci demo honeypot smoke status health once dry-run scanners bot dashboard \
        probe ml-export ml-train ml-report docker-build docker-up docker-down docker-logs clean \
        wheels-arm64 image-arm64 backup backup-verify backup-push mac-install mac-uninstall

PY ?= python3
VENV := .venv
BIN := $(VENV)/bin
# The interpreter that runs pip/downloads. Defaults to the venv, and CI overrides
# it with the system interpreter so the ARM64 probe needs no installed suite.
PYTHON ?= $(BIN)/python
# Where snapshots live is the bot's answer, not a guess made here. DATA_DIR moves
# them, and a push that read a stale data/backups left over in the checkout would
# upload old snapshots and report success - the one failure a backup job must not
# be able to have. Recursive (=) so the interpreter runs only for the targets that
# ask, and an empty answer is a hard error rather than a fallback.
BACKUP_DIR = $(shell $(BIN)/python -m bot.main --print-backup-dir 2>/dev/null)
# Where the macOS LaunchAgents go and where they write. The log directory lives
# inside the user container on purpose: ~/Library/Logs needs no Full Disk Access
# grant, and the supervisor bounds these files the way compose bounds container logs.
LAUNCH_DIR := $(HOME)/Library/LaunchAgents
LOG_DIR := $(HOME)/Library/Logs/alpha-sentinel
# Off-box destination for snapshots. rclone remote name, not a path.
BACKUP_REMOTE ?= gdrive:alpha-sentinel/backups

# ARM64 (Oracle Always Free A1, Apple Silicon, Raspberry Pi) has no guarantee of
# a compiler in the runtime image, so every pin has to resolve to a wheel. pip
# matches ``--platform`` *exactly* and does not expand manylinux compatibility:
# pandas ships its aarch64 wheel as manylinux_2_24, pyarrow as manylinux_2_17, so
# the whole list is needed. One missing tag reads as "no such version", which is
# how this target caught pandas 2.3.3 on its first run.
ARM64_PLATFORMS := manylinux2014_aarch64 manylinux_2_17_aarch64 manylinux_2_24_aarch64 \
                   manylinux_2_28_aarch64 manylinux_2_34_aarch64
ARM64_PLATFORM_ARGS := $(foreach platform,$(ARM64_PLATFORMS),--platform $(platform))

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Create venv and install pinned deps
	$(PY) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -r requirements.txt

lint: ## flake8 (zero findings expected)
	$(BIN)/python -m flake8 . --count --statistics

test: ## Hermetic pytest suite (no keys, no network)
	$(BIN)/python -m pytest --junitxml=junit.xml

audit: lint test ## Lint + tests

ci: audit ## Full local gate: lint, tests, byte-compile, smoke
	$(BIN)/python -m compileall -q bot core config scanners storage ai_engine dashboard ml ops tests
	$(MAKE) smoke

demo: ## Run the dual-agent flow offline (no API key needed)
	DEMO_MODE=true $(BIN)/python -m ai_engine.dual_audit_flow --demo

honeypot: ## Demo run that trips the risk agent's veto
	DEMO_MODE=true $(BIN)/python -m ai_engine.dual_audit_flow --demo --honeypot

smoke: ## Offline smoke: status, health, dry-run cycle, scanner probe, demo agents
	$(BIN)/python -m bot.main --status
	@# Health runs against a scratch store on purpose: the real one may hold a run
	@# from last week, and a smoke step must not depend on the dev machine's history.
	@scratch=$$(mktemp -d); HOT_DB_PATH=$$scratch/hot.db DEMO_MODE=true $(BIN)/python -m bot.main --health
	DEMO_MODE=true $(BIN)/python -m bot.main --once --dry-run --no-alerts --limit 1
	DEMO_MODE=true $(BIN)/python -m scanners --token 0x0000000000000000000000000000000000000001 --chain ethereum
	DEMO_MODE=true $(BIN)/python -m ai_engine.dual_audit_flow --demo

status: ## Print hot-store status
	$(BIN)/python -m bot.main --status

health: ## Cycle-freshness check on the configured store (same command as the container)
	$(BIN)/python -m bot.main --health

ml-export: ## Export training_rows for a notebook, with the prior baseline
	$(BIN)/python -m ml.dataset export --out data/exports/train.parquet --baseline

ml-train: ## Train the alpha model (needs requirements-ml.txt)
	$(BIN)/python -m ml.train --out models/alpha_model.pkl

ml-report: ## Shadow agreement: is the learned scorer ready to replace the LLM?
	$(BIN)/python -m ml.shadow --days 90

once: ## One real cycle, then exit
	$(BIN)/python -m bot.main --once

dry-run: ## One cycle with no writes and no alerts
	$(BIN)/python -m bot.main --once --dry-run --no-alerts

scanners: ## Probe one crypto token through every scanner layer
	$(BIN)/python -m scanners --token 0x0000000000000000000000000000000000000001

probe: ## Probe one listed instrument (equity) through the applicable layers
	DEMO_MODE=true $(BIN)/python -m scanners --symbol AAPL --asset-class equity

bot: ## Start the async scanner loop
	$(BIN)/python -m bot.main

dashboard: ## Start the Streamlit dashboard (loopback; set DASHBOARD_PASSWORD to gate it)
	$(BIN)/streamlit run dashboard/app.py --server.port 8501 --server.address 127.0.0.1

wheels-arm64: ## Prove every pin resolves to a linux/arm64 wheel (no compiler needed)
	rm -rf /tmp/arm64-wheels && mkdir -p /tmp/arm64-wheels
	$(PYTHON) -m pip download -q -r requirements.txt --only-binary=:all: \
		$(ARM64_PLATFORM_ARGS) --python-version 311 --implementation cp --abi cp311 --abi none \
		-d /tmp/arm64-wheels
	@echo "wheels-arm64: $$(ls /tmp/arm64-wheels | wc -l | tr -d ' ') wheels resolved for linux/arm64"

image-arm64: ## Build the image for linux/arm64 (needs docker buildx)
	docker buildx build --platform linux/arm64 --tag alpha-sentinel:arm64 --load .

backup: ## Snapshot the hot store (verified), upload if R2 is set, prune old copies
	$(BIN)/python -m bot.main --backup

backup-verify: ## Rehearse restoring the newest snapshot
	@dir='$(BACKUP_DIR)'; \
	if [ -z "$$dir" ]; then echo "cannot resolve the snapshot directory from settings" >&2; exit 1; fi; \
	latest=$$(ls -1t "$$dir"/alpha_sentinel-*.db 2>/dev/null | head -1); \
	if [ -z "$$latest" ]; then echo "no snapshots in $$dir - run 'make backup' first" >&2; exit 1; fi; \
	$(BIN)/python -m bot.main --verify-backup "$$latest"

backup-push: ## Copy snapshots off the machine (needs rclone; override BACKUP_REMOTE)
	@command -v rclone >/dev/null 2>&1 || { echo "rclone is not installed: brew install rclone" >&2; exit 1; }
	@dir='$(BACKUP_DIR)'; \
	if [ -z "$$dir" ]; then echo "cannot resolve the snapshot directory from settings" >&2; exit 1; fi; \
	if [ ! -d "$$dir" ]; then echo "nothing to push: $$dir does not exist - run 'make backup' first" >&2; exit 1; fi; \
	count=$$(ls -1 "$$dir"/alpha_sentinel-*.db 2>/dev/null | wc -l | tr -d ' '); \
	if [ "$$count" = "0" ]; then echo "nothing to push: no snapshots in $$dir" >&2; exit 1; fi; \
	rclone copy "$$dir" "$(BACKUP_REMOTE)"; \
	echo "pushed $$count snapshot(s) from $$dir -> $(BACKUP_REMOTE)"

mac-install: ## Write the macOS LaunchAgents for this checkout (loads nothing itself)
	@mkdir -p $(LAUNCH_DIR) $(LOG_DIR)
	@for unit in bot watchdog; do \
		sed -e "s|__REPO__|$(CURDIR)|g" -e "s|__HOME__|$(HOME)|g" \
			ops/com.alphasentinel.$$unit.plist > $(LAUNCH_DIR)/com.alphasentinel.$$unit.plist || exit 1; \
		echo "wrote $(LAUNCH_DIR)/com.alphasentinel.$$unit.plist"; \
	done
	@echo "nothing is running yet. When ready:"
	@echo "  launchctl load $(LAUNCH_DIR)/com.alphasentinel.bot.plist"
	@echo "  launchctl load $(LAUNCH_DIR)/com.alphasentinel.watchdog.plist"

mac-uninstall: ## Stop and remove the LaunchAgents this target wrote
	@for unit in bot watchdog; do \
		launchctl unload $(LAUNCH_DIR)/com.alphasentinel.$$unit.plist 2>/dev/null || true; \
		rm -f $(LAUNCH_DIR)/com.alphasentinel.$$unit.plist; \
	done
	@echo "unloaded and removed (logs under $(LOG_DIR) are left alone)"

docker-build: ## Build the container image
	docker compose build

docker-up: ## Start bot + dashboard in the background
	docker compose up -d --build

docker-down: ## Stop the stack
	docker compose down

docker-logs: ## Tail bot logs
	docker compose logs -f bot

clean: ## Remove caches and test artifacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache junit.xml test-results

