"""抖店商品发布驱动 — fxg.jinritemai.com 商家后台。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from skills._base import BaseDriver

logger = logging.getLogger(__name__)

LOGIN_URL = "https://fxg.jinritemai.com/"
PRODUCT_ADD_URL = "https://fxg.jinritemai.com/ffa/g/create"


class DouyinShopDriver(BaseDriver):
    """抖店商品发布：登录检测 + 商品创建页自动填充。"""

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
            if "passport" in url or "login" in url:
                return False
            content = (await page.content() or "")[:3000]
            if "扫码登录" in content or "账号登录" in content:
                return False
            return "jinritemai.com" in url
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
        options = options or {}
        try:
            await page.goto(PRODUCT_ADD_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            url = (page.url or "").lower()
            if "passport" in url or "login" in url:
                return {"ok": False, "error": "登录已过期，请重新登录抖店"}

            return {
                "ok": True,
                "message": "已打开抖店商品创建页面，请在浏览器中填写商品信息并手动发布",
                "url": page.url,
            }
        except Exception as e:
            logger.exception("[DOUYIN-SHOP] publish failed")
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
            if "passport" in url or "login" in url:
                return {"ok": False, "error": "登录已过期，请重新登录抖店"}

            filled: List[str] = []

            if title:
                try:
                    sel = 'input[placeholder*="商品标题"], input[placeholder*="标题"], input[name*="title"]'
                    inp = page.locator(sel).first
                    if await inp.count() > 0:
                        await inp.click()
                        await inp.fill(title)
                        filled.append("标题")
                        logger.info("[DOUYIN-SHOP] filled title: %s", title[:50])
                except Exception as e:
                    logger.warning("[DOUYIN-SHOP] fill title failed: %s", e)

            if main_image_paths:
                try:
                    upload = page.locator('input[type="file"]').first
                    if await upload.count() > 0:
                        await upload.set_input_files(main_image_paths)
                        filled.append(f"主图({len(main_image_paths)}张)")
                        logger.info("[DOUYIN-SHOP] uploaded %d main images", len(main_image_paths))
                        await asyncio.sleep(2)
                except Exception as e:
                    logger.warning("[DOUYIN-SHOP] upload main images failed: %s", e)

            if detail_image_paths:
                try:
                    await self._upload_detail_images(page, detail_image_paths)
                    filled.append(f"详情图({len(detail_image_paths)}张)")
                    logger.info("[DOUYIN-SHOP] uploaded %d detail images", len(detail_image_paths))
                except Exception as e:
                    logger.warning("[DOUYIN-SHOP] upload detail images failed: %s", e)

            msg_parts = ["已打开抖店商品创建页面"]
            if filled:
                msg_parts.append(f"已自动填充: {', '.join(filled)}")
            msg_parts.append("请检查并补充其余信息后手动发布")

            return {"ok": True, "message": "，".join(msg_parts), "url": page.url, "auto_filled": filled}
        except Exception as e:
            logger.exception("[DOUYIN-SHOP] open_product_form failed")
            return {"ok": False, "error": str(e)}

    async def _upload_detail_images(self, page: Any, paths: List[str]) -> None:
        """在抖店商品编辑页的「商品详情」富文本区域上传图片。

        抖店的详情编辑器一般有一个「图片」按钮，点击后弹出文件选择。
        如果找不到精确选择器，尝试滚动到「商品详情」区域后查找 file input。
        """
        detail_selectors = [
            'div[class*="detail"] input[type="file"]',
            'div[class*="description"] input[type="file"]',
            'div[class*="richtext"] input[type="file"]',
            '.detail-editor input[type="file"]',
        ]
        uploaded = False
        for sel in detail_selectors:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                await loc.set_input_files(paths)
                uploaded = True
                await asyncio.sleep(3)
                break

        if not uploaded:
            all_uploads = page.locator('input[type="file"]')
            count = await all_uploads.count()
            if count >= 2:
                await all_uploads.nth(count - 1).set_input_files(paths)
                await asyncio.sleep(3)
            elif count == 1:
                logger.warning("[DOUYIN-SHOP] only 1 file input found, detail upload may conflict with main images")
                await all_uploads.first.set_input_files(paths)
                await asyncio.sleep(3)
            else:
                logger.warning("[DOUYIN-SHOP] no file input found for detail images")
