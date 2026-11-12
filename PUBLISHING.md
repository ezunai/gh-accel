# 发布流程

本项目同时发布到 **GitHub**（主仓库）、**Gitee**（国内镜像）、**PyPI**。

## 仓库结构

| 远程 | 地址 | 作用 |
| --- | --- | --- |
| `origin` | `https://github.com/osmos/gh-accel.git` | 主仓库；CI、Release、Issue 都以它为准 |
| `gitee` | `https://gitee.com/osmos/gh-accel.git` | 国内镜像，clone 与安装脚本走这里 |

## 首次发布

### 1. 在平台创建空仓库

```bash
# GitHub（不要初始化 README/LICENSE/.gitignore，保留历史）
gh repo create osmos/gh-accel --public --description "本地 GitHub 下载加速代理" --source=. --remote=origin --push

# Gitee
# 在 https://gitee.com/osmos/gh-accel 新建仓库（同样选「空仓库」），然后：
git push -u gitee main
```

两个仓库都要设为公开。

### 2. 打 tag 并推送

```bash
git tag -a v1.0.0 -m "release: v1.0.0"
git push origin main --tags
git push gitee main --tags
```

### 3. GitHub Release

```bash
gh release create v1.0.0 \
  --title "v1.0.0" \
  --notes-file CHANGELOG.md
```

Gitee 的 Release 可在网页端手动创建，或用 Gitee API。

### 4. 发布到 PyPI

CI 已配好：推送 `v*` tag 时自动构建并发布（`.github/workflows/ci.yml` 的
`publish` job）。需要先配置凭据，二选一：

**Trusted Publisher（推荐，无需长期 token）**

在 PyPI 侧为 `osmos/gh-accel` 添加 Trusted Publisher，填：

- Owner: `osmos`
- Repository: `gh-accel`
- Workflow: `ci.yml`
- Environment: `pypi`（若未创建 environment 则留空）

**API Token**

在仓库 Settings → Secrets → Actions 添加 `PYPI_API_TOKEN`，并把 CI 里
`publish` job 的 `uses: pypa/gh-action-pypi-publish@release/v1` 改为：

```yaml
      - name: 发布到 PyPI
        uses: pypa/gh-action-pypi-publish@release/v1
        with:
          password: ${{ secrets.PYPI_API_TOKEN }}
```

手动发布（不走 CI）：

```bash
make build
uvx twine upload dist/*
```

## 后续版本发布

版本号是**单一来源**：`src/gh_accel/__init__.py` 的 `__version__`。
`pyproject.toml` 里写的是 `dynamic = ["version"]`，构建时自动读取。

```bash
# 1. 改版本号
sed -i 's/__version__ = ".*"/__version__ = "1.1.0"/' src/gh_accel/__init__.py

# 2. 更新 CHANGELOG
$EDITOR CHANGELOG.md

# 3. 提交
git add -A && git commit -m "release: v1.1.0"

# 4. 打 tag 并推送（推送 tag 触发 PyPI 发布）
git tag -a v1.1.0 -m "release: v1.1.0"
git push origin main --tags
git push gitee main --tags

# 5. GitHub Release
gh release create v1.1.0 --title "v1.1.0" --notes-file CHANGELOG.md
```

## 保持 Gitee 镜像同步

Gitee 没有内置的 GitHub 镜像同步功能（有依赖 CI 额度的「镜像仓库」功能）。
两个选择：

**手动同步**（最省事）

```bash
git push gitee main --tags
```

**自动同步**（GitHub Actions → Gitee API）

在仓库 Secrets 里加 `GITEE_TOKEN`（Gitee 私人令牌，需要 `projects` 权限），
然后加一个 workflow：

```yaml
name: Sync to Gitee
on:
  push:
    branches: [main]
    tags: ["v*"]
jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: 推送到 Gitee
        env:
          GITEE_TOKEN: ${{ secrets.GITEE_TOKEN }}
        run: |
          git remote add gitee https://osmos:$GITEE_TOKEN@gitee.com/osmos/gh-accel.git
          git push gitee main --tags
```

## 校验发布物

```bash
make check        # 测试 + 静态检查
make build        # 构建 sdist + wheel
uvx twine check dist/*
```

## 给发行版打标签

打 tag 前确认：

- `src/gh_accel/__init__.py` 的版本号已更新
- `CHANGELOG.md` 里有对应版本的条目与日期
- `pyproject.toml` 里的仓库 URL 指向正确账号
- `make check` 与 `make build` 均通过
- `twine check dist/*` 通过
