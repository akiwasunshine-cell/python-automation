"""quotes.toscrape.com/js から名言と著者名を Playwright で収集し、Markdown とスクリーンショットを保存する。

JavaScript で描画されるページのため、実ブラウザを画面表示モードで動かして取得する。
既定では Windows 標準の Microsoft Edge を使う (Playwright 同梱の Chromium が
この環境では起動できないため)。

事前準備:
    pip install -r requirements.txt
    python -m playwright install chromium   # --browser chromium を使う場合のみ必要

使い方:
    python scrape_quotes.py                     # 全ページを取得
    python scrape_quotes.py --max-pages 3       # 先頭3ページだけ取得
    python scrape_quotes.py --browser chromium  # Playwright 同梱の Chromium を使う
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

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

START_URL = "https://quotes.toscrape.com/js/"
USER_AGENT_TOKEN = "python-automation-quote-scraper"  # robots.txt の照合に使う名前
TIMEOUT_MS = 15_000
WAIT_MIN_SEC = 1.0
WAIT_MAX_SEC = 3.0

PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / "output"
LOG_DIR = PROJECT_DIR / "logs"

logger = logging.getLogger("scrape_quotes")


@dataclass
class Quote:
    text: str
    author: str


class FetchError(Exception):
    """接続エラーなど、処理を継続できない取得エラー。"""


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"scrape_quotes_{datetime.now():%Y%m%d}.log"
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


class PoliteBrowser:
    """robots.txt を守り、リクエスト間にランダムな待機を入れてページを開く。"""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.robots = RobotFileParser()
        self._requested = False

    def _wait(self) -> None:
        if not self._requested:
            self._requested = True
            return
        delay = random.uniform(WAIT_MIN_SEC, WAIT_MAX_SEC)
        logger.info("%.1f 秒待機します", delay)
        time.sleep(delay)

    def load_robots(self, site_url: str) -> None:
        robots_url = urljoin(site_url, "/robots.txt")
        logger.info("robots.txt を取得します: %s", robots_url)
        self._wait()
        try:
            response = self.page.request.get(robots_url, timeout=TIMEOUT_MS)
        except PlaywrightError as e:
            raise FetchError(f"接続エラー: {robots_url} ({e.message})") from e

        # urllib.robotparser.RobotFileParser.read() と同じ規則で解釈する
        status = response.status
        if status in (401, 403):
            logger.warning("robots.txt へのアクセスが拒否されました。全パスを禁止として扱います")
            self.robots.disallow_all = True
        elif 400 <= status < 500:
            logger.info("robots.txt がありません (HTTP %d)。全パスを許可として扱います", status)
            self.robots.allow_all = True
        elif status >= 500:
            raise FetchError(f"robots.txt の取得に失敗しました (HTTP {status})")
        else:
            self.robots.parse(response.text().splitlines())
            logger.info("robots.txt を読み込みました")

    def can_fetch(self, url: str) -> bool:
        return self.robots.can_fetch(USER_AGENT_TOKEN, url)

    def open(self, url: str) -> bool:
        """ページを開き、名言が描画されるまで待つ。robots.txt で禁止されている場合は False を返す。"""
        if not self.can_fetch(url):
            logger.warning("robots.txt で禁止されているためスキップします: %s", url)
            return False
        self._wait()
        logger.info("取得中: %s", url)
        try:
            response = self.page.goto(url, timeout=TIMEOUT_MS, wait_until="domcontentloaded")
        except PlaywrightError as e:
            raise FetchError(f"接続エラー: {url} ({e.message})") from e
        if response is not None and not response.ok:
            raise FetchError(f"HTTP エラー: {url} (HTTP {response.status})")
        try:
            # JavaScript で名言が描画されるまで待つ
            self.page.wait_for_selector("div.quote", timeout=TIMEOUT_MS)
        except PlaywrightError as e:
            raise FetchError(f"名言が表示されませんでした: {url} ({e.message})") from e
        return True


def extract_quotes(page: Page) -> list[Quote]:
    quotes: list[Quote] = []
    for item in page.locator("div.quote").all():
        text = item.locator("span.text").inner_text().strip()
        author = item.locator("small.author").inner_text().strip()
        quotes.append(Quote(text=text, author=author))
    return quotes


def find_next_page(page: Page) -> str | None:
    next_link = page.locator("li.next a")
    if next_link.count() == 0:
        return None
    href = next_link.first.get_attribute("href")
    return urljoin(page.url, href) if href else None


def scrape(browser: PoliteBrowser, max_pages: int | None, screenshot_path: Path) -> list[Quote]:
    quotes: list[Quote] = []
    url: str | None = START_URL
    page_no = 0
    while url and (max_pages is None or page_no < max_pages):
        if urlparse(url).netloc != urlparse(START_URL).netloc:
            logger.warning("対象サイト外の URL のため停止します: %s", url)
            break
        if not browser.open(url):
            break
        page_no += 1
        if page_no == 1:
            browser.page.screenshot(path=str(screenshot_path), full_page=True)
            logger.info("スクリーンショットを保存しました: %s", screenshot_path)
        page_quotes = extract_quotes(browser.page)
        logger.info("%d ページ目: %d 件", page_no, len(page_quotes))
        quotes.extend(page_quotes)
        url = find_next_page(browser.page)
    return quotes


def escape_md(text: str) -> str:
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def write_markdown(quotes: list[Quote], path: Path, screenshot_path: Path, fetched_at: datetime) -> None:
    lines = [
        "# 名言一覧 (quotes.toscrape.com/js)",
        "",
        f"- 取得日時: {fetched_at:%Y-%m-%d %H:%M:%S}",
        f"- 取得元: {START_URL}",
        f"- 件数: {len(quotes)} 件",
        "",
        "| No. | 名言 | 著者 |",
        "| ---: | --- | --- |",
    ]
    for i, quote in enumerate(quotes, start=1):
        lines.append(f"| {i} | {escape_md(quote.text)} | {escape_md(quote.author)} |")
    lines += ["", "## スクリーンショット (1ページ目)", "", f"![1ページ目のスクリーンショット]({screenshot_path.name})"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="quotes.toscrape.com/js の名言を収集します")
    parser.add_argument("--max-pages", type=int, default=None, help="取得する最大ページ数 (省略時は全ページ)")
    parser.add_argument(
        "--browser",
        choices=["msedge", "chrome", "chromium"],
        default="msedge",
        help="使用するブラウザ (既定: Windows 標準の msedge。chromium は Playwright 同梱版)",
    )
    args = parser.parse_args()

    setup_logging()
    started_at = datetime.now()
    OUTPUT_DIR.mkdir(exist_ok=True)
    markdown_path = OUTPUT_DIR / f"quotes_{started_at:%Y%m%d}.md"
    screenshot_path = OUTPUT_DIR / f"quotes_{started_at:%Y%m%d}.png"

    with sync_playwright() as p:
        # ブラウザを画面に表示して実行する
        channel = None if args.browser == "chromium" else args.browser
        try:
            # GPU プロセスが起動できずページが閉じる環境があるため、GPU を無効にする
            browser = p.chromium.launch(headless=False, channel=channel, args=["--disable-gpu"])
        except PlaywrightError as e:
            logger.error("ブラウザ (%s) を起動できませんでした: %s", args.browser, e.message.splitlines()[0])
            return 1
        try:
            page = browser.new_page()
            polite = PoliteBrowser(page)
            polite.load_robots(START_URL)
            quotes = scrape(polite, args.max_pages, screenshot_path)
        except FetchError as e:
            logger.error("%s", e)
            logger.error("処理を中断して終了します")
            return 1
        finally:
            browser.close()

    write_markdown(quotes, markdown_path, screenshot_path, started_at)
    logger.info("%d 件を保存しました: %s", len(quotes), markdown_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
