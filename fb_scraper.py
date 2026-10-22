from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlencode

from selenium import webdriver
from selenium.common.exceptions import (
    JavascriptException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait


from fb_runtime import COOKIE_FILE, OUTPUT_DIR, atomic_json, is_logged_in, load_cookies
from fb_download import download_images_from_json, image_key
from fb_validation import collection_summary
MESSAGE_SELECTOR = '[data-ad-preview="message"], [data-ad-comet-preview="message"]'

MORE_TEXTS = ("查看更多", "顯示更多", "See more")
LESS_TEXTS = ("顯示較少", "收回", "See less")
NEXT_PHOTO_LABELS = ("下一張相片", "下一張照片", "下一張", "Next photo")
PREVIOUS_PHOTO_LABELS = ("上一張相片", "上一張照片", "上一張", "Previous photo")
CLOSE_LABELS = ("關閉", "Close")


def setup_driver():
    options = Options()
    options.add_argument("--lang=zh-TW")
    options.add_argument('--autoplay-policy=user-gesture-required')
    options.add_argument('--mute-audio')
    driver = webdriver.Chrome(options=options)
    try:
        driver.set_page_load_timeout(45)
        driver.maximize_window()
        return driver
    except BaseException:
        try:
            driver.quit()
        except WebDriverException:
            pass
        raise


def normalize_text(text, controls=None):
    """移除 Facebook 的展開/收合控制文字，但保留貼文換行。"""
    text = (text or "").replace("\u200b", "").strip()
    labels = list(MORE_TEXTS + LESS_TEXTS if controls is None else controls)
    changed = True
    while changed:
        changed = False
        for control in list(labels):
            cleaned = re.sub(
                rf"(?:\s|…|\.\.\.)*{re.escape(control)}\s*$", "", text
            ).rstrip()
            if cleaned != text:
                text = cleaned
                changed = True
                if controls is not None:
                    labels.remove(control)
    return text


def visible_text_control_labels(message):
    """Only strip labels backed by a visible semantic control in the message."""
    if message is None:
        return []
    checks = ' or '.join(f'normalize-space()={json.dumps(label, ensure_ascii=False)}'
                         for label in MORE_TEXTS + LESS_TEXTS)
    selector = f".//*[(self::button or @role='button') and ({checks})]"
    labels = []
    for element in message.find_elements(By.XPATH, selector):
        try:
            if (element.is_displayed() and not is_comment_element(element)
                    and not element.find_elements(By.XPATH, selector)):
                labels.append(element.text.strip())
        except StaleElementReferenceException:
            continue
    return labels


def get_text_hash(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def unique_urls(urls):
    result = []
    seen = set()
    for url in urls:
        if not url:
            continue
        key = image_key(url)
        if key not in seen:
            seen.add(key)
            result.append(url)
    return result


def find_post_container(driver, message_element):
    """取得此文字真正所屬的單一貼文外框。"""
    try:
        container = driver.execute_script(
            "return arguments[0].closest('div[aria-posinset]');", message_element
        )
        if container is not None:
            return container
    except JavascriptException:
        pass

    # 找到下一篇文字之前的最大外框，不能在第一個留言連結就停止。
    return driver.execute_script("""
        let node=arguments[0].parentElement, best=node;
        for(let i=0; node && i<25; i++,node=node.parentElement) {
            if(node.tagName==='BODY' || node.tagName==='HTML') break;
            if(node.querySelectorAll('[data-ad-preview="message"], [data-ad-comet-preview="message"]').length>1) break;
            best=node;
            if(node.getAttribute('role')==='article') break;
        }
        return best;
    """, message_element)


def find_message_element(container):
    elements = container.find_elements(By.CSS_SELECTOR, MESSAGE_SELECTOR)
    for element in elements:
        try:
            if element.is_displayed() and not is_comment_element(element):
                return element
        except StaleElementReferenceException:
            continue
    return None


def click_see_more(driver, container):
    """在單篇貼文範圍內按『查看更多』，並等待全文確實展開。"""
    message = find_message_element(container)
    if message is None:
        return False

    before = message.text.strip()
    xpath = (
        ".//*[(self::div or self::span or self::a or @role='button') and ("
        "normalize-space()='查看更多' or normalize-space()='顯示更多' "
        "or normalize-space()='See more' or @aria-label='查看更多' "
        "or @aria-label='顯示更多' or @aria-label='See more')]"
    )
    buttons = message.find_elements(By.XPATH, xpath)
    if not buttons:
        buttons = container.find_elements(By.XPATH, xpath)

    for button in buttons:
        try:
            if not button.is_displayed() or is_comment_element(button):
                continue
            # An author's ordinary link may itself say "See more". Do not
            # navigate away from the feed while trying to expand its text.
            if button.find_elements(By.XPATH, "./ancestor-or-self::a[not(@role='button')]"):
                continue
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", button
            )
            time.sleep(0.15)
            try:
                button.click()
            except WebDriverException:
                driver.execute_script("arguments[0].click();", button)

            def expanded(_driver):
                try:
                    current = find_message_element(container)
                    if current is None:
                        return False
                    current_text = current.text.strip()
                    return current_text != before and not any(
                        label in current_text for label in MORE_TEXTS
                    )
                except StaleElementReferenceException:
                    return True

            try:
                WebDriverWait(driver, 3).until(expanded)
            except TimeoutException:
                pass
            return True
        except (StaleElementReferenceException, JavascriptException):
            continue
    return False


def is_comment_element(element):
    ancestors = element.find_elements(By.XPATH, "./ancestor-or-self::*[@role='article' and @aria-label]")
    return any('留言' in (node.get_attribute('aria-label') or '')
               or 'comment' in (node.get_attribute('aria-label') or '').lower()
               for node in ancestors)


def is_comment_link(link):
    href = link.get_attribute('href') or ''
    query = parse_qs(urlparse(href).query)
    if query.get('comment_id') or query.get('reply_comment_id'):
        return True
    return is_comment_element(link)


def extract_post_url(container):
    for xpath in (
        ".//a[contains(@href,'/posts/')]",
        ".//a[contains(@href,'story_fbid=')]",
        ".//a[contains(@href,'/permalink/')]",
    ):
        for link in container.find_elements(By.XPATH, xpath):
            href = link.get_attribute("href")
            if href and not is_comment_link(link):
                return href

    # 有些動態牆版型不放 permalink，但相片網址的 set=pcb.<貼文ID>
    # 仍可作為穩定的貼文識別，避免相同文字的不同貼文被誤去重。
    for link in container.find_elements(
        By.XPATH, ".//a[contains(@href,'/photo') and contains(@href,'set=pcb.')]"
    ):
        href = link.get_attribute("href")
        if href and not is_comment_link(link):
            photo_set = parse_qs(urlparse(href).query).get("set", [""])[0]
            post_id = photo_set.removeprefix("pcb.")
            if post_id:
                return href
    return ""


def extract_post_id(post_url):
    if not post_url:
        return ""
    parsed = urlparse(post_url)
    query = parse_qs(parsed.query)
    if query.get("story_fbid"):
        return query["story_fbid"][0]
    if query.get("set") and query["set"][0].startswith("pcb."):
        return query["set"][0].removeprefix("pcb.")
    match = re.search(r"/(?:posts|permalink)/([A-Za-z0-9]+)", parsed.path)
    return match.group(1) if match else ""


def get_image_dimensions(driver, image):
    try:
        width, height = driver.execute_script(
            "return [arguments[0].naturalWidth || arguments[0].width || 0, "
            "arguments[0].naturalHeight || arguments[0].height || 0];",
            image,
        )
        return int(width or 0), int(height or 0)
    except (WebDriverException, TypeError, ValueError):
        return 0, 0


def collect_thumbnail_urls(driver, container):
    urls = []
    for image in container.find_elements(
        By.XPATH, ".//a[contains(@href,'/photo') and contains(@href,'fbid=')]//img[contains(@src,'scontent')]"
    ):
        try:
            if not image.is_displayed() or is_comment_element(image):
                continue
            src = image.get_attribute("src")
            width, height = get_image_dimensions(driver, image)
            # 避免把專頁頭像或表情圖示當成貼文圖片。
            if src and max(width, height) >= 180:
                urls.append(src)
        except StaleElementReferenceException:
            continue
    return unique_urls(urls)


def current_theater_image(driver):
    """只取相片瀏覽器中目前可見、面積最大的媒體圖。"""
    candidates = driver.find_elements(
        By.XPATH,
        "//img[contains(@src,'scontent') and "
        "(@data-visualcompletion='media-vc-image' or ancestor::*[@role='dialog'])]",
    )
    ranked = []
    for image in candidates:
        try:
            if not image.is_displayed():
                continue
            src = image.get_attribute("src")
            width, height = get_image_dimensions(driver, image)
            rect = image.rect
            loaded = driver.execute_script('return arguments[0].complete;', image)
            if src and loaded and max(width, height) >= 180:
                # 以畫面顯示面積挑主圖，避免背景縮圖的原始解析度較大。
                ranked.append((rect['width'] * rect['height'], src))
        except StaleElementReferenceException:
            continue
    return max(ranked, default=(0, ""))[1]


def current_photo_id(driver, image_url=""):
    query = parse_qs(urlparse(driver.current_url).query)
    if query.get("fbid"):
        return query["fbid"][0]
    video = re.search(r'/videos/(?:pcb\.\d+/)?(\d+)', urlparse(driver.current_url).path)
    if video:
        return 'video:' + video.group(1)
    reel = re.search(r'/reel/(\d+)', urlparse(driver.current_url).path)
    if reel:
        return 'video:' + reel.group(1)
    if '/watch' in urlparse(driver.current_url).path and query.get('v'):
        return 'video:' + query['v'][0]
    return image_key(image_url) if image_url else ""


def current_photo_set(driver):
    return media_set_from_url(driver.current_url)


def media_set_from_url(url):
    query = parse_qs(urlparse(url).query)
    if query.get('set'):
        return query['set'][0]
    video_set = re.search(r'/videos/(pcb\.\d+)/', urlparse(url).path)
    return video_set.group(1) if video_set else ''


def find_visible_control(driver, labels):
    checks = " or ".join(
        f"@aria-label={json.dumps(label, ensure_ascii=False)}" for label in labels
    )
    for element in driver.find_elements(By.XPATH, f"//*[{checks}]"):
        try:
            if element.is_displayed() and element.is_enabled():
                return element
        except StaleElementReferenceException:
            continue
    return None


def post_signature(post_url, text, thumbnail_urls):
    post_id = extract_post_id(post_url)
    if post_id:
        return f"post:{post_id}"
    first_image = ""
    if thumbnail_urls:
        first_url = thumbnail_urls[0]
        parsed = urlparse(first_url)
        query = parse_qs(parsed.query)
        if parsed.hostname in {'facebook.com', 'www.facebook.com', 'm.facebook.com'}:
            media_id = query.get('fbid', query.get('v', ['']))[0]
            first_image = parsed.path + ':' + media_id
        else:
            first_image = image_key(first_url)
    return f"fallback:{get_text_hash(text + '|' + first_image)}"


def photo_links_in_post(container):
    links = []
    for link in container.find_elements(By.XPATH, ".//a[contains(@href,'/photo') and contains(@href,'fbid=')]"):
        href = link.get_attribute('href')
        if not href or is_comment_link(link):
            continue
        query = parse_qs(urlparse(href).query)
        if not query.get('fbid'):
            continue
        clean = 'https://www.facebook.com/photo/?fbid=' + query['fbid'][0]
        if query.get('set'):
            clean += '&set=' + query['set'][0]
        if clean not in links:
            links.append(clean)
    return links


def media_links_in_post(driver, container):
    """只找媒體格/同篇 pcb 相簿入口，排除留言中的文字影片連結。"""
    links = []
    anchors = driver.execute_script("""
        return Array.from(arguments[0].querySelectorAll('a[href]')).filter(a=>{
            let parent=a.closest('[role=article][aria-label]');
            return !parent || !/留言|comment/i.test(parent.getAttribute('aria-label'));
        }).map(a=>({
            href:a.href, tile:!!a.querySelector('img,video'),
            overlay:/^\\+\\d+$/.test(a.innerText.trim())
        }));
    """, container)
    for anchor in anchors:
        href = anchor['href'] or ''
        parsed = urlparse(href)
        if parsed.hostname not in {'facebook.com', 'www.facebook.com', 'm.facebook.com'}:
            continue
        query = parse_qs(parsed.query)
        if query.get('comment_id') or query.get('reply_comment_id'):
            continue
        is_photo = '/photo' in parsed.path and bool(query.get('fbid'))
        is_video = bool(re.search(r'/(?:videos|reel)/', parsed.path)) or ('/watch' in parsed.path and bool(query.get('v')))
        if not (is_photo or is_video):
            continue
        is_tile = anchor['tile']
        is_overlay = anchor['overlay']
        if not (is_tile or is_overlay or media_set_from_url(href).startswith('pcb.')):
            continue
        # 清掉追蹤與留言 query，但保留相簿 set，不把影片入口變成全站影片。
        keep = {key: query[key][0] for key in ('fbid', 'set', 'v', 'id') if query.get(key)}
        clean = 'https://www.facebook.com' + parsed.path
        if keep:
            clean += '?' + urlencode(keep)
        if clean not in links:
            links.append(clean)
    return links


def album_overlay_count(driver, container):
    # +N 也可能在影片 anchor 或沒有 href 的 button/span 裡。
    return driver.execute_script("""
        let counts=Array.from(arguments[0].querySelectorAll('a,[role="button"],span,div'))
          .filter(e=>{
            let parent=e.closest('[role=article][aria-label]');
            return (!parent || !/留言|comment/i.test(parent.getAttribute('aria-label')))
              && e.getBoundingClientRect().width && e.getBoundingClientRect().height;
          })
          .map(e=>/^\\+(\\d+)$/.exec(e.innerText.trim())).filter(Boolean).map(m=>+m[1]);
        return Math.max(0,...counts);
    """, container)


def snapshot_post(driver, container):
    click_see_more(driver, container)
    message = find_message_element(container)
    raw = message.text.strip() if message is not None else ""
    links = photo_links_in_post(container)
    media_links = media_links_in_post(driver, container)
    overlay_count = album_overlay_count(driver, container)
    visible_count = len(media_links)
    expected = max(visible_count, visible_count - 1 + overlay_count) if overlay_count and visible_count else (None if overlay_count else visible_count or None)
    video_links = []
    for anchor in container.find_elements(By.XPATH, ".//a[contains(@href,'/reel/') or contains(@href,'/videos/') or contains(@href,'/watch')]"):
        href = anchor.get_attribute('href')
        if href and not is_comment_link(anchor) and href not in video_links:
            video_links.append(href)
    has_video = any(not is_comment_element(video) for video in container.find_elements(By.TAG_NAME, 'video')) or bool(video_links)
    thumbnails = collect_thumbnail_urls(driver, container) if links else []
    url = extract_post_url(container)
    if not url and video_links:
        url = video_links[0]
    return {
        'text': normalize_text(raw, controls=visible_text_control_labels(message)), 'post_url': url,
        'image_urls': thumbnails, 'photo_page_urls': links,
        'media_page_urls': media_links,
        'has_more_media': bool(overlay_count),
        'expected_media_count': expected,
        'media_count_exact': bool(visible_count and overlay_count),
        'expected_image_count': None,
        'media_type': 'photos' if links else ('video' if has_video else 'text'),
        'video_urls': video_links,
        'text_complete': message is not None and not any(label in raw for label in MORE_TEXTS),
    }


def wait_photo(driver, previous=None, timeout=12):
    """等完整載入、URL 和圖檔都換好，且兩次觀測一致。"""
    last = None
    def ready(d):
        nonlocal last
        url = current_theater_image(d)
        photo_id = current_photo_id(d, url)
        media_type = 'photo'
        parsed = urlparse(d.current_url)
        if not photo_id.startswith('video:') and not ('/photo' in parsed.path and parse_qs(parsed.query).get('fbid')):
            # 動態牆縮圖不能被誤認為已開啟相片瀏覽器。
            return False
        if photo_id.startswith('video:'):
            # 同一篇相簿的影片也有左右箭頭，必須穿過它而不是停止。
            url = d.current_url
            media_type = 'video'
            try:
                d.execute_script('document.querySelectorAll("video").forEach(v=>v.pause());')
            except JavascriptException:
                return False
        state = (photo_id, image_key(url))
        if not url or not photo_id:
            return False
        if previous and (photo_id == previous[0] or image_key(url) == previous[1]):
            return False
        if state != last:
            last = state
            return False
        return {'fbid': photo_id, 'url': url, 'set': current_photo_set(d), 'type': media_type}
    return WebDriverWait(driver, timeout, poll_frequency=.3).until(ready)


def advance_photo(driver, labels, previous):
    for attempt in range(3):
        # 先檢查上次是否只是載入較慢，避免重點按鈕而跳過一張。
        if attempt:
            try:
                return wait_photo(driver, previous, timeout=4), 'moved'
            except TimeoutException:
                pass
        control = find_visible_control(driver, labels)
        if control is None:
            # 有些版型只有滑鼠移到照片上才顯示左右箭頭。
            try:
                image = driver.find_element(By.CSS_SELECTOR, 'img[data-visualcompletion="media-vc-image"]')
                ActionChains(driver).move_to_element(image).perform()
            except WebDriverException:
                pass
            try:
                control = WebDriverWait(driver, 3).until(lambda d: find_visible_control(d, labels))
            except TimeoutException:
                continue
        try:
            if attempt:
                driver.execute_script('arguments[0].click();', control)
            else:
                control.click()
        except WebDriverException:
            try:
                driver.execute_script('arguments[0].click();', control)
            except (JavascriptException, StaleElementReferenceException):
                continue
        try:
            return wait_photo(driver, previous), 'moved'
        except TimeoutException:
            print(f'  相片切換未完成，重試 {attempt+1}/3；原 fbid={previous[0]}，目前 fbid={current_photo_id(driver)}，目前圖檔={image_key(current_theater_image(driver))}', flush=True)
    return None, 'navigation_failed' if find_visible_control(driver, labels) else 'controls_unavailable'


def resolve_media_entry(driver, post):
    """在永久連結內點 +N/媒體格，取得 Facebook 實際給出的相簿上下文。"""
    if not post.get('post_url'):
        return None
    permalink = urlparse(post['post_url'])
    query = parse_qs(permalink.query)
    if query.get('story_fbid'):
        clean_query = {k: query[k][0] for k in ('story_fbid', 'id') if query.get(k)}
        driver.get('https://www.facebook.com/permalink.php?' + urlencode(clean_query))
    else:
        driver.get(post['post_url'])
    prefix = re.sub(r'\s+', '', post.get('text', ''))[:24]
    def locate(d):
        for message in d.find_elements(By.CSS_SELECTOR, MESSAGE_SELECTOR):
            try:
                if prefix and prefix in re.sub(r'\s+', '', message.text):
                    dialog = d.execute_script("return arguments[0].closest('[role=dialog]');", message)
                    return dialog if dialog is not None else find_post_container(d, message)
            except StaleElementReferenceException:
                continue
        # 純圖片可用同篇 pcb ID 核對；不使用整個 body 作為媒體範圍。
        post_id = extract_post_id(post.get('post_url', ''))
        roots = d.find_elements(By.CSS_SELECTOR, '[role="dialog"], div[aria-posinset], [role="article"]')
        for root in roots:
            try:
                if is_comment_element(root):
                    continue
                message = find_message_element(root)
                if message is not None and prefix and prefix in re.sub(r'\s+', '', message.text):
                    return root
                if post_id and any(media_set_from_url(link) == 'pcb.' + post_id
                                   for link in media_links_in_post(d, root)):
                    return root
            except StaleElementReferenceException:
                continue
        return False
    try:
        container = WebDriverWait(driver, 8).until(locate)
        target = driver.execute_script("""
            let root=arguments[0];
            // 外層版面容器也可能只有 +N 文字；從最內層往外找真正入口。
            let own=e=>{let p=e.closest('[role=article][aria-label]');return !p || !/留言|comment/i.test(p.getAttribute('aria-label'));};
            let more=Array.from(root.querySelectorAll('a,[role="button"],span,div')).filter(own).reverse()
              .find(e=>/^\\+\\d+$/.test(e.innerText.trim()) && e.getBoundingClientRect().width);
            if(more) return more.closest('a,[role="button"]') || more;
            return Array.from(root.querySelectorAll('a[href]')).filter(own)
              .find(a=>a.querySelector('img,video') && /photo|videos|reel|watch/.test(a.href)) || null;
        """, container)
        if target is None:
            return None
        href = target.get_attribute('href')
        if href and '/photo' in urlparse(href).path and media_set_from_url(href).startswith('pcb.'):
            driver.get(href)
            wait_photo(driver)
            return driver.current_url
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", target)
        driver.execute_script('arguments[0].click();', target)
        wait_photo(driver)
        return driver.current_url
    except (TimeoutException, StaleElementReferenceException, JavascriptException):
        return None


def open_album_entry(driver, post, seed, expected_set):
    driver.get(seed)
    try:
        state = wait_photo(driver)
        if state['set'] == expected_set:
            return state
    except TimeoutException:
        pass
    # 某些 /videos/pcb... 網址會被重新導到不含相簿上下文的 watch。
    # 改從永久連結的 +N 點入，而不是在全站影片串中繼續翻頁。
    if resolve_media_entry(driver, post):
        state = wait_photo(driver)
        if state['set'] == expected_set:
            return state
    raise TimeoutException('無法取得同篇相簿上下文')


def collect_photo_snapshot(driver, post):
    """使用保存的貼文相片入口，在獨立分頁巡覽，不影響動態牆 DOM。"""
    post['images_complete'] = False
    post['expected_image_count'] = None
    post['album_verified_media_count'] = 0
    post['image_collection_status'] = 'incomplete'
    seeds = list(dict.fromkeys(post.get('media_page_urls', post.get('photo_page_urls', []))))
    if not seeds:
        if not (post.get('has_more_media') and post.get('post_url')):
            post['images_complete'] = False
            post['image_collection_status'] = 'unverified_no_media_entry'
            post['expected_image_count'] = None
            return
    sets = [media_set_from_url(seed) for seed in seeds if media_set_from_url(seed).startswith('pcb.')]
    expected_set = max(set(sets), key=sets.count) if sets else ''
    if expected_set:
        seeds = [seed for seed in seeds if media_set_from_url(seed) == expected_set]
    expected_count = post.get('expected_media_count')
    exact_count = post.get('media_count_exact', False)
    previous_records = post.get('image_records', []) + post.get('album_video_records', [])
    records = {}
    verified_ids = set()
    stops = []
    exhausted = False
    feed_handle = driver.current_window_handle
    driver.switch_to.new_window('tab')
    try:
        if not expected_set and post.get('post_url'):
            resolved = resolve_media_entry(driver, post)
            if resolved and media_set_from_url(resolved).startswith('pcb.'):
                expected_set = media_set_from_url(resolved)
                seeds = [resolved] + [s for s in seeds if media_set_from_url(s) == expected_set]
        if not expected_set:
            # 沒有同篇相簿上下文時，不能將 Reel 推薦串當作貼文相簿，
            # 也不能因看不見照片就宣告已抓齊。
            stops.append('unverified_no_album_context')
            seeds = []
        records.update({record['fbid']: record for record in previous_records
                        if record.get('set') == expected_set and expected_set
                        and record.get('type') in {'photo', 'video'}
                        and record.get('fbid') and record.get('url')})
        # 若一個入口遇到載入失敗，改從另一張可見縮圖接續，不直接跳過貼文。
        for seed_index, seed in enumerate(seeds):
            try:
                start = open_album_entry(driver, post, seed, expected_set)
            except TimeoutException:
                stops.append('open_timeout')
                continue
            if start['set'] != expected_set:
                stops.append('entry_set_mismatch')
                continue
            records[start['fbid']] = start
            verified_ids.add(start['fbid'])
            direction_stops = []
            for labels in (NEXT_PHOTO_LABELS, PREVIOUS_PHOTO_LABELS):
                try:
                    state = open_album_entry(driver, post, seed, expected_set)
                except TimeoutException:
                    direction_stops.append('open_timeout')
                    continue
                if state['set'] != expected_set:
                    direction_stops.append('entry_set_mismatch')
                    continue
                visited = {state['fbid']}
                records[state['fbid']] = state
                verified_ids.add(state['fbid'])
                stop = 'safety_limit'
                for _ in range(500):
                    if exact_count and expected_count and len(verified_ids) == expected_count:
                        stop = 'expected_count_reached'
                        exhausted = True
                        break
                    previous = (state['fbid'], image_key(state['url']))
                    next_state, reason = advance_photo(driver, labels, previous)
                    if next_state is None:
                        stop = reason
                        break
                    if expected_set and next_state['set'] != expected_set:
                        stop = 'set_boundary'
                        break
                    state = next_state
                    if state['fbid'] in visited:
                        stop = 'cycle' if state['fbid'] == start['fbid'] else 'unexpected_cycle'
                        break
                    visited.add(state['fbid'])
                    is_new = state['fbid'] not in records
                    records[state['fbid']] = state
                    verified_ids.add(state['fbid'])
                    if is_new and len(records) % 10 == 0:
                        print(f"  貼文 {post['index']}: 已巡覽 {len(records)}/{expected_count or '未知'} 個相簿項目（包含影片）", flush=True)
                direction_stops.append(stop)
                if stop == 'expected_count_reached':
                    break
                if stop == 'cycle':
                    exhausted = True
                    break
            stops.extend(direction_stops)
            if len(direction_stops) == 2 and all(s in {'end', 'set_boundary'} for s in direction_stops):
                exhausted = True
            enough = not expected_count or len(verified_ids) >= expected_count
            if exhausted and enough:
                break
            print(f"  貼文 {post['index']}: 已巡覽 {len(records)}/{expected_count or '未知'}，停止原因 {direction_stops}；換媒體入口重試 ({seed_index+1}/{len(seeds)})", flush=True)
    except WebDriverException as exc:
        stops.append('webdriver_error:' + type(exc).__name__)
        print(f"警告：相簿瀏覽器中斷，已保留 {len(records)} 個項目。", flush=True)
    finally:
        active_exception = sys.exc_info()[0]
        post['album_verified_media_count'] = len(verified_ids)
        if exhausted:
            records = {key: record for key, record in records.items() if key in verified_ids}
        post['image_records'] = [record for record in records.values() if record['type'] == 'photo']
        post['album_video_records'] = [record for record in records.values() if record['type'] == 'video']
        # KeyboardInterrupt skips the code after finally. Make discovered full
        # images available to --download-json before that interruption escapes.
        post['image_urls'] = unique_urls(
            [record['url'] for record in post['image_records']] + post['image_urls'])
        gallery_videos = [record['url'] for record in post['album_video_records']]
        post['video_urls'] = list(dict.fromkeys(post['video_urls'] + gallery_videos))
        post['album_media_count'] = len(records)
        post['album_video_count'] = len(gallery_videos)
        if gallery_videos:
            post['media_type'] = 'mixed' if post['image_records'] else 'video'
        try:
            driver.close()
        except WebDriverException:
            pass
        try:
            driver.switch_to.window(feed_handle)
        except WebDriverException:
            stops.append('restore_feed_failed')
            post['image_stop_reasons'] = stops
            if active_exception is None:
                raise
    count_matches = (len(records) == expected_count if exact_count and expected_count
                     else not expected_count or len(records) >= expected_count)
    post['images_complete'] = exhausted and count_matches
    if exact_count and expected_count and len(records) > expected_count:
        stops.append('media_count_conflict')
    full_urls = [record['url'] for record in post['image_records']]
    if len(unique_urls(full_urls)) != len(full_urls):
        post['images_complete'] = False
        stops.append('image_identity_conflict')
    post['image_urls'] = unique_urls(full_urls if post['images_complete'] else full_urls + post['image_urls'])
    post['expected_image_count'] = len(post['image_records']) if post['images_complete'] else None
    post['image_collection_status'] = ('complete' if full_urls else 'confirmed_no_photos') if post['images_complete'] else ('unverified_no_album_context' if not expected_set else 'incomplete')
    post['image_stop_reasons'] = stops
    if not post['images_complete']:
        print(f"警告：貼文 {post['index']} 相簿未抓齊，已巡覽 {len(records)} / {post['expected_media_count']}，原因 {stops}", flush=True)


def save_posts(json_path, data):
    if json_path:
        atomic_json(json_path, data)


def discover_post_containers(driver):
    """Only bounded feed posts; never infer an entire page from a media link."""
    roots = driver.find_elements(By.CSS_SELECTOR, 'div[aria-posinset], [role="feed"] [role="article"]')
    roots += [find_post_container(driver, message)
              for message in driver.find_elements(By.CSS_SELECTOR, MESSAGE_SELECTOR)]
    seen = set()
    for root in roots:
        try:
            if root is None or root.id in seen or not root.is_displayed():
                continue
            # Nested articles usually belong to comments or shared content.
            nested = driver.execute_script(
                "return !!arguments[0].parentElement.closest('div[aria-posinset], [role=feed] [role=article]');",
                root,
            )
            if nested:
                continue
            seen.add(root.id)
            yield root
        except StaleElementReferenceException:
            continue


def scroll_and_collect(driver, max_posts, keyword=None, mode='all', json_path=None, cookie_path=COOKIE_FILE):
    results = []
    seen = set()
    scanned_posts = set()
    idle = 0
    # 第一階段只保存文字/入口快照；相簿開關不再讓後面的貼文元素失效。
    while len(results) < max_posts and idle < 12:
        discovered = 0
        if not is_logged_in(driver):
            raise ValueError("抓取期間登入失效或需要驗證；已保存的資料會保留，請重新登入。")
        for container in discover_post_containers(driver):
            try:
                post = snapshot_post(driver, container)
                if not post or not (post['text'] or post['media_page_urls'] or post['video_urls']):
                    continue
                signature = post_signature(post['post_url'], post['text'], post['image_urls'] or post['media_page_urls'] or post['video_urls'])
                # 捲動進度與關鍵字命中是兩件事。不符合的貼文也代表
                # Facebook 還在載入新內容，不能因此提前停止搜尋。
                if signature not in scanned_posts:
                    scanned_posts.add(signature)
                    discovered += 1
                if keyword and keyword not in post['text']:
                    continue
                if signature in seen:
                    continue
                seen.add(signature)
                post['index'] = len(results)
                results.append(post)
                print(f"已定位 {len(results)}/{max_posts} 篇：{post['text'][:25]}；{post['media_type']}，相簿 {post['expected_media_count']} 項", flush=True)
                save_posts(json_path, results)
                if len(results) >= max_posts:
                    break
            except StaleElementReferenceException:
                # 只小步捲動，每一輪重新取得 DOM。
                continue
        idle = 0 if discovered else idle + 1
        if len(results) < max_posts:
            driver.execute_script('window.scrollBy(0, 650);')
            time.sleep(3)
    if len(results) < max_posts:
        print(f'警告：只定位 {len(results)}/{max_posts} 篇；連續 12 次捲動沒有發現新貼文。已檢查 {len(scanned_posts)} 篇，可能已到底或頁面未繼續載入。', flush=True)
    original_driver = driver
    try:
        for post in results:
            if mode in {'image', 'all'}:
                for attempt in range(3):
                    try:
                        collect_photo_snapshot(driver, post)
                        save_posts(json_path, results)
                        if post['images_complete']:
                            break
                        if post['image_collection_status'].startswith('unverified_'):
                            print(f"貼文 {post['index']} 尚無法確認是否藏有照片：{post['image_collection_status']}", flush=True)
                            break
                        driver.title  # 明確偵測 session 是否已中斷。
                    except KeyboardInterrupt:
                        save_posts(json_path, results)
                        raise
                    except WebDriverException:
                        save_posts(json_path, results)
                        try:
                            driver.quit()
                        except WebDriverException:
                            pass
                        print('Chrome 連線中斷，重新啟動後接續相簿。', flush=True)
                        driver = setup_driver()
                        load_cookies(driver, cookie_path)
                    print(f"貼文 {post['index']} 相簿重試 {attempt+1}/3", flush=True)
            else:
                post['image_urls'] = []
                post['image_collection_status'] = 'not_requested'
            if mode == 'image':
                post['text'] = ''
            save_posts(json_path, results)
    finally:
        if driver is not original_driver:
            try:
                driver.quit()
            except WebDriverException:
                pass
    return results


def positive_int(value):
    try:
        result = int(value)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError("篇數必須是正整數") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("篇數必須大於 0")
    return result


def parse_options(argv=None):
    parser = argparse.ArgumentParser(description="Facebook 貼文文字與圖片收集工具")
    parser.add_argument('--url', help="目標 Facebook 專頁網址；省略時詢問")
    parser.add_argument('--mode', choices=('text', 'image', 'all'))
    parser.add_argument('--max-posts', type=positive_int)
    parser.add_argument('--keyword', help="全文包含比對；英文區分大小寫")
    parser.add_argument('--cookies', type=Path, default=COOKIE_FILE)
    parser.add_argument('--output', type=Path, default=OUTPUT_DIR)
    parser.add_argument('--download-json', type=Path, help='使用既有 fb_posts.json 補下載圖片，不開啟瀏覽器')
    options = parser.parse_args(argv)
    if options.download_json is not None:
        return options
    interactive = options.url is None or options.mode is None or options.max_posts is None

    def valid_url(value):
        try:
            parsed = urlparse(value)
            return parsed.scheme == 'https' and parsed.hostname in {'facebook.com', 'www.facebook.com', 'm.facebook.com'}
        except ValueError:
            return False

    while options.url is None:
        value = input('請輸入 Facebook 專頁網址：').strip()
        if valid_url(value):
            options.url = value
        else:
            print('請輸入 https:// 開頭的 Facebook 網址。')
    if not valid_url(options.url):
        parser.error('--url 必須是 https:// 的 Facebook 網址')
    if options.mode is None:
        while True:
            choice = input("模式：1=文字，2=圖片，3=全部：").strip()
            if choice in {'1', '2', '3'}:
                options.mode = {'1': 'text', '2': 'image', '3': 'all'}[choice]
                break
            print("請輸入 1、2 或 3。")
    if options.max_posts is None:
        while True:
            try:
                options.max_posts = positive_int(input("最多抓幾篇貼文？"))
                break
            except argparse.ArgumentTypeError as exc:
                print(exc)
    if options.keyword is None and interactive:
        while True:
            choice = input('是否使用關鍵字篩選（y/n，直接 Enter 表示不篩選）？').strip().lower()
            if choice in {'y', 'n', ''}:
                if choice == 'y':
                    options.keyword = input('請輸入關鍵字：').strip() or None
                break
            print('請輸入 y 或 n。')
    options.keyword = options.keyword.strip() or None if options.keyword else None
    return options


def main():
    try:
        options = parse_options()
    except (EOFError, KeyboardInterrupt):
        print("已取消輸入。")
        return 130
    if options.download_json is not None:
        try:
            failed = download_images_from_json(options.download_json, options.download_json.parent / 'images')
            return 1 if failed else 0
        except KeyboardInterrupt:
            print("下載已中止；可用相同 --download-json 指令接續。")
            return 130
        except (ValueError, OSError) as exc:
            print(f"補下載失敗：{exc}")
            return 1
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    session_folder = options.output / f"session_{timestamp}"
    image_folder = session_folder / 'images'
    json_path = str(session_folder / 'fb_posts.json')
    driver = None
    try:
        session_folder.mkdir(parents=True, exist_ok=False)
        driver = setup_driver()
        load_cookies(driver, options.cookies)
        driver.get(options.url)
        WebDriverWait(driver, 15).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, MESSAGE_SELECTOR + ', div[aria-posinset], [role="feed"] [role="article"]')
        )
        print("開始收集貼文……")
        data = scroll_and_collect(driver, options.max_posts, options.keyword, options.mode, json_path,
                                  cookie_path=options.cookies)
        save_posts(json_path, data)
        print(f"共蒐集 {len(data)} 篇，資料位置：{session_folder}")
        if options.mode in {'image', 'all'}:
            download_images_from_json(json_path, str(image_folder))
            downloaded = json.loads(Path(json_path).read_text(encoding='utf-8'))
            incomplete = [p['index'] for p in downloaded if not p.get('images_complete', False)]
            failed = sum(len(p.get('failed_image_files', [])) for p in downloaded)
            print(f"完成檢查：貼文 {len(data)}/{options.max_posts} 篇，圖片未確認抓齊：{incomplete}，下載失敗 {failed} 張")

        final_posts = json.loads(Path(json_path).read_text(encoding='utf-8'))
        summary = collection_summary(final_posts, options.max_posts, options.mode)
        atomic_json(session_folder / 'summary.json', summary)
        if summary['status'] == 'partial':
            print(f"結果未全部確認：篇數 {summary['collected_posts']}/{summary['requested_posts']}，"
                  f"文字未確認 {summary['text_incomplete_indexes']}，圖片未確認 {summary['images_unverified_indexes']}，"
                  f"下載未確認 {summary['downloads_unverified_indexes']}")
        return 1 if summary['failed_image_count'] else (0 if summary['status'] == 'complete' else 3)
    except KeyboardInterrupt:
        print(f"已中止。已寫入的貼文與下載進度保留於：{session_folder}")
        return 130
    except (ValueError, OSError, WebDriverException) as exc:
        print(f"執行失敗：{exc}\n已寫入的資料保留於：{session_folder}")
        return 1
    finally:
        if driver is not None:
            try:
                driver.quit()
            except WebDriverException:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
