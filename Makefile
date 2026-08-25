# TradeOS 常用命令入口（Phase 0 工程基建）。
# 在项目 Python 环境（tradeos-py312）内执行；用裸命令名，不硬编码机器路径。
# `make check` 按 HANDBOOK 规定跑四件，任一步失败即停止（Make 默认行为）。

.PHONY: dev migrate test check check\:skeleton clean

dev:
	docker compose -f infra/docker-compose.yml up -d

migrate:
	python3 scripts/run_alembic.py upgrade head

test:
	pytest -q

check:
	ruff check .
	mypy domains shared tool_gateway apps workflows notification_gateway infra
	python3 scripts/check_boundaries.py
	python3 scripts/scan_sensitive.py
	pytest -q

# 目标名含冒号，需转义；调用方式：make 'check:skeleton'
check\:skeleton:
	python3 scripts/check_boundaries.py --skeleton

clean:
	find . -type d -name __pycache__ -not -path './.git/*' -not -path './.worktrees/*' -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache
