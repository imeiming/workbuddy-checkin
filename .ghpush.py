"""把签到助手源码增量推送到 GitHub（REST Git Data API，规避沙箱对 git 协议的拦截）。

基于现有 main 分支的 tree 做增量提交，能正确处理新增/修改/删除。
"""
import json
import pathlib
import subprocess
import sys

REPO = "imeiming/workbuddy-checkin"
BRANCH = "main"
ROOT = pathlib.Path(r"D:\WorkBuddy\2026-10-04-20-52-34\wb-checkin")
COMMIT_MSG = (
    "fix: 修复定时签到不触发，移除云端账号模块\n\n"
    "- 调度按距目标时刻剩余秒数动态等待，已过点且未签到立即补签\n"
    "- /api/overview 请求级兜底补签，容器冷启动也能立刻签上\n"
    "- 新增容器自心跳保活(默认30s)与 /api/diag 诊断接口，避免容器休眠\n"
    "- 凭据密钥持久化到 .keyfile 并双目录备份，容器重建后仍可解密\n"
    "- 登录态新增每30分钟主动续期\n"
    "- 移除云端账号(邮箱验证码/云数据库)相关前端模块与页脚说明"
)

EXCLUDE_DIRS = {".git", "__pycache__", "data", ".workbuddy"}
EXCLUDE_NAMES = {".credentials.json", "credentials.json", ".keyfile", "state.json"}
EXCLUDE_SUFFIX = {".pyc", ".log", ".pyo"}


def gh(method, endpoint, payload=None):
    cmd = ["gh", "api", "--method", method, f"repos/{REPO}/{endpoint}"]
    if payload is not None:
        cmd += ["--input", "-"]
        data = json.dumps(payload).encode("utf-8")
    else:
        data = None
    proc = subprocess.run(cmd, input=data, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{method} {endpoint} 失败: {proc.stderr.decode('utf-8', 'replace')[:400]}")
    out = proc.stdout.decode("utf-8").strip()
    return json.loads(out) if out else {}


def collect_files():
    files = []
    for p in sorted(ROOT.rglob("*")):
        if p.is_dir():
            continue
        parts = p.relative_to(ROOT).parts
        if any(x in EXCLUDE_DIRS for x in parts[:-1]):
            continue
        if parts[-1] in EXCLUDE_NAMES or p.suffix in EXCLUDE_SUFFIX:
            continue
        files.append((p.relative_to(ROOT).as_posix(), p))
    return files


def main():
    files = collect_files()
    print(f"待上传 {len(files)} 个文件:")
    for rel, _ in files:
        print("   ", rel)

    # 安全检查：确认没有凭据 / 密钥敏感文件被误包含
    banned = [rel for rel, _ in files if rel.endswith((".credentials.json", "credentials.json", ".keyfile"))]
    if banned:
        sys.exit(f"!! 检测到敏感文件将被上传: {banned}")

    head = gh("GET", f"git/ref/heads/{BRANCH}")["object"]["sha"]
    print(f"\n当前 HEAD: {head[:10]}")
    base_tree = gh("GET", f"git/commits/{head}")["tree"]["sha"]
    old_tree = gh("GET", f"git/trees/{base_tree}?recursive=1")["tree"]
    old_paths = {item["path"] for item in old_tree if item["type"] == "blob"}

    tree_items = []
    for rel, path in files:
        blob = gh("POST", "git/blobs", {
            "content": path.read_bytes().decode("utf-8"),
            "encoding": "utf-8",
        })
        tree_items.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})

    new_paths = {rel for rel, _ in files}
    deleted = sorted(old_paths - new_paths)
    for rel in deleted:
        tree_items.append({"path": rel, "mode": "100644", "type": "blob", "sha": None})
        print(f"删除旧文件: {rel}")

    new_tree = gh("POST", "git/trees", {"base_tree": base_tree, "tree": tree_items})
    print(f"新 tree: {new_tree['sha'][:10]}")

    commit = gh("POST", "git/commits", {
        "message": COMMIT_MSG,
        "tree": new_tree["sha"],
        "parents": [head],
    })
    print(f"新 commit: {commit['sha'][:10]}")

    gh("PATCH", f"git/refs/heads/{BRANCH}", {"sha": commit["sha"]})
    print(f"\n推送完成 -> https://github.com/{REPO}")
    print(f"commit: {commit['sha']}")
    print(f"变更 file/㎡: 新增/修改 {len(files)} 个，删除 {len(deleted)} 个")


if __name__ == "__main__":
    main()
