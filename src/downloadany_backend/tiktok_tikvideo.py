"""Extract TikTok HD media from tikvideo's download-button response."""

import base64
import binascii
import json
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

import requests


class _DownloadHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.href = None
        self.label = ""
        self.thumbnail = None

    def handle_starttag(self, tag, attrs):
        if tag == "img" and self.thumbnail is None:
            src = dict(attrs).get("src")
            parsed = urlparse(src) if src else None
            if (
                parsed and parsed.scheme in {"http", "https"}
                and parsed.hostname and not parsed.username and not parsed.password
            ):
                # HTMLParser decodes &amp; while preserving signed query values.
                self.thumbnail = src
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.label = ""

    def handle_data(self, data):
        if self.href is not None:
            self.label += data

    def handle_endtag(self, tag):
        if tag == "a" and self.href is not None:
            self.links.append((self.href, " ".join(self.label.split())))
            self.href = None


def extract_tiktok(url: str, *, timeout: float = 30, proxy: str | None = None) -> dict:
    with requests.Session() as session:
        session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://tikvideo.app/en"})
        if proxy:
            session.proxies.update({"http": proxy, "https": proxy})
        response = session.post(
            "https://tikvideo.app/api/ajaxSearch",
            data={"q": url, "lang": "en", "cftoken": ""},
            timeout=timeout,
        )
        response.raise_for_status()
        result = response.json()
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise ValueError("TikTok 解析服务未返回成功结果")
    markup = result.get("data")
    if not isinstance(markup, str):
        raise ValueError("TikTok 解析服务缺少下载按钮")
    parser = _DownloadHTML()
    parser.feed(markup)
    for href, label in parser.links:
        if "Download MP4 HD" not in label:
            continue
        link = urlparse(href)
        if link.scheme != "https" or link.hostname != "dl.snapcdn.app" or link.path != "/get":
            continue
        try:
            token = parse_qs(link.query)["token"][0]
            parts = token.split(".")
            if len(parts) != 3:
                continue
            # Decode only. This third-party payload is not signature-verified.
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            target = payload.get("url") if isinstance(payload, dict) else None
            parsed = urlparse(target) if isinstance(target, str) else None
            if not parsed or parsed.scheme != "https" or parsed.username or parsed.password:
                continue
            host = (parsed.hostname or "").lower()
            if host != "tokcdn.com" and not host.endswith(".tokcdn.com"):
                continue
        except (KeyError, ValueError, UnicodeError, binascii.Error):
            continue
        video_id = re.search(r"/video/(\d+)", url)
        return {
            "id": video_id.group(1) if video_id else urlparse(url).path.strip("/"),
            "title": "TikTok 视频",
            "platform": "tiktok",
            "webpage_url": url,
            "thumbnail": parser.thumbnail,
            "video_url": target,
            "ext": "mp4",
            "quality": "MP4 HD",
            "formats": [],
        }
    raise ValueError("TikTok 解析服务未返回有效的 MP4 HD tokcdn 下载地址")
