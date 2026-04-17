# 电商视觉素材自动化生成 Skill 设计方案

## 1. 目标

基于产品透明图、多角度图、结构化卖点与规格数据，自动生成一整套电商上架视觉素材，覆盖：

- 主图
- SKU 图
- 透明图
- 白底图
- 详情图
- 素材图
- 橱窗图

目标不是“单张图片生成”，而是“同一 SKU 的整包视觉资产自动生产”，强调：

- 产品一致性
- 场景一致性
- 风格一致性
- 文案一致性
- 命名与交付一致性


## 2. 需求来源

来源文件：

- `D:\下载\电商视觉素材自动化生成 Skills 需求规格表.xlsx`

工作表：

1. `整体需求概览`
2. `详细输出图片需求表`
3. `系统输入与技术要求`
4. `风格细化参考-供AI训练 调参使用`


## 3. 产品定义

### 3.1 系统定位

这是一个“电商视觉素材整包生产 Skill”，不是单次文生图 Skill。

它需要把输入的产品素材与结构化信息，转成多个用途不同、尺寸不同、风格统一、平台可上传的视觉资产包。

### 3.2 适用品类

当前需求明确聚焦：

- 人宠共居小家具

但整体架构应支持未来扩展到：

- 宠物家具
- 收纳类家具
- 家居小件
- 特定服饰或箱包类 SKU


## 4. 输入模型

### 4.1 必填输入

- `sku`
- `product_images`
  - 至少 1 张正面透明 PNG
  - 建议包含侧面、背面、细节图
- `selling_points`
  - 卖点标题
  - 卖点描述
- `specs`
  - 尺寸
  - 材质
  - 重量
  - 参数项
- `style`
  - `memphis_vintage`
  - `creamy_wood`
  - `french_creamy`

### 4.2 可选输入

- `scene_preferences`
  - 是否包含宠物
  - 是否包含人物模特
  - 装饰元素偏好
- `reference_images`
  - 风格参考图
  - 构图参考图
- `icon_assets`
  - 详情页和橱窗图图标
- `template_id`
  - 详情图模板
  - 橱窗图模板

### 4.3 建议统一输入对象

```json
{
  "sku": "SKU12345",
  "product_images": [
    {
      "role": "front",
      "url": "..."
    },
    {
      "role": "side",
      "url": "..."
    }
  ],
  "selling_points": [
    {
      "title": "防带砂",
      "description": "走廊式结构减少猫砂外带",
      "icon": "anti_litter"
    }
  ],
  "specs": {
    "size": "80x45x60cm",
    "material": "实木+板材"
  },
  "style": "creamy_wood",
  "scene_preferences": {
    "include_pet": true,
    "pet_type": "cat",
    "include_human": false
  }
}
```


## 5. 输出模型

### 5.1 目标产物

- 主图：5 张
- SKU 图：3 至 5 张
- 透明图：1 张
- 白底图：1 张
- 详情图：多张长图切片
- 素材图：3 张多尺寸版本
- 橱窗图：4 至 6 张

### 5.2 输出目录结构

建议：

```text
{SKU}/
  manifest.json
  preview.html
  main/
  sku/
  detail/
  showcase/
  material/
  transparent/
  white_bg/
```

### 5.3 文件命名

格式：

```text
{SKU}_{类型}_{序号}_{宽}x{高}.{格式}
```

示例：

- `SKU12345_主图_01_1440x1440.jpg`
- `SKU12345_橱窗卖点_03_1440x1920.jpg`
- `SKU12345_详情图_02_790x1380.jpg`


## 6. 风格系统

### 6.1 风格不是简单 prompt 枚举

风格必须进入独立配置层，至少影响：

- 色彩基调
- 材质选择
- 背景家具与软装
- 光照方向与色温
- 文案视觉语气
- 版式和装饰元素

### 6.2 建议风格配置对象

```json
{
  "style_id": "creamy_wood",
  "palette": ["原木", "奶白", "米杏", "低饱和蓝"],
  "materials": ["橡木", "白蜡木", "哑光奶白门板"],
  "lighting": {
    "type": "soft_natural",
    "temperature": "warm"
  },
  "scene_objects": ["绿植", "中古灯具", "地毯", "餐边柜"],
  "negative_rules": ["禁止高饱和刺眼配色", "禁止赛博感", "禁止过度镜面反射"]
}
```

### 6.3 当前风格来源

Excel 已给出三套风格的：

- 色彩逻辑
- 材质元素
- 光影特点
- 中英文关键词

后续应沉淀为：

- `style_presets/*.json`
- 或内置常量配置


## 7. 核心能力拆分

### 7.1 阶段 1：输入解析与标准化

职责：

- 校验图片数量与角色
- 校验卖点字段完整性
- 校验风格参数
- 建立 SKU 任务上下文

输出：

- `normalized_input.json`

### 7.2 阶段 2：产品理解

职责：

- 识别产品类别
- 识别结构特征
- 识别可展示角度
- 识别卖点和参数映射关系

输出：

- `product_analysis.json`

### 7.3 阶段 3：镜头规划

职责：

- 规划主图镜头
- 规划 SKU 图角度
- 规划橱窗图卖点分配
- 规划详情图每屏主题

输出：

- `shot_plan.json`

### 7.4 阶段 4：主图场景生成

职责：

- 生成主图 5 张
- 同时处理方图与竖图比例
- 锁定“主图第 1 张”为后续衍生锚点

输出：

- `main_images/*`

### 7.5 阶段 5：SKU 图生成

职责：

- 基于规划角度与功能点生成 SKU 图
- 保证透视准确
- 保持与主图一致的风格与光照

输出：

- `sku_images/*`

### 7.6 阶段 6：透明图与白底图生成

职责：

- 从主图第 1 张或输入透明图生成统一角度透明图
- 输出淘宝规范白底图

输出：

- `transparent/*`
- `white_bg/*`

### 7.7 阶段 7：素材图衍生

职责：

- 基于主图第 1 张同场景生成多尺寸素材图
- 保证主体不被过度裁切

输出：

- `material/*`

### 7.8 阶段 8：详情图排版

职责：

- 将卖点、规格、图标映射为多屏长图
- 自动换行、版式适配、图文排布
- 自动切分宽度 790 的长图切片

输出：

- `detail/*`

### 7.9 阶段 9：橱窗图生成

职责：

- 每个卖点一张卡片
- 标题、局部场景、图标、装饰元素组合
- 适配竖版卡片流量场景

输出：

- `showcase/*`

### 7.10 阶段 10：交付与预览

职责：

- 写入 `manifest.json`
- 生成 `preview.html`
- 输出压缩包或云存储链接


## 8. 系统架构建议

### 8.1 对外能力形态

建议作为单一主 capability：

- `ecommerce.visual_asset_pipeline`

支持动作：

- `run_pipeline`
- `start_pipeline`
- `poll_pipeline`

### 8.2 推荐能力输入契约

```json
{
  "capability_id": "ecommerce.visual_asset_pipeline",
  "payload": {
    "action": "start_pipeline",
    "sku": "SKU12345",
    "product_images": [],
    "selling_points": [],
    "specs": {},
    "style": "creamy_wood",
    "scene_preferences": {},
    "template_id": "detail_template_01"
  }
}
```

### 8.3 推荐输出契约

```json
{
  "ok": true,
  "job_id": "xxxx",
  "status": "completed",
  "result": {
    "manifest_path": "...",
    "preview_html": "...",
    "assets": {
      "main_images": [],
      "sku_images": [],
      "detail_images": [],
      "showcase_images": [],
      "transparent_image": {},
      "white_bg_image": {},
      "material_images": []
    }
  }
}
```


## 9. OpenClaw / MCP 接入建议

为了不改 OpenClaw 代码，Skill 应遵循当前项目已有模式：

- OpenClaw 只调用 `lobster` MCP
- MCP 只暴露 `invoke_capability`
- Skill 通过稳定的 `capability_id` 注册
- 本地后端执行具体流水线

也就是说：

```text
OpenClaw
 -> lobster MCP
 -> invoke_capability
 -> 本地后端 API
 -> Skill runner
 -> 输出结果
```

这样后续新增类似视觉 Skill 时，不需要修改 OpenClaw 本身。


## 10. 技术难点

### 10.1 一致性问题

最难的不是单图，而是整包一致性：

- 主图和 SKU 图的产品造型不能漂
- 详情图中产品角度和前序图要能对应
- 同一 SKU 的场景色温和风格不能散

### 10.2 主图第 1 张锚点问题

需求中多处依赖“主图第 1 张”：

- 透明图要与其角度一致
- 白底图要与其角度一致
- 素材图要与其场景一致

因此主图第 1 张必须成为全流程锚点图。

### 10.3 详情图是排版系统，不只是生成系统

详情图能力重点在：

- 文案分层
- 模板填充
- 自动裁图
- 自动切屏
- 字数溢出控制

这部分更像“图文渲染引擎”，不能只依赖文生图。

### 10.4 精确抠图要求高

透明图要求边缘误差 <= 2 像素，这意味着：

- 不能只靠粗糙 AI 抠图
- 需要高质量 matting 能力
- 需要边缘清理策略


## 11. 推荐实现路线

### 11.1 MVP 范围

第一阶段建议先做：

- 主图
- SKU 图
- 透明图
- 白底图
- 简化版详情图

先不做：

- 完整预览页
- 复杂橱窗图模板系统
- 多 SKU 并发交付优化

### 11.2 第二阶段

- 橱窗图完整实现
- 多模板详情图
- 多风格精调
- 批量任务队列
- 交付压缩包与 HTML 预览

### 11.3 第三阶段

- 质量评分与自动重生图
- 角色宠物/人物可控注入
- 细粒度构图控制
- 更高精度白底与透明图修正


## 12. 配置建议

建议将该 Skill 设计为“配置驱动”：

- 风格配置
- 尺寸配置
- 模板配置
- 卖点映射配置
- 输出类型开关

例如：

```yaml
style: creamy_wood
output_targets:
  main_images: true
  sku_images: true
  transparent_image: true
  white_bg_image: true
  detail_pages: true
  showcase_images: true
detail_template: template_a
showcase_template: template_b
```


## 13. 结论

这不是一个单点生图功能，而是一个完整的“电商视觉资产生产流水线”。

后续工程实现时必须优先确保：

- 输入协议稳定
- 主图第 1 张锚点稳定
- 风格配置独立
- 流程按阶段拆分
- 详情图和橱窗图走模板化渲染
- OpenClaw 只通过 MCP 能力调用，不直接耦合 Skill 内部实现

只有按流水线思路实现，才能真正满足这份需求表中的“整包输出、一致交付、可批量处理”的目标。


## 14. 接口协议

本节定义对外稳定契约，目标是：

- 让 OpenClaw 只依赖 `invoke_capability`
- 让 MCP 层只关心 `capability_id + payload`
- 让后端实现可替换，但外部协议不变

### 14.1 capability_id

建议正式能力 ID：

- `ecommerce.visual_asset_pipeline`

如果考虑和现有项目命名风格保持一致，也可采用：

- `comfly.ecommerce.visual_asset_pipeline`

本文以下统一使用：

- `ecommerce.visual_asset_pipeline`

### 14.2 action 设计

统一支持三种动作：

- `run_pipeline`
- `start_pipeline`
- `poll_pipeline`

含义：

- `run_pipeline`
  - 同步执行
  - 适合调试和小批量
- `start_pipeline`
  - 异步执行
  - 返回 `job_id`
  - 推荐作为正式调用方式
- `poll_pipeline`
  - 查询任务状态
  - 用于 OpenClaw / 前端轮询

### 14.3 invoke_capability 请求形态

统一请求：

```json
{
  "capability_id": "ecommerce.visual_asset_pipeline",
  "payload": {
    "action": "start_pipeline",
    "sku": "SKU12345",
    "product_images": [
      {
        "role": "front",
        "asset_id": "..."
      }
    ],
    "selling_points": [
      {
        "title": "防带砂",
        "description": "走廊式结构减少猫砂外带",
        "icon": "anti_litter"
      }
    ],
    "specs": {
      "material": "实木+板材",
      "size": "80x45x60cm"
    },
    "style": "creamy_wood",
    "scene_preferences": {
      "include_pet": true,
      "pet_type": "cat",
      "include_human": false
    },
    "output_targets": {
      "main_images": true,
      "sku_images": true,
      "transparent_image": true,
      "white_bg_image": true,
      "detail_pages": true,
      "material_images": true,
      "showcase_images": true
    },
    "detail_template_id": "detail_template_01",
    "showcase_template_id": "showcase_template_01",
    "auto_save": true
  }
}
```

### 14.4 顶层字段设计

推荐字段如下。

#### 通用字段

- `action`
- `job_id`
- `sku`
- `auto_save`
- `output_dir`
- `isolate_job_dir`

#### 产品输入字段

- `product_images`
- `reference_images`
- `product_name_hint`
- `product_direction_hint`

#### 内容输入字段

- `selling_points`
- `specs`
- `brand`
- `compliance_notes`

#### 风格与场景字段

- `style`
- `style_reference_images`
- `scene_preferences`

#### 模板与输出字段

- `output_targets`
- `detail_template_id`
- `showcase_template_id`
- `angle_plan`

### 14.5 字段详细约定

#### `product_images`

类型：

```json
[
  {
    "role": "front | side | back | detail",
    "asset_id": "optional",
    "image_url": "optional"
  }
]
```

规则：

- 至少 1 张 `front`
- `asset_id` 与 `image_url` 二选一
- 后端归一化后转成统一公网 URL

#### `selling_points`

类型：

```json
[
  {
    "title": "防带砂",
    "description": "走廊式结构减少猫砂外带",
    "icon": "anti_litter",
    "priority": 1
  }
]
```

规则：

- 用于详情图和橱窗图核心文案来源
- `title` 用于标题
- `description` 用于长文案
- `icon` 可选

#### `specs`

类型：

```json
{
  "size": "80x45x60cm",
  "material": "实木+板材",
  "weight": "18kg",
  "load_capacity": "40kg"
}
```

规则：

- 保留对象结构
- 后端不要写死字段名
- 详情图排版引擎按配置映射展示

#### `style`

枚举：

- `memphis_vintage`
- `creamy_wood`
- `french_creamy`

规则：

- 必填
- 不允许传自由文本替代正式 style_id

#### `scene_preferences`

类型：

```json
{
  "include_pet": true,
  "pet_type": "cat",
  "include_human": false,
  "human_type": "",
  "decor_tags": ["green_plant", "vintage_lamp"]
}
```

规则：

- 只做正向约束，不替代风格系统

#### `output_targets`

类型：

```json
{
  "main_images": true,
  "sku_images": true,
  "transparent_image": true,
  "white_bg_image": true,
  "detail_pages": true,
  "material_images": true,
  "showcase_images": true
}
```

规则：

- 默认全开
- 允许只跑部分输出
- MVP 阶段可先支持部分字段

### 14.6 同步返回协议

`run_pipeline` 推荐返回：

```json
{
  "ok": true,
  "pipeline": "ecommerce_visual_asset",
  "action": "run_pipeline",
  "status": "completed",
  "result": {
    "sku": "SKU12345",
    "manifest_path": "...",
    "preview_html_path": "...",
    "archive_path": "...",
    "assets": {
      "main_images": [],
      "sku_images": [],
      "detail_images": [],
      "showcase_images": [],
      "material_images": [],
      "transparent_image": {},
      "white_bg_image": {}
    }
  },
  "saved_assets": {}
}
```

失败返回：

```json
{
  "ok": false,
  "pipeline": "ecommerce_visual_asset",
  "action": "run_pipeline",
  "status": "failed",
  "error": "具体错误信息"
}
```

### 14.7 异步返回协议

`start_pipeline` 返回：

```json
{
  "ok": true,
  "async": true,
  "pipeline": "ecommerce_visual_asset",
  "action": "start_pipeline",
  "job_id": "32位hex",
  "status": "queued",
  "poll_path": "/api/ecommerce-visual/pipeline/jobs/{job_id}"
}
```

`poll_pipeline` 返回：

```json
{
  "ok": true,
  "pipeline": "ecommerce_visual_asset",
  "action": "poll_pipeline",
  "job_id": "32位hex",
  "status": "running",
  "progress": {
    "current_stage": "generate_main_images",
    "percent": 42
  }
}
```

完成时：

```json
{
  "ok": true,
  "pipeline": "ecommerce_visual_asset",
  "action": "poll_pipeline",
  "job_id": "32位hex",
  "status": "completed",
  "result": {},
  "saved_assets": {}
}
```


## 15. 任务状态机

### 15.1 顶层状态

推荐顶层 job 状态枚举：

- `queued`
- `running`
- `completed`
- `failed`
- `cancelled`

当前项目已有 detail skill 是：

- `running`
- `completed`
- `failed`

建议未来统一扩展到 5 态，但至少兼容现有 3 态。

### 15.2 阶段状态

每个阶段推荐状态：

- `pending`
- `running`
- `success`
- `failed`
- `skipped`

### 15.3 顶层状态流转

```text
queued -> running -> completed
queued -> running -> failed
queued -> cancelled
running -> cancelled
```

### 15.4 阶段流转

```text
pending -> running -> success
pending -> running -> failed
pending -> skipped
```

### 15.5 推荐阶段定义

建议阶段名固定，不随版本改名：

1. `01_normalize_input`
2. `02_product_analysis`
3. `03_shot_planning`
4. `04_generate_main_images`
5. `05_generate_sku_images`
6. `06_generate_transparent_and_white_bg`
7. `07_generate_material_images`
8. `08_render_detail_pages`
9. `09_generate_showcase_images`
10. `10_export_delivery_bundle`

### 15.6 页面级状态

对于详情图、橱窗图、主图、SKU 图，建议支持页面级追踪：

- `planned`
- `generating`
- `rendering`
- `success`
- `failed`

用于前端展示：

- 哪张图失败了
- 哪个卖点卡片失败了
- 哪个详情页切片失败了

### 15.7 错误分类

建议错误类型字段：

- `validation_error`
- `input_missing`
- `asset_unreachable`
- `analysis_failed`
- `generation_failed`
- `layout_failed`
- `export_failed`
- `storage_failed`
- `internal_error`

错误对象示例：

```json
{
  "type": "generation_failed",
  "stage": "04_generate_main_images",
  "message": "主图第1张生成失败",
  "retryable": true
}
```


## 16. Manifest 结构

manifest 是前后端、异步任务、调试和交付的共同事实来源。

推荐每个 job 输出一个：

- `manifest.json`

### 16.1 顶层结构

```json
{
  "job_id": "32位hex",
  "sku": "SKU12345",
  "pipeline": "ecommerce_visual_asset",
  "status": "running",
  "created_at": "ISO8601",
  "updated_at": "ISO8601",
  "finished_at": null,
  "config": {},
  "input_summary": {},
  "stages": {},
  "assets": {},
  "deliverables": {},
  "usage": {},
  "errors": []
}
```

### 16.2 config

作用：

- 保存本次生成采用的正式配置
- 方便复跑
- 方便问题排查

建议包含：

```json
{
  "style": "creamy_wood",
  "detail_template_id": "detail_template_01",
  "showcase_template_id": "showcase_template_01",
  "output_targets": {},
  "image_models": {
    "scene_model": "xxx",
    "matting_model": "xxx",
    "layout_model": "xxx"
  }
}
```

### 16.3 input_summary

不保存敏感信息，保存摘要：

```json
{
  "product_image_count": 4,
  "front_image_count": 1,
  "selling_point_count": 6,
  "style": "creamy_wood",
  "scene_preferences": {
    "include_pet": true
  }
}
```

### 16.4 stages

结构建议：

```json
{
  "01_normalize_input": {
    "status": "success",
    "attempts": 1,
    "started_at": "ISO8601",
    "updated_at": "ISO8601",
    "error": null
  },
  "04_generate_main_images": {
    "status": "running",
    "attempts": 1,
    "started_at": "ISO8601",
    "updated_at": "ISO8601",
    "error": null
  }
}
```

### 16.5 assets

按产物类型归档：

```json
{
  "main_images": [
    {
      "index": 1,
      "role": "hero_anchor",
      "status": "success",
      "path": "...",
      "width": 1440,
      "height": 1440
    }
  ],
  "sku_images": [],
  "detail_images": [],
  "showcase_images": [],
  "material_images": [],
  "transparent_image": {},
  "white_bg_image": {}
}
```

### 16.6 deliverables

用于最终交付：

```json
{
  "preview_html_path": "...",
  "archive_path": "...",
  "folder_path": "...",
  "cloud_urls": []
}
```

### 16.7 usage

记录模型和算力使用：

```json
{
  "summary": {
    "analysis_count": 2,
    "image_generation_count": 18,
    "layout_render_count": 6
  },
  "breakdown": {},
  "records": []
}
```

### 16.8 errors

记录最近错误：

```json
[
  {
    "stage": "04_generate_main_images",
    "type": "generation_failed",
    "message": "主图第2张生成失败",
    "retryable": true,
    "ts": "ISO8601"
  }
]
```


## 17. 与当前项目模式对齐

### 17.1 当前已存在模式

当前项目中的 `comfly_ecommerce_detail` 已经具备这些模式：

- `run/start/poll` 三动作
- job_id 轮询
- manifest 进度读取
- `saved_assets` 自动入库
- `last_steps` 进度摘要

这个新视觉 Skill 建议完全复用同样的模式。

### 17.2 当前 detail skill 可复用的部分

可直接借鉴：

- 后端 API 结构
- job store 内存态
- manifest 读取接口
- `auto_save` 行为
- `progress` 返回形态

### 17.3 需要新扩展的部分

相比 detail skill，还需要额外支持：

- 更复杂的输入协议
- 更细粒度资产分组
- 锚点图概念
- 模板配置
- 批量 SKU
- 交付包与预览页


## 18. 实施建议

如果按当前项目方式落地，推荐先定死以下规范再开始编码：

1. `capability_id` 不再改名
2. `action` 只保留 `run/start/poll`
3. stage 名称固定
4. manifest 顶层结构固定
5. `result/assets/progress/error` 字段名固定
6. 主图第 1 张强制标记为 `hero_anchor`
7. 详情图与橱窗图统一走模板渲染，不混入任意输出结构

这样可以保证：

- OpenClaw 不需要改
- MCP 归一化逻辑稳定
- 前端轮询逻辑稳定
- 生成器脚本可以持续迭代但不破坏外部调用方


## 19. 开发任务拆解

本节将需求拆成可执行开发任务，目标是：

- 明确哪些模块新建
- 明确哪些模块复用
- 明确 MVP 第一版先做到哪里
- 明确每块的输入输出责任

### 19.1 建议模块划分

建议按下面 8 个模块实施。

#### 模块 A：协议与接入层

职责：

- 定义 capability
- 对接 MCP `invoke_capability`
- 提供后端 API `run/start/poll`

建议文件：

- `mcp/capability_catalog.json`
- `skill_registry.json`
- `mcp/http_server.py`
- `backend/app/api/ecommerce_visual_asset.py`

#### 模块 B：输入解析与标准化

职责：

- 解析 `product_images`
- 解析 `selling_points`
- 解析 `specs`
- 解析 `style`
- 归一化成本地流水线输入结构

建议文件：

- `backend/app/services/ecommerce_visual_asset_input_builder.py`
- `skills/ecommerce_visual_asset/scripts/schema/*.json`

#### 模块 C：任务管理与进度

职责：

- job 创建
- job 状态维护
- manifest 写入与读取
- progress 摘要输出

建议文件：

- `backend/app/services/ecommerce_visual_asset_job_store.py`
- `backend/app/services/ecommerce_visual_asset_manifest.py`

#### 模块 D：产品分析与镜头规划

职责：

- 产品理解
- 卖点结构化
- 镜头规划
- 主图锚点策略

建议文件：

- `skills/ecommerce_visual_asset/scripts/product_analyzer.py`
- `skills/ecommerce_visual_asset/scripts/shot_planner.py`

#### 模块 E：视觉场景生成

职责：

- 主图生成
- SKU 图生成
- 橱窗图生成
- 多尺寸素材图衍生

建议文件：

- `skills/ecommerce_visual_asset/scripts/scene_generator.py`
- `skills/ecommerce_visual_asset/scripts/output_deriver.py`

#### 模块 F：抠图与白底

职责：

- 透明图生成
- 白底图生成
- 边缘清理

建议文件：

- `skills/ecommerce_visual_asset/scripts/matting_pipeline.py`

#### 模块 G：详情图排版引擎

职责：

- 模板读取
- 文案布局
- 长图渲染
- 自动切屏

建议文件：

- `skills/ecommerce_visual_asset/scripts/detail_renderer.py`
- `skills/ecommerce_visual_asset/templates/detail/*.json`

#### 模块 H：交付与预览

职责：

- 产出预览 HTML
- 生成 zip
- 生成结果索引

建议文件：

- `skills/ecommerce_visual_asset/scripts/export_bundle.py`
- `skills/ecommerce_visual_asset/templates/preview/*.html`


## 20. MVP 范围定义

### 20.1 MVP 目标

先做出“能稳定出一整套核心素材”的第一版，不追求一次性覆盖 Excel 里的全部高级能力。

MVP 的目标应是：

- 单 SKU 可跑通
- 可异步执行
- 可轮询进度
- 可交付主图、SKU 图、透明图、白底图、简化详情图
- 风格支持先做 2 到 3 种 preset

### 20.2 MVP 必做

- `run/start/poll` 三动作
- 单 SKU 输入协议
- 主图 5 张
- SKU 图 3 张起
- 透明图 1 张
- 白底图 1 张
- 简化版详情图
- manifest
- 预览 HTML
- zip 打包

### 20.3 MVP 可降级实现

以下部分允许先做简化实现：

- 主图竖版
  - 可以先基于同场景重构或高质量裁切
- 透明图
  - 可优先使用输入透明图或主图锚点抠图
- 白底图
  - 可先基于透明图合成
- 详情图
  - 先只做 1 套模板
- 风格系统
  - 先做固定 preset，不开放细粒度调参 UI

### 20.4 MVP 暂不做

- 多 SKU 批量并发调度
- 高级橱窗图模板系统
- 多模板详情页切换
- 人物模特复杂注入
- 高级质量评分自动重生成
- 云交付回调


## 21. 模块复用建议

### 21.1 可直接复用当前 detail skill 的部分

当前已有的 `comfly_ecommerce_detail` 可以复用的内容：

- API 入口模式
- job store 结构
- manifest 读取思路
- `saved_assets` 入库逻辑
- `run/start/poll` 状态形态
- `auto_save` 策略

优先复用文件设计，不建议直接复制整个业务逻辑。

### 21.2 可局部复用的能力

- 图片下载与 URL 归一化
- 资源落盘与命名
- 本地 runs/job_runs 目录结构
- 模型调用封装
- usage 统计

### 21.3 不建议直接复用的部分

以下内容不建议直接沿用旧 detail skill 逻辑：

- 详情图单一路径脚本结构
- 针对单产品长图的文案规划逻辑
- 单一 `page_results` 输出结构

因为新 Skill 不是“详情图流水线”，而是“全套电商资产流水线”。


## 22. 开发里程碑

### 里程碑 1：协议与任务骨架

交付：

- capability 注册
- API 路由
- job store
- manifest 骨架
- 最小 demo 返回

完成标准：

- OpenClaw / MCP 可以调用
- `start_pipeline` 返回 job_id
- `poll_pipeline` 可看到阶段进度

### 里程碑 2：产品分析与镜头规划

交付：

- 输入解析器
- 产品分析器
- 卖点/角度规划器
- 风格 preset 加载器

完成标准：

- 能输出 `product_analysis.json`
- 能输出 `shot_plan.json`

### 里程碑 3：主图 / SKU 图 / 透明图 / 白底图

交付：

- 主图生成器
- SKU 图生成器
- 抠图白底处理

完成标准：

- 一次任务能得到核心商品视觉素材
- 主图第 1 张被明确标记为锚点图

### 里程碑 4：详情图与交付包

交付：

- 详情图模板渲染
- 长图切片
- preview.html
- zip 打包

完成标准：

- 可交付一套可浏览的完整结果

### 里程碑 5：橱窗图与风格增强

交付：

- 橱窗图卖点卡片生成
- 更强风格一致性
- 更好的小尺寸适配


## 23. 任务清单

### 23.1 协议层任务

- 新增 capability 定义
- 新增 skill registry 包定义
- 新增 MCP 入参归一化逻辑
- 新增后端 API 路由

### 23.2 数据层任务

- 定义输入 schema
- 定义 manifest schema
- 定义 result schema
- 定义预览索引 schema

### 23.3 引擎层任务

- 做产品分析器
- 做镜头规划器
- 做风格 preset 装载器
- 做主图生成器
- 做 SKU 图生成器
- 做透明图/白底图生成器
- 做详情图渲染器
- 做素材图多尺寸衍生器
- 做橱窗图生成器

### 23.4 交付层任务

- 产出文件夹结构
- 产出 preview.html
- 产出 zip
- 产出 saved_assets

### 23.5 质量保障任务

- 命名规则校验
- 尺寸校验
- 文件大小校验
- 图片数量校验
- 风格一致性基础检查


## 24. 建议文件结构

推荐最终目录结构：

```text
backend/app/api/
  ecommerce_visual_asset.py

backend/app/services/
  ecommerce_visual_asset_job_store.py
  ecommerce_visual_asset_pipeline_runner.py
  ecommerce_visual_asset_input_builder.py

skills/ecommerce_visual_asset/
  scripts/
    pipeline.py
    product_analyzer.py
    shot_planner.py
    scene_generator.py
    matting_pipeline.py
    detail_renderer.py
    output_deriver.py
    export_bundle.py
  templates/
    detail/
    showcase/
    preview/
  style_presets/
    memphis_vintage.json
    creamy_wood.json
    french_creamy.json
  runs/
```


## 25. 先后顺序建议

真正开做时，建议按这个顺序，不要一开始就试图把全量图片一次写完：

1. 先把协议和 job 框架搭起来
2. 再把产品分析和镜头规划稳定下来
3. 再做主图锚点图
4. 再做透明图和白底图
5. 再做 SKU 图
6. 再做详情图
7. 最后做橱窗图和 preview/zip

原因：

- 主图锚点图是全链路基础
- 详情图和橱窗图都依赖前面分析结果
- preview/zip 只有在产物结构稳定后再做才不会反复返工


## 26. 第一版 MVP 开发计划

本节只关注“第一版能上线试跑”的范围，不追求一次满足 Excel 全量需求。

### 26.1 第一版目标

第一版交付目标：

- 能接收单 SKU 输入
- 能通过 MCP / OpenClaw 调起
- 能异步执行并轮询状态
- 能产出一套基础可用素材：
  - 主图
  - SKU 图
  - 透明图
  - 白底图
  - 简化详情图
- 能导出结果目录、manifest、preview

### 26.2 第一版明确不做

为了尽快落地，第一版暂不做：

- 橱窗图完整能力
- 多 SKU 批处理
- 多模板详情图系统
- 复杂人物模特注入
- 高级自动质检和自动重生成
- 云回调交付
- 配置界面


## 27. 第一版文件级任务

### 27.1 MCP 和注册层

需要新增或修改：

- `mcp/capability_catalog.json`
- `skill_registry.json`
- `mcp/http_server.py`

第一版目标：

- 注册 `ecommerce.visual_asset_pipeline`
- 支持 `run_pipeline`
- 支持 `start_pipeline`
- 支持 `poll_pipeline`
- 入参归一化

完成标准：

- `invoke_capability(capability_id="ecommerce.visual_asset_pipeline", payload=...)` 可进入后端

### 27.2 后端 API 层

需要新增：

- `backend/app/api/ecommerce_visual_asset.py`

第一版接口：

- `POST /api/ecommerce-visual/pipeline/run`
- `POST /api/ecommerce-visual/pipeline/start`
- `GET /api/ecommerce-visual/pipeline/jobs/{job_id}`

第一版职责：

- 鉴权
- 参数校验
- 输入归一化
- 调用 runner
- 返回统一结构

完成标准：

- 三个接口能稳定返回
- `start` 返回 job_id
- `poll` 可读进度

### 27.3 后端 service 层

需要新增：

- `backend/app/services/ecommerce_visual_asset_job_store.py`
- `backend/app/services/ecommerce_visual_asset_pipeline_runner.py`
- `backend/app/services/ecommerce_visual_asset_input_builder.py`

第一版职责：

- job 生命周期维护
- pipeline 输入归一化
- 运行脚本模块
- 读取 manifest 进度

完成标准：

- runner 能被 API 调用
- job store 能支持 `running/completed/failed`
- progress 能从 manifest 映射出来

### 27.4 skill 脚本层

需要新增目录：

- `skills/ecommerce_visual_asset/`

需要新增脚本：

- `skills/ecommerce_visual_asset/scripts/pipeline.py`
- `skills/ecommerce_visual_asset/scripts/product_analyzer.py`
- `skills/ecommerce_visual_asset/scripts/shot_planner.py`
- `skills/ecommerce_visual_asset/scripts/scene_generator.py`
- `skills/ecommerce_visual_asset/scripts/matting_pipeline.py`
- `skills/ecommerce_visual_asset/scripts/detail_renderer.py`
- `skills/ecommerce_visual_asset/scripts/export_bundle.py`

第一版要求：

- `pipeline.py` 可独立读取 JSON 输入并执行
- 各模块先保证接口稳定，内部可以逐步增强

完成标准：

- 本地命令行可直接跑
- pipeline 可以落盘到 `runs/`

### 27.5 模板与配置层

需要新增：

- `skills/ecommerce_visual_asset/style_presets/creamy_wood.json`
- `skills/ecommerce_visual_asset/style_presets/memphis_vintage.json`
- `skills/ecommerce_visual_asset/style_presets/french_creamy.json`
- `skills/ecommerce_visual_asset/templates/detail/template_01.json`
- `skills/ecommerce_visual_asset/templates/preview/index.html`

第一版要求：

- 先只做 1 套详情图模板
- 3 套风格 preset 先以配置文件固化

完成标准：

- pipeline 不写死风格细节
- 详情图模板可替换


## 28. 第一版阶段计划

### 阶段 A：先搭空骨架

先完成：

- capability 注册
- API 三接口
- job store
- pipeline 空脚本
- manifest 骨架

这一阶段不要求出图，只要求：

- 调用通
- 状态能跑通
- 目录结构能生成

完成标志：

- 调 `start_pipeline` 后能看到 job 从 `running` 到 `completed`
- 即使结果里只有 mock 产物也可以

### 阶段 B：接入产品分析与镜头规划

实现：

- 输入解析
- 产品分析
- 卖点结构化
- 镜头规划

完成标志：

- 能产出
  - `normalized_input.json`
  - `product_analysis.json`
  - `shot_plan.json`

### 阶段 C：做主图锚点能力

实现：

- 主图 5 张
- 主图第 1 张锚点标记
- 基础 SKU 图生成

完成标志：

- `assets.main_images` 有结果
- `hero_anchor` 字段存在

### 阶段 D：做透明图 / 白底图 / 素材图

实现：

- 透明图
- 白底图
- 主图第 1 张多尺寸素材图

完成标志：

- `transparent_image`
- `white_bg_image`
- `material_images`
  均有结果

### 阶段 E：做简化版详情图

实现：

- 1 套模板
- 卖点映射
- 宽 790 长图切片

完成标志：

- `detail_images` 有结果
- 页面数量和切片逻辑稳定

### 阶段 F：做交付层

实现：

- `preview.html`
- `manifest.json`
- `zip`

完成标志：

- 用户能直接打开预览页检查结果


## 29. 第一版输入输出裁剪建议

为了降低第一版复杂度，建议只支持这一组最小输入：

```json
{
  "sku": "SKU12345",
  "product_images": [
    {
      "role": "front",
      "image_url": "..."
    },
    {
      "role": "side",
      "image_url": "..."
    }
  ],
  "selling_points": [
    {
      "title": "...",
      "description": "..."
    }
  ],
  "specs": {},
  "style": "creamy_wood"
}
```

第一版输出只强制要求：

- `main_images`
- `sku_images`
- `transparent_image`
- `white_bg_image`
- `detail_images`
- `manifest_path`
- `preview_html_path`


## 30. 第一版验收标准

### 30.1 接口验收

- 能成功调用 `run/start/poll`
- 错误能返回明确 `status/error`
- progress 能反映阶段状态

### 30.2 产物验收

- 主图数量符合预期
- SKU 图至少 3 张
- 透明图、白底图各 1 张
- 详情图可读、可切片
- 文件命名符合规则

### 30.3 一致性验收

- 主图和 SKU 图风格一致
- 主图第 1 张与透明图/白底图角度一致
- 详情图文案来源与卖点输入一致

### 30.4 可维护性验收

- 产物和进度都写入 manifest
- 各阶段脚本职责分离
- 模板和风格不写死在主脚本


## 31. 第一版之后的自然扩展点

第一版完成后，最自然的增强方向是：

1. 加橱窗图
2. 加第二套和第三套详情模板
3. 加批量 SKU
4. 加更精细的风格控制
5. 加质量评分和失败页自动重试

也就是说，第一版完成后，不需要推倒重来，只需要在现有 pipeline 上继续补阶段和模板即可。


## 32. 文件级 TODO 清单

本节目标：

- 直接指导后续代码落地
- 明确每个文件第一版先写什么
- 避免出现“文件建了但边界混乱”的情况

### 32.1 `mcp/capability_catalog.json`

TODO：

- 新增 `ecommerce.visual_asset_pipeline`
- `description` 说明该能力是整包视觉资产流水线
- `upstream` 设为 `local`
- `upstream_tool` 设为 `invoke`
- `arg_schema` 按本文协议补齐

第一版先支持字段：

- `action`
- `job_id`
- `sku`
- `product_images`
- `selling_points`
- `specs`
- `style`
- `scene_preferences`
- `output_targets`
- `auto_save`
- `output_dir`
- `isolate_job_dir`

完成标志：

- MCP tools 列表中能识别这个 capability


### 32.2 `skill_registry.json`

TODO：

- 新增一个 package，例如 `ecommerce_visual_asset_skill`
- `package_config` 里写默认风格与默认输出目标
- `capabilities` 中挂载 `ecommerce.visual_asset_pipeline`
- `tags` 添加：
  - `ecommerce`
  - `visual`
  - `main-image`
  - `detail-page`
  - `showcase`

第一版重点：

- 让它能被“技能商店 / 安装列表”识别
- 不要求一开始就做复杂商品介绍文案

完成标志：

- `/skills/store`
- `/skills/install`
  这一套链路能认出它


### 32.3 `mcp/http_server.py`

TODO：

- 新增 `_normalize_invoke_ecommerce_visual_asset_args`
- 支持顶层误传归一化
- 支持 `payload.payload` 误套壳修正
- 把 `ecommerce.visual_asset_pipeline` 映射到后端 API 路径

建议映射：

- `run_pipeline` -> `/api/ecommerce-visual/pipeline/run`
- `start_pipeline` -> `/api/ecommerce-visual/pipeline/start`
- `poll_pipeline` -> `/api/ecommerce-visual/pipeline/jobs/{job_id}`

第一版重点：

- 只解决路由和参数归一化
- 不在 MCP 层做业务逻辑

完成标志：

- `invoke_capability` 能正确进后端


### 32.4 `backend/app/api/ecommerce_visual_asset.py`

TODO：

- 定义 `EcommerceVisualAssetPayload`
- 定义 `EcommerceVisualAssetRunBody`
- 实现：
  - `run`
  - `start`
  - `job_status`

第一版职责：

- 鉴权
- 字段校验
- 输入转 runner 所需结构
- 统一返回 `ok/status/result/error`

不应在这里做：

- 复杂产品分析
- prompt 拼装
- 图片生成逻辑

完成标志：

- API 层只保留“薄控制器”职责


### 32.5 `backend/app/services/ecommerce_visual_asset_input_builder.py`

TODO：

- 解析 `product_images`
- 做 `asset_id / image_url` 统一
- 解析 `selling_points`
- 解析 `specs`
- 解析 `style`
- 补全默认 `output_targets`

建议输出对象：

```json
{
  "sku": "...",
  "product_images": [],
  "selling_points": [],
  "specs": {},
  "style": "...",
  "scene_preferences": {},
  "output_targets": {}
}
```

第一版重点：

- 先只做字段归一化
- 不做复杂推断


### 32.6 `backend/app/services/ecommerce_visual_asset_job_store.py`

TODO：

- 复用现有 detail skill 的 job store 模式
- 增加 `queued` 状态支持
- 存储：
  - `job_id`
  - `user_id`
  - `inp`
  - `status`
  - `error`
  - `result`
  - `saved_assets`
  - `job_output_dir`

第一版重点：

- 保持内存态简单实现
- 支持 3 天 TTL 清理


### 32.7 `backend/app/services/ecommerce_visual_asset_pipeline_runner.py`

TODO：

- 加载脚本模块
- 调用 `run_pipeline`
- 组织输入输出
- 对接本地 `runs/`

建议职责：

- 不写具体生成逻辑
- 只做“后端 <-> skill 脚本”的桥接

第一版重点：

- runner 层结构先立住
- 便于以后替换脚本实现


### 32.8 `skills/ecommerce_visual_asset/scripts/pipeline.py`

TODO：

- 定义 pipeline 主入口
- 读取 JSON 输入
- 创建 run 目录
- 写 `manifest.json`
- 调用各阶段子模块
- 汇总结果

建议主流程：

1. normalize_input
2. product_analysis
3. shot_planning
4. generate_main_images
5. generate_sku_images
6. generate_transparent_and_white_bg
7. generate_material_images
8. render_detail_pages
9. export_bundle

第一版重点：

- 先只做串行执行
- 先不要过早引入复杂并发


### 32.9 `skills/ecommerce_visual_asset/scripts/product_analyzer.py`

TODO：

- 根据输入图片和结构化卖点生成产品分析
- 输出：
  - `product_name`
  - `category`
  - `audience`
  - `visual_constraints`
  - `core_features`
  - `safe_claims`

第一版重点：

- 结合用户结构化输入
- 不完全依赖纯视觉识别


### 32.10 `skills/ecommerce_visual_asset/scripts/shot_planner.py`

TODO：

- 规划主图 5 张
- 规划 SKU 图
- 规划详情图主题页
- 标记主图第 1 张为锚点

建议输出：

```json
{
  "main_images": [],
  "sku_images": [],
  "detail_pages": []
}
```

第一版重点：

- 保证镜头列表稳定
- 保证结果可复用到后续全部阶段


### 32.11 `skills/ecommerce_visual_asset/scripts/scene_generator.py`

TODO：

- 根据 `shot_plan` 生成主图和 SKU 图
- 支持 style preset
- 保持统一光影和场景语义

第一版重点：

- 先做主图和 SKU 图
- 橱窗图留到后续扩展


### 32.12 `skills/ecommerce_visual_asset/scripts/matting_pipeline.py`

TODO：

- 基于锚点主图或输入透明图生成透明图
- 基于透明图生成白底图
- 做边缘清理

第一版重点：

- 先优先复用输入透明图
- 主图抠图作为备选


### 32.13 `skills/ecommerce_visual_asset/scripts/detail_renderer.py`

TODO：

- 读取详情模板
- 把卖点与规格映射进模板
- 生成宽 790 的详情长图
- 自动切屏

第一版重点：

- 只支持 1 套模板
- 每屏 1 个主题
- 不做复杂动画或高级装饰


### 32.14 `skills/ecommerce_visual_asset/scripts/export_bundle.py`

TODO：

- 生成 `preview.html`
- 生成结果索引
- 打 zip

第一版重点：

- 先确保本地可预览
- 云交付留到后面


### 32.15 `skills/ecommerce_visual_asset/style_presets/*.json`

TODO：

- 先固化 3 套 preset
- 每套至少包含：
  - palette
  - materials
  - lighting
  - decor_tags
  - negative_rules

完成标志：

- pipeline 可按 `style` 读取 preset


### 32.16 `skills/ecommerce_visual_asset/templates/detail/template_01.json`

TODO：

- 定义详情页屏幕结构
- 定义文字区、图片区、图标区
- 定义不同屏幕主题：
  - 卖点总览
  - 材质结构
  - 尺寸参数
  - 场景说明

第一版重点：

- 模板是 JSON 驱动，不写死在代码里


### 32.17 `skills/ecommerce_visual_asset/templates/preview/index.html`

TODO：

- 显示所有输出图片分组
- 支持按类型浏览：
  - 主图
  - SKU 图
  - 详情图
  - 白底/透明图

第一版重点：

- 简单静态预览即可
- 不做前端复杂交互


## 33. 第一版实际开发顺序

建议严格按下面顺序推进。

### 第一步：搭协议和后端骨架

涉及文件：

- `mcp/capability_catalog.json`
- `skill_registry.json`
- `mcp/http_server.py`
- `backend/app/api/ecommerce_visual_asset.py`
- `backend/app/services/ecommerce_visual_asset_job_store.py`
- `backend/app/services/ecommerce_visual_asset_pipeline_runner.py`

完成后应达到：

- 能启动 job
- 能轮询 job
- 即使结果是 mock 也能完整走通

### 第二步：搭 skill 目录与主脚本

涉及文件：

- `skills/ecommerce_visual_asset/scripts/pipeline.py`
- `skills/ecommerce_visual_asset/runs/`

完成后应达到：

- 本地命令行能跑
- 会写 manifest
- 会出最小结果 JSON

### 第三步：补产品分析和镜头规划

涉及文件：

- `product_analyzer.py`
- `shot_planner.py`
- `style_presets/*.json`

完成后应达到：

- 有稳定的中间结果
- 可以为后续图片生成提供锚点

### 第四步：补主图 / SKU 图 / 透明图 / 白底图

涉及文件：

- `scene_generator.py`
- `matting_pipeline.py`

完成后应达到：

- 有一套核心视觉产物

### 第五步：补详情图与预览页

涉及文件：

- `detail_renderer.py`
- `export_bundle.py`
- `templates/detail/template_01.json`
- `templates/preview/index.html`

完成后应达到：

- 可交付可预览


## 34. 开始写代码前的最终检查

正式开工前，建议先确认这 8 件事都已经定死：

1. capability_id 名称不改
2. action 枚举不改
3. 输入 schema 第一版范围不再继续膨胀
4. 输出 schema 第一版范围不再继续膨胀
5. 主图第 1 张锚点策略明确
6. 详情图第一版只做 1 套模板
7. 第一版不做橱窗图
8. 预览页只做静态 HTML

这样就能避免第一版在开发过程中反复返工。
