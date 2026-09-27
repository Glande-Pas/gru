# Copyright Glande-Pas and contributors
# Licensed under the EUPL, see LICENSE.md

""" Fetch a remote zip archive's central directory via HTTP Range requests, without downloading
the archive itself. The central directory is a small, fixed-format index consolidated at the end
of any zip file (see the ZIP format spec's End Of Central Directory record and central directory
file headers) -- it lists every entry's name, CRC-32, and sizes, regardless of how large the
archive's actual (compressed) content is. """

from __future__ import annotations

import struct
import requests
import typing
from typing import NamedTuple

if typing.TYPE_CHECKING:
    from requests import Session


class RemoteZipEntry(NamedTuple):
    """ One central directory record -- the same fields zipfile.ZipInfo exposes for a local
    archive (filename/CRC/sizes), fetched here without downloading the archive itself. """
    filename: str
    crc32: int
    compress_size: int
    file_size: int


# End Of Central Directory record: fixed 22 bytes + a variable-length (0-65535 byte) comment.
_EOCD_SIZE = 22
_EOCD_SIGNATURE = b'PK\x05\x06'
_EOCD_FORMAT = '<4sHHHHLLH'
_EOCD_MAX_COMMENT = 0xFFFF

# How far from the end of the archive to search for the EOCD record: its own size plus the
# largest comment a zip can carry.
_EOCD_SEARCH_MARGIN = _EOCD_SIZE + _EOCD_MAX_COMMENT

# Central directory file header: fixed 46 bytes + a variable-length filename/extra/comment tail.
_CDFH_SIZE = 46
_CDFH_SIGNATURE = b'PK\x01\x02'
_CDFH_FORMAT = '<4sHHHHHHLLLHHHHHLL'


def _range_get(url: str, start: int, end: int, session: Session) -> bytes:
    """ bytes [start, end] (inclusive), via a single HTTP Range request. """
    with session.get(url, headers={'Range': f'bytes={start}-{end}'}, allow_redirects=True) as resp:
        resp.raise_for_status()
        if resp.status_code != 206:
            raise ValueError(f'Server did not honor the Range request for {url} (no partial content support)')
        return resp.content


def _find_eocd(tail: bytes) -> tuple[int, int, int]:
    """ Search `tail` (the archive's own last bytes) for a valid End Of Central Directory record.
    Found via its own comment-length field: the signature happening to appear inside binary
    comment data by coincidence is exceedingly unlikely to *also* have a comment_length landing
    exactly at the end of `tail` -- so a candidate is only accepted once that's verified.
    Returns (total_entries, cd_size, cd_offset). """
    idx = tail.rfind(_EOCD_SIGNATURE)
    while idx != -1:
        if idx + _EOCD_SIZE <= len(tail):
            fields = struct.unpack(_EOCD_FORMAT, tail[idx:idx + _EOCD_SIZE])
            total_entries, cd_size, cd_offset, comment_len = fields[4], fields[5], fields[6], fields[7]
            if idx + _EOCD_SIZE + comment_len == len(tail):
                return total_entries, cd_size, cd_offset
        idx = tail.rfind(_EOCD_SIGNATURE, 0, idx)
    raise ValueError('End Of Central Directory record not found')


def fetch_remote_zip_directory(url: str, content_size: int, expected_entries: int | None = None,
                               session: Session | None = None) -> list[RemoteZipEntry]:
    """ Fetch and parse `url`'s zip central directory: one Range request for a tail chunk
    containing the EOCD record, from which the central directory is sliced directly if it's
    small enough to already be inside that tail (the common case), else fetched with a second,
    targeted Range request. `content_size` is a HEAD request's content-length. `expected_entries`,
    if given, is cross-checked against the archive's own count. `session`, if given, is used
    instead of the plain `requests` module (e.g. a requests_cache.CachedSession).

    Raises ValueError if the EOCD can't be found, the entry count doesn't match
    `expected_entries`, or a central directory record is malformed; a server that doesn't honor
    Range requests surfaces as a normal requests.HTTPError. """
    sess: typing.Any = session or requests  # plain `requests` module has a matching .get()
    tail_start = max(0, content_size - _EOCD_SEARCH_MARGIN)
    tail = _range_get(url, tail_start, content_size - 1, sess)

    total_entries, cd_size, cd_offset = _find_eocd(tail)
    if expected_entries is not None and total_entries != expected_entries:
        raise ValueError(f'Central directory reports {total_entries} entries, expected {expected_entries}')

    local_start = cd_offset - tail_start
    if 0 <= local_start and local_start + cd_size <= len(tail):
        data = tail[local_start:local_start + cd_size]
    else:
        data = _range_get(url, cd_offset, cd_offset + cd_size - 1, sess)

    entries = []
    pos = 0
    for _ in range(total_entries):
        if data[pos:pos + 4] != _CDFH_SIGNATURE:
            raise ValueError(f'Malformed central directory record at offset {pos}')
        (_, _, _, _, _, _, _, crc32, comp_size, uncomp_size, fn_len, extra_len, comment_len2,
         _, _, _, _) = struct.unpack(_CDFH_FORMAT, data[pos:pos + _CDFH_SIZE])
        name_start = pos + _CDFH_SIZE
        filename = data[name_start:name_start + fn_len].decode('utf-8', errors='replace')
        entries.append(RemoteZipEntry(filename, crc32, comp_size, uncomp_size))
        pos = name_start + fn_len + extra_len + comment_len2

    if pos != len(data):
        raise ValueError(f'Central directory size mismatch: parsed {pos} of {len(data)} bytes')

    return entries
