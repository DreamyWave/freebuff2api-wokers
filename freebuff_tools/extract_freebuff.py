#!/usr/bin/env python3
"""Freebuff 一键获取 authToken 脚本（授权码轮询流程，交互方式对齐 cline_oauth.py）。

用法：
  python3 extract_freebuff.py login           # 开始登录（授权链接推 TG + 轮询拿 token）
  python3 extract_freebuff.py tgsend          # 测试 TG 连通性（发一条测试消息）
  python3 extract_freebuff.py show            # 显示已保存的凭证（脱敏）
  python3 extract_freebuff.py session         # 测试开 session（POST）
  python3 extract_freebuff.py chat [消息]     # 发一条消息测试模型 API
  python3 extract_freebuff.py quota           # 查用量 /api/v1/usage

流程（与官方 CLI 一致）：
  1. 生成设备指纹 fingerprintId
  2. POST https://www.codebuff.com/api/auth/cli/code → 拿 Google 登录 URL + fingerprintHash
  3. 授权链接打印 + 推送 TG，用户在浏览器打开并登录（脚本自动轮询）
  4. 轮询 /api/auth/cli/status → 成功拿到 user（含 authToken）
  5. authToken 保存到本地 / 推送 TG，之后直接作为 Bearer 调模型 API

GitHub Actions 里的安全行为（重要）：
  * 配置了 TG_BOT_TOKEN / TG_CHAT_ID 时，授权链接与 authToken 一律推送到 Telegram，
    **authToken 绝不打印到标准输出/日志**（即使误打印也会被 ::add-mask:: 打码）。
  * 未配置 TG 时（本地手动跑），保持原样打印，方便直接查看。
  * TG 推送失败时直接报错退出，绝不把 token 落到日志里。

环境变量：
  TG_BOT_TOKEN         Telegram Bot Token（可选；与 TG_CHAT_ID 一起配置才推送）
  TG_CHAT_ID           Telegram 接收 chat_id（可选）
  FREEBUFF_TOKEN       手动指定 authToken（跳过 credentials 文件）

依赖：仅 Python 3 标准库，无需 pip 安装任何东西。
"""
import argparse
import base64
import json
import os
import secrets
import sys
import time
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
# CI / Telegram helpers（对齐 cline_oauth.py 的交互方式）
# ---------------------------------------------------------------------------

def in_ci():
    return os.environ.get("GITHUB_ACTIONS") == "true"


def tg_configured():
    return bool(os.environ.get("TG_BOT_TOKEN") and os.environ.get("TG_CHAT_ID"))


def send_tg(text):
    """推送文本到 Telegram，失败返回 False。"""
    token = os.environ.get("TG_BOT_TOKEN")
    chat = os.environ.get("TG_CHAT_ID")
    if not token or not chat:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat, "text": text}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode() or "{}")
            return bool(data.get("ok", True))
    except Exception as e:
        print(f"   ⚠️ TG 发送失败: {e}")
        return False


def mask_value(value):
    """在 CI 中把敏感值加入 GitHub Actions 日志掩码（即使误打出也被打码）。"""
    if in_ci() and value:
        print(f"::add-mask::{value}")


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


def cmd_tgsend(args):
    """测试 TG 连通性：发一条测试消息。"""
    if not tg_configured():
        print("❌ 未设置 TG_BOT_TOKEN / TG_CHAT_ID")
        sys.exit(1)
    ok = send_tg("✅ TG 连通性测试成功！\nFreebuff 提取工作流可以正常向你发消息。")
    if ok:
        print("✅ 测试消息已发送到 TG，请查收。")
    else:
        print("❌ TG 发送失败，请检查 TG_BOT_TOKEN / TG_CHAT_ID。")
        sys.exit(1)


def cmd_login(args):
    # 交互方式：TG 配置了就推 TG；CI 环境强制要求 TG（workflow 第一步也会拦）
    use_tg = tg_configured()

    fingerprint_id = args.fingerprint or gen_fingerprint()
    print(f"🚀 启动 Freebuff 登录流程（fingerprintId: {fingerprint_id}）...\n")

    status, data, _ = _http("POST", "/api/auth/cli/code", {"fingerprintId": fingerprint_id})
    if status != 200 or not data:
        print(f"❌ 请求登录 URL 失败: HTTP {status} {data}")
        sys.exit(1)

    login_url = data["loginUrl"]
    fingerprint_hash = data["fingerprintHash"]
    expires_at = data["expiresAt"]
    # loginUrl 含一次性 auth_code，CI 里掩码，避免暴露到日志
    mask_value(login_url)

    print("=" * 60)
    print("1️⃣  在浏览器打开下面这个链接：")
    print(f"    {login_url}")
    print("2️⃣  用 Google 账号登录并授权")
    print("3️⃣  脚本自动轮询等待，最多 5 分钟")
    print("=" * 60)

    # 把授权链接推送到 TG，方便在手机上完成授权
    if use_tg:
        tg_msg = (
            "🔑 *Freebuff 授权请求*\n\n"
            "请在浏览器打开下面链接并完成登录：\n"
            f"{login_url}\n\n"
            "脚本将自动轮询等待，最多 5 分钟。"
        )
        ok = send_tg(tg_msg)
        if not ok:
            print("❌ 授权链接推送 TG 失败（请检查 TG_BOT_TOKEN / TG_CHAT_ID）")
            sys.exit(1)
        print("📨 授权链接已推送到 Telegram。")
    else:
        print("ℹ️ 未配置 TG，授权链接仅在下方日志中显示。")

    print(f"\n🔄 等待你授权（脚本自动轮询，最多 {POLL_TIMEOUT // 60} 分钟）...")
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

            email = user.get("email", "unknown")
            # 邮箱 / id 同样视为敏感信息：打码，避免进入 Actions 日志
            mask_value(email)
            mask_value(str(user.get("id", "")))
            print(f"✅ 登录成功! 账号: {email}")

            save_credentials(user)

            # 关键安全点：CI + 配置了 TG 时，authToken 只推 TG，绝不打印到日志
            auth_token = user["authToken"]
            if use_tg:
                mask_value(auth_token)  # 兜底：即使万一打出也会被 Actions 掩码
                ok = send_tg(
                    "🔑 *Freebuff authToken 已获取*\n\n"
                    f"账号：`{email}`\n"
                    f"id：`{user.get('id')}`\n"
                    f"credits：`{user.get('credits')}`\n\n"
                    "把下面这行填进 Cloudflare Worker 机密变量 `FREEBUFF_TOKEN`"
                    "（多账号则换行追加）：\n"
                    f"`{auth_token}`"
                )
                if not ok:
                    print("❌ authToken 推送 TG 失败！token 未打印到日志，请检查 TG 配置后重试。")
                    sys.exit(1)
                print("🔑 authToken 已通过 Telegram 私密发送（未写入日志）。")
            else:
                mask_value(auth_token)
                print("\n🔑 把下面这行填进 Cloudflare Worker 的机密变量 FREEBUFF_TOKEN：")
                print("    " + auth_token)
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
            "run_id": run_id or f"run-{secrets.token_hex(6)}",
            "client_id": f"cli-{secrets.token_hex(6)}",
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

    sub.add_parser("tgsend", help="测试 TG 连通性（发一条测试消息）")

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

    sub.add_parser("quota", help="查用量")

    args = p.parse_args()
    {
        "login": cmd_login,
        "show": cmd_show,
        "session": cmd_session,
        "chat": cmd_chat,
        "quota": cmd_quota,
        "tgsend": cmd_tgsend,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
