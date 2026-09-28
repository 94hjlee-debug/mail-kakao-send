from __future__ import annotations

"""
직원용 카카오 수신자 셀프 등록 웹서버.

흐름
1) 관리자가 이 파일을 자신의 PC에서 실행
2) 직원이 /register 페이지에 접속해 이름 + 등록 PIN 입력
3) 카카오 로그인/동의(friends,talk_message)
4) 로그인한 직원의 Kakao user id를 발신자의 친구 목록에서 찾아 uuid 확인
5) Conf/kakao_friend_recipients.json 에 이름 -> uuid 저장
6) Conf/kakao.txt 에 이름을 자동 추가

중요
- 직원의 access/refresh token은 저장하지 않습니다.
- 발신자 토큰은 기존 Conf/kakao_sender_token.json을 사용합니다.
- 등록 웹주소/리다이렉트 URI는 직원 휴대폰에서 실제로 접근 가능해야 합니다.
"""

import hashlib
import hmac
import html
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = app_dir()
CONF_DIR = ROOT / "Conf"
ENV_FILE = CONF_DIR / ".env"
SENDER_TOKEN_FILE = CONF_DIR / "kakao_sender_token.json"
RECIPIENTS_FILE = CONF_DIR / "kakao_friend_recipients.json"
KAKAO_LIST_FILE = CONF_DIR / "kakao.txt"
ALLOWED_NAMES_FILE = CONF_DIR / "kakao_allowed.txt"

KAKAO_AUTHORIZE_URL = "https://kauth.kakao.com/oauth/authorize"
KAKAO_TOKEN_URL = "https://kauth.kakao.com/oauth/token"
KAKAO_USER_ME_URL = "https://kapi.kakao.com/v2/user/me"
KAKAO_FRIENDS_URL = "https://kapi.kakao.com/v1/api/talk/friends"
SCOPES = "friends,talk_message"

# state -> {name, created_at}
PENDING: dict[str, dict[str, Any]] = {}
STATE_TTL_SECONDS = 10 * 60


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def normalize_name(name: str) -> str:
    return " ".join(name.strip().split())


def load_allowed_names() -> list[str]:
    if not ALLOWED_NAMES_FILE.exists():
        return []
    names: list[str] = []
    seen: set[str] = set()
    for raw in ALLOWED_NAMES_FILE.read_text(encoding="utf-8").splitlines():
        name = normalize_name(raw)
        if not name or name.startswith("#"):
            continue
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            names.append(name)
    return names


def canonical_allowed_name(name: str) -> str | None:
    wanted = normalize_name(name).casefold()
    allowed = load_allowed_names()
    if not allowed:
        return normalize_name(name) or None
    for item in allowed:
        if item.casefold() == wanted:
            return item
    return None


def load_active_names() -> list[str]:
    if not KAKAO_LIST_FILE.exists():
        return []
    names: list[str] = []
    seen: set[str] = set()
    for raw in KAKAO_LIST_FILE.read_text(encoding="utf-8").splitlines():
        name = normalize_name(raw)
        if not name or name.startswith("#"):
            continue
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            names.append(name)
    return names


def save_active_names(names: list[str]) -> None:
    unique: list[str] = []
    seen: set[str] = set()
    for raw in names:
        name = normalize_name(raw)
        if not name:
            continue
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(name)
    write_text_atomic(KAKAO_LIST_FILE, "".join(f"{name}\n" for name in unique))


def ensure_active_name(name: str) -> None:
    names = load_active_names()
    if not any(n.casefold() == name.casefold() for n in names):
        names.append(name)
        save_active_names(names)


def load_registry() -> dict[str, dict]:
    if not RECIPIENTS_FILE.exists():
        return {}
    try:
        data = json.loads(RECIPIENTS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"수신자 파일을 읽지 못했습니다: {e}") from e
    recipients = data.get("recipients", {}) if isinstance(data, dict) else {}
    if not isinstance(recipients, dict):
        raise RuntimeError("kakao_friend_recipients.json 형식이 올바르지 않습니다.")
    return {str(k): v for k, v in recipients.items() if isinstance(v, dict)}


def save_registry(registry: dict[str, dict]) -> None:
    write_json_atomic(
        RECIPIENTS_FILE,
        {
            "recipients": registry,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        },
    )


def find_registry_name(registry: dict[str, dict], name: str) -> str | None:
    wanted = name.casefold()
    for saved_name in registry:
        if saved_name.casefold() == wanted:
            return saved_name
    return None


def api_json(request: urllib.request.Request, error_title: str) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{error_title}({e.code}): {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"{error_title}: {e}") from e


def exchange_code(code: str, redirect_uri: str) -> dict:
    client_id = os.getenv("KAKAO_REST_API_KEY", "").strip()
    client_secret = os.getenv("KAKAO_CLIENT_SECRET", "").strip()
    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code": code,
    }
    if client_secret:
        data["client_secret"] = client_secret
    request = urllib.request.Request(
        KAKAO_TOKEN_URL,
        data=urllib.parse.urlencode(data).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
    )
    return api_json(request, "직원 토큰 발급 실패")


def user_me(access_token: str) -> dict:
    request = urllib.request.Request(
        KAKAO_USER_ME_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    return api_json(request, "직원 사용자 정보 조회 실패")


def refresh_sender_access_token() -> str:
    if not SENDER_TOKEN_FILE.exists():
        raise RuntimeError(
            "발신자 토큰 파일이 없습니다. 먼저 kakao_friend_setup.py 메뉴 1에서 발신자 인증을 해주세요."
        )
    try:
        token_data = json.loads(SENDER_TOKEN_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"발신자 토큰 파일을 읽지 못했습니다: {e}") from e

    refresh_token = str(token_data.get("refresh_token", "")).strip()
    if not refresh_token:
        raise RuntimeError("발신자 refresh_token이 없습니다.")

    client_id = os.getenv("KAKAO_REST_API_KEY", "").strip()
    client_secret = os.getenv("KAKAO_CLIENT_SECRET", "").strip()
    data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }
    if client_secret:
        data["client_secret"] = client_secret

    request = urllib.request.Request(
        KAKAO_TOKEN_URL,
        data=urllib.parse.urlencode(data).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
    )
    payload = api_json(request, "발신자 토큰 갱신 실패")
    access_token = str(payload.get("access_token", "")).strip()
    if not access_token:
        raise RuntimeError("발신자 access_token을 받지 못했습니다.")

    new_refresh_token = str(payload.get("refresh_token", "")).strip()
    if new_refresh_token and new_refresh_token != refresh_token:
        token_data["refresh_token"] = new_refresh_token
        token_data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        write_json_atomic(SENDER_TOKEN_FILE, token_data)

    return access_token


def sender_friends(access_token: str) -> list[dict]:
    friends: list[dict] = []
    offset = 0
    limit = 100
    while True:
        query = urllib.parse.urlencode(
            {
                "offset": offset,
                "limit": limit,
                "order": "asc",
                "friend_order": "nickname",
            }
        )
        request = urllib.request.Request(
            f"{KAKAO_FRIENDS_URL}?{query}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        payload = api_json(request, "발신자 친구 목록 조회 실패")
        elements = payload.get("elements", []) or []
        friends.extend(x for x in elements if isinstance(x, dict))
        total_count = int(payload.get("total_count", len(friends)))
        if not elements or len(friends) >= total_count:
            break
        offset += len(elements)
    return friends


def find_employee_in_sender_friends(employee_user_id: int) -> dict | None:
    access_token = refresh_sender_access_token()
    friends = sender_friends(access_token)

    # 등록 실패 원인을 바로 확인할 수 있도록 서버 콘솔에 진단 정보를 남깁니다.
    # kakao_allowed.txt의 이름과 카카오 닉네임을 비교하는 것이 아니라,
    # 현재 로그인한 카카오 계정의 user id와 발신자 친구목록 API의 id를 비교합니다.
    print(f"[친구목록 진단] 발신자 친구 API 반환: {len(friends)}명")
    print(f"[친구목록 진단] 현재 등록 로그인 Kakao user id: {employee_user_id}")

    for friend in friends:
        nickname = str(friend.get("profile_nickname", "")).strip() or "(닉네임 없음)"
        raw_id = friend.get("id")
        print(f"[친구목록 진단] 친구: {nickname} / id={raw_id}")
        try:
            friend_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if friend_id == employee_user_id:
            print(f"[친구목록 진단] 일치 계정 발견: {nickname}")
            return friend

    print("[친구목록 진단] 현재 로그인 계정과 일치하는 친구를 찾지 못했습니다.")
    return None


def register_employee(name: str, employee_user_id: int) -> tuple[str, str]:
    friend = find_employee_in_sender_friends(employee_user_id)
    if not friend:
        raise RuntimeError(
            "발신자의 카카오 친구 목록에서 현재 로그인한 계정을 찾지 못했습니다. "
            "발신자와 실제 카카오톡 친구인지, 해당 직원이 friends/talk_message 동의 및 앱 연결을 완료했는지 확인하세요."
        )

    uuid = str(friend.get("uuid", "")).strip()
    nickname = str(friend.get("profile_nickname", "")).strip() or "(닉네임 없음)"
    if not uuid:
        raise RuntimeError("친구 정보에 메시지 발송용 uuid가 없습니다.")

    registry = load_registry()

    # 동일 Kakao UUID가 다른 관리 이름으로 등록돼 있으면 이전 이름은 제거합니다.
    for saved_name, info in list(registry.items()):
        if str(info.get("uuid", "")).strip() == uuid and saved_name.casefold() != name.casefold():
            registry.pop(saved_name, None)
            active = [n for n in load_active_names() if n.casefold() != saved_name.casefold()]
            save_active_names(active)

    existing_name = find_registry_name(registry, name)
    if existing_name and existing_name != name:
        registry.pop(existing_name, None)

    registry[name] = {
        "uuid": uuid,
        "kakao_user_id": employee_user_id,
        "profile_nickname": nickname,
        "registered_at": datetime.now().isoformat(timespec="seconds"),
        "registered_via": "self_web",
    }
    save_registry(registry)
    ensure_active_name(name)
    return nickname, uuid


def cleanup_pending() -> None:
    now = time.time()
    expired = [state for state, info in PENDING.items() if now - float(info.get("created_at", 0)) > STATE_TTL_SECONDS]
    for state in expired:
        PENDING.pop(state, None)


def page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Malgun Gothic',sans-serif;background:#f5f6f7;margin:0;padding:24px;color:#222}}
.card{{max-width:520px;margin:40px auto;background:white;border-radius:16px;padding:28px;box-shadow:0 2px 12px rgba(0,0,0,.08)}}
h1{{font-size:24px;margin:0 0 12px}} p{{line-height:1.6}} label{{display:block;margin-top:18px;font-weight:600}}
input{{width:100%;box-sizing:border-box;padding:12px;margin-top:7px;border:1px solid #ccc;border-radius:8px;font-size:16px}}
button,.button{{display:block;width:100%;box-sizing:border-box;margin-top:22px;padding:13px;border:0;border-radius:8px;background:#fee500;color:#191919;font-size:16px;font-weight:700;text-align:center;text-decoration:none;cursor:pointer}}
.note{{font-size:13px;color:#666;margin-top:16px}} .ok{{font-size:18px;font-weight:700}} .error{{color:#b00020;font-weight:700}}
</style>
</head>
<body><div class="card">{body}</div></body></html>""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "KakaoRegister/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {self.address_string()} - {format % args}")

    def send_html(self, status: int, title: str, body: str) -> None:
        payload = page(title, body)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def redirect(self, url: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", url)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path in {"/", "/register"}:
            self.show_register()
            return
        if parsed.path == "/kakao/callback":
            self.handle_callback(parsed)
            return
        if parsed.path == "/health":
            self.send_html(200, "정상", "<h1>정상</h1><p>등록 서버가 실행 중입니다.</p>")
            return
        self.send_html(404, "404", "<h1>페이지를 찾을 수 없습니다.</h1>")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/register":
            self.start_registration()
            return
        self.send_html(404, "404", "<h1>페이지를 찾을 수 없습니다.</h1>")

    def show_register(self, error: str = "") -> None:
        allowed = load_allowed_names()
        allow_note = (
            f"등록 가능 인원 {len(allowed)}명으로 제한되어 있습니다."
            if allowed
            else "kakao_allowed.txt가 없어 이름 제한 없이 등록됩니다. 운영 시 허용 명단 사용을 권장합니다."
        )
        error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
        self.send_html(
            200,
            "카카오 알림 등록",
            f"""
<h1>카카오 알림 등록</h1>
<p>업무 알림을 받을 직원 본인이 등록해 주세요.</p>
{error_html}
<form method="post" action="/register">
<label for="name">직원 이름</label>
<input id="name" name="name" maxlength="40" autocomplete="name" required placeholder="예: 이호준책임">
<label for="pin">등록 PIN</label>
<input id="pin" name="pin" type="password" maxlength="50" required placeholder="관리자가 안내한 PIN">
<button type="submit">카카오로 등록하기</button>
</form>
<p class="note">{html.escape(allow_note)}<br>등록 과정에서 카카오 로그인 및 친구목록/메시지 전송 동의가 필요합니다.</p>
""",
        )

    def start_registration(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        form = urllib.parse.parse_qs(raw)
        entered_name = normalize_name((form.get("name") or [""])[0])
        entered_pin = (form.get("pin") or [""])[0]

        expected_pin = os.getenv("KAKAO_REGISTER_PIN", "").strip()
        if not expected_pin:
            self.show_register("서버의 KAKAO_REGISTER_PIN 설정이 없습니다. 관리자에게 문의하세요.")
            return
        if not hmac.compare_digest(entered_pin, expected_pin):
            self.show_register("등록 PIN이 올바르지 않습니다.")
            return

        name = canonical_allowed_name(entered_name)
        if not name:
            self.show_register("등록 허용 명단에 없는 이름입니다. 관리자에게 문의하세요.")
            return

        client_id = os.getenv("KAKAO_REST_API_KEY", "").strip()
        redirect_uri = os.getenv("KAKAO_REGISTER_REDIRECT_URI", "").strip()
        if not client_id or not redirect_uri:
            self.show_register("카카오 앱 키 또는 Redirect URI 서버 설정이 없습니다.")
            return

        cleanup_pending()
        state = secrets.token_urlsafe(32)
        PENDING[state] = {"name": name, "created_at": time.time()}

        authorize_url = KAKAO_AUTHORIZE_URL + "?" + urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": SCOPES,
                "state": state,
            }
        )
        self.redirect(authorize_url)

    def handle_callback(self, parsed: urllib.parse.SplitResult) -> None:
        params = urllib.parse.parse_qs(parsed.query)
        if params.get("error"):
            desc = (params.get("error_description") or params.get("error") or ["로그인이 취소되었습니다."])[0]
            self.send_html(400, "등록 실패", f'<h1>등록 실패</h1><p class="error">{html.escape(desc)}</p>')
            return

        code = (params.get("code") or [""])[0]
        state = (params.get("state") or [""])[0]
        cleanup_pending()
        pending = PENDING.pop(state, None)
        if not code or not pending:
            self.send_html(400, "등록 실패", '<h1>등록 실패</h1><p class="error">등록 요청이 만료되었거나 state가 올바르지 않습니다. 처음부터 다시 진행해 주세요.</p>')
            return

        try:
            redirect_uri = os.getenv("KAKAO_REGISTER_REDIRECT_URI", "").strip()
            token = exchange_code(code, redirect_uri)
            employee_access_token = str(token.get("access_token", "")).strip()
            if not employee_access_token:
                raise RuntimeError("직원 access_token을 받지 못했습니다.")

            me = user_me(employee_access_token)
            employee_user_id = int(me.get("id"))
            name = str(pending["name"])
            nickname, _uuid = register_employee(name, employee_user_id)

            self.send_html(
                200,
                "등록 완료",
                f"""
<h1>등록 완료</h1>
<p class="ok">{html.escape(name)} 님의 카카오 알림 등록이 완료되었습니다.</p>
<p>카카오 닉네임: {html.escape(nickname)}</p>
<p class="note">직원 토큰은 저장하지 않았습니다. 이제 이 창을 닫으셔도 됩니다.</p>
""",
            )
            print(f"[등록 완료] 관리 이름={name}, 카카오 닉네임={nickname}, Kakao user id={employee_user_id}")
        except Exception as e:
            print(f"[등록 실패] {e}")
            self.send_html(
                400,
                "등록 실패",
                f"""
<h1>등록 실패</h1>
<p class="error">{html.escape(str(e))}</p>
<p>다음을 확인한 뒤 다시 시도해 주세요.</p>
<ul>
<li>발신자와 직원이 실제 카카오톡 친구인지</li>
<li>직원이 friends / talk_message 동의를 완료했는지</li>
<li>테스트 단계라면 직원 계정이 앱 멤버/테스터 조건을 충족하는지</li>
</ul>
<a class="button" href="/register">처음부터 다시 등록</a>
""",
            )


def main() -> None:
    load_env(ENV_FILE)

    required = {
        "KAKAO_REST_API_KEY": os.getenv("KAKAO_REST_API_KEY", "").strip(),
        "KAKAO_REGISTER_REDIRECT_URI": os.getenv("KAKAO_REGISTER_REDIRECT_URI", "").strip(),
        "KAKAO_REGISTER_PIN": os.getenv("KAKAO_REGISTER_PIN", "").strip(),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        print("[오류] Conf/.env에 다음 값이 필요합니다: " + ", ".join(missing))
        raise SystemExit(1)

    if not SENDER_TOKEN_FILE.exists():
        print("[오류] Conf/kakao_sender_token.json이 없습니다.")
        print("       먼저 python kakao_friend_setup.py 실행 후 메뉴 1에서 발신자 인증을 하세요.")
        raise SystemExit(1)

    host = os.getenv("KAKAO_REGISTER_HOST", "0.0.0.0").strip() or "0.0.0.0"
    try:
        port = int(os.getenv("KAKAO_REGISTER_PORT", "5000"))
    except ValueError:
        port = 5000

    allowed = load_allowed_names()
    print("=" * 72)
    print("카카오 직원 셀프 등록 웹서버")
    print(f"바인딩: http://{host}:{port}")
    print(f"Redirect URI: {required['KAKAO_REGISTER_REDIRECT_URI']}")
    if allowed:
        print(f"허용 명단: {ALLOWED_NAMES_FILE} ({len(allowed)}명)")
    else:
        print("주의: Conf/kakao_allowed.txt가 없어 이름 제한 없이 등록됩니다.")
    print("직원에게는 이 PC에 실제로 접근 가능한 주소의 /register 링크를 보내세요.")
    print("예: http://192.168.0.10:5000/register")
    print("종료: Ctrl+C")
    print("=" * 72)

    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
