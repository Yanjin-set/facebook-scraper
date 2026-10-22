from selenium import webdriver
from selenium.webdriver.chrome.options import Options
import argparse
from pathlib import Path
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.support.ui import WebDriverWait

from fb_runtime import COOKIE_FILE, FB_URL, atomic_json, is_logged_in, load_cookies

def setup_driver(headless=False):
    options = Options()
    if headless:
        options.add_argument("--headless")
        options.add_argument("--disable-gpu")
    options.add_argument("--lang=zh-TW")
    driver = webdriver.Chrome(options=options)
    try:
        driver.set_page_load_timeout(45)
        return driver
    except BaseException:
        try:
            driver.quit()
        except WebDriverException:
            pass
        raise

def save_cookies(driver, path=COOKIE_FILE):
    atomic_json(path, driver.get_cookies())

def login_and_save_cookie(path=COOKIE_FILE, timeout=300):
    driver = setup_driver()
    try:
        driver.get(FB_URL)
        print("請在瀏覽器登入 Facebook。完成後回到此視窗按 Enter；登入前瀏覽器不會自動關閉。")
        while True:
            input("登入完成後請按 Enter 繼續...")
            if is_logged_in(driver):
                save_cookies(driver, path)
                print("已儲存登入 Cookie。")
                return
            print("尚未確認登入成功，請完成登入後再按 Enter。")
    except EOFError:
        print("無法讀取終端機輸入，將等待瀏覽器完成登入。")
        WebDriverWait(driver, timeout).until(is_logged_in)
        save_cookies(driver, path)
        print("已儲存登入 Cookie。")
    finally:
        try:
            driver.quit()
        except WebDriverException:
            pass

def auto_login_with_cookie():
    driver = setup_driver()
    try:
        load_cookies(driver)
        print("已確認 Cookie 登入成功")
        return driver
    except BaseException:
        try:
            driver.quit()
        except WebDriverException:
            pass
        raise


def main():
    parser = argparse.ArgumentParser(description='在獨立 Chrome 視窗手動登入並儲存 Cookie')
    parser.add_argument('--cookies', type=Path, default=COOKIE_FILE)
    parser.add_argument('--timeout', type=int, default=300, help='無終端輸入時等待登入秒數，預設 300')
    options = parser.parse_args()
    if options.timeout <= 0:
        parser.error('--timeout 必須大於 0')
    print("Facebook 自動登入系統")
    try:
        login_and_save_cookie(options.cookies, options.timeout)
        return 0
    except KeyboardInterrupt:
        print('登入已取消。')
        return 130
    except (ValueError, OSError, WebDriverException) as exc:
        print(f'登入未完成：{type(exc).__name__}。請確認瀏覽器、網路或重新登入。')
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
