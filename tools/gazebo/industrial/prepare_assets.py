"""Install only reviewed CC0 sources; validate content and record observed hashes.

No Blender, pip install or modification of the existing Gazebo/PX4 environment.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import struct
import sys
import urllib.request

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE/'assets.json').read_text())
LOCK = HERE/'assets.lock.json'


def fetch(url, limit):
    request = urllib.request.Request(url, headers={'User-Agent': 'IndustrialGazeboAssetSetup/1.0'})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(limit+1)
    if len(data) > limit:
        raise ValueError(f'Asset exceeds {limit} bytes: {url}')
    return data


def inspect_glb(data):
    if len(data) < 20:
        raise ValueError('Truncated GLB')
    magic, version, size = struct.unpack_from('<4sII', data)
    if magic != b'glTF' or version != 2 or size != len(data):
        raise ValueError('Expected a complete GLB 2.0 file')
    length, kind = struct.unpack_from('<II', data, 12)
    if kind != 0x4e4f534a or 20+length > len(data):
        raise ValueError('Invalid GLB JSON chunk')
    doc = json.loads(data[20:20+length])
    offset = 12
    binary = None
    while offset < len(data):
        if offset+8 > len(data):
            raise ValueError('Truncated GLB chunk header')
        chunk_length, chunk_kind = struct.unpack_from('<II', data, offset)
        offset += 8
        if chunk_length % 4 or offset+chunk_length > len(data):
            raise ValueError('Invalid GLB chunk length')
        if chunk_kind == 0x004e4942:
            if binary is not None:
                raise ValueError('Multiple BIN chunks')
            binary = data[offset:offset+chunk_length]
        offset += chunk_length
    buffers = doc.get('buffers', [])
    if len(buffers) != 1 or binary is None or not 0 < buffers[0]['byteLength'] <= len(binary):
        raise ValueError('Expected one embedded BIN buffer')
    for view in doc.get('bufferViews', []):
        if (view.get('buffer', 0) != 0 or view.get('byteOffset', 0) < 0
                or view.get('byteLength', 0) <= 0
                or view.get('byteOffset', 0)+view['byteLength'] > buffers[0]['byteLength']):
            raise ValueError('Invalid GLB buffer view')
    unsupported = set(doc.get('extensionsRequired', [])) - {'KHR_materials_unlit', 'KHR_texture_transform'}
    if unsupported:
        raise ValueError(f'Needs an unverified loader extension: {sorted(unsupported)}')
    # Do not depend on network sidecars or absolute paths embedded in a downloaded model.
    if any('uri' in b for b in doc.get('buffers', [])):
        raise ValueError('GLB must contain its own binary buffer')
    for image in doc.get('images', []):
        if image.get('mimeType') not in ('image/png', 'image/jpeg') or 'bufferView' not in image:
            raise ValueError('Worker images must be embedded PNG/JPEG')
    accessors = doc.get('accessors', [])
    triangles = 0
    primitives = 0
    for mesh in doc.get('meshes', []):
        for primitive in mesh.get('primitives', []):
            if primitive.get('mode', 4) != 4:
                raise ValueError('Only TRIANGLES geometry is supported in this first stage')
            index = primitive.get('indices', primitive['attributes']['POSITION'])
            count = accessors[index]['count']
            if count % 3:
                raise ValueError('Incomplete triangle list')
            triangles += count//3
            primitives += 1
    if not 1 <= triangles <= 250000:
        raise ValueError(f'Worker mesh budget exceeded or empty: {triangles} triangles')
    return {'triangles_in_mesh_definitions': triangles, 'primitives': primitives,
            'images': len(doc.get('images', [])), 'materials': len(doc.get('materials', [])),
            'skins': len(doc.get('skins', [])), 'animations': len(doc.get('animations', [])),
            'bytes': len(data), 'render_verified': False}


def install_file(path, data, entry, lock, metrics=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    key = str(path.relative_to(HERE))
    # Never silently overwrite an installed file with changed content.
    if path.exists() and path.read_bytes() != data:
        raise ValueError(f'Existing asset differs; preserve/review it before replacement: {path}')
    temporary = path.with_suffix(path.suffix+'.part')
    temporary.write_bytes(data)
    temporary.replace(path)
    lock[key] = {**entry, 'sha256': digest, 'downloaded_at': datetime.now(timezone.utc).isoformat()}
    if metrics:
        lock[key]['metrics'] = metrics
    LOCK.write_text(json.dumps(lock, indent=2)+'\n')


def check(site=False):
    lock = json.loads(LOCK.read_text()) if LOCK.exists() else {}
    paths = [CONFIG['worker']['path']]
    if site:
        paths += ['models/industrial_materials/materials/textures/'+name
                  for name in CONFIG['concrete']['maps'].values()]
    for name in paths:
        path = HERE/name
        if not path.is_file() or name not in lock:
            raise ValueError(f'Missing reviewed asset: {name}; run prepare_assets.py'+(' --site' if site else ''))
        if lock[name].get('license') != 'CC0-1.0' or hashlib.sha256(path.read_bytes()).hexdigest() != lock[name]['sha256']:
            raise ValueError(f'Asset license/hash mismatch: {name}')
        if path.suffix == '.glb':
            inspect_glb(path.read_bytes())
    print('PASS: reviewed asset files and recorded SHA256 match (not a render test).')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', action='store_true', help='Also download the reviewed 1K concrete maps')
    parser.add_argument('--check', action='store_true', help='Offline file/hash check; no network')
    args = parser.parse_args()
    try:
        if args.check:
            check(args.site)
            return 0
        lock = json.loads(LOCK.read_text()) if LOCK.exists() else {}
        entry = CONFIG['worker']
        path = HERE/entry['path']
        if not path.exists():
            data = fetch(entry['url'], 50*1024*1024)
            metrics = inspect_glb(data)
            install_file(path, data, entry, lock, metrics)
            print(json.dumps(metrics, indent=2))
        if args.site:
            entry = CONFIG['concrete']
            metadata = json.loads(fetch(entry['api'], 4*1024*1024))
            for map_name, filename in entry['maps'].items():
                path = HERE/'models/industrial_materials/materials/textures'/filename
                if path.exists():
                    continue
                url = metadata[map_name][entry['resolution']]['jpg']['url']
                if not url.startswith('https://dl.polyhaven.org/'):
                    raise ValueError('Unexpected Poly Haven download host')
                data = fetch(url, 5*1024*1024)
                if not data.startswith(b'\xff\xd8\xff') or not data.endswith(b'\xff\xd9'):
                    raise ValueError('Expected a complete JPG texture')
                install_file(path, data, {**entry, 'url': url}, lock)
        check(args.site)
    except Exception as error:
        print(f'ASSETS NOT READY: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
