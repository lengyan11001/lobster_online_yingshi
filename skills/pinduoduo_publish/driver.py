"""拼多多商品发布驱动 — mms.pinduoduo.com 商家后台。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from skills._base import BaseDriver

logger = logging.getLogger(__name__)

LOGIN_URL = "https://mms.pinduoduo.com/"
PRODUCT_ADD_URL = "https://mms.pinduoduo.com/goods/goods_publish"


class PinduoduoDriver(BaseDriver):

    def login_url(self) -> str:
        return LOGIN_URL

    def product_add_url(self) -> str:
        return PRODUCT_ADD_URL

    async def check_login(self, page: Any, navigate: bool = True) -> bool:
        try:
            if navigate:
                await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(3)
            url = (page.url or "").lower()
            if "login" in url or "passport" in url:
                return False
            content = (await page.content() or "")[:3000]
            if "请登录" in content or "扫码登录" in content:
                return False
            return "pinduoduo.com" in url and "login" not in url
        except Exception:
            return False

    async def publish(
        self,
        page: Any,
        file_path: str,
        title: str,
        description: str,
        tags: str,
        options: Optional[Dict[str, Any]] = None,
        cover_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            await page.goto(PRODUCT_ADD_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            url = (page.url or "").lower()
            if "login" in url or "passport" in url:
                return {"ok": False, "error": "登录已过期，请重新登录拼多多商家后台"}

            return {
                "ok": True,
                "message": "已打开拼多多商品发布页面，请在浏览器中填写商品信息并手动发布",
                "url": page.url,
            }
        except Exception as e:
            logger.exception("[PINDUODUO] publish failed")
            return {"ok": False, "error": str(e)}

    async def open_product_form(
        self,
        page: Any,
        *,
        title: Optional[str] = None,
        price: Optional[str] = None,
        category: Optional[str] = None,
        main_image_paths: Optional[List[str]] = None,
        detail_image_paths: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        try:
            await page.goto(PRODUCT_ADD_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            url = (page.url or "").lower()
            if "login" in url or "passport" in url:
                return {"ok": False, "error": "登录已过期，请重新登录拼多多商家后台"}

            filled: List[str] = []

            if title:
                try:
                    sel = 'input[placeholder*="标题"], input[placeholder*="商品名称"], input[name*="title"]'
                    inp = page.locator(sel).first
                    if await inp.count() > 0:
                        await inp.click()
                        await inp.fill(title)
                        filled.append("标题")
                        logger.info("[PINDUODUO] filled title: %s", title[:50])
                except Exception as e:
                    logger.warning("[PINDUODUO] fill title failed: %s", e)

            if main_image_paths:
                try:
                    upload = page.locator('input[type="file"]').first
                    if await upload.count() > 0:
                        await upload.set_input_files(main_image_paths)
                        filled.append(f"主图({len(main_image_paths)}张)")
                        logger.info("[PINDUODUO] uploaded %d main images", len(main_image_paths))
                        await asyncio.sleep(2)
                except Exception as e:
                    logger.warning("[PINDUODUO] upload main images failed: %s", e)

            msg_parts = ["已打开拼多多商品创建页面"]
            if filled:
                msg_parts.append(f"已自动填充: {', '.join(filled)}")
            msg_parts.append("请检查并补充其余信息后手动发布")

            return {"ok": True, "message": "，".join(msg_parts), "url": page.url, "auto_filled": filled}
        except Exception as e:
            logger.exception("[PINDUODUO] open_product_form failed")
            return {"ok": False, "error": str(e)}
