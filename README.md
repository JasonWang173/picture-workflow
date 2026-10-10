# 商品图 × 商家模板图片合成

一个面向外卖海报的 Python 图片合成工具。它会把商品图和商家模板合成为一张竖版海报，并尽量保持模板中的品牌信息、活动文案和底部规则说明。

项目提供两种合成策略：

- `overlay`：优先使用 PNG 模板原生透明通道，仅将 Logo、图案和活动文字叠加到铺满画布的商品图上；没有透明通道时才去除与画布边缘连通的黑色、白色或浅色底。商品图始终按比例 cover，不拉伸变形。
- `cutout`：自动识别绿布/绿幕或大块白色、浅色占位区，直接将商品原图按占位区尺寸等比嵌入，不对商品图做模糊或二次主体叠加；没有可识别占位区时才使用 `rembg` 自动抠出商品并居中合成。
- `template_replace`（前端名称“模板替换”）：无需上传商品图，识别模板中的透明图片区、白色/绿色占位区或已有矩形商品照片，并将商品区域清除为绿色占位区；Logo、活动文案、背景、边框和输出尺寸保持不变。生成的模板可以直接交给 `cutout` 模式继续放入自己的商品图。
- `auto`（默认）：优先尊重模板的原生透明通道；没有 alpha 时再检测商品占位区，并在模板叠加和主体抠图之间判断。

## 安装

建议使用 Python 3.10+：

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
```

`rembg` 只在 `cutout` 模式没有检测到商品占位区时使用；按上面的 `requirements.txt` 安装即可。它默认使用轻量的 `u2netp` 模型（首次运行约下载 4.6 MB），如果不安装它，程序会自动使用 OpenCV GrabCut 作为兜底。

## 命令行用法

合成一张海报：

```bash
python -m food_compositor \
  --products ./inputs/burger.png ./inputs/noodles.png \
  --template ./inputs/template.jpg \
  --output-dir ./outputs \
  --mode auto
```

常用参数：

```text
--mode auto|overlay|cutout|template_replace
                                合成策略，默认 auto；模板替换请使用 template_replace
--size 1080x1920                 输出尺寸，默认沿用模板尺寸
--logo-pill / --no-logo-pill     overlay 模式下保留左上角 Logo 白色胶囊
--outline 0..1                   为模板图形添加白色可读性描边，默认 0.78
--quality 1..100                 JPEG 质量，默认 100
```

也可以在 Python 中调用：

```python
from food_compositor import compose

compose(
    product_path="inputs/burger.png",  # template_replace 模式可传 None
    template_path="inputs/template.jpg",
    output_path="outputs/burger.jpg",
    mode="auto",
)
```

模板替换只抠掉模板里的商品图，不需要商品素材；生成的绿色区域可直接用于下一步主体抠图：

```python
compose(
    product_path=None,
    template_path="inputs/306.jpg",
    output_path="outputs/306_blank.jpg",
    mode="template_replace",
    size="1280x720",
)
```

## 设计要点

1. 商品图先按画布比例 `cover` 裁剪，避免拉伸食物形状。
2. PNG 模板优先使用原生 alpha 通道，透明区域不会参与合成；无 alpha 的 JPG 模板才通过边缘主背景色（黑/白/浅灰）连通区域转透明，保留抗锯齿边缘和文字内部封闭区域。商品图按比例 cover 铺满画布，不拉伸变形。
3. 主体抠图会识别绿布/绿幕和大块浅色占位区，只替换该区域；商品原图使用等比例高质量重采样，不变形、不额外模糊。模板中的 Logo、标题和规则文案保持原始清晰度。没有可识别占位区的模板才按照商品主体的 alpha 边界抠图，并生成接触阴影。
4. 原图不会被覆盖，输出目录只写入新的 JPG/PNG 文件。
5. JPG 输出严格保持所选画布尺寸，并通过 JPEG 质量和色度采样自适应压缩到 300KB 以下；优先保持 4:4:4 和高质量，进入 4:2:0 时优先保留亮度细节（文字、描边和食物纹理），输出阶段不会改变宽高或拉伸商品图。

## 运行示例

把自己的商品图和模板图放在 `inputs/` 后运行：

```bash
python -m food_compositor \
  --products \
  ./inputs/burger.png \
  ./inputs/noodles.png \
  --template ./inputs/template.jpg \
  --output-dir ./outputs --mode auto
```

## React 前端工作台

项目现在包含一个 React + Vite 前端和 FastAPI 接口。启动后可以拖入商品图、模板图，选择合成方式，查看成品并下载 JPG。

### 一键启动生产预览

```bash
pip install -r requirements-web.txt
npm --prefix web install
npm --prefix web run build
python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>。

### 前端开发模式

终端一：

```bash
python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

终端二：

```bash
npm --prefix web install
npm --prefix web run dev
```

开发地址是 <http://localhost:5173>，Vite 会把 `/api` 请求代理到 Python 服务。

前端入口在 [web/src/App.tsx](web/src/App.tsx:1)，视觉样式在 [web/src/styles.css](web/src/styles.css:1)，接口在 [server.py](server.py:1)。如需在本机显示“试用示例”，可将自己的模板图和商品图分别放在仓库根目录并命名为 `1.jpg` 和 `商品1.png`；这些本地素材不会提交到 Git。

商品图区域支持选择整个文件夹或一次多选图片；模板图保持单张。批量任务会逐张合成，前端逐张预览结果，并支持选择部分图片下载或下载全部 `food-composites.zip`。画布尺寸固定提供 `1080×1920 · 竖版` 和 `1280×720 · 横版`，下载图片严格按选中的尺寸输出。

“模板替换”和“主体抠图合成”会完整保留模板版式：源模板与所选画布方向不一致时等比缩放并使用模板边缘色补齐画布，不裁掉标题、Logo 或边框；商品图仍在识别出的占位区内按比例铺满。PNG 模板直接使用透明通道；无 alpha 的模板才会移除与画布边缘连通的黑色、白色或浅色区域，同时保留描边文字内部的封闭填充；输出 JPEG 使用高质量、4:4:4 色度采样以保持小字边缘清晰。
