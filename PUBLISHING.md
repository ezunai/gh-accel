# 发布流程

本项目**只发布到 GitHub**：源码仓库 + Release。不发布到 PyPI、Gitee 或其它包仓库。

## 仓库结构

| 远程 | 地址 | 作用 |
| --- | --- | --- |
| `origin` | `https://github.com/ezunai/gh-accel.git` | 主仓库；CI、Release、Issue 都以它为准 |

## 本机的网络约束

这台机器上 `github.com:443` **不可达**（TCP 建连失败），但：

| 通道 | 状态 | 用途 |
| --- | --- | --- |
| `github.com:22`（SSH） | 通 | `git push`（需先注册 SSH 公钥） |
| `api.github.com:443` | 通 | 建仓库、加公钥、建 Release |

所以推送走 SSH，仓库/Release 的操作走 API。

## 首次发布

### 1. 创建仓库

```bash
TOKEN=$(printf "protocol=https\nhost=github.com\n\n" | git credential fill | sed -n 's/^password=//p')

curl -s -X POST --noproxy '*' \
  -H "Authorization: Bearer $TOKEN" \
  https://api.github.com/user/repos \
  -d '{"name":"gh-accel","description":"本地 GitHub 下载加速代理","private":false}'
```

### 2. 注册 SSH 公钥

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519 -C "gh-accel@$(hostname)"
curl -s -X POST --noproxy '*' \
  -H "Authorization: Bearer $TOKEN" \
  https://api.github.com/user/keys \
  -d "{\"title\":\"$(hostname)\",\"key\":\"$(cat ~/.ssh/id_ed25519.pub)\"}"
```

### 3. 推送

```bash
git remote set-url origin git@github.com:ezunai/gh-accel.git
git push -u origin main
git push origin --tags
```

### 4. GitHub Release

```bash
gh release create v1.1.0 --title "v1.1.0" --notes-file CHANGELOG.md
```

本机没有 `gh` 时，直接用 API：

```bash
BODY=$(python3 - <<'PY'
import pathlib, json
s = pathlib.Path("CHANGELOG.md").read_text(encoding="utf-8")
start = s.index("## [1.1.0]")
end = s.index("## [1.0.0]")
print(json.dumps("\n".join(s[start:end].strip().splitlines()[1:]).strip(),
                 ensure_ascii=False))
PY
)

curl -s -X POST --noproxy '*' \
  -H "Authorization: Bearer $TOKEN" \
  https://api.github.com/repos/ezunai/gh-accel/releases \
  -d "{\"tag_name\":\"v1.1.0\",\"name\":\"v1.1.0\",\"body\":$BODY}"
```

## 后续版本发布

版本号是**单一来源**：`src/gh_accel/__init__.py` 的 `__version__`。
`pyproject.toml` 里写的是 `dynamic = ["version"]`，构建时自动读取。

```bash
# 1. 改版本号
sed -i 's/__version__ = ".*"/__version__ = "1.2.0"/' src/gh_accel/__init__.py

# 2. 更新 CHANGELOG
$EDITOR CHANGELOG.md

# 3. 提交
git add -A && git commit -m "release: v1.2.0"

# 4. 打 tag 并推送
git tag -a v1.2.0 -m "release: v1.2.0"
git push origin main --tags

# 5. GitHub Release
gh release create v1.2.0 --title "v1.2.0" --notes-file CHANGELOG.md
```

## 校验构建产物

构建分发包只是为了验证 `pyproject.toml` 的元数据和打包内容没问题，**产物
不发布到任何包仓库**。

```bash
make check        # 测试 + 静态检查
make build        # 构建 sdist + wheel
uvx twine check dist/*   # 只做元数据校验，不上传
```

## 给发行版打标签

打 tag 前确认：

- `src/gh_accel/__init__.py` 的版本号已更新
- `CHANGELOG.md` 里有对应版本的条目与日期
- `pyproject.toml` 里的仓库 URL 指向正确账号
- `make check` 与 `make build` 均通过
- `twine check dist/*` 通过（仅校验，不上传）
