.PHONY: help install lint test audit ci demo honeypot smoke status health once dry-run scanners bot dashboard \
        probe docker-build docker-up docker-down docker-logs clean

PY ?= python3
VENV := .venv
BIN := $(VENV)/bin

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
	$(BIN)/python -m compileall -q bot core config scanners storage ai_engine dashboard tests
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

dashboard: ## Start the Streamlit dashboard
	$(BIN)/streamlit run dashboard/app.py --server.port 8501

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

