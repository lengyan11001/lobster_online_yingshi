"""发布驱动抽象基类，供各平台 skill 实现。"""
from __future__ import annotations

import abc
from typing import Any, Dict, List, Optional


class BaseDriver(abc.ABC):
    """Every platform driver must implement these methods."""

    @abc.abstractmethod
    def login_url(self) -> str:
        """Return the platform's creator/login page URL."""

    def product_add_url(self) -> str:
        """Return the platform's product creation page URL (ecommerce only)."""
        return self.login_url()

    @abc.abstractmethod
    async def check_login(self, page: Any) -> bool:
        """Return True if the current browser page has a valid logged-in session."""

    @abc.abstractmethod
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
        """Upload and publish content. Returns {"ok": bool, "url": str, "error": str}."""

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
        """Navigate to the product creation page and auto-fill available fields.

        Subclasses should override to implement platform-specific auto-fill.
        Default: navigate to product_add_url only.
        """
        url = self.product_add_url()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            return {"ok": False, "error": f"无法打开商品创建页: {e}"}
        return {
            "ok": True,
            "message": "已打开商品创建页面，请手动填写并发布",
            "url": page.url,
        }
