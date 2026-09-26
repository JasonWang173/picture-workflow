# 商品图 × 商家模板图片合成

一个面向外卖海报的 Python 图片合成工具。它会把商品图和商家模板合成为一张竖版海报，并尽量保持模板中的品牌信息、活动文案和底部规则说明。

项目提供两种合成策略：

- `overlay`：提取模板里的 Logo、图案和活动文字，去除可移除的浅色底，再叠加到商品图背景上。适合没有明确商品占位区的平面活动模板。
- `cutout`：自动识别绿布/绿幕或大块白色、浅色占位区，直接将商品原图按占位区尺寸等比嵌入，不对商品图做模糊或二次主体叠加；没有可识别占位区时才使用 `rembg` 自动抠出商品并居中合成。
- `auto`（默认）：优先检测商品占位区，有占位区走区域替换，否则根据模板类型在模板叠加和主体抠图之间判断。

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
--mode auto|overlay|cutout       合成策略，默认 auto
--size 1080x1920                 输出尺寸，默认沿用模板尺寸
--logo-pill / --no-logo-pill     overlay 模式下保留左上角 Logo 白色胶囊
--outline 0..1                   为模板图形添加白色可读性描边，默认 0.78
--quality 1..100                 JPEG 质量，默认 100
```

也可以在 Python 中调用：

```python
from food_compositor import compose

compose(
    product_path="inputs/burger.png",
    template_path="inputs/template.jpg",
    output_path="outputs/burger.jpg",
    mode="auto",
)
```

## 设计要点

1. 商品图先按画布比例 `cover` 裁剪，避免拉伸食物形状。
2. 模板白底通过软阈值转透明，保留抗锯齿边缘；文案额外生成轻微描边，防止叠在复杂食物纹理上时失读。
3. 主体抠图会识别绿布/绿幕和大块浅色占位区，只替换该区域；商品原图使用等比例高质量重采样，不变形、不额外模糊。模板中的 Logo、标题和规则文案保持原始清晰度。没有可识别占位区的模板才按照商品主体的 alpha 边界抠图，并生成接触阴影。
4. 原图不会被覆盖，输出目录只写入新的 JPG/PNG 文件。
5. JPG 输出会自动保持比例并压缩到 300KB 以下；优先搜索 JPEG 质量，只有复杂图片仍超限时才按原比例缩小画布，不会拉伸商品图。

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

商品图区域支持选择整个文件夹或一次多选图片；模板图保持单张。批量任务会逐张合成，前端逐张预览结果，并支持选择部分图片下载或下载全部 `food-composites.zip`。尺寸选项包含 `1280×720 · 横版`、`1080×1920 · 竖版`，以及两种移动端竖版尺寸。

横版 `1280×720` 会将商品图按比例铺满画布，并在模板已经是同尺寸时直接使用模板原始像素合成，减少 Logo 和活动文字的重复缩放。模板去白底时只移除与画布边缘连通的白色区域，会保留描边文字内部的白色填充；输出 JPEG 使用高质量、4:4:4 色度采样以保持小字边缘清晰。
