"""books.toscrape.com から書籍のタイトル・価格・在庫状況を収集し、Markdown に保存する。

使い方:
    python scrape_books.py               # 全ページを取得
    python scrape_books.py --max-pages 3 # 先頭3ページだけ取得
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://books.toscrape.com/"
USER_AGENT = "python-automation-book-scraper/1.0"
TIMEOUT_SEC = 10
WAIT_MIN_SEC = 1.0
WAIT_MAX_SEC = 3.0

PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / "output"
LOG_DIR = PROJECT_DIR / "logs"

logger = logging.getLogger("scrape_books")


@dataclass
class Book:
    title: str
    price: str
    availability: str


class FetchError(Exception):
    """接続エラーなど、処理を継続できない取得エラー。"""


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"scrape_books_{datetime.now():%Y%m%d}.log"
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


class PoliteClient:
    """robots.txt を守り、リクエスト間にランダムな待機を入れる HTTP クライアント。"""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.robots = RobotFileParser()
        self._last_request_at: float | None = None

    def _wait(self) -> None:
        if self._last_request_at is None:
            return
        delay = random.uniform(WAIT_MIN_SEC, WAIT_MAX_SEC)
        logger.info("%.1f 秒待機します", delay)
        time.sleep(delay)

    def _request(self, url: str) -> requests.Response:
        self._wait()
        try:
            response = self.session.get(url, timeout=TIMEOUT_SEC)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise FetchError(f"接続エラー: {url} ({e})") from e
        except requests.RequestException as e:
            raise FetchError(f"リクエストエラー: {url} ({e})") from e
        finally:
            self._last_request_at = time.monotonic()
        return response

    def load_robots(self) -> None:
        robots_url = urljoin(self.base_url, "/robots.txt")
        logger.info("robots.txt を取得します: %s", robots_url)
        response = self._request(robots_url)

        # urllib.robotparser.RobotFileParser.read() と同じ規則で解釈する
        if response.status_code in (401, 403):
            logger.warning("robots.txt へのアクセスが拒否されました。全パスを禁止として扱います")
            self.robots.disallow_all = True
        elif 400 <= response.status_code < 500:
            logger.info("robots.txt がありません (HTTP %d)。全パスを許可として扱います", response.status_code)
            self.robots.allow_all = True
        elif response.status_code >= 500:
            raise FetchError(f"robots.txt の取得に失敗しました (HTTP {response.status_code})")
        else:
            self.robots.parse(response.text.splitlines())
            logger.info("robots.txt を読み込みました")

    def can_fetch(self, url: str) -> bool:
        return self.robots.can_fetch(USER_AGENT, url)

    def get_html(self, url: str) -> str | None:
        """ページを取得する。robots.txt で禁止されている場合は None を返す。"""
        if not self.can_fetch(url):
            logger.warning("robots.txt で禁止されているためスキップします: %s", url)
            return None
        logger.info("取得中: %s", url)
        response = self._request(url)
        try:
            response.raise_for_status()
        except requests.HTTPError as e:
            raise FetchError(f"HTTP エラー: {url} ({e})") from e
        response.encoding = "utf-8"  # 自動判定だと "£" が文字化けするため明示する
        return response.text


def parse_books(html: str) -> list[Book]:
    soup = BeautifulSoup(html, "html.parser")
    books: list[Book] = []
    for article in soup.select("article.product_pod"):
        link = article.select_one("h3 a")
        price = article.select_one("p.price_color")
        availability = article.select_one("p.availability")
        if link is None or price is None or availability is None:
            logger.warning("書籍情報の一部が見つからないためスキップします")
            continue
        books.append(
            Book(
                # 一覧では長いタイトルが省略されるため、title 属性の完全なタイトルを使う
                title=str(link.get("title") or link.get_text(strip=True)),
                price=price.get_text(strip=True),
                availability=availability.get_text(strip=True),
            )
        )
    return books


def find_next_page(html: str, current_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    next_link = soup.select_one("li.next a")
    if next_link is None or not next_link.get("href"):
        return None
    return urljoin(current_url, str(next_link["href"]))


def scrape(client: PoliteClient, max_pages: int | None) -> list[Book]:
    books: list[Book] = []
    url: str | None = BASE_URL
    page = 0
    while url and (max_pages is None or page < max_pages):
        if urlparse(url).netloc != urlparse(BASE_URL).netloc:
            logger.warning("対象サイト外の URL のため停止します: %s", url)
            break
        html = client.get_html(url)
        if html is None:
            break
        page += 1
        page_books = parse_books(html)
        logger.info("%d ページ目: %d 件", page, len(page_books))
        books.extend(page_books)
        url = find_next_page(html, url)
    return books


def escape_md(text: str) -> str:
    return text.replace("\\", "\\\\").replace("|", "\\|")


def write_markdown(books: list[Book], path: Path, fetched_at: datetime) -> None:
    lines = [
        "# 書籍一覧 (books.toscrape.com)",
        "",
        f"- 取得日時: {fetched_at:%Y-%m-%d %H:%M:%S}",
        f"- 取得元: {BASE_URL}",
        f"- 件数: {len(books)} 件",
        "",
        "| No. | タイトル | 価格 | 在庫状況 |",
        "| ---: | --- | ---: | --- |",
    ]
    for i, book in enumerate(books, start=1):
        lines.append(
            f"| {i} | {escape_md(book.title)} | {escape_md(book.price)} | {escape_md(book.availability)} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="books.toscrape.com の書籍情報を収集します")
    parser.add_argument("--max-pages", type=int, default=None, help="取得する最大ページ数 (省略時は全ページ)")
    args = parser.parse_args()

    setup_logging()
    started_at = datetime.now()
    client = PoliteClient(BASE_URL)

    try:
        client.load_robots()
        books = scrape(client, args.max_pages)
    except FetchError as e:
        logger.error("%s", e)
        logger.error("処理を中断して終了します")
        return 1

    OUTPUT_DIR.mkdir(exist_ok=True)
    output_path = OUTPUT_DIR / f"books_{started_at:%Y%m%d}.md"
    write_markdown(books, output_path, started_at)
    logger.info("%d 件を保存しました: %s", len(books), output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
