"""Shared paths, login validation, and cookie loading."""
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlparse

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

BASE_DIR = Path(__file__).resolve().parent
COOKIE_FILE = BASE_DIR / "fb_cookies.json"
OUTPUT_DIR = BASE_DIR / "fb_output"
FB_URL = "https://www.facebook.com"
FACEBOOK_HOSTS = {'facebook.com', 'www.facebook.com', 'm.facebook.com'}


def atomic_json(path, data):
    """Keep the previous complete file until the replacement is ready."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as file:
            temporary = Path(file.name)
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def is_logged_in(driver):
    url = driver.current_url.lower()
    cookie = driver.get_cookie("c_user")
    return (
        urlparse(url).hostname in FACEBOOK_HOSTS
        and bool(cookie and cookie.get('value'))
        and not any(part in url for part in ("/login", "/checkpoint", "/two_step_verification"))
        and not any(element.is_displayed() for element in driver.find_elements(By.NAME, "email"))
    )


def load_cookies(driver, path=COOKIE_FILE):
    path = Path(path)
    try:
        cookies = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"找不到 Cookie：{path}。請先執行 fb_login.py 登入。") from exc
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("Cookie 檔案格式錯誤，請重新執行 fb_login.py。") from exc
    if not isinstance(cookies, list) or not cookies:
        raise ValueError("Cookie 必須是非空陣列，請重新登入。")
    validated = []
    for original in cookies:
        if not isinstance(original, dict) or not {"name", "value"} <= original.keys():
            raise ValueError("Cookie 欄位不完整，請重新登入。")
        if (not isinstance(original['name'], str) or not original['name']
                or not isinstance(original['value'], str)):
            raise ValueError("Cookie 名稱與值必須是字串。")
        domain = original.get('domain', 'facebook.com')
        if not isinstance(domain, str) or not (
            domain.lstrip('.').lower() == 'facebook.com'
            or domain.lstrip('.').lower().endswith('.facebook.com')
        ):
            raise ValueError("Cookie 網域必須屬於 Facebook。")
        cookie = {key: value for key, value in original.items() if key in {
            "name", "value", "domain", "path", "secure", "httpOnly", "expiry", "sameSite"
        }}
        validated.append(cookie)
    driver.get(FB_URL)
    rejected = 0
    for cookie in validated:
        try:
            driver.add_cookie(cookie)
        except WebDriverException:
            rejected += 1
    if rejected:
        print(f"警告：{rejected} 個 Cookie 無法載入。")
    driver.get(FB_URL)
    try:
        WebDriverWait(driver, 15).until(is_logged_in)
    except WebDriverException as exc:
        raise ValueError("無法確認登入成功，Cookie 可能過期或帳號需要驗證。請執行 fb_login.py。") from exc
