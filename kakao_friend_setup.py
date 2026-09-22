from __future__ import annotations

"""
카카오톡 '친구에게 보내기' 초기 설정 도우미.

기능
1) 발신자 1명 인증 -> Conf/kakao_sender_token.json 저장
2) 수신 직원을 앱에 연결/동의시킴 -> 토큰은 저장하지 않음
3) 발신자의 카카오 친구 목록을 조회 -> 실제 수신자 선택 ->
   Conf/kakao_friend_recipients.json에 uuid만 저장

주의
- 카카오디벨로퍼스 앱에서 카카오 로그인, Redirect URI, friends/talk_message 동의항목,
  카카오톡 친구 목록/메시지 사용 권한이 준비되어 있어야 합니다.
- 사용 권한 승인 전 테스트에서는 앱 멤버만 친구 목록 응답에 포함됩니다.
- 수신 직원은 발신자의 실제 카카오톡 친구이면서 같은 앱에 연결되어 있어야 합니다.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = app_dir()
CONF_DIR = ROOT / "Conf"
ENV_FILE = CONF_DIR / ".env"
SENDER_TOKEN_FILE = CONF_DIR / "kakao_sender_token.json"
RECIPIENTS_FILE = CONF_DIR / "kakao_friend_recipients.json"
REDIRECT_URI = "http://localhost:5000"
SCOPES = "friends,talk_message"


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def extract_code(pasted: str) -> str:
    pasted = pasted.strip()
    if "code=" not in pasted:
        return pasted
    query = urllib.parse.urlsplit(pasted).query
    params = urllib.parse.parse_qs(query)
    if "code" in params and params["code"]:
        return params["code"][0]
    after = pasted.split("code=", 1)[1]
    return after.split("&", 1)[0]


def make_authorize_url(client_id: str) -> str:
    # prompt=login: 여러 사람이 같은 PC/브라우저에서 순차 인증할 때
    # 이전 로그인 세션 때문에 잘못된 계정으로 연결되는 것을 줄입니다.
    return "https://kauth.kakao.com/oauth/authorize?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPES,
            "prompt": "login",
        }
    )


def request_authorization_code(client_id: str, title: str) -> str:
    print("\n" + "=" * 72)
    print(title)
    print("아래 URL을 브라우저에서 열고 해당 사용자의 카카오계정으로 로그인/동의하세요.")
    print("동의 후 localhost 연결 오류가 떠도 정상입니다.")
    print("주소창의 전체 주소 또는 code= 뒤의 값을 복사해 붙여넣으세요.")
    print("=" * 72)
    print(make_authorize_url(client_id))
    print()
    pasted = input("주소(또는 code 값): ").strip()
    if not pasted:
        raise RuntimeError("인가 코드 입력이 없습니다.")
    return extract_code(pasted)


def exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "code": code,
    }
    if client_secret:
        data["client_secret"] = client_secret

    request = urllib.request.Request(
        "https://kauth.kakao.com/oauth/token",
        data=urllib.parse.urlencode(data).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"토큰 발급 실패({e.code}): {detail}") from e


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> tuple[str, str | None]:
    data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }
    if client_secret:
        data["client_secret"] = client_secret
    request = urllib.request.Request(
        "https://kauth.kakao.com/oauth/token",
        data=urllib.parse.urlencode(data).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"토큰 갱신 실패({e.code}): {detail}") from e
    return str(payload["access_token"]), (
        str(payload["refresh_token"]) if payload.get("refresh_token") else None
    )


def register_sender(client_id: str, client_secret: str) -> None:
    code = request_authorization_code(
        client_id,
        "[발신자 인증] 실제로 16명에게 메시지를 보낼 카카오계정으로 로그인하세요.",
    )
    payload = exchange_code(client_id, client_secret, code)
    refresh_token = str(payload.get("refresh_token", "")).strip()
    if not refresh_token:
        raise RuntimeError(f"refresh_token이 없습니다. 응답: {payload}")

    write_json_atomic(
        SENDER_TOKEN_FILE,
        {
            "refresh_token": refresh_token,
            "registered_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    print(f"\n발신자 인증 완료: {SENDER_TOKEN_FILE}")
    print("이 파일은 비밀정보이므로 GitHub에 올리지 마세요.")


def connect_recipient(client_id: str, client_secret: str) -> None:
    code = request_authorization_code(
        client_id,
        "[직원 앱 연결/동의] 메시지를 받을 직원 본인의 카카오계정으로 로그인하세요.",
    )
    payload = exchange_code(client_id, client_secret, code)
    if not payload.get("access_token"):
        raise RuntimeError(f"access_token이 없습니다. 응답: {payload}")
    # 수신 직원 토큰은 발송에 필요하지 않으므로 저장하지 않습니다.
    print("\n직원 앱 연결/동의가 완료되었습니다.")
    print("이 직원의 토큰은 저장하지 않습니다.")
    print("다음 직원도 등록하려면 같은 메뉴를 다시 실행하세요.")


def load_sender_refresh_token() -> str:
    if not SENDER_TOKEN_FILE.exists():
        raise RuntimeError("발신자 토큰이 없습니다. 먼저 메뉴 1을 실행하세요.")
    try:
        data = json.loads(SENDER_TOKEN_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"발신자 토큰 파일을 읽지 못했습니다: {e}") from e
    refresh_token = str(data.get("refresh_token", "")).strip()
    if not refresh_token:
        raise RuntimeError("발신자 refresh_token이 없습니다. 메뉴 1을 다시 실행하세요.")
    return refresh_token


def sender_access_token(client_id: str, client_secret: str) -> str:
    refresh_token = load_sender_refresh_token()
    access_token, new_refresh_token = refresh_access_token(client_id, client_secret, refresh_token)
    if new_refresh_token and new_refresh_token != refresh_token:
        current = json.loads(SENDER_TOKEN_FILE.read_text(encoding="utf-8"))
        current["refresh_token"] = new_refresh_token
        current["updated_at"] = datetime.now().isoformat(timespec="seconds")
        write_json_atomic(SENDER_TOKEN_FILE, current)
    return access_token


def get_friends(access_token: str) -> list[dict]:
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
            f"https://kapi.kakao.com/v1/api/talk/friends?{query}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"친구 목록 조회 실패({e.code}): {detail}") from e

        elements = payload.get("elements", []) or []
        friends.extend(elements)
        total_count = int(payload.get("total_count", len(friends)))
        if not elements or len(friends) >= total_count:
            break
        offset += len(elements)

    return friends


def parse_selection(raw: str, max_index: int) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for token in raw.replace(" ", "").split(","):
        if not token:
            continue
        if "-" in token:
            left, right = token.split("-", 1)
            start, end = int(left), int(right)
            values = range(start, end + 1)
        else:
            values = [int(token)]
        for value in values:
            if not 1 <= value <= max_index:
                raise ValueError(f"선택 번호가 범위를 벗어났습니다: {value}")
            if value not in seen:
                seen.add(value)
                result.append(value)
    return result


def select_recipients(client_id: str, client_secret: str) -> None:
    access_token = sender_access_token(client_id, client_secret)
    friends = get_friends(access_token)
    if not friends:
        print("\n조회 가능한 친구가 없습니다.")
        print("수신 직원이 발신자의 카카오 친구인지, 같은 앱에 연결/동의했는지,")
        print("그리고 앱의 친구 목록/메시지 사용 권한이 준비됐는지 확인하세요.")
        return

    print("\n조회된 친구 목록")
    print("-" * 72)
    for i, friend in enumerate(friends, start=1):
        nickname = str(friend.get("profile_nickname", "(닉네임 없음)"))
        favorite = "★" if friend.get("favorite") else " "
        print(f"{i:>3}. {favorite} {nickname}")
    print("-" * 72)
    print("보낼 직원을 번호로 선택하세요. 예: 1,2,5-8")
    raw = input("선택: ").strip()
    indexes = parse_selection(raw, len(friends))
    if not indexes:
        print("선택된 수신자가 없습니다.")
        return

    uuids: list[str] = []
    selected_names: list[str] = []
    for index in indexes:
        friend = friends[index - 1]
        uuid = str(friend.get("uuid", "")).strip()
        if not uuid:
            continue
        uuids.append(uuid)
        selected_names.append(str(friend.get("profile_nickname", "(닉네임 없음)")))

    write_json_atomic(
        RECIPIENTS_FILE,
        {
            "receiver_uuids": uuids,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    print(f"\n수신자 {len(uuids)}명 저장 완료: {', '.join(selected_names)}")
    print(f"저장 위치: {RECIPIENTS_FILE}")
    print("이 파일도 GitHub에 올리지 마세요.")


def main() -> None:
    load_env(ENV_FILE)
    client_id = os.getenv("KAKAO_REST_API_KEY", "").strip()
    client_secret = os.getenv("KAKAO_CLIENT_SECRET", "").strip()
    if not client_id:
        print(f"[오류] {ENV_FILE}에 KAKAO_REST_API_KEY가 없습니다.")
        return

    while True:
        print("\n" + "=" * 72)
        print("카카오 친구 발송 초기 설정")
        print("1. 발신자 인증/갱신")
        print("2. 수신 직원 앱 연결/동의")
        print("3. 발신자의 친구 목록 조회 및 수신자 선택")
        print("4. 종료")
        print("=" * 72)
        choice = input("선택: ").strip()

        try:
            if choice == "1":
                register_sender(client_id, client_secret)
            elif choice == "2":
                connect_recipient(client_id, client_secret)
            elif choice == "3":
                select_recipients(client_id, client_secret)
            elif choice == "4":
                break
            else:
                print("1~4 중에서 선택하세요.")
        except (RuntimeError, ValueError) as e:
            print(f"[오류] {e}")


if __name__ == "__main__":
    main()
