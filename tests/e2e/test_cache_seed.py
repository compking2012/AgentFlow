import base64
import hashlib
import json

from .cache_seed import seed_locked_npm_cache


def test_only_matching_public_archive_bytes_are_reused(tmp_path):
    archive = b'package archive fixture'
    checksum = hashlib.sha512(archive).digest()
    hex_value = checksum.hex()
    source = tmp_path / 'source'
    relative = f'_cacache/content-v2/sha512/{hex_value[:2]}/{hex_value[2:4]}/{hex_value[4:]}'
    file = source / relative
    file.parent.mkdir(parents=True)
    file.write_bytes(archive)
    lock = tmp_path / 'lock.json'
    entry = {'resolved': 'https://registry.npmjs.org/example/-/example.tgz',
             'integrity': 'sha512-' + base64.b64encode(checksum).decode()}
    lock.write_text(json.dumps({'packages': {'example': entry}}))
    destination = tmp_path / 'destination'
    assert seed_locked_npm_cache(destination, [source], [lock]) == ['example']
    assert (destination / relative).read_bytes() == archive
    assert seed_locked_npm_cache(destination, [source], [lock]) == []
    file.write_bytes(b'incorrect archive')
    assert seed_locked_npm_cache(tmp_path / 'bad', [source], [lock]) == []
    assert not (tmp_path / 'bad').exists()
    file.write_bytes(archive)
    entry['resolved'] = 'https://private.invalid/package.tgz'
    lock.write_text(json.dumps({'packages': {'private': entry}}))
    assert seed_locked_npm_cache(tmp_path / 'private', [source], [lock]) == []
