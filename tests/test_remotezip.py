"""Tests for gru.remotezip: parsing a zip's central directory via HTTP Range requests, without
downloading the archive -- verified against zipfile's own (trusted) reading of the same bytes."""

import io
import typing
import zipfile

import pytest
import requests

from gru.remotezip import fetch_remote_zip_directory


def _make_zip(entries: dict[str, bytes], comment: bytes = b'') -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
        zf.comment = comment
    return buf.getvalue()


class FakeRangeResponse:
    def __init__(self, content: bytes, status_code: int = 206):
        self.content = content
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code} error')


def _fake_get(zip_bytes: bytes, *, status_code: int = 206, honor_range: bool = True):
    def get(url, headers, allow_redirects=True):
        if not honor_range:
            return FakeRangeResponse(zip_bytes, status_code=200)
        range_header = headers['Range']
        start, end = (int(n) for n in range_header.removeprefix('bytes=').split('-'))
        return FakeRangeResponse(zip_bytes[start:end + 1], status_code=status_code)
    return get


def _counting_get(zip_bytes: bytes) -> tuple[typing.Callable, list[str]]:
    """Same as _fake_get(), but also records every Range header requested -- for asserting *how
    many* Range requests fetch_remote_zip_directory() actually made."""
    calls: list[str] = []

    def get(url, headers, allow_redirects=True):
        calls.append(headers['Range'])
        start, end = (int(n) for n in headers['Range'].removeprefix('bytes=').split('-'))
        return FakeRangeResponse(zip_bytes[start:end + 1])
    return get, calls


class TestFetchRemoteZipDirectory:
    def test_matches_zipfile_own_reading(self, monkeypatch):
        entries = {'MyAddon/MyAddon.txt': b'## Title: MyAddon\n', 'MyAddon/Data.lua': b'return 42\n'}
        zip_bytes = _make_zip(entries)
        monkeypatch.setattr('gru.remotezip.requests.get', _fake_get(zip_bytes))

        result = fetch_remote_zip_directory('http://x/addon.zip', len(zip_bytes), len(entries))

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            expected = {info.filename: (info.CRC, info.compress_size, info.file_size) for info in zf.infolist()}
        actual = {e.filename: (e.crc32, e.compress_size, e.file_size) for e in result}
        assert actual == expected

    def test_single_entry(self, monkeypatch):
        zip_bytes = _make_zip({'solo.txt': b'hello world'})
        monkeypatch.setattr('gru.remotezip.requests.get', _fake_get(zip_bytes))

        [entry] = fetch_remote_zip_directory('http://x/addon.zip', len(zip_bytes), 1)
        assert entry.filename == 'solo.txt'

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            [info] = zf.infolist()
        assert entry.crc32 == info.CRC
        assert entry.file_size == info.file_size

    def test_many_entries(self, monkeypatch):
        entries = {f'Addon/file{n}.lua': f'-- file {n}'.encode() for n in range(30)}
        zip_bytes = _make_zip(entries)
        monkeypatch.setattr('gru.remotezip.requests.get', _fake_get(zip_bytes))

        result = fetch_remote_zip_directory('http://x/addon.zip', len(zip_bytes), len(entries))
        assert {e.filename for e in result} == set(entries)

    def test_nonempty_comment_is_supported(self, monkeypatch):
        """A comment shifts the EOCD earlier than the archive's last 22 bytes -- the tail search
        must still find it and parse the central directory it points to correctly."""
        entries = {'a.txt': b'x', 'b.txt': b'y'}
        zip_bytes = _make_zip(entries, comment=b'a comment, not empty')
        monkeypatch.setattr('gru.remotezip.requests.get', _fake_get(zip_bytes))

        result = fetch_remote_zip_directory('http://x/addon.zip', len(zip_bytes), len(entries))

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            expected = {info.filename: info.CRC for info in zf.infolist()}
        assert {e.filename: e.crc32 for e in result} == expected

    def test_comment_containing_the_eocd_signature_does_not_confuse_the_search(self, monkeypatch):
        """A comment that happens to contain literal EOCD signature bytes must not be mistaken
        for the real record -- only a candidate whose own comment-length field lands exactly at
        the end of the fetched tail is accepted."""
        entries = {'a.txt': b'x'}
        zip_bytes = _make_zip(entries, comment=b'decoy' + b'PK\x05\x06' + b'decoy2')
        monkeypatch.setattr('gru.remotezip.requests.get', _fake_get(zip_bytes))

        result = fetch_remote_zip_directory('http://x/addon.zip', len(zip_bytes), len(entries))

        assert {e.filename for e in result} == set(entries)

    def test_expected_entries_omitted_skips_validation(self, monkeypatch):
        zip_bytes = _make_zip({'a.txt': b'x', 'b.txt': b'y'})
        monkeypatch.setattr('gru.remotezip.requests.get', _fake_get(zip_bytes))

        result = fetch_remote_zip_directory('http://x/addon.zip', len(zip_bytes))

        assert {e.filename for e in result} == {'a.txt', 'b.txt'}

    def test_entry_count_mismatch_raises(self, monkeypatch):
        zip_bytes = _make_zip({'a.txt': b'x', 'b.txt': b'y'})
        monkeypatch.setattr('gru.remotezip.requests.get', _fake_get(zip_bytes))

        with pytest.raises(ValueError, match='expected 5'):
            fetch_remote_zip_directory('http://x/addon.zip', len(zip_bytes), 5)

    def test_server_ignoring_range_raises(self, monkeypatch):
        zip_bytes = _make_zip({'a.txt': b'x'})
        monkeypatch.setattr('gru.remotezip.requests.get', _fake_get(zip_bytes, honor_range=False))

        with pytest.raises(ValueError, match='did not honor'):
            fetch_remote_zip_directory('http://x/addon.zip', len(zip_bytes), 1)

    def test_http_error_propagates(self, monkeypatch):
        zip_bytes = _make_zip({'a.txt': b'x'})
        monkeypatch.setattr('gru.remotezip.requests.get', _fake_get(zip_bytes, status_code=404))

        with pytest.raises(requests.HTTPError):
            fetch_remote_zip_directory('http://x/addon.zip', len(zip_bytes), 1)

    def test_ordinary_archive_needs_only_one_range_request(self, monkeypatch):
        """A small central directory is already contained in the tail response -- no second
        request should be made."""
        entries = {f'Addon/file{n}.lua': f'-- file {n}'.encode() for n in range(30)}
        zip_bytes = _make_zip(entries)
        get, calls = _counting_get(zip_bytes)
        monkeypatch.setattr('gru.remotezip.requests.get', get)

        result = fetch_remote_zip_directory('http://x/addon.zip', len(zip_bytes), len(entries))

        assert {e.filename for e in result} == set(entries)
        assert len(calls) == 1

    def test_central_directory_larger_than_margin_falls_back_to_second_request(self, monkeypatch):
        """A central directory bigger than the tail margin needs a second, targeted request."""
        # 300 entries * (46-byte header + 300-char filename) = ~103KB, comfortably past the margin.
        entries = {f'{"F" * 296}{n:04d}': b'x' for n in range(300)}
        zip_bytes = _make_zip(entries)
        get, calls = _counting_get(zip_bytes)
        monkeypatch.setattr('gru.remotezip.requests.get', get)

        result = fetch_remote_zip_directory('http://x/addon.zip', len(zip_bytes), len(entries))

        assert {e.filename for e in result} == set(entries)
        assert len(calls) == 2
