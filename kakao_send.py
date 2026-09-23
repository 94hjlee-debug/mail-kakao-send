from __future__ import annotations

import html as html_lib
import http.cookiejar
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = app_dir()
CONF_DIR = ROOT / "Conf"
OUTPUT_DIR = ROOT / "output"
STATE_DIR = ROOT / "state"
ENV_FILE = CONF_DIR / ".env"
MAIL_LIST_FILE = CONF_DIR / "maillist.txt"
SITES_FILE = CONF_DIR / "sites.txt"
# 카카오톡 "친구에게 보내기"용 설정.
# - sender token: 발신자 1명의 refresh_token (외부 공개 금지)
# - recipients: 친구 목록 API에서 선택한 수신자 uuid 목록 (외부 공개 금지)
KAKAO_SENDER_TOKEN_FILE = CONF_DIR / "kakao_sender_token.json"
KAKAO_RECIPIENTS_FILE = CONF_DIR / "kakao_friend_recipients.json"
# 실제 친구 발송 대상 이름 목록. 한 줄에 한 명, #으로 시작하면 제외합니다.
KAKAO_LIST_FILE = CONF_DIR / "kakao.txt"
# [수정] 날짜 정보가 없는 사이트(예: cionew 처럼 목록형 페이지)를 위한
# "이미 보낸 링크" 캐시 파일. 여기 기록된 링크는 다음 실행부터 제외됩니다.
SEEN_LINKS_FILE = STATE_DIR / "seen_links.json"
SEEN_LINKS_KEEP_DAYS = 30  # 이 기간이 지난 캐시 항목은 자동으로 정리합니다.
# [추가] 하루 여러 번(예: 09:10/14:00/19:00) 실행할 때, 그날 이미 보낸 내용과
# 똑같으면(추가로 새로 올라온 글/새 오류가 없으면) 메일·카카오톡 발송을
# 건너뛰기 위한 "오늘 보낸 내용" 기록 파일. 날짜가 바뀌면 자동으로 초기화됩니다.
SENT_STATE_FILE = STATE_DIR / "sent_today.json"


@dataclass
class Site:
    name: str
    url: str
    note: str


@dataclass
class Item:
    title: str
    pub_date: str
    link: str


def load_env(path: Path) -> None:
    """Conf/.env 값을 실행 환경에 적용합니다.

    이 프로그램은 .env를 운영 설정의 기준으로 사용하므로, 같은 이름의 Windows
    환경변수가 이미 있어도 .env 값으로 덮어씁니다.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def env_flag(name: str, default: bool = False) -> bool:
    """1/true/yes/on이면 True, 0/false/no/off이면 False를 반환합니다."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def load_mail_list(path: Path) -> list[str]:
    if not path.exists():
        print(f"메일 목록 파일이 없습니다: {path}")
        return []

    recipients: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        address = line.split(",")[0].strip()
        key = address.lower()
        if "@" in address and key not in seen:
            seen.add(key)
            recipients.append(address)
    return recipients


def load_sites(path: Path) -> list[Site]:
    if not path.exists():
        raise FileNotFoundError(f"사이트 목록 파일이 없습니다: {path}")

    sites: list[Site] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split(",", 2)]
        if len(parts) < 2:
            continue
        name, url = parts[0], parts[1]
        note = parts[2] if len(parts) > 2 else ""
        if name in {"사이트명", "사이트 명"}:
            continue
        if not url.startswith("http"):
            continue
        sites.append(Site(name=name, url=url, note=note))
    if not sites:
        raise RuntimeError(f"읽을 사이트 주소가 없습니다: {path}")
    return sites


# [추가] "이미 보낸 링크" 캐시 로드/저장/정리 -----------------------------------

def load_seen_links(path: Path) -> dict[str, str]:
    """{링크: 최초로 발견한 날짜(YYYY-MM-DD)} 형태의 캐시를 읽어옵니다."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_seen_links(path: Path, seen: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")


def prune_seen_links(seen: dict[str, str], keep_days: int = SEEN_LINKS_KEEP_DAYS) -> dict[str, str]:
    """너무 오래된 캐시 항목은 지워서 파일이 무한정 커지지 않게 합니다."""
    cutoff = datetime.now() - timedelta(days=keep_days)
    pruned: dict[str, str] = {}
    for link, date_str in seen.items():
        try:
            seen_dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        if seen_dt >= cutoff:
            pruned[link] = date_str
    return pruned


# ------------------------------------------------------------------------

# [추가] 하루 여러 번 실행 시 "새 내용이 있을 때만" 발송하기 위한 상태 기록 ------
#
# 각 사이트/글마다 "서명(signature)"을 만들어서, 이번 실행에서 만든 서명
# 집합이 오늘 이미 보낸 서명 집합에 다 포함돼 있으면(=새로운 게 하나도 없으면)
# 메일/카카오톡 발송을 건너뜁니다. 날짜가 바뀌면(다음날 09:10 실행 등) 자동으로
# 초기화되어, 그날의 첫 실행은 내용이 있든 없든 항상 발송합니다.
def build_run_signatures(results: list[tuple[Site, list[Item], str | None]]) -> set[str]:
    signatures: set[str] = set()
    for site, items, err in results:
        if err:
            signatures.add(f"{site.name}::오류")
        for item in items:
            key = item.link or item.title
            signatures.add(f"{site.name}::{key}")
    return signatures


def load_sent_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_sent_state(path: Path, date_str: str, signatures: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"date": date_str, "signatures": sorted(signatures)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ------------------------------------------------------------------------

# [추가] 일부 사이트는 서버가 인증서 체인 중 "중간 인증기관(중간 CA)" 인증서를
# 빠뜨리고 보내는 경우가 있습니다. 일반 브라우저(엣지/크롬)는 이걸 자동으로
# 보완해서 접속이 되지만, 파이썬의 기본 urllib 요청은 그런 보완을 하지 않아서
# "CERTIFICATE_VERIFY_FAILED (unable to get local issuer certificate)" 오류로
# 막힙니다. 금융지주 입찰공고(nhfngroup.com)에서 실제로 이 오류가 확인됐습니다.
# 이 목록에 있는 사이트만 예외적으로 인증서 검증을 건너뛰고, 그 외 모든
# 사이트는 기존처럼 정상적으로 인증서를 검증합니다. (공개된 입찰공고 게시판을
# 읽어오기만 하는 용도라 위험도는 낮지만, 검증을 건너뛴다는 점은 알고 계시면
# 좋을 것 같아 주석으로 남겨둡니다.)
SSL_VERIFY_SKIP_HOSTS = {"nhfngroup.com"}


def _ssl_context_for(url: str) -> ssl.SSLContext | None:
    host = (urlsplit(url).hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in SSL_VERIFY_SKIP_HOSTS):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    return None  # None이면 urlopen 기본(정상) 인증서 검증을 사용합니다.


def fetch_bytes(url: str) -> tuple[str, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    context = _ssl_context_for(url)
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        content_type = response.headers.get("Content-Type", "")
        return content_type, response.read()


def decode_body(content_type: str, data: bytes) -> str:
    match = re.search(r"charset=([\w-]+)", content_type, re.I)
    encoding = match.group(1) if match else "utf-8"
    try:
        return data.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return data.decode("utf-8", errors="replace")


def looks_like_rss(url: str, content_type: str, text: str) -> bool:
    lowered = text.lstrip()[:200].lower()
    return (
        "rss" in url.lower()
        or "xml" in content_type.lower()
        or lowered.startswith("<?xml")
        or lowered.startswith("<rss")
    )


def parse_rss_items(text: str) -> list[Item]:
    root = ET.fromstring(text)
    items: list[Item] = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        if title:
            items.append(Item(title=title, pub_date=pub_date, link=link))
    return items


def clean_title(text: str) -> str:
    text = html_lib.unescape(re.sub(r"\s+", " ", text)).strip()
    text = re.sub(r"\s*새글\s*$", "", text).strip()
    if text in {"새글", "더보기", "다음", "이전", "홈", "로그인"}:
        return ""
    if len(text) < 2:  # 너무 짧은 텍스트 제외
        return ""
    return text


def parse_flexible_date(text: str) -> str | None:
    """다양한 날짜 및 상대적 시간 형식을 오늘 YYYY-MM-DD 형태로 변환"""
    today_str = datetime.now().strftime("%Y-%m-%d")

    # 1. 정규식 20XX-MM-DD, 20XX.MM.DD, 20XX/MM/DD
    match = re.search(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
    if match:
        y, m, d = match.groups()
        m_i, d_i = int(m), int(d)
        if 1 <= m_i <= 12 and 1 <= d_i <= 31:  # [수정] 말도 안 되는 월/일 오매칭 방지
            return f"{y}-{m_i:02d}-{d_i:02d}"

    # 2. 상대 시간 표현 (방금, X분 전, X시간 전) -> 오늘 글 처리
    if re.search(r"(방금|분\s*전|시간\s*전|초\s*전)", text):
        return today_str

    # 3. '어제' 표현
    if "어제" in text:
        return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    return None


class BoardHTMLParser(HTMLParser):
    """기존 테이블(tr) 기반 게시판 파서"""
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self._in_tr = False
        self._in_a = False
        self._skip_depth = 0
        self._row_text: list[str] = []
        self._row_links: list[tuple[str, str]] = []
        self._a_href = ""
        self._a_text: list[str] = []
        self.rows: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: (value or "") for key, value in attrs}
        if tag in {"script", "style", "nav", "footer"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "tr":
            self._in_tr = True
            self._row_text = []
            self._row_links = []
        if self._in_tr and tag == "a":
            href = attrs_dict.get("href", "").strip()
            if href and not href.lower().startswith(("javascript:", "#")):
                self._in_a = True
                self._a_href = href
                self._a_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "nav", "footer"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "a" and self._in_a:
            title = clean_title("".join(self._a_text))
            if title:
                self._row_links.append((title, self._a_href))
            self._in_a = False
        if tag == "tr" and self._in_tr:
            self._finish_row()
            self._in_tr = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not self._in_tr:
            return
        self._row_text.append(data)
        if self._in_a:
            self._a_text.append(data)

    def _finish_row(self) -> None:
        row = " ".join(self._row_text)
        pub_date = parse_flexible_date(row) or ""
        if not self._row_links:
            return
        title, href = pick_row_link(self._row_links)
        if not title:
            return
        self.rows.append((title, pub_date, urljoin(self.base_url, href)))


class GeneralHTMLParser(HTMLParser):
    """비-게시판형 웹사이트(뉴스, 카드형 목록) 범용 파서"""
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self._skip = False
        self._in_a = False
        self._current_href = ""
        self._current_text: list[str] = []
        self.items: list[tuple[str, str, str]] = []
        self._seen_links: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: (value or "") for key, value in attrs}
        if tag in {"script", "style", "header", "footer", "nav"}:
            self._skip = True
            return
        if tag == "a" and not self._skip:
            href = attrs_dict.get("href", "").strip()
            if href and not href.lower().startswith(("javascript:", "#")):
                full_url = urljoin(self.base_url, href)
                if full_url not in self._seen_links:
                    self._in_a = True
                    self._current_href = full_url
                    self._current_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "header", "footer", "nav"}:
            self._skip = False
            return
        if tag == "a" and self._in_a:
            title = clean_title("".join(self._current_text))
            # 뉴스 기사 타이틀 길이 조건 (보통 10자 이상)
            if title and len(title) >= 8:
                # [수정] 예전에는 날짜를 못 찾으면 무조건 "오늘"로 채워서
                # cionew 같은 목록형 페이지가 매번 "오늘 새 글"로 잘못 잡혔습니다.
                # 이제는 실제로 날짜를 찾은 경우에만 채우고, 못 찾으면 빈 값으로 두어
                # 아래 todays_items()의 "이미 보낸 링크" 캐시로 신규 여부를 판단합니다.
                pub_date = parse_flexible_date(title) or ""
                self.items.append((title, pub_date, self._current_href))
                self._seen_links.add(self._current_href)
            self._in_a = False

    def handle_data(self, data: str) -> None:
        if self._in_a and not self._skip:
            self._current_text.append(data)


def pick_row_link(links: list[tuple[str, str]]) -> tuple[str, str]:
    for title, href in links:
        if any(k in href for k in ["artclView", "View.do", "article", "news", "detail"]):
            return title, href
    return links[0]


def parse_html_items(base_url: str, text: str) -> list[Item]:
    # 1차 시도: 게시판 파서
    board_parser = BoardHTMLParser(base_url)
    board_parser.feed(text)
    if board_parser.rows:
        return [Item(title=t, pub_date=d, link=l) for t, d, l in board_parser.rows]

    # 2차 시도: 일반 파서 (비게시판형 뉴스 사이트 대응)
    general_parser = GeneralHTMLParser(base_url)
    general_parser.feed(text)
    return [Item(title=t, pub_date=d, link=l) for t, d, l in general_parser.items]


def fetch_items(site: Site) -> list[Item]:
    # [수정] 예전에는 여기서 에러를 잡아 빈 목록만 돌려줘서, 실제로는 수집이
    # 실패(예: 440)했는데도 리포트에는 그냥 "오늘 등록된 글이 없습니다"로만
    # 보여서 원인을 알 수 없었습니다. 이제는 에러를 그대로 위(main)로 올려서
    # 리포트에 "수집 상태: 오류 발생 (...)"으로 실제 원인이 찍히게 합니다.
    if FIRST_EPRO_HOST_MARKER in site.url:
        return fetch_first_epro_items(site)
    if NONGHYUP_BID_PATH_MARKER in site.url:
        return fetch_nonghyup_bid_items(site)
    content_type, data = fetch_bytes(site.url)
    text = decode_body(content_type, data)
    if looks_like_rss(site.url, content_type, text):
        items = parse_rss_items(text)
    else:
        items = parse_html_items(site.url, text)
    return items


def item_date(pub_date: str) -> str | None:
    if not pub_date:
        return None
    raw = pub_date.strip()
    date_part = raw[:10].replace(".", "-").replace("/", "-")
    try:
        return datetime.strptime(date_part, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        pass
    # [추가] "20260914"처럼 구분자 없는 8자리 날짜 형식도 지원 (first-epro 등 API 응답 대응)
    digits = raw[:8]
    if len(digits) == 8 and digits.isdigit():
        try:
            return datetime.strptime(digits, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


# [추가] 농협(nonghyup.com) e홍보센터 입찰공고 게시판 수집기 -------------------
#
# 이 게시판은 목록이 페이지 HTML에 그대로 들어있는(자바스크립트 AJAX 필요
# 없는) 평범한 게시판형 페이지입니다. 다만 제목 링크가 진짜 주소(href)가
# 아니라 <a href="#none" onclick="fn_select_brdView('7472');"> 처럼 자바
# 스크립트로 폼을 제출해 상세화면으로 이동하는 방식이라, 기존 게시판
# 파서(BoardHTMLParser)는 href가 "#"으로 시작하는 링크를 전부 걸러내도록
# 되어 있어 이 게시판에서는 글을 하나도 못 찾습니다. 그래서 이 게시판
# 전용으로 onclick 안의 글 번호(blbdSqno)를 정규식으로 뽑아내는 간단한
# 파서를 별도로 둡니다. 상세 페이지는 /ecenter/bid/bidView.do?blbdSqno=번호
# 형태의 일반 GET 주소로도 그대로 열리는 것을 확인했습니다(로그인 불필요).
NONGHYUP_BID_PATH_MARKER = "nonghyup.com/ecenter/bid/bidList.do"

_NONGHYUP_BID_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
_NONGHYUP_BID_LINK_RE = re.compile(
    r"onclick=\"fn_select_brdView\('(\d+)'\);\">\s*(.*?)\s*</a>", re.S
)
_NONGHYUP_BID_DATE_RE = re.compile(r"(\d{4}\.\d{2}\.\d{2})")


def fetch_nonghyup_bid_items(site: Site) -> list[Item]:
    content_type, data = fetch_bytes(site.url)
    text = decode_body(content_type, data)

    # 입찰공고 목록 표(<table class="list">...</table>)만 잘라냅니다.
    # 못 찾으면 페이지 전체에서 찾습니다(화면 구조가 살짝 바뀌어도 최대한 동작하도록).
    table_match = re.search(r'<table class="list".*?</table>', text, re.S)
    block = table_match.group(0) if table_match else text

    items: list[Item] = []
    for row_match in _NONGHYUP_BID_ROW_RE.finditer(block):
        row = row_match.group(1)
        link_match = _NONGHYUP_BID_LINK_RE.search(row)
        if not link_match:
            continue  # 제목 링크가 없는 행(헤더 등)은 건너뜁니다.
        bid_id, title_raw = link_match.groups()
        title = clean_title(title_raw)
        if not title:
            continue

        date_match = _NONGHYUP_BID_DATE_RE.search(row)
        pub_date = date_match.group(1).replace(".", "-") if date_match else ""

        link = urljoin(site.url, f"/ecenter/bid/bidView.do?blbdSqno={bid_id}")
        items.append(Item(title=title, pub_date=pub_date, link=link))

    if not items:
        found = "찾음" if table_match else "못 찾음"
        raise RuntimeError(
            f"입찰공고 목록을 찾지 못했습니다 (목록 표: {found}). "
            f"사이트 화면 구조가 바뀌었을 수 있습니다."
        )
    return items


# [수정] FIRSTePro(농협정보시스템 전자구매/계약) 입찰공고 수집기 -----------------
#
# 그동안 "공고 목록 화면 주소(03_notice_list2.jsp)"로 곧바로 들어가서 목록 API
# (/nhepro/mainNoticeList2.so)를 호출하는 방식으로 여러 차례 시도했지만, 실제
# 브라우저로 "홈페이지 → 공고 화면 → 검색 버튼"까지 그대로 재현해도 이 API는
# 매번 세션 오류(440)만 돌려줬습니다 (원석님 PC에서 직접 확인).
#
# 진짜 원인을 찾았습니다: 화면의 "더보기" 버튼은 03_notice_list2.jsp로 바로
# 가는 게 아니라, 실제로는 아래처럼 한 단계를 더 거칩니다.
#   /session/viewContents/view.so?realUrl=/mainHtml/03_customer/03_notice_list2.jsp
# 즉 "래퍼(경유지) 주소"를 거쳐야 서버가 세션에 뭔가를 남기고, 그래야만 그다음
# mainNoticeList2.so 호출이 통과됩니다. 03_notice_list2.jsp 주소로 아무리
# 먼저 들어가도(홈페이지를 거쳤어도) 이 경유지를 거치지 않으면 계속 440이
# 났던 것이었습니다. 이 경유지 주소로 먼저 들어간 뒤 같은 세션(쿠키)으로
# 목록 API를 호출하도록 고쳤습니다. Playwright 같은 브라우저 자동화 없이,
# 쿠키를 유지하는 일반 HTTP 요청만으로 전체 목록(페이지당 10건, 최신순)을
# 가져올 수 있습니다.
FIRST_EPRO_HOST_MARKER = "first-epro.com"
FIRST_EPRO_HOME_PATH = "/welcome.so"
# [수정] 실제 "더보기" 버튼이 이동하는 경유지 주소. 이 주소를 먼저 거쳐야
# 목록 API 호출이 세션에서 허용됩니다.
FIRST_EPRO_LIST_WRAPPER_PATH = (
    "/session/viewContents/view.so?realUrl=/mainHtml/03_customer/03_notice_list2.jsp"
)
FIRST_EPRO_LIST_API_PATH = "/nhepro/mainNoticeList2.so"


def _fetch_first_epro_records(site: Site) -> list[dict]:
    """FIRSTePro 목록을 여러 페이지 조회합니다.

    과거 날짜 테스트 시 1페이지(최신 10건)만 보면 대상 날짜의 공고가 이미
    다음 페이지로 밀려 있을 수 있어 SEARCH_MAX_PAGES 만큼 조회합니다.
    대상 날짜보다 오래된 글까지 도달하면 더 이상 페이지를 읽지 않습니다.
    """
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))

    home_url = urljoin(site.url, FIRST_EPRO_HOME_PATH)
    wrapper_url = urljoin(site.url, FIRST_EPRO_LIST_WRAPPER_PATH)
    api_url = urljoin(site.url, FIRST_EPRO_LIST_API_PATH)

    opener.open(urllib.request.Request(home_url, headers={"User-Agent": USER_AGENT}), timeout=30).read()
    opener.open(urllib.request.Request(wrapper_url, headers={"User-Agent": USER_AGENT}), timeout=30).read()

    raw_max_pages = os.getenv("SEARCH_MAX_PAGES", "5").strip()
    try:
        max_pages = max(1, min(int(raw_max_pages), 20))
    except ValueError:
        max_pages = 5

    target = target_search_date()
    all_records: list[dict] = []
    seen_keys: set[tuple[str, str, str]] = set()

    for page_no in range(1, max_pages + 1):
        form_data = urlencode(
            {
                "selectcustomer": "g_customer",
                "buyerNm": "",
                "buyerCd": "",
                "selectType": "title",
                "searchText": "",
                "pageNo": str(page_no),
                "countFlag": "0",
            }
        ).encode("utf-8")
        api_request = urllib.request.Request(
            api_url,
            data=form_data,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        with opener.open(api_request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")

        records = json.loads(raw)
        if not isinstance(records, list):
            raise RuntimeError(f"예상과 다른 응답 형식입니다: {raw[:200]!r}")
        if not records:
            break

        added = 0
        page_dates: list[str] = []
        for record in records:
            key = (
                str(record.get("BUYER_CD", "")),
                str(record.get("BID_NUM", "")),
                str(record.get("BID_CNT", "")),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_records.append(record)
            added += 1
            d = item_date(str(record.get("REG_DATE", "")))
            if d:
                page_dates.append(d)

        if added == 0:
            break

        # 최신순 목록이므로 현재 페이지에서 이미 대상일보다 과거까지 내려왔다면 종료합니다.
        if page_dates and min(page_dates) <= target:
            break

    return all_records


def fetch_first_epro_items(site: Site) -> list[Item]:
    # 가끔 일시적으로 실패할 수 있어 한 번 재시도합니다.
    last_error: Exception | None = None
    records: list[dict] = []
    for attempt in range(2):
        try:
            records = _fetch_first_epro_records(site)
            last_error = None
            break
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as e:
            last_error = e
            continue
    if last_error is not None:
        raise RuntimeError(f"목록을 받지 못했습니다(2번 시도): {last_error}") from last_error

    items: list[Item] = []
    for record in records:
        ann_item_title = str(record.get("ANN_ITEM_TITLE", "")).strip()
        subject = str(record.get("SUBJECT", "")).strip()
        title = clean_title(f"{ann_item_title} {subject}".strip())
        if not title:
            continue

        pub_date = item_date(str(record.get("REG_DATE", ""))) or ""

        buyer_cd = record.get("BUYER_CD", "")
        bid_num = record.get("BID_NUM", "")
        bid_cnt = record.get("BID_CNT", "")
        bid_status = str(record.get("BID_STATUS", ""))
        real_url = (
            "/nhepro/CBDI/CBDR0014/view.so"
            if bid_status in {"2303", "2330"}
            else "/nhepro/CBDI/CBDR0012/view.so"
        )
        link = urljoin(
            site.url,
            f"/session/viewContents/view.so?realUrl={real_url}"
            f"&buyerCd={buyer_cd}&bidNum={bid_num}&bidCnt={bid_cnt}",
        )
        items.append(Item(title=title, pub_date=pub_date, link=link))

    return items


def search_days_ago() -> int:
    """Conf/.env의 SEARCH_DAYS_AGO 값을 읽습니다. 0=오늘, 1=어제, 2=이틀 전."""
    raw = os.getenv("SEARCH_DAYS_AGO", "0").strip()
    try:
        value = int(raw)
    except ValueError:
        print(f"SEARCH_DAYS_AGO={raw!r} 값이 올바르지 않아 0(오늘)으로 처리합니다.")
        return 0
    if value < 0:
        print("SEARCH_DAYS_AGO는 0 이상이어야 하므로 0(오늘)으로 처리합니다.")
        return 0
    return value


def target_search_date() -> str:
    return (datetime.now() - timedelta(days=search_days_ago())).strftime("%Y-%m-%d")


def todays_items(items: list[Item], seen_links: dict[str, str]) -> list[Item]:
    """
    SEARCH_DAYS_AGO로 지정한 날짜의 글만 골라냅니다.

    - SEARCH_DAYS_AGO=0: 오늘
    - SEARCH_DAYS_AGO=1: 어제
    - SEARCH_DAYS_AGO=2: 이틀 전

    날짜가 있는 항목은 해당 날짜와 정확히 비교합니다.
    날짜가 없는 항목은 과거 날짜를 판별할 근거가 없으므로,
    오늘 검색(0)일 때는 처음 본 링크만 신규로 포함합니다.
    이미 seen_links에 기록된 링크는 기록된 최초 발견일이 검색 대상일과
    같을 때만 포함합니다.
    """
    target = target_search_date()
    days_ago = search_days_ago()
    matched: list[Item] = []
    for item in items:
        parsed_dt = item_date(item.pub_date)
        if parsed_dt:
            if parsed_dt == target:
                matched.append(item)
            continue

        if not item.link:
            continue
        first_seen = seen_links.get(item.link)
        if first_seen == target:
            matched.append(item)
        elif days_ago == 0 and first_seen is None:
            matched.append(item)
    return matched


def build_report(results: list[tuple[Site, list[Item], str | None]]) -> str:
    target = target_search_date()
    days_ago = search_days_ago()
    lines = [
        f"수집 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"검색 대상일: {target} (SEARCH_DAYS_AGO={days_ago})",
        "",
    ]
    for site, items, err in results:
        lines.append(f"[{site.name}]")
        if site.note:
            lines.append(f"비고: {site.note}")

        if err:
            lines.append(f"수집 상태: 오류 발생 ({err})")
        elif not items:
            lines.append(f"{target} 등록된 글이 없습니다.")
        else:
            for index, item in enumerate(items, start=1):
                lines.append(f"{index}. {item.title}")
                if item.pub_date:
                    lines.append(f"   등록: {item.pub_date}")
                if item.link:
                    lines.append(f"   링크: {item.link}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_file(text: str) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    target = target_search_date()
    path = OUTPUT_DIR / f"보도자료_{target}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def build_message(body: str, saved_path: Path, mail_from: str, mail_to: str, count: int) -> EmailMessage:
    target = target_search_date()
    message = EmailMessage()
    message["Subject"] = f"[보도자료/뉴스] {target} ({count}건 수집)"
    message["From"] = mail_from
    message["To"] = mail_to
    message.set_content(body, charset="utf-8")
    message.add_attachment(
        saved_path.read_bytes(),
        maintype="text",
        subtype="plain",
        filename=saved_path.name,
    )
    return message


def send_email(body: str, saved_path: Path, count: int) -> bool:
    """메일을 발송하고 한 명 이상에게 성공하면 True를 반환합니다."""
    host = os.getenv("SMTP_HOST", "").strip()
    try:
        port = int(os.getenv("SMTP_PORT", "587"))
    except ValueError:
        print("SMTP_PORT 값이 올바르지 않습니다.")
        return False
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    mail_from = os.getenv("MAIL_FROM", user).strip()
    recipients = load_mail_list(MAIL_LIST_FILE)

    if not all([host, user, password]):
        print(f"메일 서버 설정({ENV_FILE.name})이 없어 메일 발송을 건너뜁니다.")
        return False
    if not recipients:
        print("보낼 메일 주소가 없어 메일 발송을 건너뜁니다.")
        return False

    try:
        context = ssl.create_default_context()
        sent_count = 0
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls(context=context)
            smtp.login(user, password)
            for mail_to in recipients:
                try:
                    smtp.send_message(build_message(body, saved_path, mail_from, mail_to, count))
                    sent_count += 1
                    print(f"메일을 보냈습니다: {mail_to}")
                except Exception as e:
                    print(f"메일 발송 실패 ({mail_to}): {e}")
        return sent_count > 0
    except Exception as e:
        print(f"메일 서버 연결/인증 실패: {e}")
        return False


# 카카오톡 "친구에게 보내기" 전송 -------------------------------------------
#
# 이 버전은 발신자 1명의 카카오 사용자 토큰을 사용해, 카카오 친구 목록 API에서
# 선택해 저장한 수신자 uuid들에게 메시지를 전송합니다. 카카오 API 제한상 한 번에
# 최대 5명까지 보낼 수 있으므로 수신자가 16명이면 5+5+5+1로 자동 분할합니다.
#
# 사전 준비는 kakao_friend_setup.py에서 진행합니다.

def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def kakao_refresh_access_token(refresh_token: str) -> tuple[str, str | None]:
    """발신자의 refresh_token으로 새 access_token을 발급합니다."""
    client_id = os.getenv("KAKAO_REST_API_KEY", "").strip()
    if not client_id:
        raise RuntimeError("KAKAO_REST_API_KEY가 설정되어 있지 않습니다 (Conf/.env 확인)")
    client_secret = os.getenv("KAKAO_CLIENT_SECRET", "").strip()

    data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }
    if client_secret:
        data["client_secret"] = client_secret

    request = urllib.request.Request(
        "https://kauth.kakao.com/oauth/token",
        data=urlencode(data).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))

    access_token = str(payload["access_token"])
    new_refresh_token = payload.get("refresh_token")
    return access_token, (str(new_refresh_token) if new_refresh_token else None)


def kakao_sender_access_token() -> str:
    """저장된 발신자 refresh_token을 읽어 access_token을 만들고 필요 시 토큰을 갱신합니다."""
    if not KAKAO_SENDER_TOKEN_FILE.exists():
        raise RuntimeError(
            f"발신자 토큰 파일이 없습니다: {KAKAO_SENDER_TOKEN_FILE}. "
            "먼저 kakao_friend_setup.py에서 발신자 인증을 진행하세요."
        )
    try:
        token_data = json.loads(KAKAO_SENDER_TOKEN_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"발신자 토큰 파일을 읽지 못했습니다: {e}") from e

    refresh_token = str(token_data.get("refresh_token", "")).strip()
    if not refresh_token:
        raise RuntimeError("발신자 refresh_token이 없습니다. 발신자 인증을 다시 진행하세요.")

    access_token, new_refresh_token = kakao_refresh_access_token(refresh_token)
    if new_refresh_token and new_refresh_token != refresh_token:
        token_data["refresh_token"] = new_refresh_token
        _write_json_atomic(KAKAO_SENDER_TOKEN_FILE, token_data)
    return access_token


def load_kakao_active_names() -> list[str]:
    """Conf/kakao.txt에서 실제 발송 대상 이름을 읽습니다."""
    if not KAKAO_LIST_FILE.exists():
        return []

    result: list[str] = []
    seen: set[str] = set()
    for raw in KAKAO_LIST_FILE.read_text(encoding="utf-8").splitlines():
        name = raw.strip()
        if not name or name.startswith("#"):
            continue
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            result.append(name)
    return result


def load_kakao_receiver_uuids() -> list[str]:
    """
    Conf/kakao.txt에 있는 이름만 실제 친구 발송 대상으로 변환합니다.

    이름 -> uuid 매핑은 kakao_friend_setup.py가 만든
    Conf/kakao_friend_recipients.json의 recipients 항목을 사용합니다.
    """
    active_names = load_kakao_active_names()
    if not active_names:
        print(f"{KAKAO_LIST_FILE.name}에 활성 사용자가 없어 친구 발송을 건너뜁니다.")
        return []

    if not KAKAO_RECIPIENTS_FILE.exists():
        print(f"카카오 수신자 매핑 파일이 없습니다: {KAKAO_RECIPIENTS_FILE}")
        return []

    try:
        data = json.loads(KAKAO_RECIPIENTS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"카카오 수신자 파일을 읽지 못했습니다: {e}") from e

    if not isinstance(data, dict):
        raise RuntimeError("kakao_friend_recipients.json 형식이 올바르지 않습니다.")

    recipients = data.get("recipients")
    if not isinstance(recipients, dict):
        if isinstance(data.get("receiver_uuids"), list):
            raise RuntimeError(
                "기존 uuid 전용 수신자 파일입니다. "
                "kakao_friend_setup.py 메뉴 3에서 사용자를 이름과 함께 다시 등록하세요."
            )
        raise RuntimeError("kakao_friend_recipients.json에 recipients 매핑이 없습니다.")

    # 대소문자 차이에도 같은 이름으로 찾을 수 있도록 인덱스 구성
    by_name: dict[str, tuple[str, dict]] = {}
    for saved_name, info in recipients.items():
        if isinstance(info, dict):
            by_name[str(saved_name).casefold()] = (str(saved_name), info)

    result: list[str] = []
    seen_uuids: set[str] = set()
    missing_names: list[str] = []
    selected_names: list[str] = []

    for active_name in active_names:
        found = by_name.get(active_name.casefold())
        if not found:
            missing_names.append(active_name)
            continue
        saved_name, info = found
        uuid = str(info.get("uuid", "")).strip()
        if not uuid:
            missing_names.append(active_name)
            continue
        if uuid not in seen_uuids:
            seen_uuids.add(uuid)
            result.append(uuid)
            selected_names.append(saved_name)

    if selected_names:
        print("카카오 친구 발송 대상: " + ", ".join(selected_names))
    if missing_names:
        print(
            "주의: kakao.txt에는 있지만 UUID 매핑이 없는 사용자: "
            + ", ".join(missing_names)
            + " (kakao_friend_setup.py 메뉴 3에서 등록 필요)"
        )

    return result


def _chunks(values: list[str], size: int = 5):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def kakao_send_custom_to_self(
    access_token: str,
    template_id: str,
    template_args: dict[str, str],
) -> None:
    """사용자 정의 리스트 템플릿으로 발신자 본인에게 전송합니다."""
    data = urlencode(
        {
            "template_id": template_id,
            "template_args": json.dumps(template_args, ensure_ascii=False),
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        "https://kapi.kakao.com/v2/api/talk/memo/send",
        data=data,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"카카오 나에게 보내기 API 오류({e.code}): {detail}") from e


def kakao_send_custom_to_friends(
    access_token: str,
    receiver_uuids: list[str],
    template_id: str,
    template_args: dict[str, str],
) -> dict:
    """사용자 정의 리스트 템플릿으로 친구에게 전송합니다(요청당 최대 5명)."""
    if not receiver_uuids or len(receiver_uuids) > 5:
        raise ValueError("receiver_uuids는 1~5명이어야 합니다.")

    data = urlencode(
        {
            "receiver_uuids": json.dumps(receiver_uuids, ensure_ascii=False),
            "template_id": template_id,
            "template_args": json.dumps(template_args, ensure_ascii=False),
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        "https://kapi.kakao.com/v1/api/talk/friends/message/send",
        data=data,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"카카오 친구 메시지 API 오류({e.code}): {detail}") from e


def _clean_kakao_text(value: str, max_chars: int) -> str:
    """카카오 표시용으로 공백을 정리하고 너무 긴 문자열만 말줄임 처리합니다."""
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3].rstrip() + "..."


def build_kakao_pages(
    results: list[tuple[Site, list[Item], str | None]], total: int
) -> list[dict[str, str]]:
    """
    카카오 사용자 정의 '리스트' 템플릿에 넘길 페이지를 만듭니다.

    템플릿 사용자 인자:
      HEADER
      SITE1 / TITLE1
      ...
      SITE5 / TITLE5

    한 메시지에 최대 5개 항목을 넣고, 6개 이상이면 다음 메시지로 나눕니다.
    링크는 넣지 않습니다.
    """
    entries: list[tuple[str, str]] = []

    for site, items, err in results:
        if err:
            entries.append((f"[{site.name}]", "수집 오류"))
            continue

        for item in items:
            title = _clean_kakao_text(item.title, 90)
            if title:
                entries.append((f"[{site.name}]", title))

    # FORCE_SEND 테스트 등에서 실제 신규 항목이 하나도 없을 때 표시할 내용
    if not entries:
        entries.append(("[신규 등록 없음]", f"{target_search_date()} 등록된 입찰공고/보도자료가 없습니다."))

    pages: list[dict[str, str]] = []
    chunks = [entries[i:i + 5] for i in range(0, len(entries), 5)]
    page_count = len(chunks)
    target = target_search_date()

    for page_no, chunk in enumerate(chunks, start=1):
        suffix = f" ({page_no}/{page_count})" if page_count > 1 else ""
        args: dict[str, str] = {
            "HEADER": f"[보도자료/뉴스] {target} ({total}건){suffix}",
        }
        for idx in range(1, 6):
            if idx <= len(chunk):
                site_name, title = chunk[idx - 1]
                args[f"SITE{idx}"] = _clean_kakao_text(site_name, 40)
                args[f"TITLE{idx}"] = _clean_kakao_text(title, 90)
            else:
                args[f"SITE{idx}"] = ""
                args[f"TITLE{idx}"] = ""
        pages.append(args)

    return pages


def _print_kakao_page(page: dict[str, str]) -> None:
    print(page.get("HEADER", ""))
    for idx in range(1, 6):
        site_name = page.get(f"SITE{idx}", "").strip()
        title = page.get(f"TITLE{idx}", "").strip()
        if site_name and title:
            print(site_name)
            print(title)


def send_kakao(results: list[tuple[Site, list[Item], str | None]], total: int) -> tuple[int, int]:
    """
    카카오 사용자 정의 '리스트' 템플릿으로 보냅니다.

    한 메시지에 최대 5개 수집 항목을 표시하며, 6개 이상이면 여러 메시지로
    나눠 전송합니다. KAKAO_SEND_TO_SELF=1이면 본인에게도 같은 내용을 보냅니다.
    """
    receiver_uuids: list[str] = []
    send_to_self = env_flag("KAKAO_SEND_TO_SELF", True)
    try:
        receiver_uuids = load_kakao_receiver_uuids()
        recipient_count = len(receiver_uuids) + (1 if send_to_self else 0)

        if recipient_count == 0:
            print("카카오톡 발송 대상이 없습니다. 친구 수신자를 선택하거나 KAKAO_SEND_TO_SELF=1로 설정하세요.")
            return 0, 0

        template_id = os.getenv("KAKAO_TEMPLATE_ID", "").strip()
        if not template_id:
            print("KAKAO_TEMPLATE_ID가 없습니다. 카카오 사용자 정의 리스트 템플릿 ID를 Conf/.env에 설정하세요.")
            return 0, recipient_count

        access_token = kakao_sender_access_token()
        pages = build_kakao_pages(results, total)

        total_success = 0
        total_failure = 0

        for page_no, page in enumerate(pages, start=1):
            print(f"카카오 메시지 내용 ({page_no}/{len(pages)}):")
            _print_kakao_page(page)
            print("카카오 템플릿 전달값:", json.dumps(page, ensure_ascii=False))

            # 1) 발신자 본인
            if send_to_self:
                try:
                    kakao_send_custom_to_self(access_token, template_id, page)
                    total_success += 1
                    print(f"카카오톡 나에게 보내기 완료 ({page_no}/{len(pages)})")
                except Exception as e:
                    total_failure += 1
                    print(f"카카오톡 나에게 보내기 실패 ({page_no}/{len(pages)}): {e}")
            elif page_no == 1:
                print("KAKAO_SEND_TO_SELF=0: 본인 카카오톡 발송은 건너뜁니다.")

            # 2) 선택된 친구들
            if receiver_uuids:
                batch_no = 0
                for batch in _chunks(receiver_uuids, 5):
                    batch_no += 1
                    try:
                        payload = kakao_send_custom_to_friends(
                            access_token, batch, template_id, page
                        )
                        success_uuids = payload.get("successful_receiver_uuids", []) or []
                        failure_info = payload.get("failure_info", []) or []

                        batch_success = len(success_uuids)
                        failed_uuids: set[str] = set()
                        for info in failure_info:
                            for uuid in info.get("receiver_uuids", []) or []:
                                failed_uuids.add(str(uuid))
                            print(
                                f"카카오톡 일부 전송 실패(메시지 {page_no}, 배치 {batch_no}): "
                                f"code={info.get('code')}, msg={info.get('msg')}"
                            )

                        batch_failure = max(len(batch) - batch_success, len(failed_uuids))
                        total_success += batch_success
                        total_failure += batch_failure
                        print(
                            f"카카오톡 친구 발송 메시지 {page_no}/{len(pages)}, 배치 {batch_no}: "
                            f"성공 {batch_success}명 / 실패 {batch_failure}명"
                        )
                    except Exception as e:
                        total_failure += len(batch)
                        print(
                            f"카카오톡 친구 발송 메시지 {page_no}/{len(pages)}, "
                            f"배치 {batch_no} 실패: {e}"
                        )
            elif send_to_self and page_no == 1:
                print("선택된 카카오 친구 수신자가 없습니다. 본인에게만 전송합니다.")

        self_note = " / 본인 포함" if send_to_self else " / 본인 제외"
        print(
            f"카카오톡 발송 완료: 메시지 {len(pages)}개, "
            f"성공 {total_success}건 / 실패 {total_failure}건{self_note}"
        )
        return total_success, total_failure

    except Exception as e:
        print(f"카카오톡 전송 실패: {e}")
        return 0, len(receiver_uuids) + (1 if send_to_self else 0)


# ------------------------------------------------------------------------


def main() -> None:
    load_env(ENV_FILE)
    target = target_search_date()
    target_dt = datetime.strptime(target, "%Y-%m-%d")
    print(f"검색 설정: SEARCH_DAYS_AGO={search_days_ago()} -> 대상일 {target}")
    if target_dt.weekday() >= 5:
        day_name = "토요일" if target_dt.weekday() == 5 else "일요일"
        print(f"참고: 검색 대상일 {target}은 {day_name}입니다. 입찰공고가 없을 수 있습니다.")
    if os.getenv("KAKAO_MESSAGE_LINK_URL", "").strip():
        print("참고: KAKAO_MESSAGE_LINK_URL은 현재 코드에서 사용하지 않습니다. 404 이동은 카카오 템플릿의 공통/컴포넌트 링크 설정을 확인하세요.")

    # [추가] 이미 보낸 링크 캐시 로드 (오래된 항목은 정리)
    seen_links = load_seen_links(SEEN_LINKS_FILE)
    seen_links = prune_seen_links(seen_links)
    today = datetime.now().strftime("%Y-%m-%d")
    newly_seen: dict[str, str] = {}

    results: list[tuple[Site, list[Item], str | None]] = []
    total = 0

    for site in load_sites(SITES_FILE):
        err_msg = None
        try:
            raw_items = fetch_items(site)
            items = todays_items(raw_items, seen_links)
            total += len(items)
            undated_count = sum(1 for item in raw_items if not item_date(item.pub_date))
            print(
                f"{site.name}: 조회 {len(raw_items)}건 / "
                f"대상일 {target_search_date()} {len(items)}건"
                + (f" / 날짜정보없음 {undated_count}건" if undated_count else "")
            )
            # seen_links는 '오늘 신규 글' 판별용 캐시입니다. 과거 날짜 테스트가
            # 현재 캐시를 오염시키지 않도록 오늘 검색일 때만 갱신합니다.
            if search_days_ago() == 0:
                for item in items:
                    if item.link:
                        newly_seen[item.link] = today
        except Exception as e:
            items = []
            err_msg = str(e)
            print(f"{site.name}: 오류 발생 ({e})")

        results.append((site, items, err_msg))

    # 오늘 검색에서만 신규 링크 캐시를 갱신합니다.
    if search_days_ago() == 0:
        seen_links.update(newly_seen)
        save_seen_links(SEEN_LINKS_FILE, seen_links)
    else:
        print("과거 날짜 조회 모드: seen_links 캐시는 변경하지 않습니다.")

    body = build_report(results)
    saved_path = save_file(body)
    print(f"저장했습니다: {saved_path}")

    # [추가] 하루 여러 번 돌릴 때, 오늘 이미 보낸 내용과 똑같으면(새 글/새 오류가
    # 없으면) 메일·카카오톡 발송을 생략합니다. 그날 첫 실행(날짜가 바뀐 뒤 처음
    # 도는 실행)은 내용이 있든 없든 항상 보냅니다.
    sent_state = load_sent_state(SENT_STATE_FILE)
    is_first_run_today = sent_state.get("date") != today
    prev_signatures = set() if is_first_run_today else set(sent_state.get("signatures", []))
    current_signatures = build_run_signatures(results)
    should_send = is_first_run_today or bool(current_signatures - prev_signatures)

    # 발송 채널 선택. .env에서 필요에 따라 켜고 끌 수 있습니다.
    send_email_enabled = env_flag("SEND_EMAIL", True)
    send_kakao_enabled = env_flag("SEND_KAKAO", True)

    print(
        "발송 설정: "
        f"메일={'ON' if send_email_enabled else 'OFF'}, "
        f"카카오={'ON' if send_kakao_enabled else 'OFF'}, "
        f"본인카카오={'ON' if env_flag('KAKAO_SEND_TO_SELF', True) else 'OFF'}"
    )

    if not send_email_enabled and not send_kakao_enabled:
        print("SEND_EMAIL=0, SEND_KAKAO=0 입니다. 파일만 저장하고 발송은 하지 않습니다.")
        return

    # FORCE_SEND=1이면 새 내용 여부와 관계없이 현재 ON인 채널로 강제 발송합니다.
    if env_flag("FORCE_SEND", False):
        if not should_send:
            print("FORCE_SEND=1: 새 내용이 없어도 활성화된 채널로 강제 발송합니다.")
        should_send = True

    if should_send:
        any_success = False

        if send_email_enabled:
            if send_email(body, saved_path, total):
                any_success = True
        else:
            print("SEND_EMAIL=0: 메일 발송을 건너뜁니다.")

        if send_kakao_enabled:
            kakao_success, kakao_failure = send_kakao(results, total)
            if kakao_success > 0:
                any_success = True
            if kakao_failure:
                print(f"주의: 카카오톡 미전송 수신자가 {kakao_failure}명 있습니다.")
        else:
            print("SEND_KAKAO=0: 카카오톡 발송을 건너뜁니다.")

        # 적어도 하나의 채널이 실제 성공한 경우에만 오늘 발송 상태를 저장합니다.
        if any_success:
            save_sent_state(SENT_STATE_FILE, today, prev_signatures | current_signatures)
        else:
            print("메일/카카오 발송 성공 건이 없어 발송 상태를 저장하지 않습니다. 다음 실행에서 재시도할 수 있습니다.")
    else:
        print("이전 실행 이후 새로 등록된 내용이 없어 활성화된 채널 발송을 생략합니다.")


if __name__ == "__main__":
    main()