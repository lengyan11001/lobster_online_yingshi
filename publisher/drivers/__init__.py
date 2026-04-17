# 发布驱动从独立 skill 加载，便于单独维护与开关
from skills.douyin_publish import DouyinDriver
from skills.toutiao_publish import ToutiaoDriver
from skills.xiaohongshu_publish import XiaohongshuDriver
from skills.douyin_shop_publish import DouyinShopDriver
from skills.xiaohongshu_shop_publish import XiaohongshuShopDriver
from skills.alibaba1688_publish import Alibaba1688Driver
from skills.taobao_publish import TaobaoDriver
from skills.pinduoduo_publish import PinduoduoDriver

DRIVERS = {
    "douyin": DouyinDriver,
    "xiaohongshu": XiaohongshuDriver,
    "toutiao": ToutiaoDriver,
    "douyin_shop": DouyinShopDriver,
    "xiaohongshu_shop": XiaohongshuShopDriver,
    "alibaba1688": Alibaba1688Driver,
    "taobao": TaobaoDriver,
    "pinduoduo": PinduoduoDriver,
}
