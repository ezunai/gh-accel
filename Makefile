# gh-accel 开发辅助
#
#   make install    安装到当前 Python 环境（editable）
#   make test       跑测试
#   make lint       静态检查
#   make check      test + lint
#   make service    安装并重启 systemd user 服务
#   make clean      清理构建产物与代理缓存

PYTHON ?= python3
PIP    ?= $(PYTHON) -m pip
SERVICE_DIR := $(HOME)/.config/systemd/user

.PHONY: help install dev test lint check service uninstall clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## 以可编辑模式安装
	$(PIP) install -e .

dev:  ## 安装开发依赖
	$(PIP) install -e ".[dev]"

test:  ## 运行测试
	$(PYTHON) -m pytest -q

lint:  ## 静态检查
	$(PYTHON) -m ruff check src tests
	$(PYTHON) -m ruff format --check src tests

check: test lint  ## 测试 + 检查

service:  ## 安装并重启 systemd user 服务
	sed 's|@GH_ACCEL_ROOT@|$(CURDIR)|g' systemd/gh-accel.service \
		> $(SERVICE_DIR)/gh-accel.service
	systemctl --user daemon-reload
	systemctl --user restart gh-accel.service
	systemctl --user status gh-accel.service --no-pager

uninstall:  ## 停用并移除 systemd user 服务
	systemctl --user disable --now gh-accel.service || true
	rm -f $(SERVICE_DIR)/gh-accel.service
	systemctl --user daemon-reload

build:  ## 构建 sdist + wheel
	uv build

publish:  ## 上传到 PyPI（需先 build）
	uv build
	uvx twine upload dist/*

clean:  ## 清理构建产物
	rm -rf build dist .pytest_cache .ruff_cache src/*.egg-info
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
