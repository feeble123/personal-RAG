"""单元 1 真实环境验收：连你本机真实服务 + 真实知识库 + 真实大模型。

和 verify_external_api.py 的分工：
- verify_external_api.py  → 临时库 + 假 embedding/大模型，只验证「代码链路通不通」
- 本脚本（live）          → 连你本机正在跑的服务、真实的 rag 库、真实的 DeepSeek，
                            所以问出来的答案是**真的**，问题可以随便问

为什么需要它：外部接口必须先用 X-API-Key 打开（.env 没配 = 接口返回 503），
内部问答能用不代表外部接口能用，本脚本就是把这条路真跑一遍。

用法（在 backend 目录，项目 venv）：
    # 1) .env 里加一行（先自己生成一个长随机串，见脚本报错时的提示）
    #    EXTERNAL_API_KEY=<长随机串>
    # 2) 重启后端（start.bat）
    # 3) 跑验收
    .venv\\Scripts\\python.exe -X utf8 scripts\\verify_external_api_live.py

    # 换问题（换成你关心的水利问题）
    ... --question "土石坝防渗体有哪些形式"

    # 连续问答，像真人一样一直问
    ... --interactive

    # 指定知识库 / 返回条数
    ... --question "..." --kb-id 12 --top-k 8
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parents[1]  # backend/
# 直接跑脚本时 Python 只把 scripts/ 放进 sys.path，把 backend/ 补上才能 import app.*
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

DEFAULT_URL = "http://127.0.0.1:8000"
DEFAULT_QUESTION = "明渠均匀流的形成条件是什么"

# 证据等级的人话解释（对外契约里的 evidence_level 字段）
EVIDENCE_TEXT = {
    "sufficient": "证据充分（可以放心采信）",
    "weak": "证据偏弱（建议人工复核）",
    "none": "没检到相关资料（模型会说明资料不足，别硬信）",
}


def _setup_stdout() -> None:
    """Windows 控制台中文不乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _info(msg: str) -> None:
    print(f"  [--]   {msg}")


def _die(msg: str, code: int = 1) -> None:
    print(f"\n  [FAIL] {msg}")
    sys.exit(code)


def _read_key() -> str:
    """从 .env（经 settings）读取外部 API Key，不回显。"""
    from app.core.config import settings

    key = (settings.external_api_key or "").strip()
    if key:
        return key

    _die(
        "对外 API 未启用：backend/.env 里没有配置 EXTERNAL_API_KEY。\n"
        "         修复步骤（两步）：\n"
        "         1) 生成一个长随机串，复制输出：\n"
        "            .venv\\Scripts\\python.exe -c \"import secrets;print(secrets.token_urlsafe(48))\"\n"
        "         2) 在 backend/.env 末尾加一行（等号后面粘贴刚才那串）：\n"
        "            EXTERNAL_API_KEY=粘贴到这里\n"
        "         然后重启后端（start.bat），再跑本脚本。",
        2,
    )


def _check_health(client: httpx.Client, base: str) -> None:
    """服务是否活着；顺便区分「服务没起」和「数据库连不上」。"""
    try:
        r = client.get(f"{base}/api/health", timeout=5)
    except httpx.ConnectError:
        _die(
            f"连不上 {base} —— 后端服务没启动。\n"
            "         先在另一个窗口运行 start.bat（或双击项目根目录的 start.bat），\n"
            "         看到「Uvicorn running」后再回来跑本脚本。"
        )
    except httpx.HTTPError as exc:
        _die(f"健康检查请求失败：{exc}")

    if r.status_code == 503:
        _die(
            "后端起来了，但数据库连不上（/api/health 返回 503）。\n"
            "         检查 PostgreSQL 服务是否在运行（本机 5432），或看后端窗口的报错日志。"
        )
    if r.status_code != 200:
        _die(f"健康检查异常：HTTP {r.status_code} {r.text[:200]}")


def _list_kbs(client: httpx.Client, base: str, key: str) -> list[dict]:
    r = client.get(f"{base}/api/v1/knowledge-bases", headers={"X-API-Key": key}, timeout=30)
    if r.status_code == 401:
        _die(
            "X-API-Key 被拒绝（401）—— 多半是改了 .env 但没重启后端，\n"
            "         或者脚本读到的 key 和服务加载的不是同一个。重启后端再试。"
        )
    if r.status_code != 200:
        _die(f"知识库列表失败：HTTP {r.status_code} {r.text[:300]}")

    items = r.json().get("items", [])
    if items:
        print(f"  {'ID':>4}  {'文档':>5}  {'切片':>7}  名称")
        print(f"  {'-' * 4}  {'-' * 5}  {'-' * 7}  {'-' * 30}")
        for kb in items:
            print(
                f"  {kb['id']:>4}  {kb.get('doc_count') or 0:>5}  "
                f"{kb.get('chunk_count') or 0:>7}  {kb['name']}"
            )
    return items


def _auth_negative(client: httpx.Client, base: str) -> None:
    """顺手确认错误 key 被拒（安全底线）。"""
    r = client.post(
        f"{base}/api/v1/search",
        json={"query": "测试"},
        headers={"X-API-Key": "definitely-wrong-key"},
        timeout=30,
    )
    if r.status_code == 401:
        _ok("错误 key 被拒绝（401）")
    elif r.status_code == 429:
        _info("错误 key 撞上限流（429），跳过这项检查")
    else:
        _die(f"错误 key 竟然返回 {r.status_code}，鉴权有问题：{r.text[:200]}")


def _print_hits(results: list[dict], show_text: int = 90) -> None:
    for i, item in enumerate(results, 1):
        src = item.get("source", {})
        loc = []
        if src.get("page"):
            loc.append(f"第{src['page']}页")
        if src.get("section"):
            loc.append(str(src["section"]))
        if src.get("clause_no"):
            loc.append(f"条款{src['clause_no']}")
        print(
            f"  [{i}] {src.get('document_name')} · {' · '.join(loc) or '无定位'}"
            f" · 相关度 {item.get('score', 0):.3f}"
        )
        text = (item.get("text") or "").replace("\n", " ").strip()
        print(f"      {text[:show_text]}{'…' if len(text) > show_text else ''}")


def do_search(
    client: httpx.Client, base: str, key: str, query: str, kb_id: int | None, top_k: int
) -> dict:
    body = {"query": query, "top_k": top_k}
    if kb_id is not None:
        body["kb_id"] = kb_id

    t0 = time.perf_counter()
    r = client.post(f"{base}/api/v1/search", json=body, headers={"X-API-Key": key}, timeout=180)
    elapsed = time.perf_counter() - t0

    if r.status_code == 429:
        return {"rate_limited": True, "elapsed": elapsed}
    if r.status_code != 200:
        _die(f"检索失败：HTTP {r.status_code} {r.text[:300]}")

    data = r.json()
    results = data.get("results", [])
    print(f"\n【检索】{query}")
    print(f"  用时 {elapsed:.2f} 秒，命中 {len(results)} 条")
    if results:
        _print_hits(results)
    else:
        _info("一条都没命中——知识库里可能没有相关内容，或问题用词差太远")
    return data


def do_chat(
    client: httpx.Client, base: str, key: str, question: str, kb_id: int | None, top_k: int
) -> dict | None:
    body = {"question": question, "top_k": top_k}
    if kb_id is not None:
        body["kb_id"] = kb_id

    print(f"\n【问答】{question}")
    print("  正在等大模型生成（真实模型通常 10~40 秒，请耐心等）...", flush=True)

    t0 = time.perf_counter()
    r = client.post(f"{base}/api/v1/chat", json=body, headers={"X-API-Key": key}, timeout=180)
    elapsed = time.perf_counter() - t0

    if r.status_code == 429:
        _info("问答触发限流（默认 20 次/分钟），等一会儿再问")
        return None
    if r.status_code == 502:
        _die(f"大模型生成失败：{r.text[:300]}\n         检查 .env 的 DEEPSEEK_API_KEY / 余额 / 网络。")
    if r.status_code != 200:
        _die(f"问答失败：HTTP {r.status_code} {r.text[:300]}")

    data = r.json()
    level = data.get("evidence_level")
    usage = data.get("usage") or {}

    print(f"  用时 {elapsed:.1f} 秒")
    print("\n  ┌─ 回答 " + "─" * 50)
    for line in (data.get("answer") or "(空)").splitlines():
        print(f"  │ {line}")
    print("  └" + "─" * 56)

    cites = data.get("citations", [])
    print(f"\n  证据等级：{EVIDENCE_TEXT.get(level, level)}")
    print(f"  引用 {len(cites)} 条：")
    if cites:
        _print_hits(cites, show_text=70)
    print(
        f"\n  本次消耗：输入 {usage.get('input_tokens') or '?'} tokens / "
        f"输出 {usage.get('output_tokens') or '?'} tokens"
    )
    return data


def interactive(client: httpx.Client, base: str, key: str, kb_id: int | None, top_k: int) -> None:
    print("\n" + "=" * 64)
    print("连续问答模式：直接输入问题回车；输入 q 或直接空回车退出")
    print("=" * 64)
    while True:
        try:
            question = input("\n你的问题> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not question or question.lower() in {"q", "quit", "exit"}:
            return
        do_chat(client, base, key, question, kb_id, top_k)


def main() -> int:
    parser = argparse.ArgumentParser(description="对外检索/问答 API 真实环境验收")
    parser.add_argument("--base-url", default=DEFAULT_URL, help=f"服务地址（默认 {DEFAULT_URL}）")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="测试问题")
    parser.add_argument("--kb-id", type=int, default=None, help="限定知识库 id（默认跨全库）")
    parser.add_argument("--top-k", type=int, default=5, help="返回/送入生成的条数（默认 5）")
    parser.add_argument("--interactive", "-i", action="store_true", help="验收跑完后进入连续问答")
    parser.add_argument("--skip-chat", action="store_true", help="只测检索，不调大模型（省费用）")
    parser.add_argument("--start", action="store_true", help="服务没起时自动拉起 uvicorn")
    parser.add_argument(
        "--use-proxy",
        action="store_true",
        help="允许走系统代理环境变量（本机服务一般不要开；开了会返回 502）",
    )
    args = parser.parse_args()

    _setup_stdout()
    base = args.base_url.rstrip("/")
    key = _read_key()  # 回显只给前后几位，绝不打印完整 key

    print("=" * 64)
    print("单元 1 真实环境验收：对外检索 + 问答 API")
    print("=" * 64)
    print(f"服务地址：{base}")
    print(f"API Key ：{key[:4]}…{key[-4:]}（共 {len(key)} 位）")

    proc: subprocess.Popen | None = None
    # trust_env=False：本机调用绝不能走系统代理，否则会莫名收到 502
    with httpx.Client(trust_env=args.use_proxy) as client:
        # 服务没起且开了 --start：自动拉一个（用完关掉）
        try:
            client.get(f"{base}/api/health", timeout=3)
        except httpx.ConnectError:
            if not args.start:
                _check_health(client, base)  # 走统一的报错提示后退出
            print("\n  服务未启动，正在拉起 uvicorn（--start）...")
            log_path = BASE_DIR / "data" / "verify_live_server.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = open(log_path, "w", encoding="utf-8")
            from urllib.parse import urlparse

            parsed = urlparse(base)
            proc = subprocess.Popen(
                [sys.executable, "-X", "utf8", "-m", "uvicorn", "app.main:app",
                 "--host", parsed.hostname or "127.0.0.1",
                 "--port", str(parsed.port or 8000)],
                cwd=str(BASE_DIR),
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            deadline = time.time() + 90
            while time.time() < deadline:
                if proc.poll() is not None:
                    tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                    _die(f"服务启动失败（code={proc.returncode}）\n--- 日志尾部 ---\n{tail}")
                try:
                    if client.get(f"{base}/api/health", timeout=2).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
            log_file.close()

        try:
            _check_health(client, base)
            _ok("服务健康检查 200")

            kbs = _list_kbs(client, base, key)
            _ok(f"知识库列表可用（{len(kbs)} 个库）")

            if args.kb_id is not None:
                if not any(kb["id"] == args.kb_id for kb in kbs):
                    _die(f"kb_id={args.kb_id} 不存在，请从上表选一个 id")
                _ok(f"指定知识库 kb_id={args.kb_id} 存在")

            _auth_negative(client, base)

            do_search(client, base, key, args.question, args.kb_id, args.top_k)

            if args.skip_chat:
                print("\n  （--skip-chat：跳过问答，不消耗大模型费用）")
            else:
                do_chat(client, base, key, args.question, args.kb_id, args.top_k)

            if args.interactive:
                interactive(client, base, key, args.kb_id, args.top_k)

            print("\n" + "=" * 64)
            print("验收通过：对外检索 + 问答接口在你的真实知识库上可用")
            print("=" * 64)
            return 0
        finally:
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                print("\n  已关闭本次 --start 拉起的服务")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
