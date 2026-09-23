from __future__ import annotations

"""
카카오톡 '친구에게 보내기' 수신자 관리 도우미.

관리 방식
- Conf/kakao.txt: 실제 발송 대상 '이름' 목록 (한 줄에 한 명)
- Conf/kakao_friend_recipients.json: 이름 -> 카카오 친구 uuid 매핑
- kakao_send.py는 두 파일을 함께 읽어 kakao.txt에 있는 이름만 발송

메뉴
1) 발신자 인증/갱신
2) 수신 직원 앱 연결/동의
3) 발송 사용자 등록 (이름 + 카카오 친구 매핑)
4) 등록 사용자 삭제
5) 등록 사용자 조회
6) 종료
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
KAKAO_LIST_FILE = CONF_DIR / "kakao.txt"
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
        # Conf/.env 값을 우선 사용
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def save_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
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


def refresh_access_token(
    client_id: str, client_secret: str, refresh_token: str
) -> tuple[str, str | None]:
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
        "[발신자 인증] 직원들에게 메시지를 보낼 카카오계정으로 로그인하세요.",
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
    print("\n직원 앱 연결/동의가 완료되었습니다.")
    print("직원 토큰은 저장하지 않습니다.")
    print("이제 메뉴 3에서 발신자의 친구 목록 중 해당 직원을 선택하고 이름을 등록하세요.")


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
    access_token, new_refresh_token = refresh_access_token(
        client_id, client_secret, refresh_token
    )
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


def load_active_names() -> list[str]:
    """Conf/kakao.txt에서 실제 발송 대상 이름을 읽습니다."""
    if not KAKAO_LIST_FILE.exists():
        return []
    names: list[str] = []
    seen: set[str] = set()
    for raw in KAKAO_LIST_FILE.read_text(encoding="utf-8").splitlines():
        name = raw.strip()
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
        name = raw.strip()
        if not name:
            continue
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(name)
    text = "".join(f"{name}\n" for name in unique)
    save_text_atomic(KAKAO_LIST_FILE, text)


def load_recipient_registry() -> dict[str, dict]:
    """이름 -> {uuid, profile_nickname, registered_at} 매핑을 읽습니다."""
    if not RECIPIENTS_FILE.exists():
        return {}
    try:
        data = json.loads(RECIPIENTS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"카카오 수신자 파일을 읽지 못했습니다: {e}") from e

    if not isinstance(data, dict):
        raise RuntimeError("kakao_friend_recipients.json 형식이 올바르지 않습니다.")

    recipients = data.get("recipients")
    if isinstance(recipients, dict):
        result: dict[str, dict] = {}
        for name, info in recipients.items():
            if isinstance(info, dict) and str(info.get("uuid", "")).strip():
                result[str(name)] = info
        return result

    # 이전 버전은 uuid 배열만 있어 이름 기반 필터링이 불가능합니다.
    if isinstance(data.get("receiver_uuids"), list) and data.get("receiver_uuids"):
        print(
            "[안내] 기존 kakao_friend_recipients.json은 이름 정보가 없는 이전 형식입니다.\n"
            "       메뉴 3에서 사용자를 이름과 함께 다시 등록하면 새 형식으로 전환됩니다."
        )
    return {}


def save_recipient_registry(registry: dict[str, dict]) -> None:
    write_json_atomic(
        RECIPIENTS_FILE,
        {
            "recipients": registry,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        },
    )


def _find_registry_name(registry: dict[str, dict], name: str) -> str | None:
    wanted = name.strip().casefold()
    for saved_name in registry:
        if saved_name.casefold() == wanted:
            return saved_name
    return None


def _ensure_active_name(name: str) -> None:
    names = load_active_names()
    if not any(saved.casefold() == name.casefold() for saved in names):
        names.append(name)
        save_active_names(names)


def register_recipient(client_id: str, client_secret: str) -> None:
    """친구 한 명을 선택해 관리 이름과 uuid를 저장하고 kakao.txt에도 활성화합니다."""
    access_token = sender_access_token(client_id, client_secret)
    friends = get_friends(access_token)
    if not friends:
        print("\n조회 가능한 친구가 없습니다.")
        print("직원 앱 연결/동의, 실제 카카오 친구 관계, 앱 권한을 확인하세요.")
        return

    registry = load_recipient_registry()
    registered_uuids = {
        str(info.get("uuid", "")).strip(): name
        for name, info in registry.items()
        if str(info.get("uuid", "")).strip()
    }

    print("\n발신자의 조회 가능한 친구 목록")
    print("-" * 72)
    for i, friend in enumerate(friends, start=1):
        nickname = str(friend.get("profile_nickname", "(닉네임 없음)"))
        uuid = str(friend.get("uuid", "")).strip()
        saved_name = registered_uuids.get(uuid)
        status = f" [등록됨: {saved_name}]" if saved_name else ""
        print(f"{i:>3}. {nickname}{status}")
    print("-" * 72)

    raw = input("등록할 친구 번호: ").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= len(friends):
        raise ValueError("친구 번호를 올바르게 입력하세요.")
    friend = friends[int(raw) - 1]
    uuid = str(friend.get("uuid", "")).strip()
    nickname = str(friend.get("profile_nickname", "(닉네임 없음)")).strip()
    if not uuid:
        raise RuntimeError("선택한 친구의 uuid가 없습니다.")

    name = input("kakao.txt에 사용할 사용자 이름(예: 홍길동): ").strip()
    if not name:
        raise ValueError("사용자 이름은 비워둘 수 없습니다.")
    if name.startswith("#"):
        raise ValueError("사용자 이름은 #으로 시작할 수 없습니다.")

    # 같은 uuid가 다른 이름으로 이미 등록돼 있으면 중복을 방지합니다.
    existing_for_uuid = registered_uuids.get(uuid)
    if existing_for_uuid and existing_for_uuid.casefold() != name.casefold():
        answer = input(
            f"이 카카오 친구는 이미 '{existing_for_uuid}' 이름으로 등록되어 있습니다. "
            f"'{name}'으로 이름을 바꿀까요? (y/N): "
        ).strip().lower()
        if answer not in {"y", "yes"}:
            print("등록을 취소했습니다.")
            return
        registry.pop(existing_for_uuid, None)
        active = [n for n in load_active_names() if n.casefold() != existing_for_uuid.casefold()]
        save_active_names(active)

    existing_name = _find_registry_name(registry, name)
    if existing_name and existing_name != name:
        # 대소문자 차이만 있는 이름은 기존 키를 정리
        registry.pop(existing_name, None)

    registry[name] = {
        "uuid": uuid,
        "profile_nickname": nickname,
        "registered_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_recipient_registry(registry)
    _ensure_active_name(name)

    print("\n사용자 등록 완료")
    print(f"관리 이름 : {name}")
    print(f"카카오 닉네임 : {nickname}")
    print(f"활성 목록 : {KAKAO_LIST_FILE}")
    print(f"UUID 매핑 : {RECIPIENTS_FILE}")
    print("kakao.txt에 이름이 있는 동안 kakao_send.py의 친구 발송 대상이 됩니다.")


def list_recipients() -> None:
    registry = load_recipient_registry()
    active_names = load_active_names()
    active_keys = {name.casefold() for name in active_names}

    all_names = list(registry.keys())
    # kakao.txt에만 있고 매핑이 없는 이름도 보여줌
    for name in active_names:
        if not any(saved.casefold() == name.casefold() for saved in all_names):
            all_names.append(name)

    if not all_names:
        print("\n등록된 사용자가 없습니다.")
        return

    print("\n등록 사용자")
    print("-" * 72)
    for i, name in enumerate(all_names, start=1):
        key = _find_registry_name(registry, name)
        info = registry.get(key, {}) if key else {}
        nickname = str(info.get("profile_nickname", "-")).strip() or "-"
        status = "발송 ON" if name.casefold() in active_keys else "발송 OFF"
        mapped = "매핑 OK" if info.get("uuid") else "UUID 없음"
        print(f"{i:>3}. {name} | {status} | {mapped} | 카카오닉네임: {nickname}")
    print("-" * 72)
    print(f"실제 발송 대상 파일: {KAKAO_LIST_FILE}")


def delete_recipient() -> None:
    registry = load_recipient_registry()
    active_names = load_active_names()

    all_names = list(registry.keys())
    for name in active_names:
        if not any(saved.casefold() == name.casefold() for saved in all_names):
            all_names.append(name)

    if not all_names:
        print("\n삭제할 사용자가 없습니다.")
        return

    print("\n삭제할 사용자")
    print("-" * 72)
    for i, name in enumerate(all_names, start=1):
        key = _find_registry_name(registry, name)
        info = registry.get(key, {}) if key else {}
        nickname = str(info.get("profile_nickname", "-")).strip() or "-"
        print(f"{i:>3}. {name} (카카오닉네임: {nickname})")
    print("-" * 72)

    raw = input("삭제할 번호: ").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= len(all_names):
        raise ValueError("삭제할 번호를 올바르게 입력하세요.")
    name = all_names[int(raw) - 1]

    answer = input(f"'{name}' 사용자를 발송목록과 UUID 매핑에서 모두 삭제할까요? (y/N): ").strip().lower()
    if answer not in {"y", "yes"}:
        print("삭제를 취소했습니다.")
        return

    key = _find_registry_name(registry, name)
    if key:
        registry.pop(key, None)
        save_recipient_registry(registry)

    active = [n for n in active_names if n.casefold() != name.casefold()]
    save_active_names(active)

    print(f"\n삭제 완료: {name}")
    print("이 사용자는 kakao_send.py의 친구 발송 대상에서 제외됩니다.")


def main() -> None:
    load_env(ENV_FILE)
    client_id = os.getenv("KAKAO_REST_API_KEY", "").strip()
    client_secret = os.getenv("KAKAO_CLIENT_SECRET", "").strip()
    if not client_id:
        print(f"[오류] {ENV_FILE}에 KAKAO_REST_API_KEY가 없습니다.")
        return

    while True:
        print("\n" + "=" * 72)
        print("카카오 친구 발송 사용자 관리")
        print("1. 발신자 인증/갱신")
        print("2. 수신 직원 앱 연결/동의")
        print("3. 발송 사용자 등록 (이름 + 카카오 친구 매핑)")
        print("4. 등록 사용자 삭제")
        print("5. 등록 사용자 조회")
        print("6. 종료")
        print("=" * 72)
        choice = input("선택: ").strip()

        try:
            if choice == "1":
                register_sender(client_id, client_secret)
            elif choice == "2":
                connect_recipient(client_id, client_secret)
            elif choice == "3":
                register_recipient(client_id, client_secret)
            elif choice == "4":
                delete_recipient()
            elif choice == "5":
                list_recipients()
            elif choice == "6":
                break
            else:
                print("1~6 중에서 선택하세요.")
        except (RuntimeError, ValueError) as e:
            print(f"[오류] {e}")


if __name__ == "__main__":
    main()
