"""Summaries separate verified completion from a normally terminated process."""
import re

from fb_download import extension_from_url, image_key


def downloads_verified(post):
    """Check serialized evidence against each expected file and source identity.

    This checks metadata consistency; the downloader owns actual file hashing
    and decoding. A summary alone cannot establish that files still exist.
    """
    urls = post.get('image_urls', [])
    manifest = post.get('download_manifest', {})
    count = post.get('downloaded_image_count', 0)
    if (not isinstance(urls, list) or not isinstance(manifest, dict)
            or type(count) is not int or count != len(urls) or len(manifest) != len(urls)):
        return False
    try:
        for index, url in enumerate(urls, 1):
            if not isinstance(url, str):
                return False
            record = manifest.get(f'image_{index:03d}{extension_from_url(url)}')
            if (not isinstance(record, dict) or record.get('image_key') != image_key(url)
                    or type(record.get('size')) is not int or record['size'] <= 0
                    or not isinstance(record.get('sha256'), str)
                    or re.fullmatch(r'[0-9a-fA-F]{64}', record['sha256']) is None):
                return False
    except ValueError:
        return False
    return True


def collection_summary(posts, requested, mode):
    text_incomplete = [post['index'] for post in posts
                       if mode in {'text', 'all'} and post.get('text_complete') is not True]
    images_incomplete = [post['index'] for post in posts
                         if mode in {'image', 'all'} and post.get('images_complete') is not True]
    failed_files = sum(len(post.get('failed_image_files', [])) for post in posts)
    downloads_incomplete = [post['index'] for post in posts
                            if mode in {'image', 'all'} and not downloads_verified(post)]
    complete = (len(posts) == requested and not text_incomplete
                and not images_incomplete and not downloads_incomplete and failed_files == 0)
    return dict(status='complete' if complete else 'partial', mode=mode,
                requested_posts=requested, collected_posts=len(posts),
                text_incomplete_indexes=text_incomplete,
                images_unverified_indexes=images_incomplete,
                downloads_unverified_indexes=downloads_incomplete,
                downloaded_images=sum(post.get('downloaded_image_count', 0) for post in posts),
                failed_image_count=failed_files)
