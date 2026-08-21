#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
이가라인(egaline.kr) 포켓몬 상품 재고 알림 봇
--------------------------------------------
- 신상품/재입고/검색 페이지를 주기적으로 확인
- 키워드(기본: 포켓몬 관련)에 맞는 상품만 골라냄
- 상품 상세페이지의 "재고 수량 N개" 를 읽어 재고 판정
- 신규 상품 등록 / 품절→재입고 순간에 텔레그램·디스코드·ntfy 로 알림

사용:
  python egaline_watcher.py --once          # 1회 실행 (cron / GitHub Actions 용)
  python egaline_watcher.py --loop          # 상주 실행 (interval_sec 마다 반복)
  python egaline_watcher.py --test-notify   # 알림 채널 연결 테스트
  python egaline_watcher.py --dump 33038    # 상세페이지 HTML 저장 (파싱 디버그용)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

BASE = "https://egaline.kr"
KST = timezone(timedelta(hours=9))
HERE = Path(__file__).resolve().parent

DEFAULT_CONFIG = {
    # 감시할 목록 페이지들 (여러 개 가능)
    "watch_urls": [
        "https://egaline.kr/product/list.html?cate_no=1074",          # 신상품
        "https://egaline.kr/product/list.html?cate_no=1115",          # 재입고
        "https://egaline.kr/product/search.html?keyword=%ED%8F%AC%EC%BC%93%EB%AA%AC",  # 검색: 포켓몬
    ],
    "list_max_pages": 3,          # 목록 페이지당 최대 몇 페이지까지 넘겨볼지
    # 키워드 필터 (상품명 기준, 공백/대소문자 무시)
    "keywords_any": ["포켓몬", "포켓몬스터", "피카츄", "pokemon", "포케몬"],
    "keywords_all": [],           # 예: ["카드"]  -> 포켓몬 AND 카드 인 것만
    "keywords_none": [],          # 예: ["도시락", "텀블러"]  -> 제외할 단어
    # 알림 조건
    "notify_new": True,           # 조건에 맞는 신규 상품이 목록에 뜨면
    "notify_restock": True,       # 품절(0개) -> 재고 있음 으로 바뀌면
    "notify_first_run": False,    # 최초 실행 시 기존 상품 전체를 알릴지 (보통 False)
    "min_stock": 1,               # 재고 몇 개 이상이면 "재고 있음" 으로 볼지
    "unknown_as_in_stock": True,  # 재고수량이 표시되지 않는 상품을 '있음'으로 볼지
    # 동작
    "interval_sec": 300,          # --loop 시 확인 주기 (초). 60 미만 권장하지 않음
    "jitter_sec": 30,             # 주기에 랜덤 오차를 줘서 패턴을 흐림
    "request_delay_sec": 1.0,     # 요청 사이 지연 (서버 배려)
    "quiet_hours": [],            # 예: [0, 1, 2, 3, 4, 5, 6] -> 해당 KST 시간대엔 알림 보류
    "state_file": "state.json",
    # 포켓몬 스토어 (공식 온라인 스토어) 감시
    "pokemonstore": {
        "enabled": False,
        "category_nos": ["488359"],
        "page_size": 50,
        "client_id": "",          # 자동으로 찾지 못할 때만 직접 입력
        "api_base": "",           # 비워두면 자동 판별
        "keywords_any": [],       # 비우면 해당 카테고리 전체 감시
        "keywords_all": [],
        "keywords_none": [],
    },
    # 로그인 쿠키 (선택). 도매가/일부 정보가 로그인 후에만 보이는 경우 사용
    "cookie": "",
    # 알림 채널
    "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
    "discord": {"enabled": False, "webhook_url": ""},
    "ntfy": {"enabled": False, "topic": "", "server": "https://ntfy.sh"},
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# /product/상품명슬러그/33038/category/1/display/2/
RE_PRODUCT_PATH = re.compile(r"/product/[^/]+/(\d+)/")
# /product/detail.html?product_no=33038
RE_PRODUCT_QS = re.compile(r"product_no=(\d+)")
# "재고 수량  120개"
RE_STOCK = re.compile(r"재고\s*수량\s*[:：]?\s*([\d,]+)\s*개")


# ────────────────────────────────────────────────────────────── 설정/상태


def load_config(path: Path) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if path.exists():
        user = json.loads(path.read_text(encoding="utf-8"))
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    # 환경변수가 있으면 우선 적용 (GitHub Actions / 도커용)
    env_map = {
        "TELEGRAM_BOT_TOKEN": ("telegram", "bot_token"),
        "TELEGRAM_CHAT_ID": ("telegram", "chat_id"),
        "DISCORD_WEBHOOK_URL": ("discord", "webhook_url"),
        "NTFY_TOPIC": ("ntfy", "topic"),
        "EGALINE_COOKIE": ("cookie", None),
    }
    for env, (sec, key) in env_map.items():
        val = os.environ.get(env)
        if not val:
            continue
        if key is None:
            cfg[sec] = val
        else:
            cfg[sec][key] = val
            cfg[sec]["enabled"] = True
    return cfg


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("[warn] state 파일이 손상되어 새로 시작합니다.", file=sys.stderr)
    return {"products": {}, "last_run": None}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ────────────────────────────────────────────────────────────── 크롤링


@dataclass
class Product:
    product_no: str
    name: str
    url: str
    stock: int | None = None       # None = 재고 정보 미공개/알 수 없음
    price: str = ""
    soldout_hint: bool = False     # 옵션에 (품절) 표기가 있는 등의 보조 신호
    site: str = "egaline"          # egaline / pokemonstore

    @property
    def key(self) -> str:
        """상태 저장용 고유 키 (사이트가 달라도 안 겹치게)"""
        return self.product_no if self.site == "egaline" else f"{self.site}:{self.product_no}"

    @property
    def site_label(self) -> str:
        return "이가라인" if self.site == "egaline" else "포켓몬 스토어"

    def in_stock(self, min_stock: int = 1) -> bool | None:
        """True=구매가능 / False=품절 / None=판단불가"""
        if self.soldout_hint:          # 옵션이 전부 품절이면 실구매 불가
            return False
        if self.stock is not None:
            return self.stock >= min_stock
        return None


class Egaline:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": BASE + "/",
        })
        if cfg.get("cookie"):
            self.s.headers["Cookie"] = cfg["cookie"]
        self.delay = float(cfg.get("request_delay_sec", 1.0))

    def get(self, url: str, tries: int = 3) -> str | None:
        for i in range(tries):
            try:
                r = self.s.get(url, timeout=20)
                if r.status_code == 200:
                    r.encoding = r.apparent_encoding or "utf-8"
                    time.sleep(self.delay)
                    return r.text
                print(f"[warn] HTTP {r.status_code} : {url}", file=sys.stderr)
            except requests.RequestException as e:
                print(f"[warn] 요청 실패({i + 1}/{tries}): {e}", file=sys.stderr)
            time.sleep(2 * (i + 1))
        return None

    # ── 목록 페이지 파싱 ────────────────────────────────────────

    def list_products(self, url: str) -> list[Product]:
        """목록/검색 페이지에서 상품 번호·이름·링크를 뽑는다."""
        found: dict[str, Product] = {}
        max_pages = int(self.cfg.get("list_max_pages", 1))
        for page in range(1, max_pages + 1):
            page_url = url if page == 1 else _with_page(url, page)
            html = self.get(page_url)
            if not html:
                break
            items = parse_list_html(html)
            new = [p for p in items if p.product_no not in found]
            for p in new:
                found[p.product_no] = p
            if not new:          # 더 넘겨도 새 상품이 없으면 중단
                break
        return list(found.values())

    # ── 상세 페이지 파싱 ────────────────────────────────────────

    def fetch_detail(self, p: Product) -> Product:
        html = self.get(f"{BASE}/product/detail.html?product_no={p.product_no}")
        if not html:
            return p
        stock, soldout, name, price = parse_detail_html(html)
        p.stock = stock
        p.soldout_hint = soldout
        if name:
            p.name = name
        if price:
            p.price = price
        return p


def _with_page(url: str, page: int) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}page={page}"


def _product_no_from_href(href: str) -> str | None:
    m = RE_PRODUCT_QS.search(href) or RE_PRODUCT_PATH.search(href)
    return m.group(1) if m else None


def _clean(text: str) -> str:
    text = re.sub(r"^\s*상품명\s*[:：]\s*", "", text or "")
    return re.sub(r"\s+", " ", text).strip()


def parse_list_html(html: str) -> list[Product]:
    """
    Cafe24 목록 스킨은 <li id="anchorBoxId_33038"> 구조가 표준이지만
    스킨마다 다르므로, 실패하면 링크 전수 조사로 폴백한다.
    """
    soup = BeautifulSoup(html, "html.parser")
    products: dict[str, Product] = {}

    # 1순위: anchorBoxId
    for li in soup.select('li[id^="anchorBoxId_"]'):
        no = li.get("id", "").split("_")[-1]
        if not no.isdigit():
            continue
        name_el = li.select_one(".description .name a, .name a, .description .name")
        a = li.select_one('a[href*="/product/"]')
        href = a.get("href") if a else f"/product/detail.html?product_no={no}"
        name = _clean(name_el.get_text(" ") if name_el else (a.get_text(" ") if a else ""))
        if not name:
            continue
        products[no] = Product(no, name, urljoin(BASE, href))

    if products:
        return list(products.values())

    # 폴백: 상품 링크를 모두 훑어서 텍스트가 있는 링크를 상품명으로 사용
    for a in soup.select('a[href*="/product/"]'):
        href = a.get("href", "")
        no = _product_no_from_href(href)
        if not no:
            continue
        name = _clean(a.get_text(" "))
        if not name or len(name) < 2:
            continue
        prev = products.get(no)
        # 더 긴(=더 온전한) 상품명을 채택
        if prev is None or len(name) > len(prev.name):
            products[no] = Product(no, name, urljoin(BASE, href))
    return list(products.values())


def parse_detail_html(html: str) -> tuple[int | None, bool, str, str]:
    """상세페이지 → (재고수량, 품절힌트, 상품명, 판매가)"""
    soup = BeautifulSoup(html, "html.parser")

    # 상품명 / 가격은 og 메타에서 가장 안정적으로 얻는다
    name = ""
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        name = _clean(re.sub(r"\s*-\s*이가라인\s*$", "", og["content"]))
    price = ""
    pm = soup.find("meta", property="product:price:amount")
    if pm and pm.get("content"):
        try:
            price = f"{int(float(pm['content'])):,}원"
        except ValueError:
            price = pm["content"]

    text = soup.get_text(" ", strip=True)

    # 재고 수량 (Cafe24 '기본 정보' 표에 노출됨)
    stock = None
    m = RE_STOCK.search(text)
    if m:
        try:
            stock = int(m.group(1).replace(",", ""))
        except ValueError:
            stock = None

    # 보조 신호: 옵션 <option> 에 (품절) 이 붙는 경우 / 재입고 알림 버튼만 노출되는 경우
    soldout_hint = False
    for sel in soup.select("select"):
        real = [o.get_text(" ", strip=True) for o in sel.select("option")
                if o.get("value") not in (None, "", "*", "**", "0")]
        # 옵션이 있는데 전부 품절 표기라면 실제로는 구매 불가
        if len(real) >= 1 and all(("품절" in t) for t in real):
            soldout_hint = True
            break

    return stock, soldout_hint, name, price


# ──────────────────────────────────────────── 포켓몬 스토어 (shopby 기반)


PS_BASE = "https://www.pokemonstore.co.kr"
# shopby(NHN커머스) 공용 API 후보. 몰마다 도메인이 다를 수 있어 순서대로 시도한다.
PS_API_CANDIDATES = [
    "https://shop-api.e-ncp.com",
    "https://api.shopby.co.kr",
]
# 스크립트 안에서 clientId 를 찾기 위한 패턴들.
# 샵바이의 clientId 는 UUID 가 아니라 base64 형태의 긴 문자열인 경우가 많다.
RE_CLIENT_ID = [
    re.compile(r"""client[_-]?id["']?\s*[:=]\s*["']([A-Za-z0-9+/=_.-]{12,200})["']""", re.I),
    re.compile(r"""["']client[_-]?id["']\s*[,:]\s*["']([A-Za-z0-9+/=_.-]{12,200})["']""", re.I),
    re.compile(r"""setClientId\s*\(\s*["']([A-Za-z0-9+/=_.-]{12,200})["']""", re.I),
]
# 위 패턴이 다 실패했을 때, 로그에 형태를 보여주기 위한 탐지용
RE_CLIENT_ID_HINT = re.compile(r"client[_-]?id", re.I)
# 열쇠일 가능성이 높은 스크립트를 먼저 살펴본다
PS_SCRIPT_HINTS = ("initialize", "env", "config", "shopby", "api", "common", "skin")
# 로그로 확인된, 값이 정의돼 있을 만한 경로들 (먼저 시도)
PS_EXTRA_SCRIPTS = [
    "/libs/api-initialize-pc.js",
    "/libs/api-initialize.js",
    "/libs/env.js",
    "/libs/external-service-config.js",
    "/libs/custom-common.js",
    "/libs/shopby-api.js",
]
# env = { clientId: "...", ... } 형태로 정의된 경우를 잡는다
RE_ENV_BLOCK = re.compile(
    r"""(?:env|config|apiOption|options)\s*=\s*\{[^{}]{0,600}?client[_-]?id["']?\s*[:=]\s*"""
    r"""["']([A-Za-z0-9+/=_.-]{12,200})["']""", re.I | re.S)


class PokemonStore:
    """
    포켓몬 스토어는 화면 껍데기만 내려오고 상품은 별도 API로 불러온다.
    그래서 (1) 사이트에서 접속 열쇠(clientId)를 찾아내고 (2) API에 직접 물어본다.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.ps = cfg.get("pokemonstore", {}) or {}
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Referer": PS_BASE + "/",
        })
        self.delay = float(cfg.get("request_delay_sec", 1.0))
        self.client_id: str | None = self.ps.get("client_id") or None
        self.api_base: str | None = self.ps.get("api_base") or None

    # ── 접속 열쇠 찾기 ─────────────────────────────────────

    def _get(self, url: str, **kw) -> requests.Response | None:
        try:
            r = self.s.get(url, timeout=20, **kw)
            time.sleep(self.delay)
            return r
        except requests.RequestException as e:
            print(f"[warn] 포켓몬스토어 요청 실패: {e}", file=sys.stderr)
            return None

    def discover_client_id(self) -> str | None:
        """사이트 HTML과 그 안의 스크립트 파일들을 뒤져 clientId 를 찾는다."""
        if self.client_id:
            print(f"[info] clientId (설정값 사용): {self.client_id[:8]}…")
            return self.client_id

        pages = [PS_BASE + "/index.html", PS_BASE + "/",
                 "https://m.pokemonstore.co.kr/index.html"]
        seen_scripts: list[str] = []

        for page in pages:
            r = self._get(page)
            if not r:
                continue
            print(f"[debug] {page} → HTTP {r.status_code}, {len(r.text):,}자")
            if r.status_code != 200:
                continue
            html = r.text

            # 1) HTML 안에 바로 들어있는 경우 (인라인 스크립트 포함)
            for pat in RE_CLIENT_ID[:2]:
                m = pat.search(html)
                if m:
                    self.client_id = m.group(1)
                    print(f"[info] clientId 발견 (HTML): {self.client_id[:8]}…")
                    return self.client_id

            # 2) 스크립트 파일 목록 수집 (확장자 제한 없이, 쿼리 붙은 것도 포함)
            for s in re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html):
                full = urljoin(page, s)
                if full not in seen_scripts:
                    seen_scripts.append(full)

        if not seen_scripts:
            print("[warn] 스크립트 파일을 하나도 찾지 못했습니다. "
                  "사이트가 접근을 막고 있을 수 있습니다.", file=sys.stderr)
            return None

        print(f"[debug] 스크립트 {len(seen_scripts)}개 발견")

        # 값이 정의돼 있을 만한 경로를 목록 맨 앞에 끼워넣는다
        for path in PS_EXTRA_SCRIPTS:
            u = PS_BASE + path
            if u not in seen_scripts:
                seen_scripts.append(u)

        def priority(u: str) -> int:
            low = u.rsplit("/", 1)[-1].lower()
            if any(u.endswith(p) for p in PS_EXTRA_SCRIPTS):
                return 0
            return 1 if any(h in low for h in PS_SCRIPT_HINTS) else 2
        seen_scripts.sort(key=priority)

        hints: dict[str, list[str]] = {}
        for src in seen_scripts[:30]:
            rr = self._get(src)
            if not rr or rr.status_code != 200:
                continue
            text = rr.text
            name = src.rsplit("/", 1)[-1][:36]

            # env = { clientId: "..." } 형태 우선
            m = RE_ENV_BLOCK.search(text)
            if m:
                self.client_id = m.group(1)
                print(f"[info] clientId 발견 ({name}, env블록): {self.client_id}")
                return self.client_id
            for pat in RE_CLIENT_ID:
                m = pat.search(text)
                if m:
                    self.client_id = m.group(1)
                    print(f"[info] clientId 발견 ({name}): {self.client_id}")
                    return self.client_id

            # 못 찾았을 때를 대비해 'clientId' 주변을 최대 2군데 기록
            if len(hints) < 10 and name not in hints:
                ctx = []
                for mm in list(RE_CLIENT_ID_HINT.finditer(text))[:2]:
                    around = text[max(0, mm.start() - 100):mm.end() + 150]
                    ctx.append(re.sub(r"\s+", " ", around))
                if ctx:
                    hints[name] = ctx

        print("[warn] clientId 를 찾지 못했습니다.", file=sys.stderr)
        if hints:
            print("[debug] 'clientId' 가 나온 부분 (형태 확인용):")
            for name, ctx in hints.items():
                for c in ctx:
                    print(f"  [{name}] {c}")
        return None

    # ── 상품 목록 ──────────────────────────────────────────

    def _api_headers(self) -> dict:
        return {
            "clientId": self.client_id or "",
            "platform": "PC",
            "version": "1.0",
            "Accept": "application/json",
            "Origin": PS_BASE,
            "Referer": PS_BASE + "/",
        }

    def _call_search(self, category_no: str, page_size: int) -> list | None:
        params = {
            "categoryNos": category_no,
            "pageNumber": 1,
            "pageSize": page_size,
            "order.by": "RECENT_PRODUCT",
            "order.direction": "DESC",
            "soldoutProductDisplay": "true",
        }
        bases = [self.api_base] if self.api_base else PS_API_CANDIDATES
        for base in bases:
            r = self._get(f"{base}/products/search", params=params, headers=self._api_headers())
            if not r:
                continue
            if r.status_code != 200:
                print(f"[warn] {base} 응답 {r.status_code}", file=sys.stderr)
                continue
            try:
                data = r.json()
            except ValueError:
                continue
            items = data.get("items") or data.get("products") or []
            if isinstance(items, list):
                self.api_base = base
                return items
        return None

    def list_products(self, category_no: str) -> list[Product]:
        if not self.discover_client_id():
            return []
        page_size = int(self.ps.get("page_size", 50))
        items = self._call_search(str(category_no), page_size)
        if items is None:
            print(f"[warn] 카테고리 {category_no} 조회 실패", file=sys.stderr)
            return []

        out: list[Product] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            no = str(it.get("productNo") or it.get("productNumber") or "").strip()
            name = _clean(str(it.get("productName") or it.get("name") or ""))
            if not no or not name:
                continue

            stock = it.get("stockCnt")
            if stock is None:
                stock = it.get("stockCount")
            try:
                stock = int(stock) if stock is not None else None
            except (TypeError, ValueError):
                stock = None

            status = str(it.get("saleStatusType") or it.get("saleStatus") or "").upper()
            soldout = bool(it.get("soldOut")) or status in {
                "SOLD_OUT", "SOLDOUT", "END", "STOP", "READY", "PROHIBITION"
            }

            price = ""
            for k in ("salePrice", "immediateDiscountedPrice", "price"):
                v = it.get(k)
                if isinstance(v, (int, float)) and v > 0:
                    price = f"{int(v):,}원"
                    break

            out.append(Product(
                product_no=no,
                name=name,
                url=f"{PS_BASE}/pages/product/view.html?productNo={no}",
                stock=stock,
                price=price,
                soldout_hint=soldout,
                site="pokemonstore",
            ))
        return out

    def collect(self) -> list[Product]:
        if not self.ps.get("enabled"):
            return []
        found: dict[str, Product] = {}
        for cat in self.ps.get("category_nos", []):
            for p in self.list_products(cat):
                found.setdefault(p.product_no, p)
        print(f"[info] 포켓몬 스토어 상품 {len(found)}건 확인")
        return list(found.values())


# ────────────────────────────────────────────────────────────── 필터


def _norm(s: str) -> str:
    return re.sub(r"[\s\-_/()\[\]]", "", s).lower()


def matches(name: str, cfg: dict) -> bool:
    n = _norm(name)
    any_kw = [_norm(k) for k in cfg.get("keywords_any", []) if k.strip()]
    none_kw = [_norm(k) for k in cfg.get("keywords_none", []) if k.strip()]
    if any_kw and not any(k in n for k in any_kw):
        return False
    # keywords_all 의 각 항목은 "카드|tcg" 처럼 | 로 대안을 넣을 수 있다.
    # 항목끼리는 AND, 항목 안의 대안끼리는 OR.
    for group in cfg.get("keywords_all", []):
        alts = [_norm(k) for k in str(group).split("|") if k.strip()]
        if alts and not any(k in n for k in alts):
            return False
    if none_kw and any(k in n for k in none_kw):
        return False
    return True


# ────────────────────────────────────────────────────────────── 알림


class Notifier:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    def send(self, title: str, body: str, url: str = "") -> None:
        sent = False
        tg = self.cfg.get("telegram", {})
        if tg.get("enabled") and tg.get("bot_token") and tg.get("chat_id"):
            sent |= self._telegram(tg, title, body, url)
        dc = self.cfg.get("discord", {})
        if dc.get("enabled") and dc.get("webhook_url"):
            sent |= self._discord(dc, title, body, url)
        nt = self.cfg.get("ntfy", {})
        if nt.get("enabled") and nt.get("topic"):
            sent |= self._ntfy(nt, title, body, url)
        if not sent:
            print(f"\n[알림 채널 미설정] {title}\n{body}\n{url}\n")

    def _telegram(self, c, title, body, url) -> bool:
        text = f"<b>{_esc(title)}</b>\n{_esc(body)}"
        if url:
            text += f'\n\n<a href="{url}">👉 상품 페이지 열기</a>'
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{c['bot_token']}/sendMessage",
                json={"chat_id": c["chat_id"], "text": text,
                      "parse_mode": "HTML", "disable_web_page_preview": False},
                timeout=15)
            if r.status_code != 200:
                print(f"[warn] 텔레그램 실패: {r.text[:200]}", file=sys.stderr)
                return False
            return True
        except requests.RequestException as e:
            print(f"[warn] 텔레그램 오류: {e}", file=sys.stderr)
            return False

    def _discord(self, c, title, body, url) -> bool:
        content = f"**{title}**\n{body}"
        if url:
            content += f"\n{url}"
        try:
            r = requests.post(c["webhook_url"], json={"content": content[:1900]}, timeout=15)
            return r.status_code < 300
        except requests.RequestException as e:
            print(f"[warn] 디스코드 오류: {e}", file=sys.stderr)
            return False

    def _ntfy(self, c, title, body, url) -> bool:
        server = c.get("server", "https://ntfy.sh").rstrip("/")
        headers = {"Title": title.encode("utf-8"), "Priority": "high", "Tags": "package"}
        if url:
            headers["Click"] = url
        try:
            r = requests.post(f"{server}/{c['topic']}",
                              data=body.encode("utf-8"), headers=headers, timeout=15)
            return r.status_code < 300
        except requests.RequestException as e:
            print(f"[warn] ntfy 오류: {e}", file=sys.stderr)
            return False


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ────────────────────────────────────────────────────────────── 메인 로직


def check_once(cfg: dict, state: dict, notifier: Notifier) -> dict:
    now = datetime.now(KST)
    first_run = not state.get("products")
    candidates: dict[str, Product] = {}

    # 1) 이가라인 — 목록에서 후보를 추린 뒤 상세페이지로 재고 확인
    site = Egaline(cfg)
    ega: dict[str, Product] = {}
    total_seen = 0
    pokemon_names: list[str] = []       # 포켓몬이긴 한데 카드가 아닌 것들
    samples: list[str] = []
    broad = {"keywords_any": cfg.get("keywords_any", [])}   # 카드 조건을 뺀 필터
    for url in cfg["watch_urls"]:
        found = site.list_products(url)
        total_seen += len(found)
        print(f"[debug] {url.split('?', 1)[-1][:40]} → 상품 {len(found)}개 수집")
        for p in found:
            if len(samples) < 3:
                samples.append(p.name)
            if matches(p.name, broad) and len(pokemon_names) < 20:
                pokemon_names.append(p.name)
            if matches(p.name, cfg) and p.product_no not in ega:
                ega[p.product_no] = p
    if total_seen == 0:
        print("[warn] 목록에서 상품을 하나도 읽지 못했습니다. 사이트 구조가 바뀌었을 수 있습니다.",
              file=sys.stderr)
    else:
        print(f"[debug] 이 중 포켓몬 상품 {len(pokemon_names)}개:")
        for n in pokemon_names[:20]:
            print(f"        {n}")
        if not pokemon_names:
            print(f"[debug] 포켓몬 상품이 없습니다. 읽어온 상품 예시: {samples}")
    print(f"[{now:%Y-%m-%d %H:%M:%S}] 이가라인 키워드 일치 {len(ega)}건 (전체 {total_seen}개 중)")
    for p in ega.values():
        site.fetch_detail(p)
        candidates[p.key] = p

    # 2) 포켓몬 스토어 — API 응답에 재고가 함께 오므로 추가 조회가 필요 없다
    ps_cfg = cfg.get("pokemonstore", {}) or {}
    if ps_cfg.get("enabled"):
        try:
            ps_filter = {
                "keywords_any": ps_cfg.get("keywords_any", []),
                "keywords_all": ps_cfg.get("keywords_all", []),
                "keywords_none": ps_cfg.get("keywords_none", []),
            }
            hits = 0
            for p in PokemonStore(cfg).collect():
                if matches(p.name, ps_filter):
                    candidates[p.key] = p
                    hits += 1
            print(f"[{now:%H:%M:%S}] 포켓몬 스토어 키워드 일치 {hits}건")
        except Exception as e:
            # 한쪽 사이트가 막혀도 다른 쪽 감시는 계속되어야 한다
            print(f"[error] 포켓몬 스토어 확인 중 오류: {type(e).__name__}: {e}", file=sys.stderr)

    # 3) 이전 기록과 비교
    events: list[tuple[str, Product, str]] = []
    min_stock = int(cfg.get("min_stock", 1))
    for key, p in candidates.items():
        prev = state["products"].get(key, {})
        prev_stock = prev.get("stock")
        prev_known = bool(prev)

        if p.soldout_hint:
            stock_txt = "품절"
        elif p.stock is None:
            stock_txt = "재고 수량 미표시"
        else:
            stock_txt = f"재고 {p.stock:,}개"

        avail = p.in_stock(min_stock)
        has_stock = avail is True or (avail is None and cfg.get("unknown_as_in_stock", True))

        if not prev_known:
            if cfg.get("notify_new", True) and has_stock and (not first_run or cfg.get("notify_first_run")):
                events.append(("🆕 신상품 입고", p, stock_txt))
        else:
            was_out = prev.get("soldout") is True
            if cfg.get("notify_restock", True) and was_out and avail is True:
                before = "품절" if prev_stock in (None, 0) else f"{prev_stock}개"
                events.append(("🔁 재입고", p, f"{before} → {stock_txt}"))

        state["products"][key] = {
            "name": p.name,
            "url": p.url,
            "stock": p.stock,
            "soldout": (avail is False),
            "price": p.price,
            "site": p.site,
            "last_seen": now.isoformat(timespec="seconds"),
        }
        flag = "✅" if has_stock else "❌"
        print(f"  {flag} [{p.site_label}] {p.name} — {stock_txt}")

    # 4) 알림 발송
    quiet = now.hour in (cfg.get("quiet_hours") or [])
    if events and quiet:
        print(f"[info] 조용한 시간대({now.hour}시)라 알림 {len(events)}건 보류")
    else:
        for kind, p, extra in events:
            body = f"{p.name}\n{p.price} · {extra}\n확인 시각 {now:%m/%d %H:%M}"
            notifier.send(f"{kind} — {p.site_label}", body, p.url)
            time.sleep(0.4)

    if first_run and not cfg.get("notify_first_run"):
        print(f"[info] 최초 실행: 현재 상품 {len(candidates)}건을 기준선으로 저장했습니다. "
              f"다음 실행부터 변화만 알립니다.")

    state["last_run"] = now.isoformat(timespec="seconds")
    return state


def main() -> int:
    ap = argparse.ArgumentParser(description="이가라인 포켓몬 재고 알림 봇")
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--once", action="store_true", help="1회만 실행")
    ap.add_argument("--loop", action="store_true", help="주기적으로 계속 실행")
    ap.add_argument("--test-notify", action="store_true", help="알림 채널 테스트")
    ap.add_argument("--dump", metavar="PRODUCT_NO", help="상세페이지 HTML 저장(디버그)")
    ap.add_argument("--check-pokemon", action="store_true",
                    help="포켓몬 스토어 연결만 점검 (알림 없음)")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    notifier = Notifier(cfg)

    if args.test_notify:
        notifier.send("🔔 테스트 알림", "이가라인 재고 알림 봇이 정상 작동합니다.", BASE)
        return 0

    if args.check_pokemon:
        ps = PokemonStore(cfg)
        cid = ps.discover_client_id()
        print(f"clientId : {cid or '못 찾음'}")
        if not cid:
            return 1
        for cat in (cfg.get("pokemonstore", {}).get("category_nos") or ["488359"]):
            items = ps.list_products(str(cat))
            print(f"\n[카테고리 {cat}] {len(items)}건  (API: {ps.api_base})")
            for p in items[:15]:
                s = "품절" if p.soldout_hint else (
                    f"재고 {p.stock}" if p.stock is not None else "재고 미표시")
                print(f"  - {p.name} / {p.price} / {s}")
        return 0

    if args.dump:
        site = Egaline(cfg)
        html = site.get(f"{BASE}/product/detail.html?product_no={args.dump}")
        if not html:
            print("가져오기 실패")
            return 1
        out = HERE / f"dump_{args.dump}.html"
        out.write_text(html, encoding="utf-8")
        stock, soldout, name, price = parse_detail_html(html)
        print(f"저장: {out}\n상품명: {name}\n가격: {price}\n재고: {stock}\n품절힌트: {soldout}")
        return 0

    state_path = Path(cfg["state_file"])
    if not state_path.is_absolute():
        state_path = HERE / state_path

    if args.loop:
        interval = max(60, int(cfg.get("interval_sec", 300)))
        jitter = int(cfg.get("jitter_sec", 0))
        print(f"감시 시작 — {interval}초 주기 (Ctrl+C 로 종료)")
        while True:
            try:
                state = load_state(state_path)
                state = check_once(cfg, state, notifier)
                save_state(state_path, state)
            except KeyboardInterrupt:
                print("\n종료합니다.")
                return 0
            except Exception as e:  # 루프는 죽지 않게
                print(f"[error] {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(interval + random.randint(0, jitter))

    # 기본: 1회 실행
    state = load_state(state_path)
    state = check_once(cfg, state, notifier)
    save_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
