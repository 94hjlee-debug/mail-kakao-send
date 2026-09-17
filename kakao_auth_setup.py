from __future__ import annotations

"""
카카오톡 "나에게 보내기" 최초 1회 설정용 스크립트 (여러 명 연속 등록 지원).

이 스크립트는 kakao_send.py와 별도로, 카카오톡 알림을 받을 사람마다 "딱 한
번씩만" 인증을 받으면 됩니다. 16명이라도 이 스크립트를 한 번 실행한 상태에서
한 사람씩 순서대로 돌아가며 등록할 수 있습니다(사람마다 스크립트를 다시 켤
필요 없음). 등록 1명당 순서는 이렇습니다:

  1) 화면에 뜨는 카카오 로그인 주소를 그 사람에게 보여줍니다(화면 공유든,
     주소를 그대로 불러주든 상관없습니다).
  2) 그 사람이 "본인의" 카카오 계정으로 로그인하고 동의를 누르면, 브라우저가
     "사이트에 연결할 수 없음"류의 오류 페이지(예: http://localhost:5000/?code=xxxx)로
     넘어갑니다(그 포트에 실제로 떠 있는 서버가 없어서 나는 정상적인 오류입니다).
     그 상태에서 주소창에 찍힌 주소 전체(또는 code= 뒤의 값)를 복사해서 이
     스크립트에 붙여넣습니다.
  3) 카카오 서버와 통신해서 "리프레시 토큰"을 받아 그 사람 이름으로
     Conf/kakao_tokens.json 파일에 저장합니다.
  4) "한 명 더 등록하시겠어요? (y/n)"에 y를 입력하면 2)~3)을 다음 사람으로 반복,
     16명이 다 끝나면 n을 입력해 종료합니다.

그 이후로는 kakao_send.py를 실행할 때마다 등록된 사람 전원(단, Conf/kakao.txt에
이름이 있는 사람만)에게 자동으로 "나에게 보내기"(각자의 카카오톡 나와의
채팅)로 결과 요약이 전송됩니다. 코드 수정은 필요 없습니다.

--------------------------------------------------------------------------
[사전 준비 - 최초 1번만, 허원석님이 하시면 됩니다. 이미 하셨다면 건너뛰세요]

1. https://developers.kakao.com 접속 -> 로그인 -> "내 애플리케이션" -> 애플리케이션 추가
   (이름은 아무거나, 예: "보도자료 알림")

2. 만든 앱 클릭 -> 왼쪽 메뉴 "앱 키"에서 "REST API 키" 복사
   -> Conf/.env 파일에 아래 줄 추가:
      KAKAO_REST_API_KEY=여기에_REST_API_키_붙여넣기

3. 왼쪽 메뉴 "카카오 로그인" -> 활성화 설정을 "ON"으로 변경

4. 같은 화면의 "Redirect URI"에 아래 주소를 등록(추가) 버튼으로 추가:
      http://localhost:5000
   (실제로 그 포트에 서버가 떠 있지 않아도 됩니다. 등록만 해두면 됩니다.
   "https://localhost.com" 같은 주소는 콘솔에서 "유효하지 않은 URI"로
   거부되는 경우가 있어, 개발용으로 흔히 쓰이는 이 형식을 사용합니다. 포트
   번호(5000)는 아무 숫자나 써도 되지만, 여기 등록한 값과 아래 코드의
   REDIRECT_URI 값이 슬래시 유무까지 정확히 같아야 합니다.)

5. 왼쪽 메뉴 "카카오 로그인" -> "동의항목"으로 이동 -> "카카오톡 메시지 전송"
   항목을 찾아서 "필수 동의" 또는 "선택 동의"로 설정(사용 함으로 켜기)

[참고] 만약 특정 팀원이 로그인 화면에서 접근이 막히는 오류가 뜨면(앱이 아직
"검수/서비스 시작" 전이라 발생할 수 있음), 카카오 디벨로퍼스 콘솔의
"앱 설정 > 팀 관리"에서 그 팀원의 카카오 계정(이메일)을 추가해주시면 됩니다.
--------------------------------------------------------------------------
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = app_dir()
CONF_DIR = ROOT / "Conf"
ENV_FILE = CONF_DIR / ".env"
KAKAO_TOKENS_FILE = CONF_DIR / "kakao_tokens.json"
# [추가] kakao_send.py가 실제 발송 대상을 거르는 데 쓰는 파일. 여기서는 등록한
# 사람을 자동으로 이 파일에도 추가해줍니다(발송 대상으로 바로 활성화).
KAKAO_LIST_FILE = CONF_DIR / "kakao.txt"
# [수정] "https://localhost.com"으로 등록하면 콘솔에서 "유효하지 않은 URI"로
# 거부되는 경우가 있어, 대신 개발용으로 흔히 쓰이는 http://localhost:포트
# 형식으로 바꿨습니다. 실제로 그 포트에 서버가 떠 있을 필요는 없습니다 —
# 로그인 동의 후 브라우저가 이 주소로 이동을 "시도"만 하면 되고(연결 실패
# 페이지가 떠도 정상), 그때 주소창에 찍힌 ?code=... 값만 복사하면 됩니다.
# 카카오 콘솔에 등록하는 값과 아래 값은 (슬래시 유무까지) 정확히 같아야 합니다.
REDIRECT_URI = "http://localhost:5000"


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_tokens() -> dict:
    if not KAKAO_TOKENS_FILE.exists():
        return {}
    try:
        data = json.loads(KAKAO_TOKENS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def save_tokens(tokens: dict) -> None:
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    KAKAO_TOKENS_FILE.write_text(
        json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def ensure_kakao_recipient(name: str) -> None:
    """kakao.txt(실제 발송 대상 목록)에 이 이름이 없으면 새로 추가합니다.

    이미 있으면(주석 처리(#)된 상태여도) 손대지 않습니다 — 일부러 꺼둔
    사람일 수 있으니, 이미 목록에 존재하는 이름은 그대로 둡니다.
    """
    is_new_file = not KAKAO_LIST_FILE.exists()
    lines = KAKAO_LIST_FILE.read_text(encoding="utf-8").splitlines() if not is_new_file else []

    for raw in lines:
        line = raw.strip()
        candidate = line[1:].strip() if line.startswith("#") else line
        candidate_name = candidate.split(",")[0].strip()
        if candidate_name.lower() == name.lower():
            return  # 이미 목록에 있음(켜져 있든 꺼져 있든) -> 그대로 둠

    CONF_DIR.mkdir(parents=True, exist_ok=True)
    with KAKAO_LIST_FILE.open("a", encoding="utf-8") as f:
        if is_new_file:
            f.write("# 카카오톡 실제 발송 대상 목록 (kakao_auth_setup.py가 자동으로 추가)\n")
            f.write("# 특정 사람을 잠깐 끄고 싶으면 그 줄 맨 앞에 #을 붙이면 됩니다.\n")
        f.write(f"{name}\n")
    print(f"-> {KAKAO_LIST_FILE.name}에도 '{name}'을(를) 추가했습니다(발송 대상으로 활성화됨).")


def extract_code(pasted: str) -> str:
    pasted = pasted.strip()
    if "code=" in pasted:
        # 주소 전체를 붙여넣은 경우: code= 뒤부터 다음 & 전까지만 뽑아냅니다.
        query = urllib.parse.urlsplit(pasted).query
        params = urllib.parse.parse_qs(query)
        if "code" in params:
            return params["code"][0]
        # urlsplit으로 못 뽑으면 직접 잘라냅니다.
        after = pasted.split("code=", 1)[1]
        return after.split("&", 1)[0]
    return pasted  # code 값만 붙여넣은 경우


def register_one(client_id: str, client_secret: str, tokens: dict) -> bool:
    """한 사람의 로그인 동의 -> 토큰 발급 -> 저장까지 진행합니다.

    성공적으로 저장했으면 True, 중간에 취소/실패했으면 False를 반환합니다.
    (tokens 딕셔너리는 성공 시 제자리에서 갱신됩니다.)
    """
    authorize_url = (
        "https://kauth.kakao.com/oauth/authorize?"
        + urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "scope": "talk_message",
            }
        )
    )

    print("=" * 70)
    print("아래 주소를 '등록할 사람'에게 전달하거나, 그 사람 옆에서 함께")
    print("브라우저로 열어주세요. 그 사람의 카카오 계정으로 로그인/동의해야 합니다.")
    print("=" * 70)
    print(authorize_url)
    print()
    print(f"로그인/동의 후 브라우저가 {REDIRECT_URI}/?code=... 로 이동을 시도하다가")
    print("'사이트에 연결할 수 없음' 같은 오류 화면이 뜹니다(정상입니다). 그 상태에서")
    print("주소창 전체를 복사해서 아래에 붙여넣어 주세요. (건너뛰려면 그냥 Enter)")
    print()

    pasted = input("주소(또는 code 값) 붙여넣기: ").strip()
    if not pasted:
        print("입력이 없어 이 사람은 건너뜁니다.")
        return False
    code = extract_code(pasted)

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
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        print(f"[오류] 토큰 발급 실패({e.code}): {detail}")
        print("코드는 1회용이라 재사용이 안 됩니다. 이 사람은 처음부터 다시 시도해주세요.")
        return False

    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        print(f"[오류] 응답에 refresh_token이 없습니다: {payload}")
        return False

    name = input("이 토큰을 저장할 이름(예: 홍길동, 아무 이름이나): ").strip()
    if not name:
        name = f"이름없음_{len(tokens) + 1}"

    tokens[name] = {"refresh_token": refresh_token}
    save_tokens(tokens)
    ensure_kakao_recipient(name)

    print(f"-> 저장 완료: {name}")
    print()
    return True


def main() -> None:
    load_env(ENV_FILE)
    client_id = os.getenv("KAKAO_REST_API_KEY", "").strip()
    client_secret = os.getenv("KAKAO_CLIENT_SECRET", "").strip()

    if not client_id:
        print(f"[오류] KAKAO_REST_API_KEY가 없습니다. {ENV_FILE} 파일에")
        print("       KAKAO_REST_API_KEY=발급받은_REST_API_키")
        print("       줄을 추가한 뒤 다시 실행해주세요. (이 스크립트 맨 위 안내 참고)")
        return

    tokens = load_tokens()
    if tokens:
        print(f"현재 등록된 사람({len(tokens)}명): {', '.join(tokens.keys())}")
        print("(같은 이름을 다시 등록하면 그 사람 토큰만 갱신됩니다)")
    print()

    registered_count = 0
    while True:
        if register_one(client_id, client_secret, tokens):
            registered_count += 1

        more = input("한 명 더 등록하시겠어요? (y/n): ").strip().lower()
        print()
        if more not in {"y", "yes", "ㅇ"}:
            break

    print("=" * 70)
    print(f"이번 실행에서 새로 등록/갱신한 인원: {registered_count}명")
    print(f"전체 등록 인원: {len(tokens)}명 -> {', '.join(tokens.keys()) if tokens else '(없음)'}")
    print(f"저장 위치: {KAKAO_TOKENS_FILE}")
    print("이제 kakao_send.py를 실행하면 등록된 사람 전원의 카카오톡")
    print("'나와의 채팅'으로 결과 요약이 자동으로 전송됩니다.")
    print("=" * 70)


if __name__ == "__main__":
    main()