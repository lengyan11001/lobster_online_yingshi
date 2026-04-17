#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用无头浏览器打开在线客户端，拦截 /api/edition 返回仅微信登录，验证登录页只显示微信扫码、不显示邮箱表单与注册。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        # 拦截 /api/edition，强制返回：自建认证 + 仅微信登录
        def handle_edition(route):
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"edition":"online","use_independent_auth":true,"use_own_wechat_login":true,"use_fubei_pay":true}',
            )

        page.route("**/api/edition**", handle_edition)
        # 可选：拦截 wechat-login-url 避免真实请求
        page.route("**/auth/wechat-login-url**", lambda r: r.fulfill(status=200, content_type="application/json", body='{"login_url":"https://open.weixin.qq.com/connect/qrconnect?appid=test"}'))

        page.goto("http://127.0.0.1:8000/", wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(1500)

        login_form = page.query_selector("#loginForm")
        register_block = page.query_selector("#registerBlock")
        own_wechat = page.query_selector("#ownWechatLoginBlock")

        form_visible = login_form.evaluate("el => el.style.display !== 'none'") if login_form else False
        register_visible = register_block.evaluate("el => el.style.display !== 'none'") if register_block else False
        wechat_visible = own_wechat.evaluate("el => el.style.display !== 'none'") if own_wechat else False

        browser.close()

    print("loginForm visible:", form_visible)
    print("registerBlock visible:", register_visible)
    print("ownWechatLoginBlock visible:", wechat_visible)
    if wechat_visible and not form_visible and not register_visible:
        print("PASS: 仅微信扫码登录展示，无邮箱/注册")
        return 0
    print("FAIL: 应只显示微信扫码，隐藏邮箱与注册")
    return 1


if __name__ == "__main__":
    sys.exit(main())
