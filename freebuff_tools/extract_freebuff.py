#!/usr/bin/env python3
"""
Freebuff authToken 提取工具（类似 cline 的提取）

流程（与官方 CLI 一致）：
  1. 生成设备指纹 fingerprintId
  2. POST /api/auth/cli/code → 拿 Google 登录 URL + fingerprintHash + expiresAt
  3. 用浏览器打开登录 URL，登录 Google 账号（可人工或自动化）
  4. 轮询 GET /api/auth/cli/status → 成功拿到 user（含 authToken）
  5. authToken 保存到本地，之后直接作为 Bearer 调模型 API

用法：
  python3 extract_freebuff.py login           # 开始登录，打印 URL 并轮询
  python3 extract_freebuff.py show            # 显示已保存的凭证
  python3 extract_freebuff.py session         # 测试开 session（POST）
  python3 extract_freebuff.py chat [消息]     # 发一条消息测试模型 API
  python3 extract_freebuff.py quota           # 查用量 /api/v1/usage

环境变量：
  FREEBUFF_TOKEN   手动指定 authToken（跳过 credentials 文件）
"""

import argparse
import base64
import json
import os
import secrets
import sys
import time
import uuid
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = "https://www.codebuff.com"
CRED_FILE = Path(__file__).resolve().parent / "freebuff_credentials.json"
POLL_INTERVAL = 5          # 秒，官方 CLI 用 5s
POLL_TIMEOUT = 5 * 60      # 秒，官方 5 分钟
REQUEST_TIMEOUT = 30

MODEL_DEFAULT = "deepseek/deepseek-v4-flash"


# ---------------------------------------------------------------------------
# Telegram helper（可选：登录交互走 TG，让 Actions 日志不暴露 URL/token）
# ---------------------------------------------------------------------------

def _tg_send(bot_token: str, chat_id: str, text: str) -> bool:
    """通过 TG bot 发一条消息。失败返回 False（不抛异常）。"""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return bool(data.get("ok"))
    except Exception as e:
        print(f"⚠️ TG 发送失败: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# HTTP helpers（标准库 urllib，无第三方依赖）
# ---------------------------------------------------------------------------

def _http(method: str, path: str, body=None, headers=None, query=None, timeout=REQUEST_TIMEOUT):
    url = BASE_URL + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = None
    hdrs = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None, resp.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw) if raw else None
        except Exception:
            parsed = raw.decode(errors="replace")[:500]
        return e.code, parsed, e.headers
    except Exception as e:
        return None, {"error": str(e)}, None


def get_token():
    tok = os.environ.get("FREEBUFF_TOKEN")
    if tok:
        return tok
    if CRED_FILE.exists():
        cred = json.loads(CRED_FILE.read_text())
        tok = cred.get("authToken")
        if not tok:
            tok = cred.get("default", {}).get("authToken")
        return tok
    return None


def save_credentials(user: dict):
    # 保留已有字段（可能含其他 profile），合并写入
    existing = {}
    if CRED_FILE.exists():
        try:
            existing = json.loads(CRED_FILE.read_text())
        except Exception:
            pass
    existing["default"] = user
    CRED_FILE.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    print(f"💾 凭证已保存 → {CRED_FILE}")


# ---------------------------------------------------------------------------
# 各功能
# ---------------------------------------------------------------------------

def gen_fingerprint():
    """官方 legacy fallback 格式：codebuff-cli-<8位随机>"""
    rand = base64.urlsafe_b64encode(secrets.token_bytes(6)).decode().rstrip("=")[:8]
    return f"codebuff-cli-{rand}"


def cmd_login(args):
    fingerprint_id = args.fingerprint or gen_fingerprint()
    print(f"🔑 fingerprintId: {fingerprint_id}")

    status, data, _ = _http("POST", "/api/auth/cli/code", {"fingerprintId": fingerprint_id})
    if status != 200 or not data:
        print(f"❌ 请求登录 URL 失败: HTTP {status} {data}")
        sys.exit(1)

    login_url = data["loginUrl"]
    fingerprint_hash = data["fingerprintHash"]
    expires_at = data["expiresAt"]

    # TG 模式：URL 只发 TG，Actions 日志里绝不出现完整 URL/token
    tg_token = os.environ.get("TG_BOT_TOKEN")
    tg_chat = os.environ.get("TG_CHAT_ID")
    use_tg = bool(tg_token and tg_chat)
    if use_tg:
        ok = _tg_send(tg_token, tg_chat,
                      f"🔑 Freebuff 登录授权（{fingerprint_id}）\n\n"
                      f"请 5 分钟内用浏览器打开并登录：\n{login_url}\n\n"
                      f"登录完成后我会自动拿到 token 并发回给你。")
        if not ok:
            print("⚠️ TG_BOT_TOKEN/TG_CHAT_ID 已设置但发送失败，退回打印 URL")
            use_tg = False
        else:
            print("📨 登录 URL 已发送到 TG，等待登录（每 5 秒轮询）…")
    if not use_tg:
        print(f"🌐 请用浏览器打开以下地址并登录 Google 账号（5 分钟内完成）:\n\n  {login_url}\n")
        print("   提示: 也可以交给自动化浏览器处理，脚本每 5 秒轮询一次状态。\n")

    # 可选自动打开浏览器
    if args.open:
        try:
            import webbrowser
            webbrowser.open(login_url)
            print("   (已在本地打开浏览器)")
        except Exception as e:
            print(f"   (自动打开失败: {e})")

    start = time.time()
    attempts = 0
    while time.time() - start < POLL_TIMEOUT:
        attempts += 1
        status, data, _ = _http(
            "GET", "/api/auth/cli/status",
            query={
                "fingerprintId": fingerprint_id,
                "fingerprintHash": fingerprint_hash,
                "expiresAt": expires_at,
            },
        )
        if status == 200 and data and data.get("user"):
            user = data["user"]
            if not user.get("authToken"):
                print(f"⚠️ 返回 user 但没有 authToken: {json.dumps(user)[:300]}")
                sys.exit(1)
            print(f"✅ 登录成功！（第 {attempts} 次轮询，{int(time.time()-start)}s）")
            print(f"   user.id:    {user.get('id')}")
            print(f"   email:      {user.get('email')}")
            print(f"   credits:    {user.get('credits')}")
            print(f"   authToken:  {user['authToken'][:20]}...（已截断）")
            save_credentials(user)
            # TG 模式：token 完整值只发 TG，日志保持截断
            if use_tg:
                _tg_send(tg_token, tg_chat,
                         f"✅ Freebuff 登录成功！\n\n"
                         f"email: {user.get('email')}\n"
                         f"id: {user.get('id')}\n"
                         f"credits: {user.get('credits')}\n\n"
                         f"authToken（完整）：\n`{user['authToken']}`\n\n"
                         f"请在 CF Worker 的 Secrets 里设置 FREEBUFF_TOKEN。")
            return user
        elif status == 401:
            print(f"   [{int(time.time()-start)}s] 尚未登录（401），继续等待…")
        elif status == 400:
            print(f"❌ 登录请求已失效: {data}")
            sys.exit(1)
        else:
            print(f"   [{int(time.time()-start)}s] 状态 {status}: {str(data)[:120]}")
        time.sleep(POLL_INTERVAL)

    print("⏰ 等待登录超时（5 分钟），请重试。")
    sys.exit(1)


def cmd_show(_args):
    tok = get_token()
    if not tok:
        print("❌ 未找到 authToken（先运行 login 或设置 FREEBUFF_TOKEN）")
        sys.exit(1)
    if CRED_FILE.exists():
        cred = json.loads(CRED_FILE.read_text())
        user = cred.get("default", {})
        print("📋 已保存凭证:")
        for k, v in user.items():
            if k == "authToken":
                print(f"   authToken: {v[:24]}...{v[-6:]}（长度 {len(v)}）")
            else:
                print(f"   {k}: {v}")
    print(f"\n🔑 token: {tok[:24]}...{tok[-6:]}（长度 {len(tok)}）")
    # 顺带验证
    status, data, _ = _http("GET", "/api/v1/freebuff/session",
                            headers={"Authorization": f"Bearer {tok}"})
    print(f"🔍 验证 GET /session → HTTP {status}: {str(data)[:200]}")


def cmd_session(args):
    tok = get_token()
    if not tok:
        print("❌ 未找到 authToken")
        sys.exit(1)
    headers = {"Authorization": f"Bearer {tok}"}
    model = args.model or MODEL_DEFAULT
    if args.post:
        headers["x-freebuff-model"] = model
        status, data, _ = _http("POST", "/api/v1/freebuff/session", headers=headers)
    else:
        status, data, _ = _http("GET", "/api/v1/freebuff/session", headers=headers)
    print(f"📡 HTTP {status}")
    print(json.dumps(data, indent=2, ensure_ascii=False) if data else "(空响应)")
    return data


def cmd_start_run(args):
    """向 /api/v1/agent-runs 发起 START，拿 runId"""
    tok = get_token()
    if not tok:
        print("❌ 未找到 authToken")
        sys.exit(1)
    headers = {"Authorization": f"Bearer {tok}"}
    body = {
        "action": "START",
        "agentId": args.agent,
        "ancestorRunIds": [],
    }
    print(f"📡 POST /api/v1/agent-runs (agent={args.agent})…")
    status, data, _ = _http("POST", "/api/v1/agent-runs", body, headers)
    print(f"→ HTTP {status}")
    print(json.dumps(data, indent=2, ensure_ascii=False) if data else "(空响应)")
    if isinstance(data, dict) and data.get("runId"):
        print(f"\n✅ runId = {data['runId']}")
    return data


def cmd_chat(args):
    tok = get_token()
    if not tok:
        print("❌ 未找到 authToken")
        sys.exit(1)

    # 1) 先确保有 active session（官方门控：无 session → 428 waiting_room_required）
    model = args.model or MODEL_DEFAULT
    headers = {"Authorization": f"Bearer {tok}"}
    status, sess, _ = _http("POST", "/api/v1/freebuff/session",
                            headers={**headers, "x-freebuff-model": model})
    print(f"📡 POST /session → HTTP {status}")
    instance_id = None
    if isinstance(sess, dict) and sess.get("status") == "active":
        instance_id = sess.get("instanceId")
        print(f"   ✅ session active, instanceId={instance_id}, "
              f"model={sess.get('model')}, expires_at={sess.get('expires_at')}")
    else:
        print(f"   ⚠️ {str(sess)[:300]}")
        if not args.force:
            print("   （使用 --force 仍尝试直发 chat 看报错）")
            sys.exit(1)

    # 1.5) 先 START 一个 run，拿真实 runId（chat 校验 run_id 存在）
    run_id = args.run_id
    if not run_id:
        s, sr, _ = _http("POST", "/api/v1/agent-runs",
                         {"action": "START", "agentId": args.agent,
                          "ancestorRunIds": []}, headers)
        if isinstance(sr, dict) and sr.get("runId"):
            run_id = sr["runId"]
            print(f"   📡 START run → HTTP {s} runId={run_id}")
        else:
            print(f"   ⚠️ START run 失败 HTTP {s}: {str(sr)[:200]}")
            if not args.force:
                sys.exit(1)

    # 2) 调 chat/completions
    chat_headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    if instance_id:
        chat_headers["x-freebuff-instance-id"] = instance_id
    body = {
        "model": model,
        "messages": [{"role": "user", "content": args.message or "Say hi in one short sentence."}],
        "stream": False,
        "codebuff_metadata": {
            "run_id": run_id or f"run-{uuid.uuid4().hex[:12]}",
            "client_id": f"cli-{uuid.uuid4().hex[:12]}",
            "cost_mode": "free",
            **({"freebuff_instance_id": instance_id} if instance_id else {}),
        },
        "provider": {"allow_fallbacks": False},
    }
    print(f"📡 POST /api/v1/chat/completions (model={model}, stream=False, run_id={run_id})…")
    status, data, _ = _http("POST", "/api/v1/chat/completions", body, chat_headers)
    print(f"→ HTTP {status}")
    if status == 200 and isinstance(data, dict):
        msg = data.get("choices", [{}])[0].get("message", {})
        print(f"✅ 回复: {msg.get('content', '')[:500]}")
        if msg.get("reasoning_content"):
            print(f"🧠 reasoning: {msg['reasoning_content'][:200]}")
        print(f"   usage: {data.get('usage')}")
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False)[:1500] if data else "(空响应)")


def cmd_quota(_args):
    tok = get_token()
    if not tok:
        print("❌ 未找到 authToken")
        sys.exit(1)
    status, data, _ = _http("POST", "/api/v1/usage", {"fingerprintId": "cli-usage"},
                            headers={"Authorization": f"Bearer {tok}"})
    print(f"📡 HTTP {status}")
    print(json.dumps(data, indent=2, ensure_ascii=False) if data else "(空响应)")


# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Freebuff authToken 提取工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="开始登录（生成 URL + 轮询拿 token）")
    p_login.add_argument("--fingerprint", help="指定 fingerprintId（默认自动生成）")
    p_login.add_argument("--open", action="store_true", help="自动打开系统浏览器")

    sub.add_parser("show", help="显示已保存凭证并验证")
    p_sess = sub.add_parser("session", help="开/查 session")
    p_sess.add_argument("--model", default=MODEL_DEFAULT)
    p_sess.add_argument("--post", action="store_true", help="POST 开 session（默认 GET）")

    p_chat = sub.add_parser("chat", help="发一条消息测试模型 API")
    p_chat.add_argument("message", nargs="?", default=None)
    p_chat.add_argument("--model", default=MODEL_DEFAULT)
    p_chat.add_argument("--agent", default="base2", help="START run 用的 agentId（默认 base2）")
    p_chat.add_argument("--run-id", default=None, help="指定 run_id（默认 START 一个）")
    p_chat.add_argument("--force", action="store_true", help="session/run 失败也直发 chat")

    p_run = sub.add_parser("startrun", help="向 /api/v1/agent-runs 发 START 拿 runId")
    p_run.add_argument("--agent", default="base2", help="agentId（默认 base2）")

    sub.add_parser("quota", help="查用量")

    args = p.parse_args()
    {
        "login": cmd_login,
        "show": cmd_show,
        "session": cmd_session,
        "chat": cmd_chat,
        "startrun": cmd_start_run,
        "quota": cmd_quota,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
