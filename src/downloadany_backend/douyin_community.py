"""Temporary Douyin extractor override based on yt-dlp PR #17826.

Remove this module once the upstream fix has shipped in the pinned stable yt-dlp.
"""

import hashlib
import json
import time
import urllib.parse
import uuid

from yt_dlp.extractor.tiktok import DouyinIE as _BuiltinDouyinIE
from yt_dlp.utils import ExtractorError
from yt_dlp.utils.traversal import traverse_obj


class DouyinIE(_BuiltinDouyinIE):
    _VALID_URL = (
        r'https?://(?:www\.)?douyin\.com/'
        r'(?:video/|jingxuan\?[^#]*?\bmodal_id=)(?P<id>[0-9]+)'
    )
    _SEC_SALT = 'A96D855A08C0A9707F8BEF0D9A527E4E'
    _REQUIRED_COOKIES = ('passport_csrf_token', 'ttwid')
    _AID = 6383
    _CSRF = uuid.uuid4().hex

    def _get_ttwid(self):
        self._request_webpage(
            f'{self._WEBPAGE_HOST}ttwid/register',
            None,
            data=json.dumps({'aid': self._AID, 'service': 'www.douyin.com'}).encode(),
            headers={'content-type': 'application/json'},
        )
        cookie = self._get_cookies(self._WEBPAGE_HOST).get('ttwid')
        return cookie.value if cookie else None

    def _ensure_required_cookies(self):
        for name in self._REQUIRED_COOKIES:
            if self._get_cookies(self._WEBPAGE_HOST).get(name):
                continue
            value = self._get_ttwid() if name == 'ttwid' else self._CSRF
            if not value:
                raise ExtractorError('Unable to generate required cookies')
            self._set_cookie('.douyin.com', name, value)

    def _get_uifid(self):
        names = ('uifid', 'uifid_temp', 'uifidtemp', 'UIFID', 'UIFID_TEMP', 'UIFIDTEMP')
        value = traverse_obj(
            self._get_cookies(self._WEBPAGE_HOST),
            (names, 'value'),
            get_all=False,
        )
        if value:
            return value
        response = self._request_webpage(self._WEBPAGE_HOST, None)
        return response.headers.get('x-tt-trace-host')

    def _sign_query(self, query):
        uifid = self._get_uifid()
        if not uifid:
            raise ExtractorError('Unable to obtain Douyin security identifier')
        timestamp = str(int(time.time()))
        query = {**query, 'uifid': uifid, 'timestamp': timestamp}
        encoded = urllib.parse.urlencode(query)
        signature = hashlib.md5(
            f'{uifid}_{timestamp}_{self._SEC_SALT}_{encoded}'.encode()
        ).hexdigest()
        return {**query, 'x-secsdk-web-signature': signature}

    def _get_aweme_detail(self, video_id):
        return traverse_obj(
            self._download_json(
                'https://www.douyin.com/aweme/v1/web/aweme/detail/',
                video_id,
                'Downloading signed web detail JSON',
                'Failed to download signed web detail JSON',
                query=self._sign_query({'aweme_id': video_id, 'aid': self._AID}),
                fatal=False,
            ),
            ('aweme_detail', {dict}),
        )

    def _real_extract(self, url):
        video_id = self._match_id(url)
        detail = traverse_obj(
            self._download_json(
                'https://www.douyin.com/aweme/v1/web/aweme/detail/',
                video_id,
                'Downloading web detail JSON',
                'Failed to download web detail JSON',
                query={'aweme_id': video_id},
                fatal=False,
            ),
            ('aweme_detail', {dict}),
        )
        if detail:
            return self._parse_aweme_video_app(detail)

        self._ensure_required_cookies()
        detail = self._get_aweme_detail(video_id)
        if not detail:
            raise ExtractorError(
                'Fresh cookies (not necessarily logged in) are needed',
                expected=not self._get_cookies(self._WEBPAGE_HOST).get('s_v_web_id'),
            )
        return {
            **self._parse_aweme_video_app(detail),
            'http_headers': {'Referer': self._WEBPAGE_HOST},
        }

