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
# [추가] 카카오톡 "나에게 보내기"용 리프레시 토큰 저장 파일. 이 파일이 없거나
# 비어 있으면 카카오톡 전송은 그냥 건너뛰고 기존처럼 메일만 보냅니다(선택 기능).
# 형식: {"이름": {"refresh_token": "..."}, ...}  -- kakao_auth_setup.py로 생성/추가.
KAKAO_TOKENS_FILE = CONF_DIR / "kakao_tokens.json"
# [추가] 실제로 카카오톡을 받을 사람 이름 목록. kakao_tokens.json에 등록돼
# 있어도 이 파일에 이름이 없으면 보내지 않습니다(등록은 해두고 잠깐 끄고 싶을
# 때 유용). maillist.txt/sites.txt와 같은 방식: 한 줄에 이름 하나, 맨 앞이
# "#"으로 시작하는 줄은 제외(주석 처리)됩니다.
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
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


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

# [추가] 카카오톡 실제 수신 대상 목록(Conf/kakao.txt) 로드 -----------------------
# maillist.txt와 같은 규칙: 한 줄에 이름 하나, 빈 줄/"#"으로 시작하는 줄은
# 제외합니다. kakao_tokens.json에 등록돼 있어도 여기 없으면 발송하지 않습니다.
def load_kakao_recipients(path: Path) -> list[str]:
    if not path.exists():
        print(f"카카오톡 수신자 목록 파일이 없습니다: {path}")
        return []

    names: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split(",")[0].strip()
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            names.append(name)
    return names


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
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))

    home_url = urljoin(site.url, FIRST_EPRO_HOME_PATH)
    wrapper_url = urljoin(site.url, FIRST_EPRO_LIST_WRAPPER_PATH)
    api_url = urljoin(site.url, FIRST_EPRO_LIST_API_PATH)

    # 1) 홈페이지 방문 (실제 사용 흐름과 맞춤; 세션 쿠키가 이때부터 생깁니다)
    opener.open(urllib.request.Request(home_url, headers={"User-Agent": USER_AGENT}), timeout=30).read()

    # 2) "더보기"가 실제로 거치는 경유지 주소를 방문합니다. 이 단계가 핵심입니다.
    opener.open(urllib.request.Request(wrapper_url, headers={"User-Agent": USER_AGENT}), timeout=30).read()

    # 3) 목록 조회 POST (화면의 #form 기본값과 동일: 검색조건 없이 1페이지 전체, 최신순 10건)
    form_data = urlencode(
        {
            "selectcustomer": "g_customer",
            "buyerNm": "",
            "buyerCd": "",
            "selectType": "title",
            "searchText": "",
            "pageNo": "1",
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
    return records


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


def todays_items(items: list[Item], seen_links: dict[str, str]) -> list[Item]:
    """
    당일 등록 글만 골라냅니다.

    - 날짜를 알 수 있는 글(게시판/RSS 등): 그 날짜가 오늘이면 포함 (기존 로직 그대로).
    - 날짜를 알 수 없는 글(cionew 같은 목록형 페이지): 예전처럼 "오늘"로 간주해
      매번 똑같이 나오게 하는 대신, seen_links 캐시에 없는(=이전에 한 번도
      보낸 적 없는) 링크만 "신규"로 취급합니다. 이렇게 하면 페이지에 항상
      떠 있는 예전 글은 걸러지고, 실제로 새로 올라온 글만 남습니다.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    matched: list[Item] = []
    for item in items:
        parsed_dt = item_date(item.pub_date)
        if parsed_dt == today:
            matched.append(item)
        elif not item.pub_date:
            if item.link not in seen_links:
                matched.append(item)
    return matched


def build_report(results: list[tuple[Site, list[Item], str | None]]) -> str:
    lines = [
        f"수집 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "대상: 당일 등록 글만",
        "",
    ]
    for site, items, err in results:
        lines.append(f"[{site.name}]")
        lines.append(f"사이트URL: {site.url}")
        if site.note:
            lines.append(f"비고: {site.note}")

        if err:
            lines.append(f"수집 상태: 오류 발생 ({err})")
        elif not items:
            lines.append("오늘 등록된 글이 없습니다.")
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
    today = datetime.now().strftime("%Y-%m-%d")
    path = OUTPUT_DIR / f"보도자료_{today}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def build_message(body: str, saved_path: Path, mail_from: str, mail_to: str, count: int) -> EmailMessage:
    today = datetime.now().strftime("%Y-%m-%d")
    message = EmailMessage()
    message["Subject"] = f"[보도자료/뉴스] {today} ({count}건 수집)"
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


def send_email(body: str, saved_path: Path, count: int) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    mail_from = os.getenv("MAIL_FROM", user).strip()
    recipients = load_mail_list(MAIL_LIST_FILE)

    if not all([host, user, password]):
        print(f"메일 서버 설정({ENV_FILE.name})이 없어 파일만 저장했습니다.")
        return
    if not recipients:
        print("보낼 메일 주소가 없어 파일만 저장했습니다.")
        return

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls(context=context)
        smtp.login(user, password)
        for mail_to in recipients:
            smtp.send_message(build_message(body, saved_path, mail_from, mail_to, count))
            print(f"메일을 보냈습니다: {mail_to}")


# [추가] 카카오톡 "나에게 보내기" 전송 -----------------------------------------
#
# 카카오는 "친구에게 보내기"는 별도 사업자 검수가 필요하지만, "나에게 보내기"는
# 검수 없이 바로 쓸 수 있습니다(문자 그대로 "내 카카오톡"에만 보낼 수 있음).
# 그래서 수신자별로 각자 자기 계정에 1회 로그인 동의를 하게 한 뒤(kakao_auth_setup.py
# 사용), 그때 발급받은 리프레시 토큰을 kakao_tokens.json에 저장해두면, 이 스크립트가
# 매번 실행될 때마다 저장된 리프레시 토큰으로 액세스 토큰을 새로 발급받아 그 사람
# "나에게 보내기"로 결과 요약을 전송합니다. 즉 94hjlee@naver.com, hojun_lee@nonghyup.com
# 두 분도 각자 한 번만 로그인 동의를 하면, 사실상 두 분 모두에게 카카오톡으로 결과를
# 받을 수 있습니다. 설정을 아직 안 했으면(kakao_tokens.json 없음) 조용히 건너뛰고
# 기존처럼 메일만 보냅니다 — 완전히 선택 기능입니다.
def kakao_refresh_access_token(refresh_token: str) -> tuple[str, str | None]:
    """저장된 리프레시 토큰으로 액세스 토큰을 새로 발급받습니다.

    반환값: (액세스 토큰, 새 리프레시 토큰 또는 None)
    카카오는 리프레시 토큰의 남은 유효기간이 1개월 미만일 때만 응답에 새
    리프레시 토큰을 함께 내려줍니다. 그때는 저장된 값을 갱신해줘야 합니다
    (없으면 기존 리프레시 토큰을 계속 씁니다).
    """
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


def kakao_send_text(access_token: str, text: str, link_url: str) -> None:
    """카카오톡 "나에게 보내기"로 기본 템플릿(텍스트형) 메시지 1건을 보냅니다."""
    template_object = {
        "object_type": "text",
        "text": text[:200],  # 텍스트형 기본 템플릿은 최대 200자
        "link": {"web_url": link_url, "mobile_web_url": link_url},
    }
    data = urlencode(
        {"template_object": json.dumps(template_object, ensure_ascii=False)}
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        data=data,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def build_kakao_summary(
    results: list[tuple[Site, list[Item], str | None]], total: int
) -> tuple[str, str]:
    """카카오톡 메시지용 요약(최대 200자)과, 메시지에 붙일 링크 1개를 만듭니다.

    전체 리포트는 길어서(200자 제한) 그대로 못 보내니, 사이트별 건수 요약만
    보내고 자세한 내용은 메일을 보라고 안내합니다. 링크는 오늘 수집된 글 중
    첫 번째 글 주소를 사용하고, 하나도 없으면 농협 홈페이지로 대신합니다.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    site_parts = [
        f"{site.name} 오류" if err else f"{site.name} {len(items)}건"
        for site, items, err in results
    ]

    link_url = "https://www.nonghyup.com"
    for _, items, err in results:
        if err:
            continue
        found = next((item.link for item in items if item.link), None)
        if found:
            link_url = found
            break

    text = (
        f"[보도자료/뉴스] {today} ({total}건)\n"
        + ", ".join(site_parts)
        + "\n자세한 내용은 이메일을 확인하세요."
    )
    if len(text) > 200:
        text = text[:197] + "..."
    return text, link_url


def send_kakao(results: list[tuple[Site, list[Item], str | None]], total: int) -> None:
    if not KAKAO_TOKENS_FILE.exists():
        return  # 설정 안 함(선택 기능) -> 조용히 건너뜀

    # [추가] kakao.txt에 있는 이름만 실제 수신 대상으로 걸러냅니다.
    allowed_names = load_kakao_recipients(KAKAO_LIST_FILE)
    if not allowed_names:
        print("카카오톡을 보낼 대상이 없어 건너뜁니다.")
        return

    try:
        tokens = json.loads(KAKAO_TOKENS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"카카오 토큰 파일을 읽지 못했습니다: {e}")
        return
    if not isinstance(tokens, dict) or not tokens:
        return

    allowed_keys = {name.lower() for name in allowed_names}
    tokens = {name: info for name, info in tokens.items() if name.lower() in allowed_keys}
    if not tokens:
        print(f"{KAKAO_LIST_FILE.name}에 있는 이름과 일치하는 등록된 사람이 없어 건너뜁니다.")
        return

    text, link_url = build_kakao_summary(results, total)
    changed = False
    for name, info in tokens.items():
        if not isinstance(info, dict):
            continue
        refresh_token = str(info.get("refresh_token", "")).strip()
        if not refresh_token:
            continue
        try:
            access_token, new_refresh_token = kakao_refresh_access_token(refresh_token)
            if new_refresh_token and new_refresh_token != refresh_token:
                info["refresh_token"] = new_refresh_token
                changed = True
            kakao_send_text(access_token, text, link_url)
            print(f"카카오톡을 보냈습니다: {name}")
        except Exception as e:
            # 카카오 전송 실패가 메일 발송 성공까지 막으면 안 되므로, 여기서
            # 잡아서 로그만 남기고 다음 사람/다음 실행으로 넘어갑니다.
            print(f"카카오톡 전송 실패 ({name}): {e}")

    if changed:
        KAKAO_TOKENS_FILE.write_text(
            json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8"
        )


# ------------------------------------------------------------------------


def main() -> None:
    load_env(ENV_FILE)

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
            print(f"{site.name}: 당일 {len(items)}건")
            for item in items:
                if item.link:
                    newly_seen[item.link] = today
        except Exception as e:
            items = []
            err_msg = str(e)
            print(f"{site.name}: 오류 발생 ({e})")

        results.append((site, items, err_msg))

    # [추가] 이번에 새로 보낸 링크들을 캐시에 반영하고 저장
    seen_links.update(newly_seen)
    save_seen_links(SEEN_LINKS_FILE, seen_links)

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

    # [추가] 테스트용 강제 발송 스위치. Conf/.env에 FORCE_SEND=1 을 넣거나,
    # 실행 전에 "set FORCE_SEND=1"(윈도우 cmd 기준)로 환경변수를 설정하면
    # 새 내용 여부와 상관없이 이번 실행은 무조건 보냅니다. 테스트가 끝나면
    # 다시 지우거나 0으로 바꿔서 평소처럼 중복 방지가 동작하게 해주세요.
    if os.getenv("FORCE_SEND", "").strip().lower() in {"1", "true", "yes"}:
        if not should_send:
            print("FORCE_SEND 설정으로 새 내용이 없어도 강제로 발송합니다(테스트용).")
        should_send = True

    # [추가] 메일도 같이 보낼지 여부를 코드 수정 없이 고를 수 있게 스위치로
    # 뺐습니다. 기본값은 "메일 안 보냄"(카카오톡만)입니다. 메일도 같이
    # 받고 싶으면 Conf/.env에 SEND_EMAIL_TOO=1 을 추가하면 됩니다.
    send_email_too = os.getenv("SEND_EMAIL_TOO", "").strip().lower() in {"1", "true", "yes"}

    if should_send:
        if send_email_too:
            send_email(body, saved_path, total)
        send_kakao(results, total)
        save_sent_state(SENT_STATE_FILE, today, prev_signatures | current_signatures)
    else:
        print("이전 실행 이후 새로 등록된 내용이 없어 메일/카카오톡 발송을 생략합니다.")


if __name__ == "__main__":
    main()