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
                        await self._wait_and_dismiss_popups(page, max_attempts=3)
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

    async def _dismiss_ai_tool_popup(self, page: Any) -> bool:
        """检测并关闭抖店上传图片后弹出的 AI 素材工具/图片编辑弹窗。

        Returns True if a popup was detected and dismissed.
        """
        dismiss_selectors = [
            'button:has-text("跳过")',
            'button:has-text("关闭")',
            'button:has-text("取消")',
            'button:has-text("不使用")',
            'button:has-text("暂不使用")',
            'button:has-text("退出")',
            '[class*="modal"] [class*="close"]',
            '[class*="dialog"] [class*="close"]',
            '[class*="drawer"] [class*="close"]',
            '[class*="Modal"] button[class*="close"]',
            '[class*="ai-tool"] button[class*="close"]',
            '[aria-label="关闭"]',
            '[aria-label="Close"]',
        ]
        for sel in dismiss_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    text = (await loc.text_content() or "").strip()[:30]
                    await loc.click()
                    logger.info("[DOUYIN-SHOP] dismissed AI tool popup via: %s (text=%s)", sel, text)
                    await asyncio.sleep(1)
                    return True
            except Exception:
                continue
        return False

    async def _wait_and_dismiss_popups(self, page: Any, max_attempts: int = 5) -> None:
        """上传图片后反复检测并关闭弹窗，直到没有更多弹窗。"""
        for attempt in range(max_attempts):
            await asyncio.sleep(2)
            dismissed = await self._dismiss_ai_tool_popup(page)
            if not dismissed:
                try:
                    snapshot = await page.evaluate("""() => {
                        const modals = document.querySelectorAll('[class*="modal"],[class*="Modal"],[class*="dialog"],[class*="Dialog"],[class*="drawer"],[class*="Drawer"],[class*="popup"],[class*="Popup"]');
                        return Array.from(modals).slice(0, 5).map(el => ({
                            tag: el.tagName,
                            cls: el.className.toString().slice(0, 100),
                            visible: el.offsetHeight > 0,
                            text: el.innerText?.slice(0, 100) || ''
                        }));
                    }""")
                    if snapshot:
                        logger.info("[DOUYIN-SHOP] popup probe attempt %d: %s", attempt + 1, snapshot)
                except Exception as e:
                    logger.debug("[DOUYIN-SHOP] popup probe error: %s", e)
                break
            logger.info("[DOUYIN-SHOP] popup dismissed on attempt %d, checking for more...", attempt + 1)

    async def _upload_detail_images(self, page: Any, paths: List[str]) -> None:
        """在抖店商品编辑页的「商品详情」富文本区域逐张上传图片。"""
        await page.evaluate("window.scrollBy(0, 600)")
        await asyncio.sleep(1)

        detail_selectors = [
            'div[class*="detail"] input[type="file"]',
            'div[class*="description"] input[type="file"]',
            'div[class*="richtext"] input[type="file"]',
            '.detail-editor input[type="file"]',
        ]

        target_sel = None
        for sel in detail_selectors:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                target_sel = sel
                break

        if not target_sel:
            all_uploads = page.locator('input[type="file"]')
            count = await all_uploads.count()
            if count >= 2:
                target_sel = f'input[type="file"] >> nth={count - 1}'
            elif count == 1:
                logger.warning("[DOUYIN-SHOP] only 1 file input, detail upload may overlap main images")
                target_sel = 'input[type="file"]'
            else:
                logger.warning("[DOUYIN-SHOP] no file input found for detail images")
                return

        loc = page.locator(target_sel).first
        is_multiple = await loc.evaluate("el => el.multiple") if await loc.count() > 0 else False

        if is_multiple:
            await loc.set_input_files(paths)
            await asyncio.sleep(3)
            logger.info("[DOUYIN-SHOP] detail images: batch uploaded %d files", len(paths))
        else:
            uploaded_count = 0
            for i, fp in enumerate(paths):
                try:
                    loc = page.locator(target_sel).first
                    if await loc.count() == 0:
                        logger.warning("[DOUYIN-SHOP] detail file input disappeared at index %d", i)
                        break
                    await loc.set_input_files(fp)
                    uploaded_count += 1
                    await asyncio.sleep(1.5)
                    await self._dismiss_ai_tool_popup(page)
                except Exception as e:
                    logger.warning("[DOUYIN-SHOP] detail image %d/%d failed: %s", i + 1, len(paths), e)
            logger.info("[DOUYIN-SHOP] detail images: uploaded %d/%d files one by one", uploaded_count, len(paths))

        await self._wait_and_dismiss_popups(page)
