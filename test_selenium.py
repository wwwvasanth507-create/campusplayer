"""
Test: can Selenium 4 launch Chrome using its built-in selenium-manager?
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from selenium import webdriver
from selenium.webdriver.common.by import By
import time

print("Creating Chrome driver using Selenium 4 built-in manager...")

opts = webdriver.ChromeOptions()
opts.add_argument('--no-sandbox')
opts.add_argument('--disable-dev-shm-usage')
opts.add_experimental_option('excludeSwitches', ['enable-logging'])

try:
    # Selenium 4.6+ auto-manages chromedriver via selenium-manager
    driver = webdriver.Chrome(options=opts)
    print("Chrome launched successfully!")
    driver.get("https://www.google.com")
    print("Page title:", driver.title)
    time.sleep(2)
    driver.quit()
    print("PASS: Selenium + Chrome working correctly")
except Exception as e:
    print(f"FAIL: {e}")
