"""Verified image downloads with per-image checkpoints and safe reuse."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
from threading import Event
import time
from urllib.parse import parse_qsl, urlencode, urlparse

from PIL import Image, UnidentifiedImageError
import requests

from fb_runtime import atomic_json

MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_DOWNLOAD_SECONDS = 120

def image_key(url):
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if host == 'fbcdn.net' or host.endswith('.fbcdn.net'):
        query = [(key, value) for key, value in query
                 if key not in {'oh', 'oe'} and not key.startswith('_nc_')]
    return parsed.netloc.lower() + parsed.path + '?' + urlencode(sorted(query))


def extension_from_url(url):
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {'.jpg', '.jpeg', '.png', '.webp', '.gif'} else '.jpg'


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as file:
        for chunk in iter(lambda: file.read(128 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def download_image(url, folder, filename):
    target = Path(folder) / filename
    temporary = target.with_name(target.name + '.part')
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        with requests.get(url, stream=True, timeout=(10, 30), headers={
            'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.facebook.com/'
        }) as response:
            response.raise_for_status()
            if not response.headers.get('Content-Type', '').lower().startswith('image/'):
                raise ValueError('回應不是圖片')
            length = response.headers.get('Content-Length')
            if length and int(length) > MAX_IMAGE_BYTES:
                raise ValueError('圖片超過大小上限')
            received = 0
            with temporary.open('wb') as file:
                for chunk in response.iter_content(chunk_size=128 * 1024):
                    if time.monotonic() - started > MAX_DOWNLOAD_SECONDS:
                        raise ValueError('圖片下載超過時間上限')
                    if chunk:
                        received += len(chunk)
                        if received > MAX_IMAGE_BYTES:
                            raise ValueError('圖片超過大小上限')
                        file.write(chunk)
                file.flush()
                os.fsync(file.fileno())
        # HTTP success alone does not establish that the image is usable.
        with Image.open(temporary) as image:
            image.verify()
        with Image.open(temporary) as image:
            image.load()
        temporary.replace(target)
        print(f'下載成功：{filename}')
        return True
    except (requests.RequestException, OSError, ValueError, SyntaxError,
            UnidentifiedImageError, Image.DecompressionBombError) as exc:
        # Request exceptions can include signed URLs. Do not write those to logs.
        print(f'下載失敗：{filename} ({type(exc).__name__})')
        return False
    finally:
        temporary.unlink(missing_ok=True)


def reusable_image(path, url, record):
    if not isinstance(record, dict) or record.get('image_key') != image_key(url):
        return False
    try:
        return (path.stat().st_size > 0
                and path.stat().st_size == record.get('size')
                and file_hash(path) == record.get('sha256'))
    except OSError:
        return False


def read_posts(json_path):
    try:
        data = json.loads(Path(json_path).read_text(encoding='utf-8'))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError('貼文 JSON 格式錯誤。') from exc
    if not isinstance(data, list):
        raise ValueError('貼文 JSON 必須是陣列。')
    indexes = set()
    for post in data:
        if not isinstance(post, dict):
            raise ValueError('每篇貼文必須是物件。')
        index = post.get('index')
        if type(index) is not int or index < 0 or index in indexes:
            raise ValueError('貼文 index 必須是非負整數且不可重複。')
        indexes.add(index)
        urls = post.get('image_urls', [])
        if not isinstance(urls, list) or not all(
            isinstance(url, str) and urlparse(url).scheme in {'http', 'https'}
            and urlparse(url).hostname for url in urls
        ):
            raise ValueError(f'貼文 {index} 的圖片網址格式錯誤。')
        if not isinstance(post.get('text', ''), str):
            raise ValueError(f'貼文 {index} 的文字格式錯誤。')
    return data


def download_images_from_json(json_path, image_folder):
    data = read_posts(json_path)
    failed_total = 0
    for post in data:
        folder = Path(image_folder) / f"post_{post['index']:03d}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'text.txt').write_text(post.get('text', ''), encoding='utf-8')
        prior = post.get('download_manifest', {})
        prior = prior if isinstance(prior, dict) else {}
        manifest = {}
        jobs = []
        expected_files = []
        for index, url in enumerate(post.get('image_urls', []), start=1):
            filename = f'image_{index:03d}{extension_from_url(url)}'
            expected_files.append(filename)
            if reusable_image(folder / filename, url, prior.get(filename)):
                manifest[filename] = prior[filename]
            else:
                jobs.append((filename, url))

        def checkpoint():
            post['download_manifest'] = dict(manifest)
            post['downloaded_image_count'] = len(manifest)
            # Includes pending files, so interruption cannot look like success.
            post['failed_image_files'] = [name for name in expected_files if name not in manifest]
            atomic_json(json_path, data)
            atomic_json(folder / 'post.json', post)

        def download_task(filename, url):
            for attempt in range(3):
                if cancelled.is_set():
                    return filename, None
                if download_image(url, folder, filename):
                    path = folder / filename
                    return filename, dict(image_key=image_key(url), size=path.stat().st_size,
                                          sha256=file_hash(path))
                if attempt < 2:
                    cancelled.wait(attempt + 1)
            return filename, None

        checkpoint()
        if manifest:
            print(f"貼文 {post['index']}：略過 {len(manifest)} 張已驗證圖片")
        pool = ThreadPoolExecutor(max_workers=4)
        futures = []
        cancelled = Event()
        try:
            for job in jobs:
                futures.append(pool.submit(download_task, *job))
            for future in as_completed(futures):
                filename, record = future.result()
                if record:
                    manifest[filename] = record
                checkpoint()
        except BaseException:
            cancelled.set()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            # Workers may finish after the main thread is interrupted. Preserve
            # their verified results too, so restarting does not download them again.
            for future in futures:
                if future.cancelled():
                    continue
                try:
                    filename, record = future.result()
                except BaseException:
                    continue
                if record:
                    manifest[filename] = record
            checkpoint()
        failed_total += len(post['failed_image_files'])
    print(f'圖片下載結束：{len(data)} 篇貼文資料夾，失敗 {failed_total} 張')
    return failed_total
