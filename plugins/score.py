from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from selenium import webdriver
from selenium.common.exceptions import SessionNotCreatedException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from shenbot_api import ConfigStorage, PluginManifest, python_config_path


if TYPE_CHECKING:
    from ica_typing import IcaClient, IcaNewMessage
else:
    IcaClient = TypeVar("IcaClient")
    IcaNewMessage = TypeVar("IcaNewMessage")


USERNAME = ""
PASSWORD = ""

COMMAND = "/score"
HOME_URL = "https://jwxt.bistu.edu.cn/jwapp/sys/homeapp/home/index.html?av=&contextPath=/jwapp#/"
LOGIN_URL = (
    "https://wxjw.bistu.edu.cn/authserver/login?service="
    "https%3A%2F%2Fjwxt.bistu.edu.cn%2Fjwapp%2Fsys%2Fhomeapp%2Findex.do%3FcontextPath%3D%2Fjwapp"
)
SCORE_API = "https://jwxt.bistu.edu.cn/jwapp/sys/cjzhcxapp/modules/wdcj/cxwdcj.do"

config = ConfigStorage(username="", password="")

PLUGIN_MANIFEST = PluginManifest(
    plugin_id="score",
    name="成绩查询",
    version="0.3.1",
    description="查询教务系统最近一个学期的成绩",
    authors=["shenjack"],
    config={"main": config},
)

_query_lock = threading.Lock()


def query_scores() -> str:
    """登录教务系统，并通过成绩 API 获取最近学期的数据。"""
    if not USERNAME or not PASSWORD:
        raise RuntimeError("请在 score 插件配置中填写 username 和 password")

    profile_dir = (Path(python_config_path()) / "score-edge-profile").resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    options = webdriver.EdgeOptions()
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.page_load_strategy = "eager"
    try:
        driver = webdriver.Edge(options=options)
    except SessionNotCreatedException:
        time.sleep(2)
        driver = webdriver.Edge(options=options)
    driver.set_page_load_timeout(30)

    try:
        driver.get(LOGIN_URL)
        if not driver.current_url.startswith("https://jwxt.bistu.edu.cn/jwapp/"):
            try:
                username_input = WebDriverWait(driver, 30).until(
                    EC.presence_of_element_located((By.NAME, "username"))
                )
            except TimeoutException as error:
                raise RuntimeError("统一认证登录页加载超时") from error

            username_input.send_keys(USERNAME)
            driver.find_element(By.NAME, "passwordText").send_keys(PASSWORD)
            driver.find_element(By.ID, "login_submit").click()

            try:
                WebDriverWait(driver, 180).until(
                    lambda d: d.current_url.startswith(
                        "https://jwxt.bistu.edu.cn/jwapp/"
                    )
                )
            except TimeoutException as error:
                raise RuntimeError("登录超时，请在 Edge 中完成安全验证") from error

        cookie = "; ".join(
            f"{item['name']}={item['value']}" for item in driver.get_cookies()
        )
        user_agent = driver.execute_script("return navigator.userAgent")
    finally:
        driver.quit()

    request = urllib.request.Request(
        SCORE_API,
        data=b"",
        method="POST",
        headers={
            "Cookie": cookie,
            "User-Agent": user_agent,
            "Referer": HOME_URL,
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))

    scores = data["datas"]["cxwdcj"]["rows"]
    latest_term = max(item["XNXQDM"] for item in scores)
    latest_scores = [item for item in scores if item["XNXQDM"] == latest_term]

    lines = [f"📊 {latest_scores[0]['XNXQDM_DISPLAY']}成绩"]
    for item in latest_scores:
        lines.append(
            f"{item['KCM']}：{item['XSZCJ']}"
            f"（{item['XF']}学分，绩点{item['JD']}）"
        )
    return "\n".join(lines)


def on_load() -> None:
    global USERNAME, PASSWORD
    main_config = PLUGIN_MANIFEST.config_unchecked("main")
    USERNAME = str(main_config.get_value("username") or "")
    PASSWORD = str(main_config.get_value("password") or "")


def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    if msg.is_from_self or (msg.content or "").strip() != COMMAND:
        return
    if msg.sender_id not in client.status.admins:
        return

    if _query_lock.locked():
        client.send_message(msg.reply_with("成绩正在查询，请稍候。"))
        return

    client.send_message(msg.reply_with("正在查询成绩；如 Edge 出现安全验证，请手动完成。"))

    def run_query() -> None:
        with _query_lock:
            try:
                result = query_scores()
            except Exception as error:
                message = str(error).strip() or type(error).__name__
                result = f"成绩查询失败：{message}"
            client.send_message(msg.reply_with(result))

    threading.Thread(target=run_query, daemon=True).start()
